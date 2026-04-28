"""
Filename: main.py
Version: 3.0.1
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

def debug_log(msg, color=Colors.WARNING):
    if DEBUG_MODE:
        log(msg, color)

shutdown_event = threading.Event()
sigint_count = 0

def signal_handler(signum, frame):
    global sigint_count
    sigint_count += 1
    if sigint_count >= 2:
        log("\nForced exit requested. Terminating immediately.", Colors.FAIL)
        os._exit(1)
    
    log("\nTermination requested (Ctrl+C). Waiting for safe teardown (press again to force)...", Colors.WARNING)
    shutdown_event.set()

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

    parser = argparse.ArgumentParser(
        description="Production-ready Subito.it scraper daemon with Telegram native photo broadcasting.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
  python main.py                 # Run with default 120s refresh rate
  python main.py -r 60           # Run with 60s refresh rate
  python main.py -d              # Run with debug logging enabled
  python main.py --skip          # Skip sending notifications on startup
        """
    )
    parser.add_argument('--refreshrate', '-r', type=int, default=120, help="Refresh rate in seconds (default: 120)")
    parser.add_argument('--debug', '-d', action='store_true', help="Enable verbose debug logging for API payloads and image fetching")
    parser.add_argument('--skip', '-s', action='store_true', help="Salta l'invio delle notifiche per gli annunci preesistenti all'avvio")
    args = parser.parse_args()

    DEBUG_MODE = args.debug

    load_dotenv()
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_TOKEN:
        log("ERROR: TELEGRAM_BOT_TOKEN missing in .env", Colors.FAIL)
        sys.exit(1)

    db = Database()
    tg = TelegramUI(TELEGRAM_TOKEN, db, shutdown_event)
    scraper = SubitoScraper()

    # System online notification
    tg.broadcast("🟢 <b>System Online</b>\nThe bot is now monitoring targets.")

    log("Starting Telegram Long-Polling thread...", Colors.OKCYAN)
    tg_thread = threading.Thread(target=tg.poll_updates, daemon=True)
    tg_thread.start()

    log("Starting Subito daemon...", Colors.BOLD)
    
    # Track the initial state of the --skip flag
    skip_next_notification = args.skip

    while not shutdown_event.is_set():
        with db.lock:
            searches_copy = {c: kw.copy() for c, kw in db.searches.items()}
            
        for category, keywords in searches_copy.items():
            if shutdown_event.is_set(): break
            
            for keyword, url in keywords.items():
                if shutdown_event.is_set(): break
                
                with db.lock:
                    is_new_url = url not in db.known_urls
                    if is_new_url: db.known_urls.append(url)

                log(f"Querying: {category} -> {keyword}", Colors.OKBLUE)
                debug_log(f"DEBUG - Target URL: {url}", Colors.OKCYAN)
                
                ads = scraper.fetch_ads(url)
                
                for ad in ads:
                    with db.lock: is_tracked = ad['link'] in db.tracked_items
                    if not is_tracked:
                        if not skip_next_notification and not is_new_url:
                            img_bytes = scraper.download_image(ad['image_url'])
                            msg_text = format_message(category, keyword, ad)
                            tg.broadcast(msg_text, img_bytes, ad['link'])
                            
                        db.add_tracked_item(ad['link'], ad['title'], ad['price'], category, keyword)
                        log(f"-> Found: {ad['title']} (€ {ad['price']})", Colors.OKGREEN)
                        
                time.sleep(1) # Polite delay between queries
                
        # Retain only the last 30 items per active search
        db.trim_tracked_items(max_items=30)
        db.save_all()

        # Reset immediately to resume normal execution
        skip_next_notification = False

        if not shutdown_event.is_set():
            log(f"Waiting {args.refreshrate}s before next scan...", Colors.HEADER)
            
            # Interruptible sleep cycle
            slept = 0
            while slept < args.refreshrate and not shutdown_event.is_set():
                time.sleep(1)
                slept += 1

    try:
        tg.broadcast("🔴 <b>System Offline</b>\nThe bot has been gracefully shut down.")
        db.save_all()
    except Exception as e:
        log(f"Error during teardown operations: {e}", Colors.FAIL)
    finally:
        log("Daemon stopped successfully.", Colors.BOLD)
        sys.exit(0)

if __name__ == "__main__":
    main()