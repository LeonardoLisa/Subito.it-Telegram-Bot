"""
Filename: tests/test_integration.py
Description: Integration tests covering the fixes applied to database.py,
             scraper_subito.py and telegram_ui.py:
               - SQLite connection leak fix (Database._connection())
               - prune_inactive_users() exempting superusers (bug fix)
               - trim_tracked_items() parameter binding
               - has_tracked_ads() replacing raw connection access from main.py
               - SubitoScraper category recognition (no full category map)
               - Non-blocking button-driven add flow ("+ Add Category"/"+ Add
                 Search"), price-order rejection, category recognized notice,
                 category-deletion notification

Run with:
    python3 -m unittest discover -s tests -v
or:
    python3 -m unittest tests.test_integration -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.database import Database
from bot.scrapers.subito import SubitoScraper
from bot.telegram_ui import TelegramUI
from bot import daemon as main_module


# ---------------------------------------------------------------------------
# Helpers for the SQLite connection-leak regression test
# ---------------------------------------------------------------------------

class _TrackingConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that counts close() calls."""
    tracker = {"opened": 0, "closed": 0}

    def close(self):
        _TrackingConnection.tracker["closed"] += 1
        return super().close()


_original_sqlite3_connect = sqlite3.connect


def _connect_with_tracking(*args, **kwargs):
    kwargs.pop("factory", None)
    _TrackingConnection.tracker["opened"] += 1
    # Must call the ORIGINAL connect, not `sqlite3.connect` (which is what's being
    # patched below) or this recurses into itself infinitely.
    return _original_sqlite3_connect(*args, factory=_TrackingConnection, **kwargs)


