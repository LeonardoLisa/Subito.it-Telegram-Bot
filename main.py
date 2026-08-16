"""
Filename: main.py
Version: 4.0.0
Date: 2026-04-30
Author: Leonardo Lisa
Description: Main orchestration daemon adapted for SQLite relational database.
             Implements per-user search polling, exclusion keywords filtering,
             superuser bypasses, and 34-day deep clean retention policies.
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
from superuser_cli import run_superuser_menu

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

def format_message(category, name, ad):
    desc = ad['description'][:200] + '...' if len(ad['description']) > 200 else ad['description']
    return (
        f"📂 <b>{html.escape(category)}</b> | 🔍 <i>{html.escape(name)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(ad['title'])}</b>\n"
        f"💶 Price: {html.escape(ad['price'])}\n"
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
    parser.add_argument('--superuser', '-su', action='store_true', help="Open the interactive superuser management menu before starting")
    args = parser.parse_args()

    DEBUG_MODE = args.debug

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log("ERROR: TELEGRAM_BOT_TOKEN missing in .env", Colors.FAIL)
        sys.exit(1)

    # --- Configurable limits (see README "Configurazione avanzata via .env") ---
    db_path = os.getenv("DB_PATH", "subito_bot.db")
    deep_clean_retention_days = int(os.getenv("DEEP_CLEAN_RETENTION_DAYS", "30"))
    tracked_ads_max_items = int(os.getenv("TRACKED_ADS_MAX_ITEMS", "150"))

    db = Database(db_path=db_path)
    db.debug_mode = DEBUG_MODE

    if args.superuser:
        run_superuser_menu(db, token)

    scraper = SubitoScraper()
    scraper.debug_mode = DEBUG_MODE

    log("Verifying WAF bypass before starting...", Colors.OKCYAN)
    waf_ok, waf_reason = scraper.verify_waf_bypass()
    if not waf_ok:
        log(f"FATAL: WAF bypass check failed ({waf_reason}). Aborting startup.", Colors.FAIL)
        sys.exit(1)
    log("WAF bypass verified successfully.", Colors.OKGREEN)

    tg = TelegramUI(token, db, shutdown_event)
    tg.debug_mode = DEBUG_MODE

    log("Starting Telegram thread...", Colors.OKCYAN)
    threading.Thread(target=tg.poll_updates, daemon=True).start()

    tg.broadcast("🟢 <b>System Online</b>", show_delete=False)
    
    skip_next = args.skip
    notified_expired_users = set()

    while not shutdown_event.is_set():
        if not is_network_online():
            log("Network offline. Retrying in 10s...", Colors.FAIL)
            time.sleep(10)
            continue

        # Trigger deep clean for long-term inactive users (separate from, and much
        # longer than, the soft subscription expiry below).
        retention_seconds = deep_clean_retention_days * 86400
        db.prune_inactive_users(retention_seconds)

        searches = db.get_all_searches()

        for s in searches:
            if shutdown_event.is_set(): break
            
            search_id = s['id']
            chat_id = s['chat_id']
            category = s['category']
            name = s['name']
            url = s['url']
            exclusion_kws = s['exclusion_kws']
            include_kws = s.get('include_kws') or []
            last_active = s['last_active']
            is_superuser = s['is_superuser']

            # Paused searches (see /add search menu "Pause") are skipped entirely,
            # without consuming a scraper request, but keep their tracked-ads history.
            if not s.get('enabled', True):
                debug_log(f"Skipping paused search: [User {chat_id}] {category} -> {name}")
                continue

            # Enforce soft active-subscription limit (bypassed for superusers). Expired
            # users' searches are left untouched here — they are only ever hard-deleted
            # by prune_inactive_users() above, after the much longer deep-clean retention.
            if not is_superuser and (time.time() - last_active > tg.TIMEOUT_SECONDS):
                if chat_id not in notified_expired_users:
                    days_valid = tg.TIMEOUT_SECONDS // 86400
                    tg.send_direct_message(
                        chat_id, 
                        f"⚠️ <b>Subscription Expired</b>\nYour {days_valid}-day access has ended. Send /sub to renew."
                    )
                    notified_expired_users.add(chat_id)
                continue
            else:
                notified_expired_users.discard(chat_id)

            log(f"Scanning: [User {chat_id}] {category} -> {name}", Colors.OKBLUE)
            ads = scraper.fetch_ads(url)

            # Determine if this is a newly created search to prevent initial spam
            is_new_search = not db.has_tracked_ads(search_id)

            for ad in ads:
                title_lower = ad['title'].lower()
                
                # Apply exclusion filters
                if exclusion_kws:
                    if any(kw.lower().strip() in title_lower for kw in exclusion_kws if kw.strip()):
                        debug_log(f"Ad excluded by keyword: {ad['title']}")
                        continue

                # Apply inclusion filter: if any include keywords are set, the ad's
                # title must contain at least one of them to be notified.
                if include_kws:
                    if not any(kw.lower().strip() in title_lower for kw in include_kws if kw.strip()):
                        debug_log(f"Ad skipped (no include keyword match): {ad['title']}")
                        continue

                # Prefer the stable URN-derived ad id over the display link, which can
                # change (redirects, tracking params) for an ad that was already seen.
                ad_id = ad.get('id') or ad['link']
                if not db.is_ad_tracked(search_id, ad_id):
                    if not skip_next and not is_new_search:
                        img = scraper.download_image(ad['image_url'])
                        msg = format_message(category, name, ad)
                        
                        # Note: tg.send_ad will be implemented in the updated telegram_ui.py
                        # to target individual users rather than globally broadcasting.
                        tg.send_ad(chat_id, msg, image_bytes=img, item_url=ad['link'])

                    db.add_tracked_ad(search_id, ad_id)
            
            # Mimic human latency between requests
            jitter = random.uniform(0.5, 1.5)
            debug_log(f"Applying jitter: {jitter:.2f}s")
            time.sleep(jitter)

        db.trim_tracked_items(max_items=tracked_ads_max_items)
        skip_next = False

        if not shutdown_event.is_set():
            log(f"Sleeping {args.refreshrate}s...", Colors.HEADER)
            slept = 0
            while slept < args.refreshrate and not shutdown_event.is_set():
                time.sleep(1)
                slept += 1

    try:
        tg.broadcast("🔴 <b>System Offline</b>", show_delete=False)
    except Exception as e:
        log(f"Teardown error: {e}", Colors.FAIL)
    finally:
        log("Teardown complete.", Colors.BOLD)
        sys.exit(0)

if __name__ == "__main__":
    main()