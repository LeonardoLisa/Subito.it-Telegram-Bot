"""
Filename: database.py
Version: 4.0.0
Date: 2026-04-30
Author: Leonardo Lisa
Description: SQLite database implementation for per-user searches, exclusion keywords, 
             superuser privileges, and cascading state management.
             Replaces previous JSON-based flat file architecture.
Requirements: built-in (sqlite3)

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

import sqlite3
import threading
import time
import json
from contextlib import contextmanager

class Database:
    def __init__(self, db_path="subito_bot.db"):
        self.lock = threading.Lock()
        self.db_path = db_path
        self.debug_mode = False
        
        # Volatile state needed by telegram_ui to avoid fetching DB for every update
        self.last_update_id = 0 
        
        self._init_db()

    def _debug_print(self, msg):
        if self.debug_mode:
            print(f"\033[93m[DB DEBUG] {msg}\033[0m")

    def _get_connection(self):
        """Returns a thread-local SQLite connection with Foreign Keys enabled."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = 1")
        return conn

    @contextmanager
    def _connection(self):
        """
        Context manager yielding a connection that is ALWAYS closed on exit.
        Note: `with sqlite3.connect(...) as conn:` only manages the transaction
        (commit/rollback), it does NOT close the connection, which used to leak
        a new connection/file descriptor on every single query.
        """
        conn = self._get_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initializes the database schema if it doesn't exist."""
        with self.lock:
            with self._connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id INTEGER PRIMARY KEY,
                        joined_at REAL NOT NULL,
                        last_active REAL NOT NULL,
                        is_superuser INTEGER DEFAULT 0
                    )
                ''')
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS searches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        category TEXT NOT NULL,
                        name TEXT NOT NULL,
                        url TEXT NOT NULL,
                        exclusion_kws TEXT DEFAULT '[]',
                        FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
                    )
                ''')
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tracked_ads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        search_id INTEGER NOT NULL,
                        ad_id TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        FOREIGN KEY(search_id) REFERENCES searches(id) ON DELETE CASCADE
                    )
                ''')

                # --- Non-destructive schema migration ---
                # `CREATE TABLE IF NOT EXISTS` above does nothing when the table already
                # exists with an older schema, so newly introduced columns must be added
                # explicitly for pre-existing databases (avoids forcing users to delete
                # subito_bot.db when upgrading).
                existing_cols = {row['name'] for row in cursor.execute("PRAGMA table_info(searches)").fetchall()}
                if 'enabled' not in existing_cols:
                    cursor.execute("ALTER TABLE searches ADD COLUMN enabled INTEGER DEFAULT 1")
                if 'include_kws' not in existing_cols:
                    cursor.execute("ALTER TABLE searches ADD COLUMN include_kws TEXT DEFAULT '[]'")
                
                # Indexes for faster queries
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_searches_chat_id ON searches(chat_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracked_ads_search_id ON tracked_ads(search_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
                
                # Table for simple key-value volatile states (like last_update_id)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS kv_store (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                ''')
                
                cursor.execute("SELECT value FROM kv_store WHERE key = 'last_update_id'")
                row = cursor.fetchone()
                if row:
                    self.last_update_id = int(row['value'])
                    
                conn.commit()

    def save_update_id(self, update_id):
        self.last_update_id = update_id
        with self.lock:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO kv_store (key, value) VALUES ('last_update_id', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(update_id),)
                )
                conn.commit()

    # --- User Management ---

    def register_user(self, chat_id):
        """Registers a new user or updates last_active if already exists."""
        now = time.time()
        with self.lock:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO users (chat_id, joined_at, last_active, is_superuser) 
                    VALUES (?, ?, ?, 0) 
                    ON CONFLICT(chat_id) DO UPDATE SET last_active=excluded.last_active
                    """,
                    (chat_id, now, now)
                )
                conn.commit()

    def remove_user(self, chat_id):
        """Removes a user and all their associated searches/ads via CASCADE."""
        with self.lock:
            with self._connection() as conn:
                conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
                conn.commit()

    def get_user(self, chat_id):
        """Returns user data as a dict or None if not found."""
        with self.lock:
            with self._connection() as conn:
                cursor = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
                row = cursor.fetchone()
                return dict(row) if row else None

    def get_all_users(self):
        """Returns a list of all user dicts."""
        with self.lock:
            with self._connection() as conn:
                cursor = conn.execute("SELECT * FROM users")
                return [dict(r) for r in cursor.fetchall()]

    def prune_inactive_users(self, retention_seconds):
        """
        Hard deletes regular users who haven't renewed past `retention_seconds`
        (deep-clean retention, configurable via DEEP_CLEAN_RETENTION_DAYS, default 30
        days). This is intentionally separate from, and much longer than, the soft
        subscription expiry (SUBSCRIPTION_TTL_DAYS, default 4 days) used to pause
        scanning in main.py: an expired-but-not-yet-pruned user's searches are never
        touched, only untouched/unscanned, until this longer retention window elapses.
        Superusers are explicitly exempt: they bypass the soft expiry entirely, so their
        `last_active` timestamp is expected to go stale on purpose and must NOT trigger
        this hard delete (which cascades to their searches).
        """
        now = time.time()
        cutoff_time = now - retention_seconds
        deleted_count = 0
        with self.lock:
            with self._connection() as conn:
                # Single statement instead of SELECT + per-row DELETE loop.
                # Cascading FK deletes (searches, tracked_ads) are handled by SQLite
                # since PRAGMA foreign_keys = 1 is set on every connection.
                cursor = conn.execute("DELETE FROM users WHERE last_active < ? AND is_superuser = 0", (cutoff_time,))
                conn.commit()
                deleted_count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

        if deleted_count > 0:
            self._debug_print(f"Pruned {deleted_count} inactive users and their associated data.")
        return deleted_count

    # --- Search Management ---

    def count_user_searches(self, chat_id):
        with self.lock:
            with self._connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) as cnt FROM searches WHERE chat_id = ?", (chat_id,))
                return cursor.fetchone()['cnt']

    def add_search(self, chat_id, category, name, url, exclusion_kws=None, include_kws=None):
        """Adds a new search for a user. New searches are enabled by default."""
        if exclusion_kws is None:
            exclusion_kws = []
        if include_kws is None:
            include_kws = []
        
        with self.lock:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO searches (chat_id, category, name, url, exclusion_kws, include_kws, enabled) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (chat_id, category, name, url, json.dumps(exclusion_kws), json.dumps(include_kws))
                )
                conn.commit()

    def remove_search(self, chat_id, name):
        """Removes a specific search for a user."""
        with self.lock:
            with self._connection() as conn:
                conn.execute("DELETE FROM searches WHERE chat_id = ? AND name = ?", (chat_id, name))
                conn.commit()

    def set_search_enabled(self, chat_id, name, enabled):
        """Pauses (enabled=False) or resumes (enabled=True) a search without touching
        its tracked-ads history, so resuming doesn't re-notify already-seen ads."""
        with self.lock:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE searches SET enabled = ? WHERE chat_id = ? AND name = ?",
                    (1 if enabled else 0, chat_id, name)
                )
                conn.commit()

    def update_search(self, chat_id, name, url=None, exclusion_kws=None, include_kws=None):
        """
        Updates one or more fields of an existing search in place, preserving its `id`
        and tracked-ads history (unlike delete + re-add). Only fields explicitly passed
        (not None) are changed.
        """
        fields = []
        params = []
        if url is not None:
            fields.append("url = ?")
            params.append(url)
        if exclusion_kws is not None:
            fields.append("exclusion_kws = ?")
            params.append(json.dumps(exclusion_kws))
        if include_kws is not None:
            fields.append("include_kws = ?")
            params.append(json.dumps(include_kws))

        if not fields:
            return

        params.extend([chat_id, name])
        with self.lock:
            with self._connection() as conn:
                conn.execute(
                    f"UPDATE searches SET {', '.join(fields)} WHERE chat_id = ? AND name = ?",
                    params
                )
                conn.commit()

    def set_superuser(self, chat_id, is_superuser):
        """Promotes or demotes a user to/from superuser status."""
        with self.lock:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE users SET is_superuser = ? WHERE chat_id = ?",
                    (1 if is_superuser else 0, chat_id)
                )
                conn.commit()

    @staticmethod
    def _parse_kw_list(raw_value):
        try:
            return json.loads(raw_value)
        except Exception:
            return []

    def get_user_searches(self, chat_id):
        """Returns a dict {Category: [{...}, ...]} for a specific user."""
        with self.lock:
            with self._connection() as conn:
                cursor = conn.execute("SELECT * FROM searches WHERE chat_id = ? ORDER BY category, name", (chat_id,))
                rows = cursor.fetchall()
                
                result = {}
                for r in rows:
                    cat = r['category']
                    if cat not in result:
                        result[cat] = []
                        
                    result[cat].append({
                        "id": r['id'],
                        "name": r['name'],
                        "url": r['url'],
                        "exclusion_kws": self._parse_kw_list(r['exclusion_kws']),
                        "include_kws": self._parse_kw_list(r['include_kws']),
                        "enabled": bool(r['enabled'])
                    })
                return result

    def get_all_searches(self):
        """Returns a flat list of all searches for the scraper daemon."""
        with self.lock:
            with self._connection() as conn:
                # JOIN to ensure we only scrape for users who haven't expired their soft
                # subscription window. The expiration check itself is left to main.py,
                # which decides based on the returned user status.
                cursor = conn.execute("SELECT s.*, u.last_active, u.is_superuser FROM searches s JOIN users u ON s.chat_id = u.chat_id")
                rows = cursor.fetchall()
                
                searches = []
                for r in rows:
                    searches.append({
                        "id": r['id'],
                        "chat_id": r['chat_id'],
                        "category": r['category'],
                        "name": r['name'],
                        "url": r['url'],
                        "exclusion_kws": self._parse_kw_list(r['exclusion_kws']),
                        "include_kws": self._parse_kw_list(r['include_kws']),
                        "enabled": bool(r['enabled']),
                        "last_active": r['last_active'],
                        "is_superuser": r['is_superuser']
                    })
                return searches

    # --- Ads Tracking Management ---

    def is_ad_tracked(self, search_id, ad_id):
        """Checks if an ad has already been seen for a specific search."""
        with self.lock:
            with self._connection() as conn:
                cursor = conn.execute("SELECT 1 FROM tracked_ads WHERE search_id = ? AND ad_id = ?", (search_id, ad_id))
                return cursor.fetchone() is not None

    def has_tracked_ads(self, search_id):
        """Returns True if at least one ad has ever been tracked for this search."""
        with self.lock:
            with self._connection() as conn:
                cursor = conn.execute("SELECT 1 FROM tracked_ads WHERE search_id = ? LIMIT 1", (search_id,))
                return cursor.fetchone() is not None

    def add_tracked_ad(self, search_id, ad_id):
        """Records an ad as seen."""
        now = time.time()
        with self.lock:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO tracked_ads (search_id, ad_id, timestamp) VALUES (?, ?, ?)",
                    (search_id, ad_id, now)
                )
                conn.commit()

    def trim_tracked_items(self, max_items=150):
        """
        Deletes old ads keeping only the latest `max_items` per search.
        Using a subquery to identify rows to delete.
        """
        with self.lock:
            with self._connection() as conn:
                # Delete rows where the id is NOT IN the top `max_items` newest ids for that search_id.
                # max_items is bound as a parameter instead of interpolated into the SQL string.
                conn.execute("""
                    DELETE FROM tracked_ads
                    WHERE id NOT IN (
                        SELECT id
                        FROM tracked_ads t2
                        WHERE t2.search_id = tracked_ads.search_id
                        ORDER BY t2.timestamp DESC
                        LIMIT ?
                    )
                """, (max_items,))
                conn.commit()
