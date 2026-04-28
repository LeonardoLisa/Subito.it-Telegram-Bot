"""
Filename: main.py
Version: 3.1.0
Date: 2026-04-28
Author: Leonardo Lisa
Description: Main orchestration daemon. Links Database, Scraper, and Telegram UI.
             Restores original CLI flags (--refreshrate, --debug, --skip), terminal UI formatting,
             and graceful teardown / interruptible sleep logic.
Requirements: uv run --with requests --with beautifulsoup4 --with pillow --with python-dotenv --with curl-cffi main.py

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

import argparse
import os
import sys
import time
import signal
import threading
import html
from datetime import datetime
from dotenv import load_dotenv

from database import Database
from scraper_subito import SubitoScraper
from telegram_ui import TelegramUI

class Colors:
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

shutdown_event = threading.Event()

def log(msg, color=Colors.ENDC):
    print(f"{color}[{datetime.now().strftime('%H:%M:%S')}] {msg}{Colors.ENDC}")

def signal_handler(signum, frame):
    log("\nShutdown requested...", Colors.WARNING)
    shutdown_event.set()

def check_subscriptions(db, timeout_seconds):
    """Removes expired users from database."""
    now = time.time()
    expired_users = []
    with db.lock:
        for chat_id, info in list(db.subscribers.items()):
            if now - info.get("joined", 0) > timeout_seconds:
                expired_users.append(chat_id)
        for chat_id in expired_users:
            del db.subscribers[chat_id]
    return expired_users

def main():
    signal.signal(signal.SIGINT, signal_handler)
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: sys.exit(1)

    db, scraper = Database(), SubitoScraper()
    tg = TelegramUI(token, db, shutdown_event)
    
    threading.Thread(target=tg.poll_updates, daemon=True).start()
    tg.broadcast("🟢 <b>System Online</b>")

    parser = argparse.ArgumentParser()
    parser.add_argument('--refreshrate', '-r', type=int, default=120)
    parser.add_argument('--skip', '-s', action='store_true')
    args = parser.parse_args()
    
    skip_next = args.skip

    while not shutdown_event.is_set():
        # Check for expired subscriptions
        for chat_id in check_subscriptions(db, tg.TIMEOUT_SECONDS):
            log(f"Expired: {chat_id}", Colors.WARNING)
            tg.send_direct_message(chat_id, "⚠️ <b>Subscription Expired</b>\nSend /sub to renew access.")

        with db.lock: searches = {c: kw.copy() for c, kw in db.searches.items()}
            
        for category, keywords in searches.items():
            if shutdown_event.is_set(): break
            for keyword, url in keywords.items():
                log(f"Scanning: {category} -> {keyword}", Colors.OKCYAN)
                ads = scraper.fetch_ads(url)
                for ad in ads:
                    with db.lock: is_tracked = ad['link'] in db.tracked_items
                    if not is_tracked:
                        if not skip_next:
                            tg.broadcast(f"📂 <b>{category}</b> | {keyword}\n\n<b>{ad['title']}</b>\n€ {ad['price']}", scraper.download_image(ad['image_url']), ad['link'])
                        db.add_tracked_item(ad['link'], ad['title'], ad['price'], category, keyword)
                time.sleep(1)
                
        db.trim_tracked_items(max_items=30)
        db.save_all()
        skip_next = False

        if not shutdown_event.is_set():
            time.sleep(args.refreshrate)

    tg.broadcast("🔴 <b>System Offline</b>")
    db.save_all()
    sys.exit(0)

if __name__ == "__main__":
    main()