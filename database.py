"""
Filename: database.py
Version: 3.1.0
Date: 2026-04-29
Author: Leonardo Lisa
Description: Atomic file I/O and state management. Implements a fixed-size retention policy (max 30 items per query).
Requirements: built-in

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import os
import json
import tempfile
import threading
import time

class Database:
    def __init__(self):
        self.lock = threading.Lock()
        self.db_file = "tracked_items.json"
        self.sub_file = "subscribers.json"
        self.search_file = "searches.json"
        self.url_file = "known_urls.json"
        
        self.tracked_items = {}
        self.subscribers = {}
        self.searches = {}
        self.known_urls = []
        self.last_update_id = 0
        self.debug_mode = False
        
        self.load_all()

    def _debug_print(self, error_msg):
        if self.debug_mode:
            print(f"\033[93m[DB ERROR] {error_msg}\033[0m")

    def _atomic_save(self, data, filepath):
        dir_name = os.path.dirname(os.path.abspath(filepath)) or '.'
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix="tmp_", suffix=".json")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, filepath)
        except Exception as e:
            self._debug_print(f"Atomic save failed for {filepath}: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def load_all(self):
        with self.lock:
            if os.path.isfile(self.db_file):
                try:
                    with open(self.db_file, 'r') as f: self.tracked_items = json.load(f)
                except Exception as e: self._debug_print(f"Load {self.db_file}: {e}")
            
            if os.path.isfile(self.sub_file):
                try:
                    with open(self.sub_file, 'r') as f:
                        data = json.load(f)
                        self.subscribers = data.get("subscribers", {})
                        self.last_update_id = data.get("last_update_id", 0)
                except Exception as e: self._debug_print(f"Load {self.sub_file}: {e}")
                
            if os.path.isfile(self.search_file):
                try:
                    with open(self.search_file, 'r') as f: self.searches = json.load(f)
                except Exception as e: self._debug_print(f"Load {self.search_file}: {e}")
                
            if os.path.isfile(self.url_file):
                try:
                    with open(self.url_file, 'r') as f: self.known_urls = json.load(f)
                except Exception as e: self._debug_print(f"Load {self.url_file}: {e}")

    def save_all(self):
        with self.lock:
            self._atomic_save(self.tracked_items, self.db_file)
            self._atomic_save({"subscribers": self.subscribers, "last_update_id": self.last_update_id}, self.sub_file)
            self._atomic_save(self.searches, self.search_file)
            self._atomic_save(self.known_urls, self.url_file)

    def trim_tracked_items(self, max_items=30):
        """Retains only the latest N items per search category/keyword to prevent database bloat."""
        with self.lock:
            grouped = {}
            for link, data in self.tracked_items.items():
                cat = data.get("search_name")
                kw = data.get("keyword")
                # Drop items that belong to deleted searches
                if cat not in self.searches or kw not in self.searches.get(cat, {}):
                    continue
                
                key = (cat, kw)
                if key not in grouped:
                    grouped[key] = []
                grouped[key].append((link, data))
            
            new_tracked = {}
            for key, items in grouped.items():
                items.sort(key=lambda x: x[1].get("timestamp", 0), reverse=True)
                for link, data in items[:max_items]:
                    new_tracked[link] = data
                    
            self.tracked_items = new_tracked

    def add_tracked_item(self, link, title, price, search_name, keyword):
        with self.lock:
            self.tracked_items[link] = {
                "title": title,
                "price": price,
                "search_name": search_name,
                "keyword": keyword,
                "timestamp": time.time()
            }