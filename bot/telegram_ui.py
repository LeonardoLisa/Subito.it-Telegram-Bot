"""
Filename: bot/telegram_ui.py
Version: 4.2.0
Date: 2026-04-30
Author: Leonardo Lisa
Description: Standardized Telegram Bot Controller updated for SQLite integration.
             Implements per-user targeted broadcasting, automatic database pruning,
             and UI flows for managing per-user searches with exclusion keywords.
Requirements: requests, curl_cffi

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

from curl_cffi import requests as cffi_requests
import requests
import os
import time
import html
import uuid
import json
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor

from .scrapers.subito import SubitoScraper
from .i18n import t, DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

class TelegramUI:
    # Defaults, all overridable via .env - see README "Configurazione avanzata via .env".
    DEFAULT_SUBSCRIPTION_TTL_DAYS = 4
    DEFAULT_CACHE_PRUNE_INTERVAL_SECONDS = 1800
    DEFAULT_MAX_REGULAR_SEARCHES = 15
    DEFAULT_MAX_SUBSCRIBERS = 3
    DEFAULT_MAX_EXCLUSION_KEYWORDS = 3
    DEFAULT_MAX_INCLUDE_KEYWORDS = 3

    # Report item 5: intentionally HARDCODED (not read from .env like the constants
    # above) - ad notification messages older than this are auto-deleted every scan
    # cycle by purge_expired_ad_notifications(). See README "Parametri di sicurezza e
    # manutenzione hardcoded" for the rationale (kept as a fixed product behavior
    # rather than a per-install tunable).
    AD_NOTIFICATION_RETENTION_DAYS = 5

    # Report item 8 (security audit): conservative, intentionally HARDCODED
    # suspicious-activity thresholds (not read from .env, unlike the tunables above)
    # - relaxing abuse protection per-install would defeat its purpose. See README
    # "Parametri di sicurezza e manutenzione hardcoded". Superusers are exempt from
    # all three (an admin pasting a long value should never lock themselves out).
    MAX_SEARCH_NAME_LENGTH = 60          # search name field
    MAX_KEYWORD_LENGTH = 40              # each individual exclusion/include keyword
    FLOOD_WINDOW_SECONDS = 10            # sliding window for the flood guard below
    FLOOD_MAX_UPDATES = 20               # max updates/window before a chat_id is banned
    OVERSIZED_INPUT_THRESHOLD = 500      # chars; only checked on short-value add/edit
                                          # steps (category/search name/keywords/link),
                                          # NOT a blanket cap on every message (e.g.
                                          # /broadcast text is intentionally unbounded)

    def __init__(self, token, db, shutdown_event):
        self.token = token
        self.db = db
        self.shutdown_event = shutdown_event
        self.debug_mode = False
        self.user_states = {}
        self.callback_cache = {}
        # In-memory-only sliding window of recent update timestamps per chat_id, used
        # by _check_flood(). Not persisted: a restart resetting the window is an
        # acceptable trade-off for a lightweight abuse guard.
        self._flood_tracker = {}
        self.start_time = time.time()
        self.last_cache_prune = time.time()

        self.TIMEOUT_SECONDS = int(os.getenv("SUBSCRIPTION_TTL_DAYS", str(self.DEFAULT_SUBSCRIPTION_TTL_DAYS))) * 24 * 3600
        self.CACHE_PRUNE_INTERVAL = int(os.getenv("CACHE_PRUNE_INTERVAL_SECONDS", str(self.DEFAULT_CACHE_PRUNE_INTERVAL_SECONDS)))
        self.MAX_REGULAR_SEARCHES = int(os.getenv("MAX_REGULAR_SEARCHES", str(self.DEFAULT_MAX_REGULAR_SEARCHES)))
        self.MAX_SUBSCRIBERS = int(os.getenv("MAX_SUBSCRIBERS", str(self.DEFAULT_MAX_SUBSCRIBERS)))
        self.MAX_EXCLUSION_KEYWORDS = int(os.getenv("MAX_EXCLUSION_KEYWORDS", str(self.DEFAULT_MAX_EXCLUSION_KEYWORDS)))
        self.MAX_INCLUDE_KEYWORDS = int(os.getenv("MAX_INCLUDE_KEYWORDS", str(self.DEFAULT_MAX_INCLUDE_KEYWORDS)))

        # /add link validation involves a network round-trip (up to ~10s). Running it here
        # instead of inline keeps the single-threaded poll_updates loop free to serve other users.
        self._add_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="add-validate")
        
    def _debug_print(self, error_msg):
        if self.debug_mode:
            print(f"\033[91m[TG_UI ERROR] {error_msg}\033[0m")

    @staticmethod
    def _format_duration(seconds):
        """Formats a duration in seconds as e.g. '1y 2mo 3d 4h 5m'. Shared by the
        uptime display and the /status remaining-subscription-time display."""
        delta = max(0, int(seconds))
        y, rem = divmod(delta, 31536000)
        mo, rem = divmod(rem, 2592000)
        d, rem = divmod(rem, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        
        parts = []
        if y > 0: parts.append(f"{y}y")
        if mo > 0: parts.append(f"{mo}mo")
        
        if d > 0:
            parts.extend([f"{d}d", f"{h}h", f"{m}m"])
        else:
            if h > 0: parts.append(f"{h}h")
            if m > 0 or not parts: parts.append(f"{m}m")
            
        return " ".join(parts)

    def _get_uptime_string(self):
        return self._format_duration(time.time() - self.start_time)

    def _prune_internal_memory(self):
        now = time.time()
        with self.db.lock:
            stale_cbs = [k for k, v in self.callback_cache.items() if now - v.get("timestamp", 0) > 1800]
            for k in stale_cbs: del self.callback_cache[k]
            
            stale_states = [k for k, v in self.user_states.items() if now - v.get("timestamp", 0) > 1800]
            for k in stale_states: del self.user_states[k]

            # Drop flood-window entries that have already fully expired, so a chat_id
            # that stops sending updates doesn't linger in memory forever.
            stale_flood = [
                k for k, timestamps in self._flood_tracker.items()
                if not timestamps or now - max(timestamps) > self.FLOOD_WINDOW_SECONDS
            ]
            for k in stale_flood: del self._flood_tracker[k]
        
        self.last_cache_prune = now

    def _check_flood(self, chat_id):
        """
        Conservative in-memory sliding-window flood guard (report item 8): flags a
        chat_id that has sent more than FLOOD_MAX_UPDATES updates within the last
        FLOOD_WINDOW_SECONDS, and permanently bans it via `_ban_for_abuse`. Callers
        are expected to skip calling this for superusers. Returns True if the update
        that triggered this call should be dropped (i.e. the chat_id was just banned).
        """
        now = time.time()
        with self.db.lock:
            timestamps = [ts for ts in self._flood_tracker.get(chat_id, []) if now - ts < self.FLOOD_WINDOW_SECONDS]
            timestamps.append(now)
            self._flood_tracker[chat_id] = timestamps
            flagged = len(timestamps) > self.FLOOD_MAX_UPDATES
        if flagged:
            self._ban_for_abuse(chat_id, "flood")
        return flagged

    def _ban_for_abuse(self, chat_id, reason):
        """
        Permanently bans a chat_id for a detected suspicious action (report item 8),
        best-effort removes its subscription/searches, and drops any pending add/edit
        state. Deliberately silent - no message is sent to the banned chat_id, so an
        attacker gets no feedback about being detected.
        """
        self.db.ban_user(chat_id, reason=reason)
        try:
            self.db.remove_user(chat_id)
        except Exception as e:
            self._debug_print(f"Ban cleanup remove_user error: {e}")
        with self.db.lock:
            self.user_states.pop(chat_id, None)
            self._flood_tracker.pop(chat_id, None)
        self._debug_print(f"Banned chat_id {chat_id} for suspicious activity: {reason}")

    def _create_callback_data(self, payload):
        cb_id = str(uuid.uuid4())[:8]
        with self.db.lock:
            payload["timestamp"] = time.time()
            self.callback_cache[cb_id] = payload
        return f"cb_{cb_id}"

    def _is_valid_subito_url(self, url):
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        try:
            parsed = urlparse(url)
            if parsed.netloc not in ["www.subito.it", "subito.it"]: return False
            if not parsed.path.startswith("/annunci-italia/vendita/"): return False
            return True
        except Exception:
            return False

    def _validate_link_and_proceed(self, chat_id, link, target_category=None):
        """
        Runs off the Telegram polling thread (submitted via `_add_executor`) so that
        validating a new search link never blocks updates for other users. Performs
        the request against Subito.it to verify the link leads to an existing, real
        search page, then advances the add-flow state machine. The category is
        already decided by which button the user pressed BEFORE being asked for the
        link ("+ Add Category" vs "+ Add Search"), so there is no category-picker
        step here: if `target_category` is None the next step is naming a brand-new
        category, otherwise the category is fixed and the next step is naming the
        search itself.
        """
        user = self.db.get_user(chat_id)
        lang = (user.get('language') if user else None) or DEFAULT_LANGUAGE
        try:
            check_res = cffi_requests.get(
                link,
                impersonate="safari15_3",
                timeout=10,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Referer": "https://www.google.com/"
                }
            )
            if check_res.status_code != 200:
                self.send_direct_message(chat_id, t("link_unreachable", lang, code=check_res.status_code))
                return
            if '__NEXT_DATA__' not in check_res.text:
                self.send_direct_message(chat_id, t("not_valid_search_page", lang))
                return
        except Exception as e:
            self._debug_print(f"Link Validation Error: {e}")
            self.send_direct_message(chat_id, t("validation_network_error", lang))
            return

        if target_category is None:
            with self.db.lock:
                self.user_states[chat_id] = {"state": "waiting_new_cat_name", "link": link, "timestamp": time.time()}
            self.send_direct_message(chat_id, t("write_new_category_name", lang))
        else:
            with self.db.lock:
                self.user_states[chat_id] = {
                    "state": "waiting_keyword", "link": link, "category": target_category, "timestamp": time.time(),
                }
            self.send_direct_message(chat_id, t("category_selected_write_name", lang, cat=html.escape(target_category)))

    def _validate_and_update_url(self, chat_id, search_name, new_link):
        """
        Background counterpart of `_validate_link_and_proceed` used when editing
        the URL of an EXISTING search: verifies the new link is a real Subito.it
        search page, then updates the search in place (preserving its tracked-ads
        history) instead of going through the add/category-selection flow.
        """
        user = self.db.get_user(chat_id)
        lang = (user.get('language') if user else None) or DEFAULT_LANGUAGE
        try:
            check_res = cffi_requests.get(
                new_link,
                impersonate="safari15_3",
                timeout=10,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Referer": "https://www.google.com/"
                }
            )
            if check_res.status_code != 200:
                self.send_direct_message(chat_id, t("link_unreachable_not_modified", lang, code=check_res.status_code))
                return
            if '__NEXT_DATA__' not in check_res.text:
                self.send_direct_message(chat_id, t("not_valid_search_page_not_modified", lang))
                return
        except Exception as e:
            self._debug_print(f"URL edit validation error: {e}")
            self.send_direct_message(chat_id, t("validation_network_error_not_modified", lang))
            return

        self.db.update_search(chat_id, search_name, url=new_link)
        confirmation = t("url_updated", lang, name=html.escape(search_name))
        if not SubitoScraper.is_category_recognized(new_link):
            confirmation += t("category_unsupported_note", lang)
        self.send_direct_message(chat_id, confirmation)

    def _render_search_menu(self, chat_id, message_id, cat_name, search_name):
        """
        Renders (or re-renders, after a pause/resume toggle) the detail menu for a
        single search: status, filters, and the Pause/Resume, Edit, Delete, Copy
        Link, Open Link and Back actions.
        """
        edit_url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        user = self.db.get_user(chat_id)
        lang = (user.get('language') if user else None) or DEFAULT_LANGUAGE
        searches = self.db.get_user_searches(chat_id)

        target_item = None
        if cat_name in searches:
            for item in searches[cat_name]:
                if item['name'] == search_name:
                    target_item = item
                    break

        if not target_item:
            try:
                requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("search_not_found", lang), "parse_mode": "HTML"}, timeout=5)
            except Exception:
                pass
            return

        url = target_item['url']
        enabled = target_item.get('enabled', True)

        msg_text = t("search_menu_header", lang, cat=html.escape(cat_name), name=html.escape(search_name))
        msg_text += (t("status_active", lang) if enabled else t("status_paused", lang)) + "\n"
        if target_item['exclusion_kws']:
            excl = ", ".join(target_item['exclusion_kws'])
            msg_text += t("exclusions_line", lang, excl=html.escape(excl))
        if target_item.get('include_kws'):
            incl = ", ".join(target_item['include_kws'])
            msg_text += t("includes_line", lang, incl=html.escape(incl))

        # Mantenuto per il tap-to-copy sui client vecchi
        msg_text += t("direct_link_line", lang, url=html.escape(url))

        cb_toggle = self._create_callback_data({"action": "toggle_enabled", "cat": cat_name, "name": search_name})
        cb_edit = self._create_callback_data({"action": "edit_menu", "cat": cat_name, "name": search_name})
        cb_rm = self._create_callback_data({"action": "rmconf", "name": search_name})
        cb_back = self._create_callback_data({"action": "cat", "cat": cat_name})

        toggle_label = t("btn_pause", lang) if enabled else t("btn_resume", lang)
        keyboard = {"inline_keyboard": [
            [{"text": toggle_label, "callback_data": cb_toggle}],
            [{"text": t("btn_edit", lang), "callback_data": cb_edit}],
            [{"text": t("btn_delete", lang), "callback_data": cb_rm}],
            [{"text": t("btn_copy_link", lang), "copy_text": {"text": url}}],
            [{"text": t("btn_open_link", lang), "url": url}],
            [{"text": t("btn_back", lang), "callback_data": cb_back}],
        ]}

        try:
            requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": msg_text, "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
        except Exception as e:
            self._debug_print(f"Search menu render error: {e}")

    def clear_offline_updates(self):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        payload = {"offset": self.db.last_update_id + 1, "timeout": 5}
        try:
            res = requests.post(url, json=payload, timeout=10)
            data = res.json()
            if data.get("ok") and data["result"]:
                for update in data["result"]:
                    self.db.save_update_id(update["update_id"])
        except Exception as e:
            self._debug_print(f"Clear queue error: {e}")

    def send_direct_message(self, chat_id, text, reply_markup=None):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup: payload["reply_markup"] = reply_markup
        try:
            res = requests.post(url, json=payload, timeout=10)
            data = res.json()
            if not data.get("ok"): self._debug_print(f"Direct Msg API Error: {data}")
        except Exception as e:
            self._debug_print(f"Direct Msg Net Error: {e}")

    def _build_ad_reply_markup(self, item_url=None, lang=DEFAULT_LANGUAGE):
        """Builds the inline keyboard markup dictionary for an ad notification."""
        reply_markup = {"inline_keyboard": []}
        if item_url:
            reply_markup["inline_keyboard"].append([{"text": t("btn_go_to_ad", lang), "url": str(item_url)}])
        reply_markup["inline_keyboard"].append([{"text": t("btn_delete", lang), "callback_data": "delete_msg"}])
        return reply_markup

    def send_ad(self, chat_id, msg_text, image_bytes=None, item_url=None, lang=DEFAULT_LANGUAGE):
        """
        Sends an ad notification directly to a specific user (replaces global broadcast
        for ads). Returns {"message_id": int, "is_photo": bool} on success, or None if
        the send failed entirely. The return value lets bot/daemon.py later edit this
        exact message in place if another of the user's searches matches the same ad
        within the same scan pass (report item 1), instead of sending a duplicate.
        """
        reply_markup = self._build_ad_reply_markup(item_url=item_url, lang=lang)
        reply_markup_json = json.dumps(reply_markup)

        try:
            if image_bytes:
                url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
                payload = {"chat_id": chat_id, "caption": msg_text, "parse_mode": "HTML", "reply_markup": reply_markup_json}
                files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
                res = requests.post(url, data=payload, files=files, timeout=15)
                data = res.json()
                if data.get("ok"):
                    message_id = data["result"]["message_id"]
                    self.db.record_sent_notification(chat_id, message_id)
                    return {"message_id": message_id, "is_photo": True}
                self._debug_print(f"Photo Send API Error: {data}")

            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": reply_markup_json}
            res = requests.post(url, json=payload, timeout=10)
            data = res.json()
            if data.get("ok"):
                message_id = data["result"]["message_id"]
                self.db.record_sent_notification(chat_id, message_id)
                return {"message_id": message_id, "is_photo": False}
            self._debug_print(f"Text Send API Error: {data}")
        except Exception as e:
            self._debug_print(f"Send Ad Net Error: {e}")
        return None

    def purge_expired_ad_notifications(self):
        """
        Deletes ad notification messages older than AD_NOTIFICATION_RETENTION_DAYS
        (report item 5) from each recipient's chat, so stale/likely-expired listings
        don't linger. Called once per scan cycle from bot/daemon.py. Deletion is
        best-effort: a failed Telegram API call (message already removed manually, the
        user blocked the bot, etc.) is logged in debug mode and the local DB record is
        dropped regardless, since retrying forever for an unreachable message has no
        benefit.
        """
        cutoff = time.time() - (self.AD_NOTIFICATION_RETENTION_DAYS * 86400)
        for record in self.db.get_expired_notifications(cutoff):
            del_url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
            try:
                requests.post(del_url, json={"chat_id": record["chat_id"], "message_id": record["message_id"]}, timeout=5)
            except Exception as e:
                self._debug_print(f"Notification purge delete error: {e}")
            self.db.delete_notification_record(record["id"])

    def edit_ad_message(self, chat_id, message_id, new_text, is_photo, item_url=None, lang=DEFAULT_LANGUAGE):
        """
        Rewrites an already-sent ad notification in place (report item 1: when another
        of the user's searches matches the same ad within the same scan pass, the ad is
        re-notified by editing the existing message instead of sending a duplicate).
        Uses editMessageCaption for photo messages and editMessageText otherwise.
        `reply_markup` MUST be explicitly re-supplied: in Telegram Bot API, omitting
        `reply_markup` on editMessageText/editMessageCaption clears the inline keyboard.
        """
        endpoint = "editMessageCaption" if is_photo else "editMessageText"
        url = f"https://api.telegram.org/bot{self.token}/{endpoint}"
        reply_markup = self._build_ad_reply_markup(item_url=item_url, lang=lang)
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        }
        if is_photo:
            payload["caption"] = new_text
        else:
            payload["text"] = new_text
            payload["disable_web_page_preview"] = True
        try:
            res = requests.post(url, json=payload, timeout=10)
            data = res.json()
            if not data.get("ok"):
                self._debug_print(f"Edit Ad Msg API Error: {data}")
        except Exception as e:
            self._debug_print(f"Edit Ad Msg Net Error: {e}")

    def _broadcast_raw(self, chat_id_to_payload, show_delete=True):
        """Sends an already-localized text to each chat_id. `chat_id_to_payload` maps
        chat_id -> (text, lang); `lang` is only used to localize the Delete button."""
        if not chat_id_to_payload: return

        for chat_id, (text, lang) in chat_id_to_payload.items():
            reply_markup = {"inline_keyboard": []}
            if show_delete:
                reply_markup["inline_keyboard"].append([{"text": t("btn_delete", lang), "callback_data": "delete_msg"}])
            reply_markup_json = json.dumps(reply_markup) if reply_markup["inline_keyboard"] else None
            try:
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
                if reply_markup_json: payload["reply_markup"] = reply_markup_json
                res = requests.post(url, json=payload, timeout=10)
                data = res.json()
                if not data.get("ok"):
                    self._debug_print(f"Text Broadcast API Error: {data}")
            except Exception as e:
                self._debug_print(f"Broadcast Net Error: {e}")

    def broadcast_system(self, key, show_delete=False, **kwargs):
        """Sends a translated system message (e.g. online/offline) to every registered
        user, in each recipient's own language."""
        users = self.db.get_all_users()
        payload = {
            u['chat_id']: (t(key, u.get('language') or DEFAULT_LANGUAGE, **kwargs), u.get('language') or DEFAULT_LANGUAGE)
            for u in users
        }
        self._broadcast_raw(payload, show_delete=show_delete)

    def broadcast_announcement(self, admin_text):
        """Sends a superuser-authored /broadcast message to every user, prefixed with a
        per-recipient translated '📢 Announcement' label. The admin's message body
        itself is sent verbatim (not translated)."""
        users = self.db.get_all_users()
        payload = {}
        escaped_admin_text = html.escape(str(admin_text))
        for u in users:
            lang = u.get('language') or DEFAULT_LANGUAGE
            payload[u['chat_id']] = (f"{t('announcement_label', lang)}\n\n{escaped_admin_text}", lang)
        self._broadcast_raw(payload, show_delete=True)

    def poll_updates(self):
        self.clear_offline_updates()
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        
        while not self.shutdown_event.is_set():
            if time.time() - self.last_cache_prune > self.CACHE_PRUNE_INTERVAL:
                self._prune_internal_memory()

            payload = {"offset": self.db.last_update_id + 1, "timeout": 60}
            try:
                res = requests.post(url, json=payload, timeout=65)
                data = res.json()
                if data.get("ok") and data["result"]:
                    for update in data["result"]:
                        self.db.save_update_id(update["update_id"])
                        self._process_update(update)
                elif not data.get("ok"):
                    self._debug_print(f"Poll API Error: {data}")
            except requests.exceptions.Timeout:
                pass
            except Exception as e:
                self._debug_print(f"Poll Exception: {e}")
                if not self.shutdown_event.is_set():
                    self.shutdown_event.wait(2)

    def _process_update(self, update):
        if "message" in update and "text" in update["message"]:
            chat_id = update["message"]["chat"]["id"]

            # Report item 8: banned chat_ids are silently dropped - no reply, so an
            # attacker gets no feedback that they've been detected/blocked.
            if self.db.is_banned(chat_id):
                return

            text = update["message"]["text"].strip()
            
            # Ensure user exists in DB before processing
            user_data = self.db.get_user(chat_id)
            is_superuser = user_data['is_superuser'] == 1 if user_data else False
            lang = (user_data.get('language') if user_data else None) or DEFAULT_LANGUAGE

            # Report item 8: flood guard. Superusers are exempt so an admin can never
            # accidentally lock themselves out.
            if not is_superuser and self._check_flood(chat_id):
                return
            
            if text == "/cancel":
                with self.db.lock: self.user_states.pop(chat_id, None)
                self.send_direct_message(chat_id, t("action_cancelled", lang))
                return

            if text.startswith("/") and chat_id in self.user_states:
                with self.db.lock:
                    del self.user_states[chat_id]
            
            if text in ["/start", "/sub"]:
                days = self.TIMEOUT_SECONDS // 86400
                if not user_data:
                    # Check capacity before registering a NEW user
                    current_users = len(self.db.get_all_users())
                    if current_users >= self.MAX_SUBSCRIBERS:
                        self.send_direct_message(chat_id, t("registrations_closed", lang))
                        return
                    
                    self.db.register_user(chat_id)
                    self.send_direct_message(chat_id, t("sub_active_days", lang, days=days))
                else:
                    self.db.register_user(chat_id) # Updates last_active
                    self.send_direct_message(chat_id, t("sub_renewed_days", lang, days=days))
                return
                
            elif text == "/unsub":
                if user_data: 
                    self.db.remove_user(chat_id)
                    self.send_direct_message(chat_id, t("unsubscribed", lang))
                else:
                    self.send_direct_message(chat_id, t("not_subscribed", lang))
                return

            # Commands below require subscription
            if not user_data:
                self.send_direct_message(chat_id, t("must_subscribe_first", lang))
                return

            if text == "/language":
                keyboard = {"inline_keyboard": [
                    [{"text": "🇬🇧 English", "callback_data": self._create_callback_data({"action": "setlang", "lang": "en"})}],
                    [{"text": "🇮🇹 Italiano", "callback_data": self._create_callback_data({"action": "setlang", "lang": "it"})}],
                ]}
                self.send_direct_message(chat_id, t("language_prompt", lang), reply_markup=keyboard)

            elif text in ["/search", "/searches"]:
                searches = self.db.get_user_searches(chat_id)
                keyboard = {"inline_keyboard": []}
                for cat in searches.keys():
                    cb_data = self._create_callback_data({"action": "cat", "cat": cat})
                    keyboard["inline_keyboard"].append([{"text": f"📂 {cat}", "callback_data": cb_data}])
                cb_addcat = self._create_callback_data({"action": "start_add_category"})
                keyboard["inline_keyboard"].append([{"text": t("btn_add_category", lang), "callback_data": cb_addcat}])
                keyboard["inline_keyboard"].append([{"text": t("btn_close", lang), "callback_data": "delete_msg"}])
                header = t("select_category", lang) if searches else t("no_active_searches", lang)
                self.send_direct_message(chat_id, header, reply_markup=keyboard)

            elif text.startswith("/broadcast"):
                if not is_superuser:
                    self.send_direct_message(chat_id, t("superuser_only", lang))
                    return
                parts = text.split(" ", 1)
                if len(parts) < 2 or not parts[1].strip():
                    self.send_direct_message(chat_id, t("usage_broadcast", lang))
                    return
                broadcast_text = parts[1].strip()
                self.broadcast_announcement(broadcast_text)
                self.send_direct_message(chat_id, t("broadcast_sent", lang))

            elif text == "/help":
                help_text = t("help_text_base", lang)
                if is_superuser:
                    help_text += t("help_broadcast_addition", lang)
                self.send_direct_message(chat_id, help_text)

            elif text == "/status":
                all_users = self.db.get_all_users()
                now = time.time()
                # Superusers always count as active (they bypass the soft expiry, see
                # bot/daemon.py); everyone else is active iff still within TIMEOUT_SECONDS.
                active_users = sum(
                    1 for u in all_users
                    if u['is_superuser'] or (now - u['last_active'] <= self.TIMEOUT_SECONDS)
                )
                inactive_users = len(all_users) - active_users
                user_search_count = self.db.count_user_searches(chat_id)
                uptime_str = self._get_uptime_string()
                
                status_msg = t("status_header", lang, uptime=uptime_str)
                status_msg += t("status_users_line", lang, active=active_users, max=self.MAX_SUBSCRIBERS, inactive=inactive_users)
                status_msg += t("status_account_header", lang, count=user_search_count)
                
                if not is_superuser:
                    status_msg += t("status_max_suffix", lang, max=self.MAX_REGULAR_SEARCHES)
                    remaining = self.TIMEOUT_SECONDS - (time.time() - user_data['last_active'])
                    if remaining > 0:
                        status_msg += t("status_expires_in", lang, duration=self._format_duration(remaining))
                    else:
                        status_msg += t("status_expired", lang)
                else:
                    status_msg += t("status_unlimited", lang)
                    
                self.send_direct_message(chat_id, status_msg)

            elif chat_id in self.user_states:
                with self.db.lock:
                    state_data = self.user_states[chat_id].copy()
                    self.user_states[chat_id]["timestamp"] = time.time()

                # Report item 8: oversized-input guard. Every add/edit step here only
                # ever expects a short value (a category/search name, a comma-separated
                # keyword list, or a link) - a message far exceeding that is suspicious
                # rather than a normal typo, so it's treated as abuse instead of just a
                # validation error. Deliberately NOT a blanket cap on every message (e.g.
                # /broadcast is handled earlier and never reaches this branch).
                if not is_superuser and len(text) > self.OVERSIZED_INPUT_THRESHOLD:
                    self._ban_for_abuse(chat_id, "oversized_input")
                    return

                if state_data["state"] in ("waiting_link_for_new_category", "waiting_link_for_category"):
                    link = text.strip()
                    if not self._is_valid_subito_url(link):
                        self.send_direct_message(chat_id, t("invalid_link_path", lang))
                        return

                    if not link.startswith("http://") and not link.startswith("https://"):
                        link = "https://" + link
                    elif link.startswith("http://"):
                        link = link.replace("http://", "https://", 1)

                    # Searches are always saved sorted by "most recent" (order=datedesc).
                    # A price-based order is explicitly rejected instead of being silently
                    # overwritten, so the user isn't surprised by a search that behaves
                    # differently than the link they submitted.
                    parsed_link = urlparse(link)
                    query_params = dict(parse_qsl(parsed_link.query))
                    requested_order = query_params.get('order', '').strip().lower()
                    if requested_order in ('pricedesc', 'priceasc'):
                        self.send_direct_message(chat_id, t("price_sort_unsupported", lang))
                        return
                    query_params['order'] = 'datedesc'
                    new_query = urlencode(query_params)
                    link = urlunparse((parsed_link.scheme, parsed_link.netloc, parsed_link.path, parsed_link.params, new_query, parsed_link.fragment))

                    target_category = state_data.get("category")  # None for the new-category flow

                    # Validating the link requires a network round-trip (up to ~10s). Run it in a
                    # background worker so this single-threaded update loop keeps serving other users.
                    self.send_direct_message(chat_id, t("verifying_link", lang))
                    self._add_executor.submit(self._validate_link_and_proceed, chat_id, link, target_category)

                elif state_data["state"] == "waiting_new_cat_name":
                    new_cat = text.strip()
                    if len(new_cat) > 30:
                        self.send_direct_message(chat_id, t("cat_name_too_long", lang))
                    elif not re.match(r'^[a-zA-Z0-9\s\-]+$', new_cat):
                        self.send_direct_message(chat_id, t("invalid_chars", lang))
                    else:
                        with self.db.lock:
                            self.user_states[chat_id]["state"] = "waiting_keyword"
                            self.user_states[chat_id]["category"] = new_cat
                        self.send_direct_message(chat_id, t("category_set_write_name", lang, cat=html.escape(new_cat)))
                        
                elif state_data["state"] == "waiting_keyword":
                    new_kw = text.strip()
                    cat = state_data["category"]

                    if len(new_kw) > self.MAX_SEARCH_NAME_LENGTH:
                        self.send_direct_message(chat_id, t("search_name_too_long", lang, max=self.MAX_SEARCH_NAME_LENGTH))
                        return
                    
                    # Check for duplicates for this user
                    searches = self.db.get_user_searches(chat_id)
                    existing_names = [item['name'] for items in searches.values() for item in items]
                    
                    base_kw = new_kw
                    counter = 1
                    while new_kw in existing_names:
                        new_kw = f"{base_kw} {counter}"
                        counter += 1

                    with self.db.lock:
                        self.user_states[chat_id]["state"] = "waiting_exclusion"
                        self.user_states[chat_id]["name"] = new_kw
                        
                    self.send_direct_message(
                        chat_id, 
                        t("search_name_set_exclusion_prompt", lang, name=html.escape(new_kw), max=self.MAX_EXCLUSION_KEYWORDS)
                    )
                        
                elif state_data["state"] == "waiting_exclusion":
                    excl_input = text.strip().lower()
                    
                    exclusion_list = []
                    if excl_input != "none":
                        raw_list = [k.strip()[:self.MAX_KEYWORD_LENGTH] for k in excl_input.split(',')]
                        exclusion_list = [k for k in raw_list if k][:self.MAX_EXCLUSION_KEYWORDS]
                    
                    with self.db.lock:
                        self.user_states[chat_id]["state"] = "waiting_include"
                        self.user_states[chat_id]["exclusion_kws"] = exclusion_list
                    
                    self.send_direct_message(
                        chat_id,
                        t("include_kw_prompt", lang, max=self.MAX_INCLUDE_KEYWORDS)
                    )

                elif state_data["state"] == "waiting_include":
                    incl_input = text.strip().lower()
                    cat = state_data["category"]
                    link = state_data["link"]
                    name = state_data["name"]
                    exclusion_list = state_data.get("exclusion_kws", [])

                    include_list = []
                    if incl_input != "none":
                        raw_list = [k.strip()[:self.MAX_KEYWORD_LENGTH] for k in incl_input.split(',')]
                        include_list = [k for k in raw_list if k][:self.MAX_INCLUDE_KEYWORDS]

                    self.db.add_search(chat_id, cat, name, link, exclusion_list, include_list)
                    
                    with self.db.lock:
                        del self.user_states[chat_id]
                        
                    excl_str = ", ".join(exclusion_list) if exclusion_list else t("label_none", lang)
                    incl_str = ", ".join(include_list) if include_list else t("label_none", lang)
                    confirmation = t(
                        "added_confirmation", lang,
                        cat=html.escape(cat), name=html.escape(name),
                        excl=html.escape(excl_str), incl=html.escape(incl_str)
                    )
                    if not SubitoScraper.is_category_recognized(link):
                        confirmation += t("category_unsupported_note", lang)
                    self.send_direct_message(chat_id, confirmation)

                elif state_data["state"] == "editing_field":
                    field = state_data["field"]
                    search_name = state_data["name"]

                    if field == "url":
                        new_link = text.strip()
                        if not self._is_valid_subito_url(new_link):
                            self.send_direct_message(chat_id, t("invalid_subito_link", lang))
                            return
                        if not new_link.startswith("http://") and not new_link.startswith("https://"):
                            new_link = "https://" + new_link
                        elif new_link.startswith("http://"):
                            new_link = new_link.replace("http://", "https://", 1)

                        parsed_link = urlparse(new_link)
                        query_params = dict(parse_qsl(parsed_link.query))
                        requested_order = query_params.get('order', '').strip().lower()
                        if requested_order in ('pricedesc', 'priceasc'):
                            self.send_direct_message(chat_id, t("price_sort_unsupported_short", lang))
                            return
                        query_params['order'] = 'datedesc'
                        new_query = urlencode(query_params)
                        new_link = urlunparse((parsed_link.scheme, parsed_link.netloc, parsed_link.path, parsed_link.params, new_query, parsed_link.fragment))

                        with self.db.lock:
                            del self.user_states[chat_id]

                        self.send_direct_message(chat_id, t("verifying_new_link", lang))
                        self._add_executor.submit(self._validate_and_update_url, chat_id, search_name, new_link)

                    elif field == "exclusion":
                        raw = text.strip().lower()
                        kw_list = [] if raw == "none" else [k.strip()[:self.MAX_KEYWORD_LENGTH] for k in raw.split(',') if k.strip()][:self.MAX_EXCLUSION_KEYWORDS]
                        self.db.update_search(chat_id, search_name, exclusion_kws=kw_list)
                        with self.db.lock:
                            del self.user_states[chat_id]
                        kw_str = ", ".join(kw_list) if kw_list else t("label_none", lang)
                        self.send_direct_message(chat_id, t("exclusion_updated", lang, kw=html.escape(kw_str)))

                    elif field == "include":
                        raw = text.strip().lower()
                        kw_list = [] if raw == "none" else [k.strip()[:self.MAX_KEYWORD_LENGTH] for k in raw.split(',') if k.strip()][:self.MAX_INCLUDE_KEYWORDS]
                        self.db.update_search(chat_id, search_name, include_kws=kw_list)
                        with self.db.lock:
                            del self.user_states[chat_id]
                        kw_str = ", ".join(kw_list) if kw_list else t("label_none", lang)
                        self.send_direct_message(chat_id, t("include_updated", lang, kw=html.escape(kw_str)))

        elif "callback_query" in update:
            cb = update["callback_query"]
            chat_id = cb["message"]["chat"]["id"]

            # Report item 8: banned chat_ids are silently dropped - no reply, so an
            # attacker gets no feedback that they've been detected/blocked.
            if self.db.is_banned(chat_id):
                return

            message_id = cb["message"]["message_id"]
            cb_id = cb["id"]
            cb_data = cb.get("data", "")
            cb_user_data = self.db.get_user(chat_id)
            lang = (cb_user_data.get('language') if cb_user_data else None) or DEFAULT_LANGUAGE
            is_superuser = cb_user_data['is_superuser'] == 1 if cb_user_data else False

            # Report item 8: flood guard. Superusers are exempt so an admin can never
            # accidentally lock themselves out.
            if not is_superuser and self._check_flood(chat_id):
                return
            
            ans_url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
            edit_url = f"https://api.telegram.org/bot{self.token}/editMessageText"
            
            if cb_data == "delete_msg":
                del_url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
                try: 
                    requests.post(del_url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
                except Exception as e:
                    self._debug_print(f"Delete msg error: {e}")
                
            elif cb_data == "search_back":
                searches = self.db.get_user_searches(chat_id)
                keyboard = {"inline_keyboard": []}
                for cat in searches.keys():
                    c_data = self._create_callback_data({"action": "cat", "cat": cat})
                    keyboard["inline_keyboard"].append([{"text": f"📂 {cat}", "callback_data": c_data}])
                cb_addcat = self._create_callback_data({"action": "start_add_category"})
                keyboard["inline_keyboard"].append([{"text": t("btn_add_category", lang), "callback_data": cb_addcat}])
                keyboard["inline_keyboard"].append([{"text": t("btn_close", lang), "callback_data": "delete_msg"}])
                header = t("select_category", lang) if searches else t("no_active_searches", lang)
                try: 
                    requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": header, "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                except Exception as e:
                    self._debug_print(f"Search back error: {e}")
                
            elif cb_data.startswith("cb_"):
                cache_id = cb_data[3:]
                with self.db.lock: cb_info = self.callback_cache.get(cache_id)
                    
                if not cb_info:
                    try: 
                        requests.post(ans_url, json={"callback_query_id": cb_id, "text": t("session_expired", lang), "show_alert": True}, timeout=5)
                    except Exception: pass
                    return
                    
                action = cb_info.get("action")
                
                if action == "cancel":
                    with self.db.lock:
                        if chat_id in self.user_states:
                            del self.user_states[chat_id]
                    try: 
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("operation_cancelled", lang), "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Cancel cb error: {e}")

                elif action == "setlang":
                    new_lang = cb_info.get("lang", DEFAULT_LANGUAGE)
                    if new_lang not in SUPPORTED_LANGUAGES:
                        new_lang = DEFAULT_LANGUAGE
                    self.db.set_language(chat_id, new_lang)
                    try:
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("language_set", new_lang), "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Set language cb error: {e}")

                elif action == "cat":
                    cat_name = cb_info["cat"]
                    searches = self.db.get_user_searches(chat_id)
                    if cat_name in searches:
                        keyboard = {"inline_keyboard": []}
                        for item in searches[cat_name]:
                            c_data = self._create_callback_data({"action": "search_menu", "cat": cat_name, "name": item['name']})
                            keyboard["inline_keyboard"].append([{"text": item['name'], "callback_data": c_data}])
                        cb_addsearch = self._create_callback_data({"action": "start_add_search", "cat": cat_name})
                        keyboard["inline_keyboard"].append([{"text": t("btn_add_search", lang), "callback_data": cb_addsearch}])
                        keyboard["inline_keyboard"].append([{"text": t("btn_back", lang), "callback_data": "search_back"}])
                        try: 
                            requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("category_select_search", lang, cat=html.escape(cat_name)), "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                        except Exception as e:
                            self._debug_print(f"Cat cb error: {e}")
                    else:
                        try:
                            requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("category_auto_deleted", lang, cat=html.escape(cat_name)), "parse_mode": "HTML"}, timeout=5)
                        except Exception as e:
                            self._debug_print(f"Empty cat cb error: {e}")
                
                elif action == "search_menu":
                    self._render_search_menu(chat_id, message_id, cb_info["cat"], cb_info["name"])

                elif action == "toggle_enabled":
                    cat_name = cb_info["cat"]
                    search_name = cb_info["name"]
                    searches = self.db.get_user_searches(chat_id)
                    current = None
                    if cat_name in searches:
                        for item in searches[cat_name]:
                            if item['name'] == search_name:
                                current = item
                                break
                    if current is not None:
                        self.db.set_search_enabled(chat_id, search_name, not current.get('enabled', True))
                    self._render_search_menu(chat_id, message_id, cat_name, search_name)

                elif action == "edit_menu":
                    cat_name = cb_info["cat"]
                    search_name = cb_info["name"]
                    cb_url = self._create_callback_data({"action": "edit_field", "cat": cat_name, "name": search_name, "field": "url"})
                    cb_excl = self._create_callback_data({"action": "edit_field", "cat": cat_name, "name": search_name, "field": "exclusion"})
                    cb_incl = self._create_callback_data({"action": "edit_field", "cat": cat_name, "name": search_name, "field": "include"})
                    cb_back = self._create_callback_data({"action": "search_menu", "cat": cat_name, "name": search_name})
                    keyboard = {"inline_keyboard": [
                        [{"text": t("btn_edit_url", lang), "callback_data": cb_url}],
                        [{"text": t("btn_edit_exclusions", lang), "callback_data": cb_excl}],
                        [{"text": t("btn_edit_includes", lang), "callback_data": cb_incl}],
                        [{"text": t("btn_back", lang), "callback_data": cb_back}],
                    ]}
                    try:
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("editing_what", lang, name=html.escape(search_name)), "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Edit menu cb error: {e}")

                elif action == "edit_field":
                    cat_name = cb_info["cat"]
                    search_name = cb_info["name"]
                    field = cb_info["field"]

                    with self.db.lock:
                        self.user_states[chat_id] = {
                            "state": "editing_field",
                            "field": field,
                            "cat": cat_name,
                            "name": search_name,
                            "timestamp": time.time(),
                        }

                    prompts = {
                        "url": t("edit_prompt_url", lang),
                        "exclusion": t("edit_prompt_exclusion", lang, max=self.MAX_EXCLUSION_KEYWORDS),
                        "include": t("edit_prompt_include", lang, max=self.MAX_INCLUDE_KEYWORDS),
                    }
                    try:
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": prompts.get(field, t("send_new_value", lang)), "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Edit field cb error: {e}")

                elif action == "start_add_category":
                    current_count = self.db.count_user_searches(chat_id)
                    if not is_superuser and current_count >= self.MAX_REGULAR_SEARCHES:
                        try:
                            requests.post(ans_url, json={"callback_query_id": cb_id, "text": t("limit_reached", lang, max=self.MAX_REGULAR_SEARCHES), "show_alert": True}, timeout=5)
                        except Exception: pass
                        return
                    with self.db.lock:
                        self.user_states[chat_id] = {"state": "waiting_link_for_new_category", "timestamp": time.time()}
                    try:
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("send_link_prompt", lang), "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Start add category cb error: {e}")

                elif action == "start_add_search":
                    cat_name = cb_info["cat"]
                    current_count = self.db.count_user_searches(chat_id)
                    if not is_superuser and current_count >= self.MAX_REGULAR_SEARCHES:
                        try:
                            requests.post(ans_url, json={"callback_query_id": cb_id, "text": t("limit_reached", lang, max=self.MAX_REGULAR_SEARCHES), "show_alert": True}, timeout=5)
                        except Exception: pass
                        return
                    with self.db.lock:
                        self.user_states[chat_id] = {"state": "waiting_link_for_category", "category": cat_name, "timestamp": time.time()}
                    try:
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": t("send_link_prompt", lang), "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Start add search cb error: {e}")

                elif action == "rmconf":
                    name = cb_info["name"]

                    # Categories aren't a stored entity: they only exist implicitly through
                    # searches. Figure out which category owned this search BEFORE deleting
                    # it, so we can tell the user if that category effectively disappeared.
                    removed_category = None
                    for cat, items in self.db.get_user_searches(chat_id).items():
                        if any(item['name'] == name for item in items):
                            removed_category = cat
                            break

                    self.db.remove_search(chat_id, name)

                    confirm_text = t("search_deleted", lang, name=html.escape(name))
                    if removed_category and removed_category not in self.db.get_user_searches(chat_id):
                        confirm_text += t("category_removed_no_searches", lang, cat=html.escape(removed_category))

                    try: 
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": confirm_text, "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Rmconf cb error: {e}")

            try:
                requests.post(ans_url, json={"callback_query_id": cb_id}, timeout=5)
            except Exception:
                pass