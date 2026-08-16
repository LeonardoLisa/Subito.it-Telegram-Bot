"""
Filename: telegram_ui.py
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

from scraper_subito import SubitoScraper

class TelegramUI:
    # Defaults, all overridable via .env - see README "Configurazione avanzata via .env".
    DEFAULT_SUBSCRIPTION_TTL_DAYS = 4
    DEFAULT_CACHE_PRUNE_INTERVAL_SECONDS = 1800
    DEFAULT_MAX_REGULAR_SEARCHES = 15
    DEFAULT_MAX_SUBSCRIBERS = 3
    DEFAULT_MAX_EXCLUSION_KEYWORDS = 3
    DEFAULT_MAX_INCLUDE_KEYWORDS = 3

    def __init__(self, token, db, shutdown_event):
        self.token = token
        self.db = db
        self.shutdown_event = shutdown_event
        self.debug_mode = False
        self.user_states = {}
        self.callback_cache = {}
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

    def _validate_and_prompt_category(self, chat_id, link):
        """
        Runs off the Telegram polling thread (submitted via `_add_executor`) so that
        validating a /add link never blocks updates for other users. Actually performs
        the request against Subito.it to verify the link leads to an existing, real
        search page, rather than trying to keep an exhaustive category-id map in sync.
        """
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

    def _validate_and_update_url(self, chat_id, search_name, new_link):
        """
        Background counterpart of `_validate_and_prompt_category` used when editing
        the URL of an EXISTING search: verifies the new link is a real Subito.it
        search page, then updates the search in place (preserving its tracked-ads
        history) instead of going through the add/category-selection flow.
        """
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
                self.send_direct_message(chat_id, f"⚠️ <b>Error:</b> Link unreachable (HTTP {check_res.status_code}). The search was NOT modified.")
                return
            if '__NEXT_DATA__' not in check_res.text:
                self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Not a valid Subito.it search page. The search was NOT modified.")
                return
        except Exception as e:
            self._debug_print(f"URL edit validation error: {e}")
            self.send_direct_message(chat_id, "⚠️ <b>Network Error:</b> Validation failed. The search was NOT modified.")
            return

        self.db.update_search(chat_id, search_name, url=new_link)
        confirmation = f"✅ URL updated for search <b>{html.escape(search_name)}</b>."
        if not SubitoScraper.is_category_recognized(new_link):
            confirmation += (
                "\n\nℹ️ This category isn't fully supported by the internal filter: "
                "results for this search may be broader than expected."
            )
        self.send_direct_message(chat_id, confirmation)

    def _render_search_menu(self, chat_id, message_id, cat_name, search_name):
        """
        Renders (or re-renders, after a pause/resume toggle) the detail menu for a
        single search: status, filters, and the Pause/Resume, Edit, Delete, Copy
        Link, Open Link and Back actions.
        """
        edit_url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        searches = self.db.get_user_searches(chat_id)

        target_item = None
        if cat_name in searches:
            for item in searches[cat_name]:
                if item['name'] == search_name:
                    target_item = item
                    break

        if not target_item:
            try:
                requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": "⚠️ Search not found.", "parse_mode": "HTML"}, timeout=5)
            except Exception:
                pass
            return

        url = target_item['url']
        enabled = target_item.get('enabled', True)

        msg_text = (
            f"📂 <b>Category:</b> {html.escape(cat_name)}\n"
            f"🔍 <b>Search:</b> {html.escape(search_name)}\n"
            f"{'▶️ <b>Status:</b> Active' if enabled else '⏸️ <b>Status:</b> Paused'}\n"
        )
        if target_item['exclusion_kws']:
            excl = ", ".join(target_item['exclusion_kws'])
            msg_text += f"🚫 <b>Exclusions:</b> {html.escape(excl)}\n"
        if target_item.get('include_kws'):
            incl = ", ".join(target_item['include_kws'])
            msg_text += f"✅ <b>Includes:</b> {html.escape(incl)}\n"

        # Mantenuto per il tap-to-copy sui client vecchi
        msg_text += f"\n🔗 <b>Direct Link:</b>\n<code>{html.escape(url)}</code>"

        cb_toggle = self._create_callback_data({"action": "toggle_enabled", "cat": cat_name, "name": search_name})
        cb_edit = self._create_callback_data({"action": "edit_menu", "cat": cat_name, "name": search_name})
        cb_rm = self._create_callback_data({"action": "rmconf", "name": search_name})
        cb_back = self._create_callback_data({"action": "cat", "cat": cat_name})

        toggle_label = "⏸️ Pause" if enabled else "▶️ Resume"
        keyboard = {"inline_keyboard": [
            [{"text": toggle_label, "callback_data": cb_toggle}],
            [{"text": "✏️ Edit", "callback_data": cb_edit}],
            [{"text": "🗑️ Delete", "callback_data": cb_rm}],
            [{"text": "🔗 Copy Link", "copy_text": {"text": url}}],
            [{"text": "🌐 Open Link", "url": url}],
            [{"text": "🔙 Back", "callback_data": cb_back}],
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
                    keyboard["inline_keyboard"].append([{"text": "❌ Close", "callback_data": "delete_msg"}])
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
                
                # Searches are always saved sorted by "most recent" (order=datedesc).
                # A price-based order is explicitly rejected instead of being silently
                # overwritten, so the user isn't surprised by a search that behaves
                # differently than the link they submitted.
                parsed_link = urlparse(link)
                query_params = dict(parse_qsl(parsed_link.query))
                requested_order = query_params.get('order', '').strip().lower()
                if requested_order in ('pricedesc', 'priceasc'):
                    self.send_direct_message(
                        chat_id,
                        "⚠️ <b>Error:</b> Price-based sorting (<code>order=priceasc</code>/<code>order=pricedesc</code>) is not supported.\n"
                        "Searches are always saved sorted by <b>most recent</b>. Remove the 'order' parameter from the link and try again."
                    )
                    return
                query_params['order'] = 'datedesc'
                new_query = urlencode(query_params)
                link = urlunparse((parsed_link.scheme, parsed_link.netloc, parsed_link.path, parsed_link.params, new_query, parsed_link.fragment))

                # Validating the link requires a network round-trip (up to ~10s). Run it in a
                # background worker so this single-threaded update loop keeps serving other users.
                self.send_direct_message(chat_id, "🔎 <i>Verifying link, please wait...</i>")
                self._add_executor.submit(self._validate_and_prompt_category, chat_id, link)

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

            elif text.startswith("/broadcast"):
                if not is_superuser:
                    self.send_direct_message(chat_id, "⚠️ <b>Action Denied.</b>\nOnly superusers can use this command.")
                    return
                parts = text.split(" ", 1)
                if len(parts) < 2 or not parts[1].strip():
                    self.send_direct_message(chat_id, "⚠️ <b>Usage:</b> /broadcast &lt;message&gt;")
                    return
                broadcast_text = parts[1].strip()
                self.broadcast(f"📢 <b>Announcement</b>\n\n{broadcast_text}")
                self.send_direct_message(chat_id, "✅ Broadcast sent to all users.")

            elif text == "/help":
                help_text = (
                    "🤖 <b>Available Commands</b>\n\n"
                    "✅ <b>/sub</b> - Subscribe to receive alerts.\n"
                    "❌ <b>/unsub</b> - Unsubscribe and delete your data.\n"
                    "🔎 <b>/search</b> - View active searches (pause/resume/edit from there).\n"
                    "➕ <b>/add &lt;link&gt;</b> - Add a new search.\n"
                    "🗑️ <b>/rm &lt;Search Name&gt;</b> - Remove a search.\n"
                    "📊 <b>/status</b> - View system status.\n"
                    "🛑 <b>/cancel</b> - Abort current action."
                )
                if is_superuser:
                    help_text += "\n📢 <b>/broadcast &lt;message&gt;</b> - Send a message to all users (superuser only)."
                self.send_direct_message(chat_id, help_text)

            elif text == "/status":
                active_count = len(self.db.get_all_users())
                user_search_count = self.db.count_user_searches(chat_id)
                uptime_str = self._get_uptime_string()
                
                status_msg = f"📊 <b>System Status</b>\nUptime: {uptime_str}\nTotal Users: {active_count}\n\n"
                status_msg += f"👤 <b>Your Account</b>\nActive Searches: {user_search_count}"
                
                if not is_superuser:
                    status_msg += f"/{self.MAX_REGULAR_SEARCHES}"
                    remaining = self.TIMEOUT_SECONDS - (time.time() - user_data['last_active'])
                    if remaining > 0:
                        status_msg += f"\nSubscription expires in: {self._format_duration(remaining)}"
                    else:
                        status_msg += "\nSubscription: expired (send /sub to renew)"
                else:
                    status_msg += "\nSubscription: Unlimited (superuser)"
                    
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
                        f"Optional: Send up to {self.MAX_EXCLUSION_KEYWORDS} <b>exclusion keywords</b> separated by commas (e.g. broken, parts, not working).\n"
                        f"Send <b>NONE</b> to skip this step."
                    )
                        
                elif state_data["state"] == "waiting_exclusion":
                    excl_input = text.strip().lower()
                    
                    exclusion_list = []
                    if excl_input != "none":
                        raw_list = [k.strip() for k in excl_input.split(',')]
                        exclusion_list = [k for k in raw_list if k][:self.MAX_EXCLUSION_KEYWORDS]
                    
                    with self.db.lock:
                        self.user_states[chat_id]["state"] = "waiting_include"
                        self.user_states[chat_id]["exclusion_kws"] = exclusion_list
                    
                    self.send_direct_message(
                        chat_id,
                        f"Optional: Send up to {self.MAX_INCLUDE_KEYWORDS} <b>include keywords</b> separated by commas: "
                        f"an ad is only notified if its title contains at least one of them (e.g. new, sealed).\n"
                        f"Send <b>NONE</b> to skip this step and match any title."
                    )

                elif state_data["state"] == "waiting_include":
                    incl_input = text.strip().lower()
                    cat = state_data["category"]
                    link = state_data["link"]
                    name = state_data["name"]
                    exclusion_list = state_data.get("exclusion_kws", [])

                    include_list = []
                    if incl_input != "none":
                        raw_list = [k.strip() for k in incl_input.split(',')]
                        include_list = [k for k in raw_list if k][:self.MAX_INCLUDE_KEYWORDS]

                    self.db.add_search(chat_id, cat, name, link, exclusion_list, include_list)
                    
                    with self.db.lock:
                        del self.user_states[chat_id]
                        
                    excl_str = ", ".join(exclusion_list) if exclusion_list else "None"
                    incl_str = ", ".join(include_list) if include_list else "None"
                    confirmation = (
                        f"✅ <b>Added!</b>\n📂 Category: {html.escape(cat)}\n🔑 Search Name: {html.escape(name)}\n"
                        f"🚫 Exclusions: {html.escape(excl_str)}\n✅ Includes: {html.escape(incl_str)}"
                    )
                    if not SubitoScraper.is_category_recognized(link):
                        confirmation += (
                            "\n\nℹ️ This category isn't fully supported by the internal filter: "
                            "results for this search may be broader than expected."
                        )
                    self.send_direct_message(chat_id, confirmation)

                elif state_data["state"] == "editing_field":
                    field = state_data["field"]
                    search_name = state_data["name"]

                    if field == "url":
                        new_link = text.strip()
                        if not self._is_valid_subito_url(new_link):
                            self.send_direct_message(chat_id, "⚠️ <b>Error:</b> Invalid Subito.it link.")
                            return
                        if not new_link.startswith("http://") and not new_link.startswith("https://"):
                            new_link = "https://" + new_link
                        elif new_link.startswith("http://"):
                            new_link = new_link.replace("http://", "https://", 1)

                        parsed_link = urlparse(new_link)
                        query_params = dict(parse_qsl(parsed_link.query))
                        requested_order = query_params.get('order', '').strip().lower()
                        if requested_order in ('pricedesc', 'priceasc'):
                            self.send_direct_message(
                                chat_id,
                                "⚠️ <b>Error:</b> Price-based sorting is not supported. Remove 'order' from the link and try again."
                            )
                            return
                        query_params['order'] = 'datedesc'
                        new_query = urlencode(query_params)
                        new_link = urlunparse((parsed_link.scheme, parsed_link.netloc, parsed_link.path, parsed_link.params, new_query, parsed_link.fragment))

                        with self.db.lock:
                            del self.user_states[chat_id]

                        self.send_direct_message(chat_id, "🔎 <i>Verifying new link, please wait...</i>")
                        self._add_executor.submit(self._validate_and_update_url, chat_id, search_name, new_link)

                    elif field == "exclusion":
                        raw = text.strip().lower()
                        kw_list = [] if raw == "none" else [k.strip() for k in raw.split(',') if k.strip()][:self.MAX_EXCLUSION_KEYWORDS]
                        self.db.update_search(chat_id, search_name, exclusion_kws=kw_list)
                        with self.db.lock:
                            del self.user_states[chat_id]
                        kw_str = ", ".join(kw_list) if kw_list else "None"
                        self.send_direct_message(chat_id, f"✅ Exclusion keywords updated: {html.escape(kw_str)}")

                    elif field == "include":
                        raw = text.strip().lower()
                        kw_list = [] if raw == "none" else [k.strip() for k in raw.split(',') if k.strip()][:self.MAX_INCLUDE_KEYWORDS]
                        self.db.update_search(chat_id, search_name, include_kws=kw_list)
                        with self.db.lock:
                            del self.user_states[chat_id]
                        kw_str = ", ".join(kw_list) if kw_list else "None"
                        self.send_direct_message(chat_id, f"✅ Include keywords updated: {html.escape(kw_str)}")

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
                keyboard["inline_keyboard"].append([{"text": "❌ Close", "callback_data": "delete_msg"}])
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
                            c_data = self._create_callback_data({"action": "search_menu", "cat": cat_name, "name": item['name']})
                            keyboard["inline_keyboard"].append([{"text": item['name'], "callback_data": c_data}])
                        keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "search_back"}])
                        try: 
                            requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"📂 <b>{html.escape(cat_name)}</b>\nSelect a search:", "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
                        except Exception as e:
                            self._debug_print(f"Cat cb error: {e}")
                    else:
                        try:
                            requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"⚠️ <b>{html.escape(cat_name)}</b> è stata eliminata perché non contiene più ricerche.", "parse_mode": "HTML"}, timeout=5)
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
                        [{"text": "🔗 Edit URL", "callback_data": cb_url}],
                        [{"text": "🚫 Edit exclusions", "callback_data": cb_excl}],
                        [{"text": "✅ Edit includes", "callback_data": cb_incl}],
                        [{"text": "🔙 Back", "callback_data": cb_back}],
                    ]}
                    try:
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": f"✏️ <b>Editing:</b> {html.escape(search_name)}\nWhat do you want to change?", "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
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
                        "url": "🔗 Send the new Subito.it link for this search:",
                        "exclusion": f"🚫 Send up to {self.MAX_EXCLUSION_KEYWORDS} exclusion keywords separated by commas, or NONE to clear them:",
                        "include": f"✅ Send up to {self.MAX_INCLUDE_KEYWORDS} include keywords separated by commas, or NONE to clear them:",
                    }
                    try:
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": prompts.get(field, "Send the new value:"), "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Edit field cb error: {e}")

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

                    # Categories aren't a stored entity: they only exist implicitly through
                    # searches. Figure out which category owned this search BEFORE deleting
                    # it, so we can tell the user if that category effectively disappeared.
                    removed_category = None
                    for cat, items in self.db.get_user_searches(chat_id).items():
                        if any(item['name'] == name for item in items):
                            removed_category = cat
                            break

                    self.db.remove_search(chat_id, name)

                    confirm_text = f"✅ Search <b>{html.escape(name)}</b> deleted."
                    if removed_category and removed_category not in self.db.get_user_searches(chat_id):
                        confirm_text += f"\n🗑️ Category <b>{html.escape(removed_category)}</b> has been removed because it no longer contains any searches."

                    try: 
                        requests.post(edit_url, json={"chat_id": chat_id, "message_id": message_id, "text": confirm_text, "parse_mode": "HTML"}, timeout=5)
                    except Exception as e:
                        self._debug_print(f"Rmconf cb error: {e}")

            try:
                requests.post(ans_url, json={"callback_query_id": cb_id}, timeout=5)
            except Exception:
                pass