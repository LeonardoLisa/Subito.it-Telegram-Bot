"""
Filename: main.py
Version: 3.5.0
Date: 2026-04-29
Author: Leonardo Lisa
Description: Main orchestration daemon. Links Database, Scraper, and Telegram UI.
             Implements TCP DNS connectivity checks, debug logging, and
             dynamic subscription expiration monitoring.
Requirements: requests beautifulsoup4 pillow python-dotenv curl-cffi

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

import argparse
import os
import sys
import time
import signal
import threading
import html
import socket
import random
from datetime import datetime
from dotenv import load_dotenv

from database import Database
from scraper_subito import SubitoScraper
from telegram_ui import TelegramUI

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

DEBUG_MODE = False

def log(msg, color=Colors.ENDC):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"{color}[{timestamp}] {msg}{Colors.ENDC}")

def debug_log(msg, color=Colors.OKCYAN):
    if DEBUG_MODE:
        log(f"DEBUG: {msg}", color)

def is_network_online(host="1.1.1.1", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
        return True
    except socket.error:
        return False

shutdown_event = threading.Event()
sigint_count = 0

def signal_handler(signum, frame):
    global sigint_count
    sigint_count += 1
    if sigint_count >= 2:
        log("\nForced exit requested. Terminating immediately.", Colors.FAIL)
        os._exit(1)

    log("\nTermination requested. Waiting for safe teardown...", Colors.WARNING)
    shutdown_event.set()

def check_subscriptions(db, timeout_seconds):
    now = time.time()
    expired_users = []
    with db.lock:
        for chat_id, info in list(db.subscribers.items()):
            joined_time = info.get("joined", 0)
            if now - joined_time > timeout_seconds:
                expired_users.append(chat_id)
        for chat_id in expired_users:
            del db.subscribers[chat_id]
    return expired_users

def format_message(search_name, keyword, ad):
    desc = ad['description'][:200] + '...' if len(ad['description']) > 200 else ad['description']
    return (
        f"📂 <b>{html.escape(search_name)}</b> | 🔍 <i>{html.escape(keyword)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(ad['title'])}</b>\n"
        f"💶 Price: € {html.escape(ad['price'])}\n"
        f"📍 Location: {html.escape(ad['location'])}\n\n"
        f"📝 <i>{html.escape(desc)}</i>"
    )

def main():
    global DEBUG_MODE

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="Subito.it Scraper Daemon")
    parser.add_argument('--refreshrate', '-r', type=int, default=120, help="Scan interval")
    parser.add_argument('--debug', '-d', action='store_true', help="Enable verbose logging")
    parser.add_argument('--skip', '-s', action='store_true', help="Skip initial notifications")
    args = parser.parse_args()

    DEBUG_MODE = args.debug

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log("ERROR: TELEGRAM_BOT_TOKEN missing in .env", Colors.FAIL)
        sys.exit(1)

    db = Database()
    db.debug_mode = DEBUG_MODE
    
    scraper = SubitoScraper()
    scraper.debug_mode = DEBUG_MODE
    
    tg = TelegramUI(token, db, shutdown_event)
    tg.debug_mode = DEBUG_MODE

    log("Starting Telegram thread...", Colors.OKCYAN)
    threading.Thread(target=tg.poll_updates, daemon=True).start()

    tg.broadcast("🟢 <b>System Online</b>", show_delete=False)
    skip_next = args.skip

    while not shutdown_event.is_set():
        if not is_network_online():
            log("Network offline. Retrying in 10s...", Colors.FAIL)
            time.sleep(10)
            continue

        days_valid = tg.TIMEOUT_SECONDS // 86400
        expired = check_subscriptions(db, tg.TIMEOUT_SECONDS)
        for chat_id in expired:
            debug_log(f"Expiring session for {chat_id}")
            tg.send_direct_message(chat_id, f"⚠️ <b>Subscription Expired</b>\nYour {days_valid}-day access has ended. Send /sub to renew.")

        with db.lock:
            searches_copy = {c: kw.copy() for c, kw in db.searches.items()}

        for category, keywords in searches_copy.items():
            if shutdown_event.is_set(): break
            for keyword, url in keywords.items():
                if shutdown_event.is_set(): break

                with db.lock:
                    is_new_url = url not in db.known_urls
                    if is_new_url: db.known_urls.append(url)

                log(f"Scanning: {category} -> {keyword}", Colors.OKBLUE)
                ads = scraper.fetch_ads(url)

                for ad in ads:
                    with db.lock: is_tracked = ad['link'] in db.tracked_items
                    if not is_tracked:
                        if not skip_next and not is_new_url:
                            img = scraper.download_image(ad['image_url'])
                            msg = format_message(category, keyword, ad)
                            tg.broadcast(msg, img, ad['link'])

                        db.add_tracked_item(ad['link'], ad['title'], ad['price'], category, keyword)
                
                # Implement random jitter between 2 and 6 seconds to mimic human behavior
                jitter = random.uniform(0.5, 1.5)
                debug_log(f"Applying jitter: {jitter:.2f}s")
                time.sleep(jitter)

        db.trim_tracked_items(max_items=30)
        db.save_all()
        skip_next = False

        if not shutdown_event.is_set():
            log(f"Sleeping {args.refreshrate}s...", Colors.HEADER)
            slept = 0
            while slept < args.refreshrate and not shutdown_event.is_set():
                time.sleep(1)
                slept += 1

    try:
        tg.broadcast("🔴 <b>System Offline</b>", show_delete=False)
        db.save_all()
    except Exception as e:
        log(f"Teardown error: {e}", Colors.FAIL)
    finally:
        log("Teardown complete.", Colors.BOLD)
        sys.exit(0)

if __name__ == "__main__":
    main()