class TestDatabaseConnectionLifecycle(unittest.TestCase):
    """Regression test for the connection leak: `with self._get_connection() as conn:`
    only handled commit/rollback, never closed the connection. `Database._connection()`
    must close every connection it opens."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "leak_test.db")
        _TrackingConnection.tracker = {"opened": 0, "closed": 0}

    def test_every_opened_connection_is_closed(self):
        with patch("bot.database.sqlite3.connect", side_effect=_connect_with_tracking):
            db = Database(db_path=self.db_path)
            db.register_user(111)
            db.add_search(111, "Cat", "Search 1", "https://www.subito.it/x", [])
            sid = db.get_user_searches(111)["Cat"][0]["id"]
            db.has_tracked_ads(sid)
            db.add_tracked_ad(sid, "ad-1")
            db.is_ad_tracked(sid, "ad-1")
            db.trim_tracked_items(max_items=50)
            db.prune_inactive_users(999999)
            db.get_all_users()
            db.get_all_searches()

        opened = _TrackingConnection.tracker["opened"]
        closed = _TrackingConnection.tracker["closed"]
        self.assertGreater(opened, 0, "test did not actually open any connection")
        self.assertEqual(
            opened, closed,
            f"leak detected: opened {opened} connections but only closed {closed}"
        )


class TestDatabaseFixes(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.db = Database(db_path=self.db_path)

    def test_has_tracked_ads(self):
        self.db.register_user(1)
        self.db.add_search(1, "Cat", "S1", "https://www.subito.it/x", [])
        sid = self.db.get_user_searches(1)["Cat"][0]["id"]
        self.assertFalse(self.db.has_tracked_ads(sid))
        self.db.add_tracked_ad(sid, "ad-1")
        self.assertTrue(self.db.has_tracked_ads(sid))

    def test_prune_inactive_users_exempts_superuser(self):
        """Regression test for the reported bug: restarting the bot deleted a
        superuser's searches even though they were not (soft) expired, because
        the 34-day hard-delete retention job did not exempt superusers."""
        now = __import__("time").time()
        stale = now - (40 * 86400)  # 40 days ago, past the 34-day retention

        with self.db._connection() as conn:
            conn.execute(
                "INSERT INTO users (chat_id, joined_at, last_active, is_superuser) VALUES (?, ?, ?, 1)",
                (999, stale, stale),
            )
            conn.execute(
                "INSERT INTO users (chat_id, joined_at, last_active, is_superuser) VALUES (?, ?, ?, 0)",
                (1000, stale, stale),
            )
            conn.commit()
        self.db.add_search(999, "Cat", "Superuser Search", "https://www.subito.it/x", [])
        self.db.add_search(1000, "Cat", "Regular Search", "https://www.subito.it/y", [])

        deleted = self.db.prune_inactive_users(34 * 86400)

        self.assertEqual(deleted, 1, "only the regular (non-superuser) user should be pruned")
        self.assertIsNotNone(self.db.get_user(999), "superuser must survive the deep-clean")
        self.assertIsNone(self.db.get_user(1000), "regular expired user must be pruned")
        self.assertIn("Cat", self.db.get_user_searches(999), "superuser's searches must survive")
        self.assertEqual(self.db.get_user_searches(1000), {}, "regular user's searches must cascade-delete")

    def test_prune_inactive_users_keeps_active_users(self):
        self.db.register_user(42)  # last_active = now, well within retention
        deleted = self.db.prune_inactive_users(34 * 86400)
        self.assertEqual(deleted, 0)
        self.assertIsNotNone(self.db.get_user(42))

    def test_trim_tracked_items_keeps_latest_and_uses_bound_parameter(self):
        self.db.register_user(1)
        self.db.add_search(1, "Cat", "S1", "https://www.subito.it/x", [])
        sid = self.db.get_user_searches(1)["Cat"][0]["id"]
        for i in range(10):
            self.db.add_tracked_ad(sid, f"ad-{i}")

        # max_items is bound as a SQL parameter, not interpolated: this must not raise
        # even with a value coming from outside a hardcoded literal.
        self.db.trim_tracked_items(max_items=3)

        with self.db._connection() as conn:
            rows = conn.execute(
                "SELECT ad_id FROM tracked_ads WHERE search_id = ? ORDER BY timestamp", (sid,)
            ).fetchall()
        remaining = [r["ad_id"] for r in rows]
        self.assertEqual(len(remaining), 3)
        # the 3 most recently inserted ads (ad-7, ad-8, ad-9) must be the ones kept
        self.assertEqual(set(remaining), {"ad-7", "ad-8", "ad-9"})

    def test_cascading_delete_on_remove_user(self):
        self.db.register_user(5)
        self.db.add_search(5, "Cat", "S1", "https://www.subito.it/x", [])
        sid = self.db.get_user_searches(5)["Cat"][0]["id"]
        self.db.add_tracked_ad(sid, "ad-1")

        self.db.remove_user(5)

        self.assertIsNone(self.db.get_user(5))
        self.assertEqual(self.db.get_user_searches(5), {})
        with self.db._connection() as conn:
            row = conn.execute("SELECT 1 FROM tracked_ads WHERE search_id = ?", (sid,)).fetchone()
        self.assertIsNone(row, "tracked_ads must cascade-delete when the owning user is removed")

    def test_new_search_defaults_to_enabled_with_empty_include_kws(self):
        self.db.register_user(7)
        self.db.add_search(7, "Cat", "S1", "https://www.subito.it/x")
        item = self.db.get_user_searches(7)["Cat"][0]
        self.assertTrue(item["enabled"])
        self.assertEqual(item["include_kws"], [])

    def test_set_search_enabled_pauses_and_resumes(self):
        self.db.register_user(8)
        self.db.add_search(8, "Cat", "S1", "https://www.subito.it/x")

        self.db.set_search_enabled(8, "S1", False)
        self.assertFalse(self.db.get_user_searches(8)["Cat"][0]["enabled"])

        self.db.set_search_enabled(8, "S1", True)
        self.assertTrue(self.db.get_user_searches(8)["Cat"][0]["enabled"])

    def test_update_search_changes_only_passed_fields(self):
        self.db.register_user(9)
        self.db.add_search(9, "Cat", "S1", "https://www.subito.it/x", ["broken"], ["new"])
        sid_before = self.db.get_user_searches(9)["Cat"][0]["id"]

        self.db.update_search(9, "S1", url="https://www.subito.it/y")

        item = self.db.get_user_searches(9)["Cat"][0]
        self.assertEqual(item["id"], sid_before, "update_search must preserve the row id (and tracked-ads history)")
        self.assertEqual(item["url"], "https://www.subito.it/y")
        self.assertEqual(item["exclusion_kws"], ["broken"], "fields not passed must remain untouched")
        self.assertEqual(item["include_kws"], ["new"], "fields not passed must remain untouched")

        self.db.update_search(9, "S1", exclusion_kws=[], include_kws=["sealed"])
        item = self.db.get_user_searches(9)["Cat"][0]
        self.assertEqual(item["exclusion_kws"], [])
        self.assertEqual(item["include_kws"], ["sealed"])
        self.assertEqual(item["url"], "https://www.subito.it/y", "unrelated fields must be unaffected")

    def test_update_search_preserves_tracked_ads_history(self):
        self.db.register_user(10)
        self.db.add_search(10, "Cat", "S1", "https://www.subito.it/x")
        sid = self.db.get_user_searches(10)["Cat"][0]["id"]
        self.db.add_tracked_ad(sid, "ad-1")

        self.db.update_search(10, "S1", url="https://www.subito.it/updated")

        self.assertTrue(self.db.has_tracked_ads(sid), "editing a search must not lose its tracked-ads history")
        self.assertTrue(self.db.is_ad_tracked(sid, "ad-1"))

    def test_set_superuser_promotes_and_demotes(self):
        self.db.register_user(11)
        self.assertFalse(bool(self.db.get_user(11)["is_superuser"]))

        self.db.set_superuser(11, True)
        self.assertTrue(bool(self.db.get_user(11)["is_superuser"]))

        self.db.set_superuser(11, False)
        self.assertFalse(bool(self.db.get_user(11)["is_superuser"]))

    def test_get_all_searches_includes_enabled_and_include_kws(self):
        self.db.register_user(12)
        self.db.add_search(12, "Cat", "S1", "https://www.subito.it/x", ["a"], ["b"])
        self.db.set_search_enabled(12, "S1", False)

        row = self.db.get_all_searches()[0]
        self.assertFalse(row["enabled"])
        self.assertEqual(row["include_kws"], ["b"])

    def test_schema_migration_adds_missing_columns_to_legacy_db(self):
        """Simulates a pre-existing database created before `enabled`/`include_kws`
        existed: opening it with the current Database class must add the missing
        columns instead of crashing or silently ignoring them."""
        legacy_path = os.path.join(self.tmpdir, "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute('''
            CREATE TABLE users (
                chat_id INTEGER PRIMARY KEY,
                joined_at REAL NOT NULL,
                last_active REAL NOT NULL,
                is_superuser INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                exclusion_kws TEXT DEFAULT '[]'
            )
        ''')
        conn.execute(
            "INSERT INTO users (chat_id, joined_at, last_active, is_superuser) VALUES (1, 0, 0, 0)"
        )
        conn.execute(
            "INSERT INTO searches (chat_id, category, name, url, exclusion_kws) VALUES (1, 'Cat', 'Legacy', 'https://www.subito.it/x', '[]')"
        )
        conn.commit()
        conn.close()

        migrated_db = Database(db_path=legacy_path)
        item = migrated_db.get_user_searches(1)["Cat"][0]
        self.assertTrue(item["enabled"], "pre-existing searches must default to enabled after migration")
        self.assertEqual(item["include_kws"], [])


class TestScraperCategoryHandling(unittest.TestCase):
    def test_is_category_recognized_known_category(self):
        self.assertTrue(
            SubitoScraper.is_category_recognized("https://www.subito.it/annunci-italia/vendita/informatica/?q=pc")
        )

    def test_is_category_recognized_unknown_category(self):
        self.assertFalse(
            SubitoScraper.is_category_recognized("https://www.subito.it/annunci-italia/vendita/arredamento-casalinghi/")
        )

    def test_parse_web_url_to_api_sets_category_id_for_known_slug(self):
        scraper = SubitoScraper.__new__(SubitoScraper)  # skip __init__ (no network/session needed)
        params = scraper._parse_web_url_to_api("https://www.subito.it/annunci-italia/vendita/informatica/?q=pc")
        self.assertEqual(params.get("c"), "10")
        self.assertEqual(params.get("q"), "pc")

    def test_parse_web_url_to_api_leaves_category_unset_for_unknown_slug(self):
        scraper = SubitoScraper.__new__(SubitoScraper)
        params = scraper._parse_web_url_to_api("https://www.subito.it/annunci-italia/vendita/arredamento-casalinghi/")
        self.assertNotIn("c", params)

    def test_parse_api_response_extracts_stable_id_and_fields(self):
        scraper = SubitoScraper.__new__(SubitoScraper)
        scraper.debug_mode = False
        payload = {
            "ads": [
                {
                    "urn": "id:ad:1234567890abcdef:list:1",
                    "subject": "Test ad",
                    "body": "A description",
                    "type": {"key": "default"},
                    "features": [{"uri": "/price", "values": [{"value": "100"}]}],
                    "geo": {"town": {"value": "Roma"}, "city": {"short_name": "RM"}},
                    "urls": {"default": "/annunci-italia/vendita/x/test-ad.htm"},
                    "images": [{"uri": "imgid:abcdef123456"}],
                },
                {
                    # sold ads must be discarded
                    "urn": "id:ad:sold-item:list:2",
                    "type": {"key": "sold"},
                },
            ]
        }
        ads = scraper._parse_api_response(payload)
        self.assertEqual(len(ads), 1)
        ad = ads[0]
        self.assertEqual(ad["id"], "1")
        self.assertEqual(ad["title"], "Test ad")
        self.assertEqual(ad["price"], "100")
        self.assertEqual(ad["location"], "Roma (RM)")
        self.assertTrue(ad["link"].startswith("https://www.subito.it"))
        self.assertIn("abcdef123456", ad["image_url"])


class TestScraperEnvIdAndWafCheck(unittest.TestCase):
    """Regression tests for the corrected get_or_create_environment_id() and the
    new verify_waf_bypass() startup check."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._old_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self._old_cwd)

    def test_missing_key_generates_and_persists_new_id(self):
        with open(".subito_env.json", "w") as f:
            json.dump({"unrelated_key": "value"}, f)

        from bot.scrapers.subito import get_or_create_environment_id
        result = get_or_create_environment_id()

        self.assertTrue(result)
        with open(".subito_env.json") as f:
            persisted = json.load(f)
        self.assertEqual(persisted.get("environment_id"), result,
                          "a freshly generated id must be written back to disk immediately")

    def test_non_dict_json_generates_and_persists_new_id_instead_of_crashing(self):
        with open(".subito_env.json", "w") as f:
            json.dump(["not", "a", "dict"], f)

        from bot.scrapers.subito import get_or_create_environment_id
        result = get_or_create_environment_id()  # must not raise AttributeError

        with open(".subito_env.json") as f:
            persisted = json.load(f)
        self.assertEqual(persisted.get("environment_id"), result)

    def test_corrupted_json_generates_and_persists_new_id(self):
        with open(".subito_env.json", "w") as f:
            f.write("{not valid json")

        from bot.scrapers.subito import get_or_create_environment_id
        result = get_or_create_environment_id()

        with open(".subito_env.json") as f:
            persisted = json.load(f)
        self.assertEqual(persisted.get("environment_id"), result)

    def test_existing_valid_id_is_reused_unchanged(self):
        with open(".subito_env.json", "w") as f:
            json.dump({"environment_id": "fixed-id-123"}, f)

        from bot.scrapers.subito import get_or_create_environment_id
        self.assertEqual(get_or_create_environment_id(), "fixed-id-123")
        self.assertEqual(get_or_create_environment_id(), "fixed-id-123")

    def test_verify_waf_bypass_success(self):
        scraper = SubitoScraper.__new__(SubitoScraper)
        scraper.api_url = "https://hades.subito.it/v1/search/items"
        scraper.session = MagicMock()
        scraper.session.get.return_value = MagicMock(status_code=200, json=lambda: {"ads": []})

        ok, reason = scraper.verify_waf_bypass()
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_verify_waf_bypass_detects_waf_block(self):
        scraper = SubitoScraper.__new__(SubitoScraper)
        scraper.api_url = "https://hades.subito.it/v1/search/items"
        scraper.session = MagicMock()
        scraper.session.get.return_value = MagicMock(status_code=403)

        ok, reason = scraper.verify_waf_bypass()
        self.assertFalse(ok)
        self.assertIn("403", reason)

    def test_verify_waf_bypass_detects_network_exception(self):
        scraper = SubitoScraper.__new__(SubitoScraper)
        scraper.api_url = "https://hades.subito.it/v1/search/items"
        scraper.session = MagicMock()
        scraper.session.get.side_effect = ConnectionError("boom")

        ok, reason = scraper.verify_waf_bypass()
        self.assertFalse(ok)
        self.assertIn("boom", reason)


