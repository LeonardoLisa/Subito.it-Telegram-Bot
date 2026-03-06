"""
Filename: subito_telegram_bot.py
Version: 2.4.0
Date: 2026-03-06
Author: Leonardo Lisa
Description: Production-ready Subito.it scraper daemon. 
             Features: HTML entity escaping, connection pooling, Long Polling,
             Atomic JSON writes, Garbage collection, Hybrid Error Handling,
             Graceful Teardown, Inline Keyboards, Native Photo broadcasting.
Requirements: pip install requests beautifulsoup4 python-dotenv

Usage:
1. Configure Telegram token in .env: TELEGRAM_BOT_TOKEN=your_token
2. Define searches in searches.json: {"Category": {"keyword": "URL"}}
3. Run: python subito_telegram_bot.py

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

import argparse
import requests
from bs4 import BeautifulSoup
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import threading
import signal
import sys
import html
import tempfile
import socket

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

SEARCHES_FILE = "searches.json"
DB_FILE = "tracked_items.json"
DELETION_QUEUE_FILE = "messages_to_delete.json"
SUBSCRIBERS_FILE = "subscribers.json"
KNOWN_URLS_FILE = "known_urls.json"

MAX_SUBSCRIBERS = 15
TIMEOUT_SECONDS = 3 * 24 * 3600
WARNING_SECONDS = 2 * 24 * 3600
TTL_30_DAYS = 30 * 24 * 3600

START_TIME = time.time()
tracked_items = {}
messages_to_delete = []
subscribers = {}
known_urls = []
last_update_id = 0
cached_searches = {}
state_lock = threading.Lock()
shutdown_event = threading.Event()
sigint_count = 0
DEBUG_MODE = False

http_session = requests.Session()
http_session.headers.update({
    "Accept": '"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"',
    "Accept-Encoding": '"gzip, deflate"',
    "Accept-Language": '"en-US,en;q=0.5"',
    "Connection": '"keep-alive"',
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Brave";v="128"',
    "Sec-Ch-Ua-Mobile": '"?0"',
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": '"document"',
    "Sec-Fetch-Mode": '"navigate"',
    "Sec-Fetch-Site": '"none"',
    "Sec-Fetch-User": '"?1"',
    "Upgrade-Insecure-Requests": '"1"',
    "User-Agent": '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"'
})

def log(msg, color=Colors.ENDC):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"{color}[{timestamp}] {msg}{Colors.ENDC}")

def debug_log(msg, color=Colors.WARNING):
    if DEBUG_MODE:
        log(msg, color)

def atomic_save(data, filepath):
    dir_name = os.path.dirname(os.path.abspath(filepath)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix="tmp_", suffix=".json")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)
    except Exception as e:
        log(f"Atomic save failed for {filepath}: {e}", Colors.FAIL)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def load_local_data():
    global tracked_items, messages_to_delete, subscribers, known_urls, last_update_id

    with state_lock:
        if os.path.isfile(DB_FILE):
            try: 
                with open(DB_FILE, 'r') as f: tracked_items = json.load(f)
            except: tracked_items = {}
                    
        if os.path.isfile(DELETION_QUEUE_FILE):
            try: 
                with open(DELETION_QUEUE_FILE, 'r') as f: messages_to_delete = json.load(f)
            except: messages_to_delete = []

        if os.path.isfile(SUBSCRIBERS_FILE):
            try: 
                with open(SUBSCRIBERS_FILE, 'r') as f:
                    data = json.load(f)
                    subscribers = data.get("subscribers", {})
                    last_update_id = data.get("last_update_id", 0)
            except: pass

        if os.path.isfile(KNOWN_URLS_FILE):
            try: 
                with open(KNOWN_URLS_FILE, 'r') as f: known_urls = json.load(f)
            except: known_urls = []

def save_local_data():
    with state_lock:
        tr_copy = tracked_items.copy()
        md_copy = messages_to_delete.copy()
        su_copy = {"subscribers": subscribers.copy(), "last_update_id": last_update_id}
        ku_copy = known_urls.copy()
        
    atomic_save(tr_copy, DB_FILE)
    atomic_save(md_copy, DELETION_QUEUE_FILE)
    atomic_save(su_copy, SUBSCRIBERS_FILE)
    atomic_save(ku_copy, KNOWN_URLS_FILE)

def get_searches():
    global cached_searches
    if not os.path.isfile(SEARCHES_FILE):
        log(f"Error: {SEARCHES_FILE} not found.", Colors.FAIL)
        return cached_searches
    try:
        with open(SEARCHES_FILE, 'r') as file:
            cached_searches = json.load(file)
            return cached_searches
    except json.JSONDecodeError as e:
        log(f"Malformed JSON in {SEARCHES_FILE}: {e}. Falling back to cached config.", Colors.WARNING)
        return cached_searches

def check_internet_connection():
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2)
        return True
    except OSError:
        return False

def send_direct_message(chat_id, text):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try: requests.post(url, data=payload, timeout=10)
    except Exception: pass

def send_telegram_broadcast(text, image_url=None, item_url=None, is_system_msg=False):
    if not TELEGRAM_TOKEN: return []
    with state_lock: current_subscribers = list(subscribers.keys())
    if not current_subscribers: return []
    
    sent_messages = []
    state_changed = False
    
    reply_markup_json = None
    if not is_system_msg:
        reply_markup = {"inline_keyboard": []}
        if item_url:
            reply_markup["inline_keyboard"].append([{"text": "🛒 Vai all'annuncio", "url": item_url}])
        reply_markup["inline_keyboard"].append([{"text": "🗑️ Elimina", "callback_data": "delete_msg"}])
        reply_markup_json = json.dumps(reply_markup)

    image_bytes = None
    if image_url:
        debug_log(f"DEBUG - Attempting to download image from: {image_url}", Colors.OKCYAN)
        try:
            img_res = requests.get(image_url, timeout=10)
            if img_res.status_code == 200:
                image_bytes = img_res.content
                debug_log(f"DEBUG - Image downloaded successfully ({len(image_bytes)} bytes).", Colors.OKGREEN)
            else:
                debug_log(f"DEBUG - CDN Download Failed: HTTP {img_res.status_code}", Colors.WARNING)
        except Exception as e: 
            debug_log(f"DEBUG - Requests Exception on image URL: {e}", Colors.FAIL)
    else:
        if not is_system_msg:
            debug_log("DEBUG - No image_url provided by the scraper for this item.", Colors.WARNING)

    for chat_id in current_subscribers:
        try:
            data = {}
            if image_bytes:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
                payload = {
                    "chat_id": chat_id,
                    "caption": text if len(text) <= 1024 else text[:1020] + "...",
                    "parse_mode": "HTML"
                }
                if reply_markup_json:
                    payload["reply_markup"] = reply_markup_json
                
                files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
                res = requests.post(url, data=payload, files=files, timeout=15)
                data = res.json()
                
                if not data.get("ok"):
                    debug_log(f"DEBUG - Telegram sendPhoto API Error: {data}", Colors.FAIL)
                
            if not image_bytes or not data.get("ok"):
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }
                if reply_markup_json:
                    payload["reply_markup"] = reply_markup_json
                res = requests.post(url, data=payload, timeout=10)
                data = res.json()

            if data.get("ok"):
                sent_messages.append({"chat_id": chat_id, "message_id": data["result"]["message_id"]})
            elif data.get("error_code") == 403:
                log(f"Cleanup: Removing blocked user {chat_id}", Colors.WARNING)
                with state_lock:
                    if chat_id in subscribers:
                        del subscribers[chat_id]
                        state_changed = True
        except Exception as e:
            debug_log(f"DEBUG - Error communicating with Telegram API: {e}", Colors.FAIL)
            
    if state_changed: save_local_data()
    return sent_messages

def clear_offline_updates():
    global last_update_id
    if not TELEGRAM_TOKEN: return
    log("Purging offline message queue...", Colors.OKCYAN)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    payload = {"offset": last_update_id + 1, "timeout": 5}
    try:
        res = requests.post(url, json=payload, timeout=10)
        data = res.json()
        if data.get("ok") and data["result"]:
            with state_lock:
                for update in data["result"]:
                    last_update_id = update["update_id"]
            save_local_data()
    except Exception: pass

def process_telegram_updates():
    global subscribers, last_update_id
    if not TELEGRAM_TOKEN or shutdown_event.is_set(): return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    payload = {"offset": last_update_id + 1, "timeout": 60}
    
    try:
        res = requests.post(url, json=payload, timeout=65)
        data = res.json()
        
        if data.get("ok") and data["result"]:
            state_changed = False
            for update in data["result"]:
                with state_lock:
                    last_update_id = update["update_id"]
                state_changed = True
                
                if "message" in update and "text" in update["message"]:
                    chat_id = str(update["message"]["chat"]["id"])
                    text = update["message"]["text"].strip()
                    
                    if text in ["/start", "/sub"]:
                        with state_lock:
                            if chat_id not in subscribers:
                                if len(subscribers) >= MAX_SUBSCRIBERS:
                                    send_direct_message(chat_id, f"⚠️ System full. Maximum {MAX_SUBSCRIBERS} active subscribers allowed.")
                                else:
                                    subscribers[chat_id] = {"joined": time.time(), "notified": False}
                                    send_direct_message(chat_id, "✅ Subscription active. It will expire in 3 days. Send /sub again to renew.")
                                    log(f"New subscriber added: {chat_id}", Colors.OKGREEN)
                            else:
                                subscribers[chat_id] = {"joined": time.time(), "notified": False}
                                send_direct_message(chat_id, "✅ Subscription renewed for another 3 days.")
                                
                    elif text == "/unsub":
                        with state_lock:
                            if chat_id in subscribers:
                                del subscribers[chat_id]
                                send_direct_message(chat_id, "❌ Unsubscribed. You will no longer receive alerts.")
                                log(f"Subscriber removed: {chat_id}", Colors.WARNING)
                            else:
                                send_direct_message(chat_id, "⚠️ You are not subscribed.")

                    elif text in ["/search", "/searches"]:
                        searches = get_searches()
                        if not searches:
                            send_direct_message(chat_id, "📂 <b>Active Searches:</b>\n\nNo active searches.")
                        else:
                            msg_text = "📂 <b>Active Searches:</b>\n\n"
                            for s_name, k_dict in searches.items():
                                msg_text += f"🔹 <b>{html.escape(s_name)}</b>\n"
                                for k in k_dict.keys(): msg_text += f"  - <i>{html.escape(k)}</i>\n"
                            send_direct_message(chat_id, msg_text)

                    elif text == "/help":
                        help_text = (
                            "🤖 <b>Available Commands</b>\n\n"
                            "🔹 /sub - Subscribe to listing alerts\n"
                            "🔹 /unsub - Unsubscribe from alerts\n"
                            "🔹 /search - View active search targets\n"
                            "🔹 /status - View bot statistics and uptime\n"
                            "🔹 /help - Show this help message"
                        )
                        send_direct_message(chat_id, help_text)

                    elif text == "/status":
                        with state_lock:
                            active_count = len(subscribers)
                            uptime_seconds = int(time.time() - START_TIME)
                            hours, remainder = divmod(uptime_seconds, 3600)
                            minutes, seconds = divmod(remainder, 60)
                            uptime_str = f"{hours}h {minutes}m {seconds}s"
                        send_direct_message(chat_id, f"📊 <b>System Status</b>\nUsers: {active_count}/{MAX_SUBSCRIBERS}\nUptime: {uptime_str}")
                
                elif "callback_query" in update:
                    cb_query = update["callback_query"]
                    cb_id = cb_query["id"]
                    cb_data = cb_query.get("data")
                    msg_obj = cb_query.get("message")
                    
                    if cb_data == "delete_msg" and msg_obj:
                        chat_id = msg_obj["chat"]["id"]
                        message_id = msg_obj["message_id"]
                        
                        del_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
                        try: requests.post(del_url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
                        except Exception: pass
                        
                        ans_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
                        try: requests.post(ans_url, json={"callback_query_id": cb_id}, timeout=5)
                        except Exception: pass
                            
            if state_changed: save_local_data()
    except requests.exceptions.Timeout:
        pass 
    except Exception as e:
        log(f"Telegram getUpdates error: {e}", Colors.FAIL)
        if not shutdown_event.is_set():
            shutdown_event.wait(2)

def manage_subscriptions():
    global subscribers
    if not TELEGRAM_TOKEN or shutdown_event.is_set(): return
    current_time = time.time()
    to_delete = []
    state_changed = False

    with state_lock:
        for chat_id, data in subscribers.items():
            joined = data.get("joined", 0)
            notified = data.get("notified", False)
            elapsed = current_time - joined

            if elapsed > TIMEOUT_SECONDS:
                to_delete.append(chat_id)
                send_direct_message(chat_id, "❌ Subscription expired. Send /sub to subscribe again.")
            elif elapsed > WARNING_SECONDS and not notified:
                send_direct_message(chat_id, "⚠️ Warning: Your subscription will expire in 1 day. Send /sub to renew.")
                data["notified"] = True
                state_changed = True

        for chat_id in to_delete:
            del subscribers[chat_id]
            state_changed = True

    if state_changed: save_local_data()

def telegram_polling_thread():
    log("Starting Telegram Long-Polling thread...", Colors.OKCYAN)
    while not shutdown_event.is_set():
        process_telegram_updates()
        manage_subscriptions()

def manage_message_deletions():
    global messages_to_delete
    if not TELEGRAM_TOKEN: return
    current_time = time.time()
    retention_period = 36 * 60 * 60 
    kept_messages = []
    state_changed = False
    
    with state_lock:
        for msg in messages_to_delete:
            if current_time - msg["timestamp"] >= retention_period:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
                payload = {"chat_id": msg["chat_id"], "message_id": msg["message_id"]}
                try: requests.post(url, data=payload, timeout=5)
                except Exception: pass 
                state_changed = True
            else:
                kept_messages.append(msg)
        messages_to_delete = kept_messages
        
    if state_changed: save_local_data()

def garbage_collect_tracking(searches):
    global tracked_items
    state_changed = False
    links_to_remove = []
    current_time = time.time()

    with state_lock:
        for link, data in tracked_items.items():
            s_name = data.get("search_name")
            k_word = data.get("keyword")
            timestamp = data.get("timestamp", current_time)
            
            if current_time - timestamp > TTL_30_DAYS:
                links_to_remove.append(link)
                continue
                
            if s_name is None or k_word is None:
                continue
                
            if s_name not in searches or k_word not in searches.get(s_name, {}):
                links_to_remove.append(link)

        for link in links_to_remove:
            del tracked_items[link]
            state_changed = True

    if state_changed:
        save_local_data()
        log(f"Garbage collector removed {len(links_to_remove)} obsolete tracked items.", Colors.WARNING)

def run_scraper(notify=True, delay=120):
    searches = get_searches()
    if not searches: return

    garbage_collect_tracking(searches)
    state_changed = False
    backoff_multiplier = 1
    MAX_BACKOFF = 32

    for search_name, keyword_dict in searches.items():
        if shutdown_event.is_set(): break
        
        for keyword, url in keyword_dict.items():
            if shutdown_event.is_set(): break
            
            if not check_internet_connection():
                log("Network offline. Halting scraping until connection is restored...", Colors.WARNING)
                while not check_internet_connection() and not shutdown_event.is_set():
                    shutdown_event.wait(5)
                log("Network connection restored.", Colors.OKGREEN)

            if shutdown_event.is_set(): break
            log(f"Querying: {search_name} -> {keyword}", Colors.OKBLUE)
            debug_log(f"DEBUG - Target URL: {url}", Colors.OKCYAN)
            
            is_new_query = False
            with state_lock:
                if url not in known_urls:
                    known_urls.append(url)
                    is_new_query = True
                    state_changed = True
            
            try:
                debug_log("DEBUG - Sending HTTP GET request...", Colors.OKCYAN)
                page = http_session.get(url, timeout=10)
                debug_log(f"DEBUG - HTTP Response Code: {page.status_code}", Colors.OKCYAN)
                
                if page.status_code in [403, 429]:
                    sleep_time = delay * backoff_multiplier
                    log(f"WAF Block detected (HTTP {page.status_code}). Exponential Backoff activated for {sleep_time}s...", Colors.FAIL)
                    shutdown_event.wait(sleep_time)
                    backoff_multiplier = min(backoff_multiplier * 2, MAX_BACKOFF)
                    continue 
                else:
                    backoff_multiplier = 1
                
                soup = BeautifulSoup(page.text, 'html.parser')
                script_tag = soup.find('script', id='__NEXT_DATA__')
                
                if not script_tag: 
                    log(f"JSON node not found. Possible soft WAF block or invalid URL: {url}", Colors.FAIL)
                    continue
                    
                debug_log("DEBUG - __NEXT_DATA__ JSON node found. Parsing...", Colors.OKCYAN)
                json_data = json.loads(script_tag.string)
                items_list = json_data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('items', {}).get('list', [])
                
                debug_log(f"DEBUG - Found {len(items_list)} items in the JSON list.", Colors.OKCYAN)
                
                for item_wrapper in items_list:
                    if shutdown_event.is_set(): break
                    
                    product = item_wrapper.get('item')
                    if not product: continue
                        
                    link = product.get('urls', {}).get('default', '')
                    with state_lock: is_tracked = link in tracked_items
                        
                    if not link or is_tracked: continue
                    if product.get('sold', False): 
                        debug_log(f"DEBUG - Item marked as sold, skipping: {link}", Colors.WARNING)
                        continue

                    title = product.get('subject', 'No Title')
                    debug_log(f"DEBUG - Processing new item: {title}", Colors.OKCYAN)
                    
                    location_geo = product.get('geo', {})
                    location = f"{location_geo.get('town', {}).get('value', 'Unknown')} ({location_geo.get('city', {}).get('shortName', '?')})"
                    
                    price = "Unknown price"
                    features = product.get('features', {})
                    price_feature = features.get('/price')
                    if price_feature and 'values' in price_feature:
                        price = price_feature['values'][0].get('key', price)

                    description = product.get('body', 'No description provided.')
                    
                    # Updated image extraction logic handling the query string cdnBaseUrl endpoint
                    images_list = product.get('images', [])
                    image_url = ''
                    
                    if images_list:
                        img_obj = images_list[0]
                        cdn_base = img_obj.get('cdnBaseUrl')
                        
                        if cdn_base:
                            # Append the required API rule using query string to request the 1x resolution JPEG
                            image_url = f"{cdn_base}?rule=gallery-desktop-1x-auto"
                        else:
                            # Fallback for legacy schemas
                            image_url = img_obj.get('secureuri') or img_obj.get('uri') or img_obj.get('url') or ''
                            if not image_url and 'scale' in img_obj and len(img_obj['scale']) > 0:
                                target_scale = img_obj['scale'][-1]
                                image_url = target_scale.get('secureuri') or target_scale.get('uri') or target_scale.get('url') or ''
                    
                    # Resolve Protocol-Relative URLs
                    if image_url and image_url.startswith('//'):
                        image_url = 'https:' + image_url
                    
                    # Extensive debug payload tracking image extraction success
                    if image_url:
                        debug_log(f"DEBUG - Successfully extracted image URL: {image_url}", Colors.OKGREEN)
                    elif images_list:
                        debug_log(f"DEBUG - Image extraction failed. Schema dump: {json.dumps(img_obj)}", Colors.WARNING)
                    else:
                        debug_log(f"DEBUG - Ad has no pictures. Full product dump for analysis: {json.dumps(product)}", Colors.WARNING)
                    
                    short_desc = description[:200] + '...' if len(description) > 200 else description
                    
                    safe_search_name = html.escape(search_name)
                    safe_keyword = html.escape(keyword)
                    safe_title = html.escape(title)
                    safe_location = html.escape(location)
                    safe_desc = html.escape(short_desc)
                    
                    msg_text = (
                        f"📂 <b>{safe_search_name}</b> | 🔍 <i>{safe_keyword}</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<b>{safe_title}</b>\n"
                        f"💶 Price: € {price}\n"
                        f"📍 Location: {safe_location}\n\n"
                        f"📝 <i>{safe_desc}</i>"
                    )

                    if notify and not is_new_query:
                        sent_msgs = send_telegram_broadcast(msg_text, image_url=image_url, item_url=link)
                        with state_lock:
                            for m in sent_msgs:
                                messages_to_delete.append({
                                    "chat_id": m["chat_id"],
                                    "message_id": m["message_id"],
                                    "timestamp": time.time()
                                })
                            
                    with state_lock:
                        tracked_items[link] = {
                            "title": title, "price": price, "timestamp": time.time(),
                            "search_name": search_name, "keyword": keyword
                        }
                        state_changed = True
                        
                    log(f"-> Found: {title} (€ {price})", Colors.OKGREEN)
                    
            except requests.exceptions.RequestException as e:
                log(f"Network error processing {keyword}: {e}", Colors.FAIL)
            except Exception as e:
                log(f"Error processing {keyword}: {e}", Colors.FAIL)
                
            if not shutdown_event.is_set():
                shutdown_event.wait(1)
            
    if state_changed: save_local_data()

def signal_handler(signum, frame):
    global sigint_count
    sigint_count += 1
    if sigint_count >= 2:
        log("\nForced exit requested. Terminating immediately.", Colors.FAIL)
        os._exit(1)
    
    log("\nTermination requested (Ctrl+C). Waiting for safe teardown (press again to force)...", Colors.WARNING)
    shutdown_event.set()

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # CLI Setup with Help definitions
    parser = argparse.ArgumentParser(
        description="Production-ready Subito.it scraper daemon with Telegram native photo broadcasting.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
  python subito_telegram_bot.py                 # Run with default 120s refresh rate
  python subito_telegram_bot.py -r 60           # Run with 60s refresh rate
  python subito_telegram_bot.py -d              # Run with debug logging enabled
        """
    )
    parser.add_argument('--refreshrate', '-r', type=int, default=120, help="Refresh rate in seconds (default: 120)")
    parser.add_argument('--debug', '-d', action='store_true', help="Enable verbose debug logging for API payloads and image fetching")
    args = parser.parse_args()

    DEBUG_MODE = args.debug

    load_local_data()
    clear_offline_updates()
    send_telegram_broadcast("🟢 <b>System Online</b>\nThe bot is now monitoring targets.", is_system_msg=True)

    tg_thread = threading.Thread(target=telegram_polling_thread, daemon=True)
    tg_thread.start()

    log("Starting Subito daemon...", Colors.BOLD)
    first_run = True
    
    while not shutdown_event.is_set():
        run_scraper(notify=not first_run, delay=args.refreshrate)
        manage_message_deletions()
        first_run = False
        
        if not shutdown_event.is_set():
            log(f"Waiting {args.refreshrate}s before next scan...", Colors.HEADER)
            
            # Interruptible sleep cycle: blocks for 1 second intervals. 
            # Breaks immediately upon SIGINT (Ctrl+C).
            slept_time = 0
            while slept_time < args.refreshrate and not shutdown_event.is_set():
                time.sleep(1)
                slept_time += 1

    try:
        send_telegram_broadcast("🔴 <b>System Offline</b>\nThe bot has been gracefully shut down.", is_system_msg=True)
        save_local_data()
    except Exception as e:
        log(f"Error during teardown operations: {e}", Colors.FAIL)
    finally:
        log("Daemon stopped successfully.", Colors.BOLD)
        sys.exit(0)