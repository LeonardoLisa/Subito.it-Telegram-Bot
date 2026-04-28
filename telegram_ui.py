"""
Filename: telegram_ui.py
Version: 3.3.0
Date: 2026-04-28
Author: Leonardo Lisa
Description: Standardized Telegram Bot Controller. Accurately restores the original 
             UI state machines, commands (/help, /status, /add, /rm, /search),
             inline keyboards, and exact message templates. Includes verbose error logging.
Requirements: requests

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
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
    # Subscription duration: 96 hours
    TIMEOUT_SECONDS = 4 * 24 * 3600

    def __init__(self, token, db, shutdown_event, max_subs=2):
        self.token = token
        self.db = db
        self.shutdown_event = shutdown_event
        self.max_subs = max_subs
        self.user_states = {}
        self.callback_cache = {}
        self.start_time = time.time()
        
    def _create_callback_data(self, payload):
        cb_id = str(uuid.uuid4())[:8]
        with self.db.lock:
            self.callback_cache[cb_id] = payload
            self.callback_cache[cb_id]["timestamp"] = time.time()
        return f"cb_{cb_id}"

    def _is_valid_subito_url(self, url):
        """Strictly validates if the provided URL is a valid Subito.it link with the correct path."""
        # Normalize URL to always have a scheme for proper parsing
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
            
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ["http", "https"]: return False
            if parsed.netloc not in ["www.subito.it", "subito.it"]: return False
            # Force the path to start strictly with /annunci-italia/vendita/
            if not parsed.path.startswith("/annunci-italia/vendita/"): return False
            return True
        except Exception:
            return False

    def clear_offline_updates(self):
        print("\033[96m[TG] Purging offline message queue...\033[0m")
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        payload = {"offset": int(self.db.last_update_id) + 1, "timeout": 5}
        try:
            res = requests.post(url, json=payload, timeout=10)
            data = res.json()
            if data.get("ok") and data["result"]:
                with self.db.lock:
                    for update in data["result"]:
                        self.db.last_update_id = update["update_id"]
                self.db.save_all()
                print(f"\033[96m[TG] Cleared {len(data['result'])} queued messages.\033[0m")
        except Exception as e:
            print(f"\033[91m[TG Clear Queue Error] {e}\033[0m")

    def send_direct_message(self, chat_id, text, reply_markup=None):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            res = requests.post(url, json=payload, timeout=10)
            data = res.json()
            if not data.get("ok"):
                print(f"\033[91m[TG API ERROR - Direct Msg] {data}\033[0m")
        except Exception as e:
            print(f"\033[91m[TG NET ERROR - Direct Msg] {e}\033[0m")

    def broadcast(self, msg_text, image_bytes=None, item_url=None):
        with self.db.lock:
            subs = list(self.db.subscribers.keys())
        if not subs: return
        
        reply_markup = {"inline_keyboard": []}
        if item_url:
            reply_markup["inline_keyboard"].append([{"text": "🛒 Go to Ad", "url": item_url}])
        reply_markup["inline_keyboard"].append([{"text": "🗑️ Delete", "callback_data": "delete_msg"}])
        reply_markup_json = json.dumps(reply_markup)

        for chat_id in subs:
            try:
                sent = False
                if image_bytes:
                    url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
                    payload = {
                        "chat_id": chat_id, 
                        "caption": msg_text[:1024], 
                        "parse_mode": "HTML", 
                        "reply_markup": reply_markup_json
                    }
                    files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
                    res = requests.post(url, data=payload, files=files, timeout=15)
                    data = res.json()
                    if data.get("ok"): 
                        sent = True
                    else:
                        print(f"\033[91m[TG API ERROR - Photo Broadcast] {data}\033[0m")

                if not sent:
                    url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                    payload = {
                        "chat_id": chat_id, 
                        "text": msg_text, 
                        "parse_mode": "HTML", 
                        "disable_web_page_preview": True,
                        "reply_markup": reply_markup_json
                    }
                    res = requests.post(url, json=payload, timeout=10)
                    data = res.json()
                    if not data.get("ok"):
                        print(f"\033[91m[TG API ERROR - Text Broadcast] {data}\033[0m")
            except Exception as e:
                print(f"\033[91m[TG NET ERROR - Broadcast] {e}\033[0m")

    def poll_updates(self):
        print("\033[96m[TG] Long-Polling Thread Started!\033[0m")
        self.clear_offline_updates()
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        
        while not self.shutdown_event.is_set():
            payload = {"offset": int(self.db.last_update_id) + 1, "timeout": 60}
            try:
                res = requests.post(url, json=payload, timeout=65)
                data = res.json()
                if data.get("ok") and data["result"]:
                    state_changed = False
                    for update in data["result"]:
                        with self.db.lock:
                            self.db.last_update_id = update["update_id"]
                        self._process_update(update)
                        state_changed = True
                    if state_changed:
                        self.db.save_all()
                elif not data.get("ok"):
                    print(f"\033[91m[TG POLL API ERROR] {data}\033[0m")
            except requests.exceptions.Timeout:
                pass
            except Exception as e:
                print(f"\033[91m[TG POLL EXCEPTION] {e}\033[0m")
                if not self.shutdown_event.is_set():
                    self.shutdown_event.wait(2)

    def _process_update(self, update):
        if "message" in update and "text" in update["message"]:
            chat_id = str(update["message"]["chat"]["id"])
            text = update["message"]["text"].strip()
            
            # Abort pending state if a new command is issued
            if text.startswith("/") and chat_id in self.user_states:
                with self.db.lock:
                    del self.user_states[chat_id]
            
            if text in ["/start", "/sub"]:
                with self.db.lock:
                    days = self.TIMEOUT_SECONDS
                    if chat_id not in self.db.subscribers:
                        if len(self.db.subscribers) >= self.max_subs:
                            self.send_direct_message(chat_id, f"⚠️ System full. Max {self.max_subs} users.")
                        else:
                            self.db.subscribers[chat_id] = {"joined": time.time(), "notified": False}
                            self.send_direct_message(chat_id, f"✅ Subscription active for {days} days.")
                    else:
                        self.db.subscribers[chat_id]["joined"] = time.time()
                        self.db.subscribers[chat_id]["notified"] = False
                        self.send_direct_message(chat_id, f"✅ Subscription renewed for {days} days.")
                
            elif text == "/unsub":
                with self.db.lock:
                    if chat_id in self.db.subscribers: 
                        del self.db.subscribers[chat_id]
                        self.send_direct_message(chat_id, "❌ Unsubscribed. You will no longer receive alerts.")
                        print(f"\033[93m[TG] Subscriber removed: {chat_id}\033[0m")
                    else:
                        self.send_direct_message(chat_id, "⚠️ You are not subscribed.")
                
            elif text in ["/search", "/searches"]:
                with self.db.lock: searches = self.db.searches.copy()
                if not searches:
                    self.send_direct_message(chat_id, "📂 <b>Active Searches:</b>\n\nNo active searches.")
                else:
                    keyboard = {"inline_keyboard": []}
                    for cat in searches.keys():
                        cb_data = self._create_callback_data({"action": "cat", "cat": cat})
                        keyboard["inline_keyboard"].append([{"text": f"📁 {cat}", "callback_data": cb_data}])
                    self.send_direct_message(chat_id, "📂 <b>Select a category:</b>", reply_markup=keyboard)

            elif text.startswith("/add"):
                parts = text.split(" ", 1)
                if len(parts) < 2:
                    self.send_direct_message(chat_id, "⚠️ <b>Usage:</b> /add &lt;link&gt;\nExample: <code>/add https://www.subito.it/annunci-italia/vendita/usato/?q=macbook</code>")
                    return
                
                link = parts[1].strip()
                if not self._is_valid_subito_url(link):
                    self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Invalid link. You must provide a valid Subito.it URL starting with /annunci-italia/vendita/.")
                    return
                    
                # Normalize link formatting before saving
                if not link.startswith("http://") and not link.startswith("https://"):
                    link = "https://" + link
                elif link.startswith("http://"):
                    link = link.replace("http://", "https://", 1)
                    
                # Synchronous HTTP validation via curl_cffi to bypass WAF
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
                        self.send_direct_message(chat_id, f"⚠️ <b>Error:</b> The link is unreachable (HTTP {check_res.status_code}).")
                        return
                    if '__NEXT_DATA__' not in check_res.text:
                        self.send_direct_message(chat_id, "⚠️ <b>Error:</b> The provided link does not appear to be a valid Subito.it search page.")
                        return
                except Exception as e:
                    print(f"\033[91m[TG LINK VALIDATION ERROR] {e}\033[0m")
                    self.send_direct_message(chat_id, "⚠️ <b>Network Error:</b> Failed to validate the link. Try again later.")
                    return
                    
                with self.db.lock: searches = self.db.searches.copy()
                keyboard = {"inline_keyboard": []}
                for cat in searches.keys():
                    cb_data = self._create_callback_data({"action": "addcat", "cat": cat})
                    keyboard["inline_keyboard"].append([{"text": f"📁 {cat}", "callback_data": cb_data}])
                
                cb_new = self._create_callback_data({"action": "addcat", "cat": "new"})
                cb_cancel = self._create_callback_data({"action": "cancel"})
                keyboard["inline_keyboard"].append([{"text": "➕ New Category", "callback_data": cb_new}])
                keyboard["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": cb_cancel}])
                
                with self.db.lock:
                    self.user_states[chat_id] = {"state": "waiting_category", "link": link, "timestamp": time.time()}
                    
                self.send_direct_message(chat_id, "📂 Choose a macro category or create a new one:", reply_markup=keyboard)

            elif text.startswith("/rm"):
                parts = text.split(" ", 1)
                if len(parts) < 2:
                    self.send_direct_message(chat_id, "⚠️ <b>Usage:</b> /rm &lt;keyword&gt;\nExample: <code>/rm macbook</code>")
                    return
                kw = parts[1].strip()
                with self.db.lock: searches = self.db.searches.copy()
                
                matches = []
                for cat, kws in searches.items():
                    if kw in kws:
                        matches.append((cat, kw))
                        
                if not matches:
                    self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Keyword not found.")
                else:
                    keyboard = {"inline_keyboard": []}
                    for cat, match_kw in matches:
                        cb_conf = self._create_callback_data({"action": "rmconf", "cat": cat, "kw": match_kw})
                        keyboard["inline_keyboard"].append([{"text": f"🗑️ {cat} - {match_kw}", "callback_data": cb_conf}])
                    
                    cb_cancel = self._create_callback_data({"action": "cancel"})
                    keyboard["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": cb_cancel}])
                    
                    self.send_direct_message(chat_id, f"🗑️ <b>Select the search you want to delete:</b>\nKeyword: {html.escape(kw)}", reply_markup=keyboard)

            elif text == "/help":
                help_text = (
                    "🤖 <b>Available Commands</b>\n\n"
                    "✅ <b>/sub</b> - Subscribe to receive listing alerts.\n"
                    "❌ <b>/unsub</b> - Unsubscribe and stop receiving alerts.\n"
                    "🔎 <b>/search</b> - View all active search targets and categories.\n"
                    "➕ <b>/add &lt;link&gt;</b> - Add a new search. You must provide a valid Subito.it search URL.\n"
                    "🗑️ <b>/rm &lt;keyword&gt;</b> - Remove an existing search.\n"
                    "📊 <b>/status</b> - View bot statistics and uptime.\n"
                    "❓ <b>/help</b> - Show this detailed help message."
                )
                self.send_direct_message(chat_id, help_text)

            elif text == "/status":
                with self.db.lock: active_count = len(self.db.subscribers)
                uptime_seconds = int(time.time() - self.start_time)
                hours, remainder = divmod(uptime_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                uptime_str = f"{hours}h {minutes}m {seconds}s"
                self.send_direct_message(chat_id, f"📊 <b>System Status</b>\nUsers: {active_count}/{self.max_subs}\nUptime: {uptime_str}")

            elif chat_id in self.user_states:
                with self.db.lock:
                    state_data = self.user_states[chat_id].copy()
                    self.user_states[chat_id]["timestamp"] = time.time()
                    
                if state_data["state"] == "waiting_new_cat_name":
                    new_cat = text.strip()
                    # Splitted validation to report exact reason for failure
                    if len(new_cat) > 30:
                        self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Category name is too long (maximum 30 characters). Try again.")
                    elif not re.match(r'^[a-zA-Z0-9\s\-]+$', new_cat):
                        self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Category name contains invalid characters. Only alphanumeric characters, spaces, and hyphens are allowed. Try again.")
                    else:
                        with self.db.lock:
                            self.user_states[chat_id]["state"] = "waiting_keyword"
                            self.user_states[chat_id]["category"] = new_cat
                        self.send_direct_message(chat_id, f"Category '<b>{html.escape(new_cat)}</b>' set. Write the keyword for this search:")
                        
                elif state_data["state"] == "waiting_keyword":
                    new_kw = text.strip()
                    cat = state_data["category"]
                    link = state_data["link"]
                    
                    with self.db.lock:
                        if cat not in self.db.searches: self.db.searches[cat] = {}
                        
                        base_kw = new_kw
                        counter = 1
                        while new_kw in self.db.searches[cat]:
                            new_kw = f"{base_kw} {counter}"
                            counter += 1
                            
                        self.db.searches[cat][new_kw] = link
                        del self.user_states[chat_id]
                        
                    self.send_direct_message(chat_id, f"✅ <b>Search successfully added!</b>\n📂 Category: {html.escape(cat)}\n🔑 Keyword: {html.escape(new_kw)}")

        elif "callback_query" in update:
            cb = update["callback_query"]
            chat_id = str(cb["message"]["chat"]["id"])
            message_id = cb["message"]["message_id"]
            cb_id = cb["id"]
            cb_data = cb.get("data", "")
            
            ans_url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
            edit_url = f"https://api.telegram.org/bot{self.token}/editMessageText"
            
            if cb_data == "delete_msg":
                del_url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
                try: requests.post(del_url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
                except Exception: pass
                
            elif cb_data == "search_back":
                with self.db.lock: searches = self.db.searches.copy()
                keyboard = {"inline_keyboard": []}
                for cat in searches.keys():
                    c_data = self._create_callback_data({"action": "cat", "cat": cat})
                    keyboard["inline_keyboard"].append([{"text": f"📁 {cat}", "callback_data": c_data}])
                try: requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": "📂 <b>Select a category:</b>", "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                except Exception: pass
                
            elif cb_data.startswith("cb_"):
                cache_id = cb_data[3:]
                with self.db.lock: cb_info = self.callback_cache.get(cache_id)
                    
                if not cb_info:
                    try: requests.post(ans_url, json={"callback_query_id": cb_id, "text": "Session expired.", "show_alert": True}, timeout=5)
                    except Exception: pass
                    return
                    
                action = cb_info.get("action")
                
                if action == "cancel":
                    with self.db.lock:
                        if chat_id in self.user_states:
                            del self.user_states[chat_id]
                    try: requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": "❌ <b>Operation cancelled.</b>", "parse_mode": "HTML"}, timeout=5)
                    except Exception: pass

                elif action == "cat":
                    cat_name = cb_info["cat"]
                    with self.db.lock: searches = self.db.searches.copy()
                    if cat_name in searches:
                        keyboard = {"inline_keyboard": []}
                        for kw, url_link in searches[cat_name].items():
                            keyboard["inline_keyboard"].append([{"text": kw, "url": url_link}])
                        keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "search_back"}])
                        try: requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"📂 <b>{html.escape(cat_name)}</b>\nSelect a search:", "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                        except Exception: pass
                        
                elif action == "addcat":
                    cat_name = cb_info["cat"]
                    with self.db.lock:
                        in_state = chat_id in self.user_states and self.user_states[chat_id]["state"] == "waiting_category"
                    if in_state:
                        if cat_name == "new":
                            with self.db.lock:
                                self.user_states[chat_id]["state"] = "waiting_new_cat_name"
                                self.user_states[chat_id]["timestamp"] = time.time()
                            try: requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": "Write the name of the new category (max 30 chars, alphanumeric):"}, timeout=5)
                            except Exception: pass
                        else:
                            with self.db.lock:
                                self.user_states[chat_id]["state"] = "waiting_keyword"
                                self.user_states[chat_id]["category"] = cat_name
                                self.user_states[chat_id]["timestamp"] = time.time()
                            try: requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"Category '<b>{html.escape(cat_name)}</b>' selected. Write the keyword for this search:", "parse_mode": "HTML"}, timeout=5)
                            except Exception: pass
                            
                elif action == "rmconf":
                    cat = cb_info["cat"]
                    kw = cb_info["kw"]
                    with self.db.lock:
                        if cat in self.db.searches and kw in self.db.searches[cat]:
                            del self.db.searches[cat][kw]
                            # Auto-Cleanup: Remove category entirely if no keywords are left
                            if not self.db.searches[cat]: 
                                del self.db.searches[cat]
                    try: requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"✅ Search <b>{html.escape(kw)}</b> successfully deleted.", "parse_mode": "HTML"}, timeout=5)
                    except Exception: pass

            try:
                requests.post(ans_url, json={"callback_query_id": cb_id}, timeout=5)
            except Exception:
                pass