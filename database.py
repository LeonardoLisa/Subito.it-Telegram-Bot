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

    def _init_db(self):
        """Initializes the database schema if it doesn't exist."""
        with self.lock:
            with self._get_connection() as conn:
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
            with self._get_connection() as conn:
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
            with self._get_connection() as conn:
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
            with self._get_connection() as conn:
                conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
                conn.commit()

    def get_user(self, chat_id):
        """Returns user data as a dict or None if not found."""
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
                row = cursor.fetchone()
                return dict(row) if row else None

    def get_all_users(self):
        """Returns a list of all user dicts."""
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT * FROM users")
                return [dict(r) for r in cursor.fetchall()]

    def prune_inactive_users(self, retention_seconds):
        """Hard deletes users who haven't renewed past the retention period (34 days)."""
        now = time.time()
        cutoff_time = now - retention_seconds
        deleted_count = 0
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT chat_id FROM users WHERE last_active < ?", (cutoff_time,))
                stale_users = [row['chat_id'] for row in cursor.fetchall()]
                
                if stale_users:
                    # SQLite 'IN' clause limit workaround by deleting one by one or chunking
                    for cid in stale_users:
                        conn.execute("DELETE FROM users WHERE chat_id = ?", (cid,))
                    conn.commit()
                    deleted_count = len(stale_users)
                    
        if deleted_count > 0:
            self._debug_print(f"Pruned {deleted_count} inactive users and their associated data.")
        return deleted_count

    # --- Search Management ---

    def count_user_searches(self, chat_id):
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) as cnt FROM searches WHERE chat_id = ?", (chat_id,))
                return cursor.fetchone()['cnt']

    def add_search(self, chat_id, category, name, url, exclusion_kws=None):
        """Adds a new search for a user."""
        if exclusion_kws is None:
            exclusion_kws = []
        
        with self.lock:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO searches (chat_id, category, name, url, exclusion_kws) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, category, name, url, json.dumps(exclusion_kws))
                )
                conn.commit()

    def remove_search(self, chat_id, name):
        """Removes a specific search for a user."""
        with self.lock:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM searches WHERE chat_id = ? AND name = ?", (chat_id, name))
                conn.commit()

    def get_user_searches(self, chat_id):
        """Returns a dict {Category: [(id, name, url, excl_kws), ...]} for a specific user."""
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT * FROM searches WHERE chat_id = ? ORDER BY category, name", (chat_id,))
                rows = cursor.fetchall()
                
                result = {}
                for r in rows:
                    cat = r['category']
                    if cat not in result:
                        result[cat] = []
                    
                    excl_kws = []
                    try:
                        excl_kws = json.loads(r['exclusion_kws'])
                    except:
                        pass
                        
                    result[cat].append({
                        "id": r['id'],
                        "name": r['name'],
                        "url": r['url'],
                        "exclusion_kws": excl_kws
                    })
                return result

    def get_all_searches(self):
        """Returns a flat list of all searches for the scraper daemon."""
        with self.lock:
            with self._get_connection() as conn:
                # JOIN to ensure we only scrape for users who haven't expired their 4-day active window
                # The expiration check logic will be handled slightly differently now, 
                # but we return all searches and let main.py decide based on user status.
                cursor = conn.execute("SELECT s.*, u.last_active, u.is_superuser FROM searches s JOIN users u ON s.chat_id = u.chat_id")
                rows = cursor.fetchall()
                
                searches = []
                for r in rows:
                    try: excl_kws = json.loads(r['exclusion_kws'])
                    except: excl_kws = []
                    
                    searches.append({
                        "id": r['id'],
                        "chat_id": r['chat_id'],
                        "category": r['category'],
                        "name": r['name'],
                        "url": r['url'],
                        "exclusion_kws": excl_kws,
                        "last_active": r['last_active'],
                        "is_superuser": r['is_superuser']
                    })
                return searches

    # --- Ads Tracking Management ---

    def is_ad_tracked(self, search_id, ad_id):
        """Checks if an ad has already been seen for a specific search."""
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT 1 FROM tracked_ads WHERE search_id = ? AND ad_id = ?", (search_id, ad_id))
                return cursor.fetchone() is not None

    def add_tracked_ad(self, search_id, ad_id):
        """Records an ad as seen."""
        now = time.time()
        with self.lock:
            with self._get_connection() as conn:
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
            with self._get_connection() as conn:
                # Delete rows where the id is NOT IN the top 150 newest ids for that search_id
                conn.execute(f"""
                    DELETE FROM tracked_ads
                    WHERE id NOT IN (
                        SELECT id
                        FROM tracked_ads t2
                        WHERE t2.search_id = tracked_ads.search_id
                        ORDER BY t2.timestamp DESC
                        LIMIT {max_items}
                    )
                """)
                conn.commit()