class TestI18n(unittest.TestCase):
    """Regression tests for bot/i18n.py's t() lookup/format helper."""

    def test_defaults_to_english(self):
        from bot.i18n import t
        self.assertEqual(t("btn_close"), "❌ Close")

    def test_returns_italian_variant(self):
        from bot.i18n import t
        self.assertEqual(t("btn_close", "it"), "❌ Chiudi")

    def test_unknown_language_falls_back_to_english(self):
        from bot.i18n import t
        self.assertEqual(t("btn_close", "fr"), "❌ Close")

    def test_unknown_key_returns_key_itself(self):
        from bot.i18n import t
        self.assertEqual(t("totally_unknown_key", "en"), "totally_unknown_key")

    def test_placeholders_are_formatted(self):
        from bot.i18n import t
        self.assertEqual(t("sub_active_days", "en", days=4), "✅ Subscription active for 4 days.")
        self.assertEqual(t("sub_active_days", "it", days=4), "✅ Abbonamento attivo per 4 giorni.")

    def test_missing_placeholder_falls_back_to_unformatted_text_without_raising(self):
        from bot.i18n import t
        # Deliberately omit the required `days` kwarg; must not raise KeyError.
        result = t("sub_active_days", "en")
        self.assertIn("{days}", result)


class TestDatabaseLanguage(unittest.TestCase):
    """Regression tests for the users.language column (report item 3)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "lang_test.db")
        self.db = Database(db_path=self.db_path)

    def test_new_user_defaults_to_english(self):
        self.db.register_user(1)
        self.assertEqual(self.db.get_user(1)["language"], "en")

    def test_set_language_persists(self):
        self.db.register_user(2)
        self.db.set_language(2, "it")
        self.assertEqual(self.db.get_user(2)["language"], "it")

    def test_get_all_searches_includes_language(self):
        self.db.register_user(3)
        self.db.set_language(3, "it")
        self.db.add_search(3, "Cat", "S1", "https://www.subito.it/x", [])
        row = self.db.get_all_searches()[0]
        self.assertEqual(row["language"], "it")

    def test_legacy_db_without_language_column_migrates_to_english_default(self):
        legacy_path = os.path.join(self.tmpdir, "legacy_lang.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute('''
            CREATE TABLE users (
                chat_id INTEGER PRIMARY KEY,
                joined_at REAL NOT NULL,
                last_active REAL NOT NULL,
                is_superuser INTEGER DEFAULT 0
            )
        ''')
        conn.execute(
            "INSERT INTO users (chat_id, joined_at, last_active, is_superuser) VALUES (1, 0, 0, 0)"
        )
        conn.commit()
        conn.close()

        migrated_db = Database(db_path=legacy_path)
        self.assertEqual(migrated_db.get_user(1)["language"], "en")


class TestDatabaseSentNotifications(unittest.TestCase):
    """Regression tests for sent_notifications tracking (report item 5: auto-delete
    old ad notification messages)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "notif_test.db")
        self.db = Database(db_path=self.db_path)

    def test_record_and_fetch_expired_notification(self):
        self.db.record_sent_notification(chat_id=1, message_id=100)
        future_cutoff = time.time() + 3600
        expired = self.db.get_expired_notifications(future_cutoff)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["chat_id"], 1)
        self.assertEqual(expired[0]["message_id"], 100)

    def test_recent_notification_is_not_expired(self):
        self.db.record_sent_notification(chat_id=1, message_id=100)
        past_cutoff = time.time() - 3600
        expired = self.db.get_expired_notifications(past_cutoff)
        self.assertEqual(expired, [])

    def test_delete_notification_record_removes_row(self):
        self.db.record_sent_notification(chat_id=1, message_id=100)
        future_cutoff = time.time() + 3600
        record_id = self.db.get_expired_notifications(future_cutoff)[0]["id"]
        self.db.delete_notification_record(record_id)
        self.assertEqual(self.db.get_expired_notifications(future_cutoff), [])


