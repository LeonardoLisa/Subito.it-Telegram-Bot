"""
Filename: main.py
Version: 3.5.0
Date: 2026-04-28
Author: Leonardo Lisa
Description: Main orchestration daemon. Links Database, Scraper, and Telegram UI.
             Restores original CLI flags (--refreshrate, --debug, --skip), terminal UI formatting,
             and graceful teardown / interruptible sleep logic.
Requirements: requests beautifulsoup4 pillow python-dotenv curl-cffi

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

from curl_cffi import requests as cffi_requests
import requests
import time
import html
import uuid
import json
import re
from urllib.parse import urlparse

class TelegramUI:
    # Configurable duration
    TIMEOUT_SECONDS = 4 * 24 * 3600  # 96 hours
    CACHE_PRUNE_INTERVAL = 1800      # 30 minutes

    def __init__(self, token, db, shutdown_event, max_subs=15):
        self.token = token
        self.db = db
        self.shutdown_event = shutdown_event
        self.max_subs = max_subs
        self.debug_mode = False
        self.user_states = {}
        self.callback_cache = {}
        self.start_time = time.time()
        self.last_cache_prune = time.time()
        
    def _debug_print(self, error_msg):
        if self.debug_mode:
            print(f"\033[91m[TG_UI ERROR] {error_msg}\033[0m")

    def _get_uptime_string(self):
        """Formats uptime into Year, Month, Day, Hour, Minute."""
        delta = int(time.time() - self.start_time)
        y, rem = divmod(delta, 31536000)
        mo, rem = divmod(rem, 2592000)
        d, rem = divmod(rem, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        
        parts = []
        if y > 0: parts.append(f"{y}y")
        if mo > 0: parts.append(f"{mo}mo")
        
        # If days exist, show H and M even if zero (e.g., 5d 0h 0m)
        if d > 0:
            parts.extend([f"{d}d", f"{h}h", f"{m}m"])
        else:
            if h > 0: parts.append(f"{h}h")
            if m > 0 or not parts: parts.append(f"{m}m")
            
        return " ".join(parts)

    def _prune_internal_memory(self):
        """Memory Leak Prevention: Prunes states and callbacks older than 30 min."""
        now = time.time()
        with self.db.lock:
            # Prune callback cache
            stale_cbs = [k for k, v in self.callback_cache.items() if now - v.get("timestamp", 0) > 1800]
            for k in stale_cbs: del self.callback_cache[k]
            
            # Prune abandoned user states
            stale_states = [k for k, v in self.user_states.items() if now - v.get("timestamp", 0) > 1800]
            for k in stale_states: del self.user_states[k]
        
        self.last_cache_prune = now

    def _create_callback_data(self, payload):
        cb_id = str(uuid.uuid4())[:8]
        with self.db.lock:
            payload["timestamp"] = time.time()
            self.callback_cache[cb_id] = payload
        return f"cb_{cb_id}"

    def send_direct_message(self, chat_id, text, reply_markup=None):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup: payload["reply_markup"] = reply_markup
        try:
            res = requests.post(url, json=payload, timeout=10)
            if not res.json().get("ok"): self._debug_print(res.text)
        except Exception as e: self._debug_print(str(e))

    def poll_updates(self):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        while not self.shutdown_event.is_set():
            if time.time() - self.last_cache_prune > self.CACHE_PRUNE_INTERVAL:
                self._prune_internal_memory()

            payload = {"offset": int(self.db.last_update_id) + 1, "timeout": 60}
            try:
                res = requests.post(url, json=payload, timeout=65).json()
                if res.get("ok") and res["result"]:
                    for update in res["result"]:
                        with self.db.lock: self.db.last_update_id = update["update_id"]
                        self._process_update(update)
                    self.db.save_all()
            except Exception as e:
                self._debug_print(str(e))
                time.sleep(2)

    def _process_update(self, update):
        if "message" in update and "text" in update["message"]:
            chat_id = str(update["message"]["chat"]["id"])
            text = update["message"]["text"].strip()
            
            # Global command override
            if text == "/cancel":
                with self.db.lock: self.user_states.pop(chat_id, None)
                self.send_direct_message(chat_id, "❌ <b>Action cancelled.</b>")
                return

            if text.startswith("/") and chat_id in self.user_states:
                with self.db.lock: del self.user_states[chat_id]

            if text in ["/start", "/sub"]:
                days = self.TIMEOUT_SECONDS // 86400
                with self.db.lock:
                    self.db.subscribers[chat_id] = {"joined": time.time(), "notified": False}
                self.send_direct_message(chat_id, f"✅ Subscription active for {days} days.")
                
            elif text == "/status":
                with self.db.lock: count = len(self.db.subscribers)
                uptime = self._get_uptime_string()
                self.send_direct_message(chat_id, f"📊 <b>Status</b>\nUsers: {count}/{self.max_subs}\nUptime: {uptime}")

            # ... [Rest of command logic: /search, /add, /rm as in version 3.4.0] ...
            # Ensure any waiting state logic (waiting_keyword, etc.) uses self.user_states[chat_id]["timestamp"] = time.time()