"""
Filename: telegram_ui.py
Version: 4.0.1
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
import time
import html
import uuid
import json
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

class TelegramUI:
    TIMEOUT_SECONDS = 4 * 24 * 3600  # 96 hours
    CACHE_PRUNE_INTERVAL = 1800      # 30 minutes
    MAX_REGULAR_SEARCHES = 15        # Maximum allowed searches for regular users
    MAX_SUBSCRIBERS = 3              # Maximum number of total allowed users

    def __init__(self, token, db, shutdown_event):
        self.token = token
        self.db = db
        self.shutdown_event = shutdown_event
        self.debug_mode = False
        self.user_states = {}
        self.callback_cache = {}
        self.start_time = time.time()
        self.last_cache_prune = time.time()
        
    def _debug_print(self, error_msg):
        if self.debug_mode:
            print(f"\033[91m[TG_UI ERROR] {error_msg}\033[0m")

    def _get_uptime_string(self):
        delta = int(time.time() - self.start_time)
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

    def _prune_internal_memory(self):
        now = time.time()
        with self.db.lock:
            stale_cbs = [k for k, v in self.callback_cache.items() if now - v.get("timestamp", 0) > 1800]
            for k in stale_cbs: del self.callback_cache[k]
            
            stale_states = [k for k, v in self.user_states.items() if now - v.get("timestamp", 0) > 1800]
            for k in stale_states: del self.user_states[k]
        
        self.last_cache_prune = now

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

    def send_ad(self, chat_id, msg_text, image_bytes=None, item_url=None):
        """Sends an ad notification directly to a specific user (replaces global broadcast for ads)."""
        reply_markup = {"inline_keyboard": []}
        if item_url:
            reply_markup["inline_keyboard"].append([{"text": "🛒 Go to Ad", "url": item_url}])
        reply_markup["inline_keyboard"].append([{"text": "🗑️ Delete", "callback_data": "delete_msg"}])
        reply_markup_json = json.dumps(reply_markup)

        try:
            sent = False
            if image_bytes:
                url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
                payload = {"chat_id": chat_id, "caption": msg_text[:1024], "parse_mode": "HTML", "reply_markup": reply_markup_json}
                files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
                res = requests.post(url, data=payload, files=files, timeout=15)
                data = res.json()
                if data.get("ok"): 
                    sent = True
                else:
                    self._debug_print(f"Photo Send API Error: {data}")

            if not sent:
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                payload = {"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": reply_markup_json}
                res = requests.post(url, json=payload, timeout=10)
                data = res.json()
                if not data.get("ok"):
                    self._debug_print(f"Text Send API Error: {data}")
        except Exception as e:
            self._debug_print(f"Send Ad Net Error: {e}")

    def broadcast(self, msg_text, show_delete=True):
        """Sends a global system message to all registered users."""
        users = self.db.get_all_users()
        if not users: return
        
        reply_markup = {"inline_keyboard": []}
        if show_delete:
            reply_markup["inline_keyboard"].append([{"text": "🗑️ Delete", "callback_data": "delete_msg"}])
            
        reply_markup_json = json.dumps(reply_markup) if reply_markup["inline_keyboard"] else None

        for user in users:
            chat_id = user['chat_id']
            try:
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                payload = {"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML", "disable_web_page_preview": True}
                if reply_markup_json: payload["reply_markup"] = reply_markup_json
                res = requests.post(url, json=payload, timeout=10)
                data = res.json()
                if not data.get("ok"):
                    self._debug_print(f"Text Broadcast API Error: {data}")
            except Exception as e:
                self._debug_print(f"Broadcast Net Error: {e}")

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
            text = update["message"]["text"].strip()
            
            # Ensure user exists in DB before processing
            user_data = self.db.get_user(chat_id)
            is_superuser = user_data['is_superuser'] == 1 if user_data else False
            
            if text == "/cancel":
                with self.db.lock: self.user_states.pop(chat_id, None)
                self.send_direct_message(chat_id, "❌ <b>Action cancelled.</b>")
                return

            if text.startswith("/") and chat_id in self.user_states:
                with self.db.lock:
                    del self.user_states[chat_id]
            
            if text in ["/start", "/sub"]:
                days = self.TIMEOUT_SECONDS
                if not user_data:
                    # Check capacity before registering a NEW user
                    current_users = len(self.db.get_all_users())
                    if current_users >= self.MAX_SUBSCRIBERS:
                        self.send_direct_message(chat_id, "⚠️ <b>Registrations Closed.</b>\nThe bot has reached its maximum user capacity.")
                        return
                    
                    self.db.register_user(chat_id)
                    self.send_direct_message(chat_id, f"✅ Subscription active for {days} days.")
                else:
                    self.db.register_user(chat_id) # Updates last_active
                    self.send_direct_message(chat_id, f"✅ Subscription renewed for {days} days.")
                return
                
            elif text == "/unsub":
                if user_data: 
                    self.db.remove_user(chat_id)
                    self.send_direct_message(chat_id, "❌ Unsubscribed. All your data has been deleted.")
                else:
                    self.send_direct_message(chat_id, "⚠️ Not subscribed.")
                return

            # Commands below require subscription
            if not user_data:
                self.send_direct_message(chat_id, "⚠️ <b>Action Denied.</b>\nYou must subscribe first using /sub.")
                return
                
            if text in ["/search", "/searches"]:
                searches = self.db.get_user_searches(chat_id)
                if not searches:
                    self.send_direct_message(chat_id, "📂 <b>Active Searches:</b>\n\nNo active searches.")
                else:
                    keyboard = {"inline_keyboard": []}
                    for cat in searches.keys():
                        cb_data = self._create_callback_data({"action": "cat", "cat": cat})
                        keyboard["inline_keyboard"].append([{"text": f"📁 {cat}", "callback_data": cb_data}])
                    self.send_direct_message(chat_id, "📂 <b>Select a category:</b>", reply_markup=keyboard)

            elif text.startswith("/add"):
                current_count = self.db.count_user_searches(chat_id)
                if not is_superuser and current_count >= self.MAX_REGULAR_SEARCHES:
                    self.send_direct_message(chat_id, f"⚠️ <b>Limit Reached.</b>\nYou cannot have more than {self.MAX_REGULAR_SEARCHES} active searches.")
                    return

                parts = text.split(" ", 1)
                if len(parts) < 2:
                    self.send_direct_message(chat_id, "⚠️ <b>Usage:</b> /add &lt;link&gt;")
                    return
                
                link = parts[1].strip()
                if not self._is_valid_subito_url(link):
                    self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Invalid Subito.it link. Must start with /annunci-italia/vendita/.")
                    return
                    
                if not link.startswith("http://") and not link.startswith("https://"):
                    link = "https://" + link
                elif link.startswith("http://"):
                    link = link.replace("http://", "https://", 1)
                
                # Automatically enforce &order=datedesc parameter
                parsed_link = urlparse(link)
                query_params = dict(parse_qsl(parsed_link.query))
                query_params['order'] = 'datedesc'
                new_query = urlencode(query_params)
                link = urlunparse((parsed_link.scheme, parsed_link.netloc, parsed_link.path, parsed_link.params, new_query, parsed_link.fragment))
                    
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
                        self.send_direct_message(chat_id, f"⚠️ <b>Error:</b> Link unreachable (HTTP {check_res.status_code}).")
                        return
                    if '__NEXT_DATA__' not in check_res.text:
                        self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Not a valid Subito.it search page.")
                        return
                except Exception as e:
                    self._debug_print(f"Link Validation Error: {e}")
                    self.send_direct_message(chat_id, "⚠️ <b>Network Error:</b> Validation failed. Try again.")
                    return
                    
                searches = self.db.get_user_searches(chat_id)
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
                    self.send_direct_message(chat_id, "⚠️ <b>Usage:</b> /rm &lt;Search Name&gt;")
                    return
                search_name = parts[1].strip()
                searches = self.db.get_user_searches(chat_id)
                
                matches = []
                for cat, items in searches.items():
                    for item in items:
                        if item['name'] == search_name:
                            matches.append((cat, item['name']))
                        
                if not matches:
                    self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Search Name not found.")
                else:
                    keyboard = {"inline_keyboard": []}
                    for cat, match_name in matches:
                        cb_conf = self._create_callback_data({"action": "rmconf", "name": match_name})
                        keyboard["inline_keyboard"].append([{"text": f"🗑️ {cat} - {match_name}", "callback_data": cb_conf}])
                    
                    cb_cancel = self._create_callback_data({"action": "cancel"})
                    keyboard["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": cb_cancel}])
                    
                    self.send_direct_message(chat_id, f"🗑️ <b>Select search to delete:</b>\nSearch Name: {html.escape(search_name)}", reply_markup=keyboard)

            elif text == "/help":
                help_text = (
                    "🤖 <b>Available Commands</b>\n\n"
                    "✅ <b>/sub</b> - Subscribe to receive alerts.\n"
                    "❌ <b>/unsub</b> - Unsubscribe and delete your data.\n"
                    "🔎 <b>/search</b> - View active searches.\n"
                    "➕ <b>/add &lt;link&gt;</b> - Add a new search.\n"
                    "🗑️ <b>/rm &lt;Search Name&gt;</b> - Remove a search.\n"
                    "📊 <b>/status</b> - View system status.\n"
                    "🛑 <b>/cancel</b> - Abort current action."
                )
                self.send_direct_message(chat_id, help_text)

            elif text == "/status":
                active_count = len(self.db.get_all_users())
                user_search_count = self.db.count_user_searches(chat_id)
                uptime_str = self._get_uptime_string()
                
                status_msg = f"📊 <b>System Status</b>\nUptime: {uptime_str}\nTotal Users: {active_count}\n\n"
                status_msg += f"👤 <b>Your Account</b>\nActive Searches: {user_search_count}"
                
                if not is_superuser:
                    status_msg += f"/{self.MAX_REGULAR_SEARCHES}"
                    
                self.send_direct_message(chat_id, status_msg)

            elif chat_id in self.user_states:
                with self.db.lock:
                    state_data = self.user_states[chat_id].copy()
                    self.user_states[chat_id]["timestamp"] = time.time()
                    
                if state_data["state"] == "waiting_new_cat_name":
                    new_cat = text.strip()
                    if len(new_cat) > 30:
                        self.send_direct_message(chat_id, "⚠️ Category name too long (max 30).")
                    elif not re.match(r'^[a-zA-Z0-9\s\-]+$', new_cat):
                        self.send_direct_message(chat_id, "⚠️ Invalid characters. Use alphanumeric, spaces, hyphens.")
                    else:
                        with self.db.lock:
                            self.user_states[chat_id]["state"] = "waiting_keyword"
                            self.user_states[chat_id]["category"] = new_cat
                        self.send_direct_message(chat_id, f"Category '<b>{html.escape(new_cat)}</b>' set. Write the Search Name:")
                        
                elif state_data["state"] == "waiting_keyword":
                    new_kw = text.strip()
                    cat = state_data["category"]
                    
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
                        f"Search Name set to '<b>{html.escape(new_kw)}</b>'.\n\n"
                        f"Optional: Send up to 3 <b>exclusion keywords</b> separated by commas (e.g. broken, parts, not working).\n"
                        f"Send <b>NONE</b> to skip this step."
                    )
                        
                elif state_data["state"] == "waiting_exclusion":
                    excl_input = text.strip().lower()
                    cat = state_data["category"]
                    link = state_data["link"]
                    name = state_data["name"]
                    
                    exclusion_list = []
                    if excl_input != "none":
                        raw_list = [k.strip() for k in excl_input.split(',')]
                        exclusion_list = [k for k in raw_list if k][:3] # Take up to 3 valid keywords
                    
                    self.db.add_search(chat_id, cat, name, link, exclusion_list)
                    
                    with self.db.lock:
                        del self.user_states[chat_id]
                        
                    excl_str = ", ".join(exclusion_list) if exclusion_list else "None"
                    self.send_direct_message(
                        chat_id, 
                        f"✅ <b>Added!</b>\n📂 Category: {html.escape(cat)}\n🔑 Search Name: {html.escape(name)}\n🚫 Exclusions: {html.escape(excl_str)}"
                    )

        elif "callback_query" in update:
            cb = update["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            message_id = cb["message"]["message_id"]
            cb_id = cb["id"]
            cb_data = cb.get("data", "")
            
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
                    keyboard["inline_keyboard"].append([{"text": f"📁 {cat}", "callback_data": c_data}])
                try: 
                    requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": "📂 <b>Select a category:</b>", "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                except Exception as e:
                    self._debug_print(f"Search back error: {e}")
                
            elif cb_data.startswith("cb_"):
                cache_id = cb_data[3:]
                with self.db.lock: cb_info = self.callback_cache.get(cache_id)
                    
                if not cb_info:
                    try: 
                        requests.post(ans_url, json={"callback_query_id": cb_id, "text": "Session expired.", "show_alert": True}, timeout=5)
                    except Exception: pass
                    return
                    
                action = cb_info.get("action")
                
                if action == "cancel":
                    with self.db.lock:
                        if chat_id in self.user_states:
                            del self.user_states[chat_id]
                    try: 
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": "❌ <b>Operation cancelled.</b>", "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Cancel cb error: {e}")

                elif action == "cat":
                    cat_name = cb_info["cat"]
                    searches = self.db.get_user_searches(chat_id)
                    if cat_name in searches:
                        keyboard = {"inline_keyboard": []}
                        for item in searches[cat_name]:
                            keyboard["inline_keyboard"].append([{"text": item['name'], "url": item['url']}])
                        keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "search_back"}])
                        try: 
                            requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"📂 <b>{html.escape(cat_name)}</b>\nSelect a search:", "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                        except Exception as e:
                            self._debug_print(f"Cat cb error: {e}")
                        
                elif action == "addcat":
                    cat_name = cb_info["cat"]
                    with self.db.lock:
                        in_state = chat_id in self.user_states and self.user_states[chat_id]["state"] == "waiting_category"
                    if in_state:
                        if cat_name == "new":
                            with self.db.lock:
                                self.user_states[chat_id]["state"] = "waiting_new_cat_name"
                                self.user_states[chat_id]["timestamp"] = time.time()
                            try: 
                                requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": "Write the name of the new category (max 30 chars, alphanumeric):"}, timeout=5)
                            except Exception as e:
                                self._debug_print(f"Add new cat cb error: {e}")
                        else:
                            with self.db.lock:
                                self.user_states[chat_id]["state"] = "waiting_keyword"
                                self.user_states[chat_id]["category"] = cat_name
                                self.user_states[chat_id]["timestamp"] = time.time()
                            try: 
                                requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"Category '<b>{html.escape(cat_name)}</b>' selected. Write the Search Name:", "parse_mode": "HTML"}, timeout=5)
                            except Exception as e:
                                self._debug_print(f"Add cat cb error: {e}")
                            
                elif action == "rmconf":
                    name = cb_info["name"]
                    self.db.remove_search(chat_id, name)
                    try: 
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"✅ Search <b>{html.escape(name)}</b> deleted.", "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Rmconf cb error: {e}")

            try:
                requests.post(ans_url, json={"callback_query_id": cb_id}, timeout=5)
            except Exception:
                pass