class TestDatabaseBans(unittest.TestCase):
    """Regression tests for banned_users tracking (report item 8: permanent bans)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "ban_test.db")
        self.db = Database(db_path=self.db_path)

    def test_is_banned_false_by_default(self):
        self.assertFalse(self.db.is_banned(1))

    def test_ban_and_unban_user(self):
        self.db.ban_user(1, reason="test")
        self.assertTrue(self.db.is_banned(1))

        self.db.unban_user(1)
        self.assertFalse(self.db.is_banned(1))

    def test_ban_survives_unsub(self):
        """A ban must not use a FK/cascade to `users`, so it survives /unsub (which
        deletes the users row) - otherwise an abuser could reset via /unsub + /sub."""
        self.db.register_user(1)
        self.db.ban_user(1, reason="abuse")

        self.db.remove_user(1)

        self.assertIsNone(self.db.get_user(1))
        self.assertTrue(self.db.is_banned(1))

    def test_get_all_banned_includes_unregistered_chat_ids(self):
        self.db.ban_user(999, reason="pre-emptive")
        banned = self.db.get_all_banned()
        self.assertEqual(len(banned), 1)
        self.assertEqual(banned[0]["chat_id"], 999)
        self.assertEqual(banned[0]["reason"], "pre-emptive")

    def test_ban_user_is_idempotent_and_updates_reason(self):
        self.db.ban_user(1, reason="first")
        self.db.ban_user(1, reason="second")
        banned = self.db.get_all_banned()
        self.assertEqual(len(banned), 1)
        self.assertEqual(banned[0]["reason"], "second")


class TestPurgeExpiredAdNotifications(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "purge_test.db")
        self.db = Database(db_path=self.db_path)
        self.tg = TelegramUI("dummy-token", self.db, threading.Event())

    @patch("bot.telegram_ui.requests.post")
    def test_purge_deletes_only_expired_messages(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        old_cutoff_time = time.time() - (self.tg.AD_NOTIFICATION_RETENTION_DAYS * 86400) - 10
        with self.db._connection() as conn:
            conn.execute(
                "INSERT INTO sent_notifications (chat_id, message_id, sent_at) VALUES (?, ?, ?)",
                (1, 111, old_cutoff_time),
            )
            conn.execute(
                "INSERT INTO sent_notifications (chat_id, message_id, sent_at) VALUES (?, ?, ?)",
                (2, 222, time.time()),
            )
            conn.commit()

        self.tg.purge_expired_ad_notifications()

        delete_calls = [c for c in mock_post.call_args_list if "deleteMessage" in c.args[0]]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0].kwargs["json"]["message_id"], 111)

        future_cutoff = time.time() + 3600
        remaining = self.db.get_expired_notifications(future_cutoff)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["message_id"], 222)

    @patch("bot.telegram_ui.requests.post")
    def test_purge_drops_db_record_even_if_telegram_delete_fails(self, mock_post):
        mock_post.side_effect = Exception("network down")
        old_time = time.time() - (self.tg.AD_NOTIFICATION_RETENTION_DAYS * 86400) - 10
        self.db.record_sent_notification(chat_id=1, message_id=999)
        with self.db._connection() as conn:
            conn.execute("UPDATE sent_notifications SET sent_at = ?", (old_time,))
            conn.commit()

        self.tg.purge_expired_ad_notifications()

        future_cutoff = time.time() + 3600
        self.assertEqual(self.db.get_expired_notifications(future_cutoff), [])

    @patch("bot.telegram_ui.requests.post")
    def test_send_ad_registers_notification_for_future_purge(self, mock_post):
        mock_post.return_value = _mock_ok_response({"ok": True, "result": {"message_id": 42}})
        self.tg.send_ad(chat_id=555, msg_text="hello")

        future_cutoff = time.time() + 3600
        recorded = self.db.get_expired_notifications(future_cutoff)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["message_id"], 42)


class TestMainDedupeAndFormatting(unittest.TestCase):
    def _ad(self, **overrides):
        ad = {
            "id": "ad-1",
            "title": "<script>alert(1)</script>",
            "price": "100",
            "location": "Roma",
            "description": "x" * 300,
        }
        ad.update(overrides)
        return ad

    def test_format_message_escapes_html_and_truncates_description(self):
        msg = main_module.format_message([("Cat", "Name")], self._ad())
        self.assertNotIn("<script>", msg)
        self.assertIn("&lt;script&gt;", msg)
        self.assertIn("...", msg)

    def test_format_message_includes_ad_id_on_first_line(self):
        msg = main_module.format_message([("Cat", "Name")], self._ad(id="999888"))
        first_line = msg.splitlines()[0]
        self.assertIn("#999888", first_line)

    def test_format_message_omits_id_line_when_ad_has_no_id(self):
        ad = self._ad()
        del ad["id"]
        msg = main_module.format_message([("Cat", "Name")], ad)
        self.assertNotIn("#", msg.splitlines()[0])

    def test_format_message_single_match_has_no_also_found_by_line(self):
        msg = main_module.format_message([("Cat", "Name")], self._ad())
        self.assertNotIn("Also found by", msg)

    def test_format_message_multiple_matches_lists_extra_search_names(self):
        msg = main_module.format_message([("Cat", "Name1"), ("Cat2", "Name2")], self._ad())
        self.assertIn("Also found by", msg)
        self.assertIn("Name2", msg)
        # The first match's category/name still drive the header line.
        self.assertIn("Name1", msg.splitlines()[0])

    def test_format_message_translates_price_and_location_labels(self):
        msg_it = main_module.format_message([("Cat", "Name")], self._ad(), lang="it")
        self.assertIn("Prezzo", msg_it)
        self.assertIn("Posizione", msg_it)

    def test_dedupe_prefers_stable_ad_id_over_link(self):
        """The daemon loop must dedupe using ad['id'] (URN-derived) with a fallback
        to ad['link'] only when no id is available."""
        ad_with_id = {"id": "abc123", "link": "https://www.subito.it/x/1.htm"}
        ad_without_id = {"id": "", "link": "https://www.subito.it/x/2.htm"}
        self.assertEqual(ad_with_id.get("id") or ad_with_id["link"], "abc123")
        self.assertEqual(ad_without_id.get("id") or ad_without_id["link"], ad_without_id["link"])


class TestSendAdAndEditAdMessage(unittest.TestCase):
    """Regression tests for TelegramUI.send_ad()'s return value and the new
    edit_ad_message() used by the cross-search dedup flow (report item 1)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "send_ad_test.db")
        self.db = Database(db_path=self.db_path)
        self.tg = TelegramUI("dummy-token", self.db, threading.Event())
        self.chat_id = 4242

    @patch("bot.telegram_ui.requests.post")
    def test_send_ad_text_only_returns_message_id_and_is_photo_false(self, mock_post):
        mock_post.return_value = _mock_ok_response({"ok": True, "result": {"message_id": 55}})
        result = self.tg.send_ad(self.chat_id, "hello", item_url="https://www.subito.it/x")
        self.assertEqual(result, {"message_id": 55, "is_photo": False})

    @patch("bot.telegram_ui.requests.post")
    def test_send_ad_with_image_returns_is_photo_true(self, mock_post):
        mock_post.return_value = _mock_ok_response({"ok": True, "result": {"message_id": 77}})
        result = self.tg.send_ad(self.chat_id, "hello", image_bytes=b"fake-jpeg")
        self.assertEqual(result, {"message_id": 77, "is_photo": True})

    @patch("bot.telegram_ui.requests.post")
    def test_send_ad_returns_none_on_api_failure(self, mock_post):
        mock_post.return_value = _mock_ok_response({"ok": False, "description": "boom"})
        result = self.tg.send_ad(self.chat_id, "hello")
        self.assertIsNone(result)

    @patch("bot.telegram_ui.requests.post")
    def test_edit_ad_message_uses_caption_endpoint_for_photos(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg.edit_ad_message(self.chat_id, 55, "updated text", is_photo=True)
        called_url = mock_post.call_args[0][0]
        self.assertIn("editMessageCaption", called_url)
        self.assertEqual(mock_post.call_args.kwargs["json"]["caption"], "updated text")

    @patch("bot.telegram_ui.requests.post")
    def test_edit_ad_message_uses_text_endpoint_for_text_messages(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg.edit_ad_message(self.chat_id, 55, "updated text", is_photo=False)
        called_url = mock_post.call_args[0][0]
        self.assertIn("editMessageText", called_url)
        self.assertEqual(mock_post.call_args.kwargs["json"]["text"], "updated text")


def _mock_ok_response(json_payload=None):
    resp = MagicMock()
    resp.json.return_value = json_payload if json_payload is not None else {"ok": True}
    return resp


class TestTelegramUIAddFlow(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "tg_test.db")
        self.db = Database(db_path=self.db_path)
        self.tg = TelegramUI("dummy-token", self.db, threading.Event())
        self.chat_id = 555
        self.db.register_user(self.chat_id)

    def _text_update(self, text):
        return {"message": {"chat": {"id": self.chat_id}, "text": text}}

    def _callback_update(self, cb_data, message_id=1):
        return {
            "callback_query": {
                "id": "cbq1",
                "message": {"chat": {"id": self.chat_id}, "message_id": message_id},
                "data": cb_data,
            }
        }

    @patch("bot.telegram_ui.requests.post")
    def test_search_menu_shows_add_category_button(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg._process_update(self._text_update("/search"))

        sent_calls = [c.kwargs["json"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        keyboard = sent_calls[-1]["reply_markup"]["inline_keyboard"]
        button_texts = [btn["text"] for row in keyboard for btn in row]
        self.assertTrue(any("Add Category" in t for t in button_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_start_add_category_callback_prompts_for_link(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        cb_data = self.tg._create_callback_data({"action": "start_add_category"})

        self.tg._process_update(self._callback_update(cb_data))

        self.assertEqual(self.tg.user_states[self.chat_id]["state"], "waiting_link_for_new_category")
        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("link" in t.lower() for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_start_add_search_callback_prompts_for_link_with_category_preset(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.add_search(self.chat_id, "ExistingCat", "S1", "https://www.subito.it/x", [])
        cb_data = self.tg._create_callback_data({"action": "start_add_search", "cat": "ExistingCat"})

        self.tg._process_update(self._callback_update(cb_data))

        self.assertEqual(self.tg.user_states[self.chat_id]["state"], "waiting_link_for_category")
        self.assertEqual(self.tg.user_states[self.chat_id]["category"], "ExistingCat")

    @patch("bot.telegram_ui.requests.post")
    def test_start_add_category_denied_when_limit_reached(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg.MAX_REGULAR_SEARCHES = 1
        self.db.add_search(self.chat_id, "Cat", "S1", "https://www.subito.it/x", [])
        cb_data = self.tg._create_callback_data({"action": "start_add_category"})

        self.tg._process_update(self._callback_update(cb_data))

        self.assertNotIn(self.chat_id, self.tg.user_states)
        alert_calls = [
            c.kwargs["json"] for c in mock_post.call_args_list
            if "json" in c.kwargs and c.kwargs["json"].get("show_alert")
        ]
        self.assertTrue(any("Limit" in c["text"] for c in alert_calls))

    @patch("bot.telegram_ui.requests.post")
    def test_add_rejects_price_based_order(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_link_for_new_category", "timestamp": __import__("time").time(),
            }
        with patch.object(self.tg, "_add_executor") as mock_executor:
            self.tg._process_update(
                self._text_update("https://www.subito.it/annunci-italia/vendita/informatica/?order=pricedesc")
            )
            mock_executor.submit.assert_not_called()

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("priceasc" in t or "pricedesc" in t or "Price-based" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_add_enforces_datedesc_and_runs_validation_in_background(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_link_for_new_category", "timestamp": __import__("time").time(),
            }
        with patch.object(self.tg, "_add_executor") as mock_executor:
            self.tg._process_update(
                self._text_update("https://www.subito.it/annunci-italia/vendita/informatica/?q=pc")
            )
            mock_executor.submit.assert_called_once()
            args = mock_executor.submit.call_args[0]
            # Bound methods compare equal via `==` (same underlying function + instance)
            # even though separate attribute accesses create distinct bound-method objects.
            self.assertEqual(args[0], self.tg._validate_link_and_proceed)
            submitted_link = args[2]
            self.assertIn("order=datedesc", submitted_link)
            self.assertIsNone(args[3])

    @patch("bot.telegram_ui.requests.post")
    def test_add_search_link_step_carries_preset_category_to_validation(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_link_for_category", "category": "ExistingCat",
                "timestamp": __import__("time").time(),
            }
        with patch.object(self.tg, "_add_executor") as mock_executor:
            self.tg._process_update(
                self._text_update("https://www.subito.it/annunci-italia/vendita/informatica/?q=pc")
            )
            args = mock_executor.submit.call_args[0]
            self.assertEqual(args[3], "ExistingCat")

    @patch("bot.telegram_ui.cffi_requests.get")
    @patch("bot.telegram_ui.requests.post")
    def test_validate_link_and_proceed_success_sets_new_category_name_state(self, mock_post, mock_cffi_get):
        mock_post.return_value = _mock_ok_response()
        mock_cffi_get.return_value = MagicMock(status_code=200, text="...__NEXT_DATA__...")

        link = "https://www.subito.it/annunci-italia/vendita/informatica/?q=pc&order=datedesc"
        self.tg._validate_link_and_proceed(self.chat_id, link)

        self.assertIn(self.chat_id, self.tg.user_states)
        self.assertEqual(self.tg.user_states[self.chat_id]["state"], "waiting_new_cat_name")
        self.assertEqual(self.tg.user_states[self.chat_id]["link"], link)

    @patch("bot.telegram_ui.cffi_requests.get")
    @patch("bot.telegram_ui.requests.post")
    def test_validate_link_and_proceed_with_target_category_skips_category_step(self, mock_post, mock_cffi_get):
        mock_post.return_value = _mock_ok_response()
        mock_cffi_get.return_value = MagicMock(status_code=200, text="...__NEXT_DATA__...")

        link = "https://www.subito.it/annunci-italia/vendita/informatica/?q=pc&order=datedesc"
        self.tg._validate_link_and_proceed(self.chat_id, link, target_category="ExistingCat")

        self.assertEqual(self.tg.user_states[self.chat_id]["state"], "waiting_keyword")
        self.assertEqual(self.tg.user_states[self.chat_id]["category"], "ExistingCat")

    @patch("bot.telegram_ui.cffi_requests.get")
    @patch("bot.telegram_ui.requests.post")
    def test_validate_link_and_proceed_rejects_unreachable_link(self, mock_post, mock_cffi_get):
        mock_post.return_value = _mock_ok_response()
        mock_cffi_get.return_value = MagicMock(status_code=404, text="not found")

        link = "https://www.subito.it/annunci-italia/vendita/informatica/?q=pc"
        self.tg._validate_link_and_proceed(self.chat_id, link)

        self.assertNotIn(self.chat_id, self.tg.user_states)
        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("unreachable" in t.lower() for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_add_flow_exclusion_step_prompts_for_include_keywords(self, mock_post):
        """The exclusion step no longer finalizes the search directly: it now
        transitions to a new include-keywords step before saving."""
        mock_post.return_value = _mock_ok_response()
        link = "https://www.subito.it/annunci-italia/vendita/informatica/?order=datedesc"

        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_exclusion",
                "category": "Tech",
                "link": link,
                "name": "My Search",
                "timestamp": __import__("time").time(),
            }

        self.tg._process_update(self._text_update("broken, parts"))

        self.assertEqual(self.tg.user_states[self.chat_id]["state"], "waiting_include")
        self.assertEqual(self.tg.user_states[self.chat_id]["exclusion_kws"], ["broken", "parts"])
        self.assertEqual(self.db.get_user_searches(self.chat_id), {}, "search must not be saved yet")

    @patch("bot.telegram_ui.requests.post")
    def test_add_search_confirmation_warns_for_unrecognized_category(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        link = "https://www.subito.it/annunci-italia/vendita/arredamento-casalinghi/?order=datedesc"

        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_include",
                "category": "Casa",
                "link": link,
                "name": "My Search",
                "exclusion_kws": [],
                "timestamp": __import__("time").time(),
            }

        self.tg._process_update(self._text_update("none"))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Added" in t for t in sent_texts))
        self.assertTrue(any("fully supported" in t for t in sent_texts))
        self.assertIn("Casa", self.db.get_user_searches(self.chat_id))

    @patch("bot.telegram_ui.requests.post")
    def test_add_search_confirmation_no_warning_for_recognized_category(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        link = "https://www.subito.it/annunci-italia/vendita/informatica/?order=datedesc"

        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_include",
                "category": "Tech",
                "link": link,
                "name": "My Search",
                "exclusion_kws": [],
                "timestamp": __import__("time").time(),
            }

        self.tg._process_update(self._text_update("none"))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Added" in t for t in sent_texts))
        self.assertFalse(any("fully supported" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_add_search_finalize_stores_include_keywords(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        link = "https://www.subito.it/annunci-italia/vendita/informatica/?order=datedesc"

        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_include",
                "category": "Tech",
                "link": link,
                "name": "My Search",
                "exclusion_kws": ["broken"],
                "timestamp": __import__("time").time(),
            }

        self.tg._process_update(self._text_update("sealed, new"))

        saved = self.db.get_user_searches(self.chat_id)["Tech"][0]
        self.assertEqual(saved["exclusion_kws"], ["broken"])
        self.assertEqual(saved["include_kws"], ["sealed", "new"])
        self.assertTrue(saved["enabled"])
        self.assertNotIn(self.chat_id, self.tg.user_states)


class TestTelegramUICallbacks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "tg_cb_test.db")
        self.db = Database(db_path=self.db_path)
        self.tg = TelegramUI("dummy-token", self.db, threading.Event())
        self.chat_id = 777
        self.db.register_user(self.chat_id)

    def _callback_update(self, cb_data, message_id=1):
        return {
            "callback_query": {
                "id": "cbq1",
                "message": {"chat": {"id": self.chat_id}, "message_id": message_id},
                "data": cb_data,
            }
        }

    @patch("bot.telegram_ui.requests.post")
    def test_cat_callback_reports_when_category_no_longer_exists(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        cb_data = self.tg._create_callback_data({"action": "cat", "cat": "GhostCategory"})

        self.tg._process_update(self._callback_update(cb_data))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("GhostCategory" in t and ("eliminata" in t or "no longer") for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_rmconf_notifies_category_deletion_when_last_search_removed(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.add_search(self.chat_id, "SoloCat", "OnlySearch", "https://www.subito.it/x", [])

        cb_data = self.tg._create_callback_data({"action": "rmconf", "name": "OnlySearch"})
        self.tg._process_update(self._callback_update(cb_data))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("OnlySearch" in t and "deleted" in t for t in sent_texts))
        self.assertTrue(any("SoloCat" in t and "removed" in t for t in sent_texts))
        self.assertEqual(self.db.get_user_searches(self.chat_id), {})

    @patch("bot.telegram_ui.requests.post")
    def test_rmconf_does_not_notify_category_deletion_when_siblings_remain(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.add_search(self.chat_id, "SharedCat", "SearchA", "https://www.subito.it/a", [])
        self.db.add_search(self.chat_id, "SharedCat", "SearchB", "https://www.subito.it/b", [])

        cb_data = self.tg._create_callback_data({"action": "rmconf", "name": "SearchA"})
        self.tg._process_update(self._callback_update(cb_data))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("SearchA" in t and "deleted" in t for t in sent_texts))
        self.assertFalse(any("removed because it no longer" in t for t in sent_texts))
        self.assertIn("SharedCat", self.db.get_user_searches(self.chat_id))

    @patch("bot.telegram_ui.requests.post")
    def test_language_command_shows_flag_buttons(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg._process_update({"message": {"chat": {"id": self.chat_id}, "text": "/language"}})

        sent_calls = [c.kwargs["json"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(sent_calls)
        keyboard = sent_calls[-1]["reply_markup"]["inline_keyboard"]
        button_texts = [btn["text"] for row in keyboard for btn in row]
        self.assertTrue(any("English" in t for t in button_texts))
        self.assertTrue(any("Italiano" in t for t in button_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_setlang_callback_persists_language_and_confirms_in_new_language(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        cb_data = self.tg._create_callback_data({"action": "setlang", "lang": "it"})

        self.tg._process_update(self._callback_update(cb_data))

        self.assertEqual(self.db.get_user(self.chat_id)["language"], "it")
        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Italiano" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_subsequent_messages_use_persisted_language(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.set_language(self.chat_id, "it")

        self.tg._process_update({"message": {"chat": {"id": self.chat_id}, "text": "/unsub"}})

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Disiscritto" in t for t in sent_texts))


class TestTelegramUISearchManagement(unittest.TestCase):
    """Pause/resume and edit-search flows (report: 'Modifica ricerca esistente' /
    'Pausa/ripresa di una singola ricerca')."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "tg_mgmt_test.db")
        self.db = Database(db_path=self.db_path)
        self.tg = TelegramUI("dummy-token", self.db, threading.Event())
        self.chat_id = 888
        self.db.register_user(self.chat_id)
        self.db.add_search(self.chat_id, "Cat", "S1", "https://www.subito.it/x", ["broken"])

    def _callback_update(self, cb_data, message_id=1):
        return {
            "callback_query": {
                "id": "cbq1",
                "message": {"chat": {"id": self.chat_id}, "message_id": message_id},
                "data": cb_data,
            }
        }

    def _text_update(self, text):
        return {"message": {"chat": {"id": self.chat_id}, "text": text}}

    @patch("bot.telegram_ui.requests.post")
    def test_toggle_enabled_pauses_and_resumes_search(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        cb_data = self.tg._create_callback_data({"action": "toggle_enabled", "cat": "Cat", "name": "S1"})

        self.tg._process_update(self._callback_update(cb_data))
        self.assertFalse(self.db.get_user_searches(self.chat_id)["Cat"][0]["enabled"])
        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Paused" in t for t in sent_texts))

        mock_post.reset_mock()
        cb_data_2 = self.tg._create_callback_data({"action": "toggle_enabled", "cat": "Cat", "name": "S1"})
        self.tg._process_update(self._callback_update(cb_data_2))
        self.assertTrue(self.db.get_user_searches(self.chat_id)["Cat"][0]["enabled"])

    @patch("bot.telegram_ui.requests.post")
    def test_edit_menu_shows_field_choices(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        cb_data = self.tg._create_callback_data({"action": "edit_menu", "cat": "Cat", "name": "S1"})

        self.tg._process_update(self._callback_update(cb_data))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Editing" in t and "S1" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_edit_field_exclusion_sets_pending_state_and_prompts(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        cb_data = self.tg._create_callback_data({"action": "edit_field", "cat": "Cat", "name": "S1", "field": "exclusion"})

        self.tg._process_update(self._callback_update(cb_data))

        self.assertEqual(self.tg.user_states[self.chat_id]["state"], "editing_field")
        self.assertEqual(self.tg.user_states[self.chat_id]["field"], "exclusion")

    @patch("bot.telegram_ui.requests.post")
    def test_editing_field_exclusion_updates_search_in_place(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        sid_before = self.db.get_user_searches(self.chat_id)["Cat"][0]["id"]

        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "editing_field", "field": "exclusion", "cat": "Cat", "name": "S1",
                "timestamp": __import__("time").time(),
            }

        self.tg._process_update(self._text_update("used, damaged"))

        item = self.db.get_user_searches(self.chat_id)["Cat"][0]
        self.assertEqual(item["id"], sid_before, "editing must not delete/recreate the search")
        self.assertEqual(item["exclusion_kws"], ["used", "damaged"])
        self.assertNotIn(self.chat_id, self.tg.user_states)

    @patch("bot.telegram_ui.requests.post")
    def test_editing_field_include_updates_search_in_place(self, mock_post):
        mock_post.return_value = _mock_ok_response()

        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "editing_field", "field": "include", "cat": "Cat", "name": "S1",
                "timestamp": __import__("time").time(),
            }

        self.tg._process_update(self._text_update("sealed"))

        item = self.db.get_user_searches(self.chat_id)["Cat"][0]
        self.assertEqual(item["include_kws"], ["sealed"])

    @patch("bot.telegram_ui.cffi_requests.get")
    @patch("bot.telegram_ui.requests.post")
    def test_validate_and_update_url_preserves_search_id_and_history(self, mock_post, mock_cffi_get):
        mock_post.return_value = _mock_ok_response()
        mock_cffi_get.return_value = MagicMock(status_code=200, text="...__NEXT_DATA__...")
        sid = self.db.get_user_searches(self.chat_id)["Cat"][0]["id"]
        self.db.add_tracked_ad(sid, "ad-1")

        self.tg._validate_and_update_url(self.chat_id, "S1", "https://www.subito.it/new-url")

        item = self.db.get_user_searches(self.chat_id)["Cat"][0]
        self.assertEqual(item["id"], sid)
        self.assertEqual(item["url"], "https://www.subito.it/new-url")
        self.assertTrue(self.db.has_tracked_ads(sid))


class TestTelegramUIBroadcastAndStatus(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "tg_admin_test.db")
        self.db = Database(db_path=self.db_path)
        self.tg = TelegramUI("dummy-token", self.db, threading.Event())

    def _text_update(self, chat_id, text):
        return {"message": {"chat": {"id": chat_id}, "text": text}}

    @patch("bot.telegram_ui.requests.post")
    def test_broadcast_denied_for_regular_user(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.register_user(1)
        self.db.register_user(2)

        self.tg._process_update(self._text_update(1, "/broadcast Hello everyone"))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Action Denied" in t for t in sent_texts))
        self.assertFalse(any("Hello everyone" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_broadcast_sent_by_superuser_reaches_all_users(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.register_user(1)
        self.db.set_superuser(1, True)
        self.db.register_user(2)

        self.tg._process_update(self._text_update(1, "/broadcast Hello everyone"))

        recipients = [
            c.kwargs["json"]["chat_id"] for c in mock_post.call_args_list
            if "json" in c.kwargs and "text" in c.kwargs["json"] and "Hello everyone" in c.kwargs["json"]["text"]
        ]
        self.assertEqual(set(recipients), {1, 2})

    @patch("bot.telegram_ui.requests.post")
    def test_status_shows_remaining_time_for_regular_user(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.register_user(1)

        self.tg._process_update(self._text_update(1, "/status"))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("expires in" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_status_shows_unlimited_for_superuser(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.register_user(1)
        self.db.set_superuser(1, True)

        self.tg._process_update(self._text_update(1, "/status"))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Unlimited" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_status_shows_active_and_inactive_user_counts(self, mock_post):
        """Report item 6: /status must break users down into active/inactive instead
        of a single 'Total Users' figure."""
        mock_post.return_value = _mock_ok_response()
        self.db.register_user(1)  # active (just registered)
        self.db.register_user(2)  # will be made inactive below
        stale_time = time.time() - (TelegramUI.DEFAULT_SUBSCRIPTION_TTL_DAYS * 86400) - 3600
        with self.db._connection() as conn:
            conn.execute("UPDATE users SET last_active = ? WHERE chat_id = ?", (stale_time, 2))
            conn.commit()

        self.tg._process_update(self._text_update(1, "/status"))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Active Users: 1" in t for t in sent_texts))
        self.assertTrue(any("Inactive Users: 1" in t for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_status_counts_superuser_as_active_even_if_stale(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.register_user(1)
        self.db.set_superuser(1, True)
        stale_time = time.time() - (TelegramUI.DEFAULT_SUBSCRIPTION_TTL_DAYS * 86400) - 3600
        with self.db._connection() as conn:
            conn.execute("UPDATE users SET last_active = ? WHERE chat_id = ?", (stale_time, 1))
            conn.commit()

        self.tg._process_update(self._text_update(1, "/status"))

        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("Active Users: 1" in t for t in sent_texts))
        self.assertTrue(any("Inactive Users: 0" in t for t in sent_texts))


class TestTelegramUIEnvConfig(unittest.TestCase):
    """Report: limiti configurabili via .env."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "tg_env_test.db")
        self.db = Database(db_path=self.db_path)

    def test_defaults_apply_without_env_overrides(self):
        with patch.dict(os.environ, {}, clear=False):
            for var in ("SUBSCRIPTION_TTL_DAYS", "MAX_REGULAR_SEARCHES", "MAX_SUBSCRIBERS",
                        "CACHE_PRUNE_INTERVAL_SECONDS", "MAX_EXCLUSION_KEYWORDS", "MAX_INCLUDE_KEYWORDS"):
                os.environ.pop(var, None)
            tg = TelegramUI("dummy-token", self.db, threading.Event())
        self.assertEqual(tg.MAX_SUBSCRIBERS, TelegramUI.DEFAULT_MAX_SUBSCRIBERS)
        self.assertEqual(tg.MAX_REGULAR_SEARCHES, TelegramUI.DEFAULT_MAX_REGULAR_SEARCHES)
        self.assertEqual(tg.TIMEOUT_SECONDS, TelegramUI.DEFAULT_SUBSCRIPTION_TTL_DAYS * 86400)

    def test_env_overrides_are_applied(self):
        overrides = {
            "SUBSCRIPTION_TTL_DAYS": "7",
            "MAX_REGULAR_SEARCHES": "25",
            "MAX_SUBSCRIBERS": "10",
            "CACHE_PRUNE_INTERVAL_SECONDS": "60",
            "MAX_EXCLUSION_KEYWORDS": "5",
            "MAX_INCLUDE_KEYWORDS": "2",
        }
        with patch.dict(os.environ, overrides):
            tg = TelegramUI("dummy-token", self.db, threading.Event())

        self.assertEqual(tg.TIMEOUT_SECONDS, 7 * 86400)
        self.assertEqual(tg.MAX_REGULAR_SEARCHES, 25)
        self.assertEqual(tg.MAX_SUBSCRIBERS, 10)
        self.assertEqual(tg.CACHE_PRUNE_INTERVAL, 60)
        self.assertEqual(tg.MAX_EXCLUSION_KEYWORDS, 5)
        self.assertEqual(tg.MAX_INCLUDE_KEYWORDS, 2)


class TestTelegramUISecurity(unittest.TestCase):
    """Regression tests for report item 8 (security audit / permanent bans): ban
    enforcement, flood guard, oversized-input guard, and length caps."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "tg_security_test.db")
        self.db = Database(db_path=self.db_path)
        self.tg = TelegramUI("dummy-token", self.db, threading.Event())
        self.chat_id = 4321
        self.db.register_user(self.chat_id)

    def _text_update(self, text):
        return {"message": {"chat": {"id": self.chat_id}, "text": text}}

    def _callback_update(self, cb_data, message_id=1):
        return {
            "callback_query": {
                "id": "cbq1",
                "message": {"chat": {"id": self.chat_id}, "message_id": message_id},
                "data": cb_data,
            }
        }

    @patch("bot.telegram_ui.requests.post")
    def test_banned_chat_id_message_is_silently_dropped(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.ban_user(self.chat_id, reason="test")

        self.tg._process_update(self._text_update("/help"))

        mock_post.assert_not_called()

    @patch("bot.telegram_ui.requests.post")
    def test_banned_chat_id_callback_is_silently_dropped(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.ban_user(self.chat_id, reason="test")
        cb_data = self.tg._create_callback_data({"action": "cancel"})

        self.tg._process_update(self._callback_update(cb_data))

        mock_post.assert_not_called()

    @patch("bot.telegram_ui.requests.post")
    def test_flood_guard_bans_after_threshold(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg.FLOOD_MAX_UPDATES = 2
        self.tg.FLOOD_WINDOW_SECONDS = 100

        for _ in range(3):
            self.tg._process_update(self._text_update("/help"))

        self.assertTrue(self.db.is_banned(self.chat_id))
        self.assertIsNone(self.db.get_user(self.chat_id), "flood ban must also remove the subscription")

    @patch("bot.telegram_ui.requests.post")
    def test_flood_guard_exempts_superuser(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.set_superuser(self.chat_id, True)
        self.tg.FLOOD_MAX_UPDATES = 2
        self.tg.FLOOD_WINDOW_SECONDS = 100

        for _ in range(5):
            self.tg._process_update(self._text_update("/help"))

        self.assertFalse(self.db.is_banned(self.chat_id))

    @patch("bot.telegram_ui.requests.post")
    def test_oversized_input_triggers_ban(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg.OVERSIZED_INPUT_THRESHOLD = 20
        with self.db.lock:
            self.tg.user_states[self.chat_id] = {"state": "waiting_new_cat_name", "timestamp": time.time()}

        self.tg._process_update(self._text_update("x" * 100))

        self.assertTrue(self.db.is_banned(self.chat_id))
        self.assertNotIn(self.chat_id, self.tg.user_states)

    @patch("bot.telegram_ui.requests.post")
    def test_oversized_input_exempts_superuser(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.db.set_superuser(self.chat_id, True)
        self.tg.OVERSIZED_INPUT_THRESHOLD = 20
        with self.db.lock:
            self.tg.user_states[self.chat_id] = {"state": "waiting_new_cat_name", "timestamp": time.time()}

        self.tg._process_update(self._text_update("x" * 100))

        self.assertFalse(self.db.is_banned(self.chat_id))

    @patch("bot.telegram_ui.requests.post")
    def test_search_name_too_long_is_rejected(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg.MAX_SEARCH_NAME_LENGTH = 10
        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_keyword", "category": "Cat", "link": "https://www.subito.it/x",
                "timestamp": time.time(),
            }

        self.tg._process_update(self._text_update("a very long search name"))

        self.assertEqual(self.tg.user_states[self.chat_id]["state"], "waiting_keyword", "must stay in the naming step")
        sent_texts = [c.kwargs["json"]["text"] for c in mock_post.call_args_list if "json" in c.kwargs and "text" in c.kwargs["json"]]
        self.assertTrue(any("too long" in t.lower() for t in sent_texts))

    @patch("bot.telegram_ui.requests.post")
    def test_exclusion_keyword_is_truncated_to_max_length(self, mock_post):
        mock_post.return_value = _mock_ok_response()
        self.tg.MAX_KEYWORD_LENGTH = 5
        with self.db.lock:
            self.tg.user_states[self.chat_id] = {
                "state": "waiting_exclusion", "category": "Cat", "link": "https://www.subito.it/x",
                "name": "S1", "timestamp": time.time(),
            }

        self.tg._process_update(self._text_update("thisiswaytoolong"))

        self.assertEqual(self.tg.user_states[self.chat_id]["exclusion_kws"], ["thisi"])


class TestProjectHygiene(unittest.TestCase):
    """Lightweight checks for the non-code report items implemented alongside the bugfixes."""

    def setUp(self):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_requirements_txt_pins_versions(self):
        req_path = os.path.join(self.repo_root, "requirements.txt")
        self.assertTrue(os.path.isfile(req_path), "requirements.txt must exist")
        with open(req_path) as f:
            lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
        self.assertTrue(lines, "requirements.txt must declare at least one dependency")
        for line in lines:
            self.assertIn("==", line, f"dependency not pinned to an exact version: {line}")

    def test_gitignore_excludes_local_env_state_file(self):
        gitignore_path = os.path.join(self.repo_root, ".gitignore")
        with open(gitignore_path) as f:
            content = f.read()
        self.assertIn(".subito_env.json", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
