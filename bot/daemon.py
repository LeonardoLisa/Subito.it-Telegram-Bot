"""
Filename: bot/daemon.py
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

from .database import Database
from .scrapers.subito import SubitoScraper
from .telegram_ui import TelegramUI
from .superuser_cli import run_superuser_menu
from .i18n import t, DEFAULT_LANGUAGE

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

def format_message(matches, ad, lang=DEFAULT_LANGUAGE):
    """
    `matches` is a list of (category, name) tuples for every search (belonging to
    the same user) that matched this ad within the current scan pass (report item 1:
    cross-search dedup). The first match drives the header line; any additional
    matches are listed on a second line instead of triggering a duplicate notification
    - see bot/daemon.py's `seen_this_pass` bookkeeping in main().

    The ad ID (report item 4) is appended to the first line. Telegram HTML messages
    have no real right-alignment/flex layout, so this is a best-effort visual push to
    the right using a couple of em-spaces before a monospaced <code> tag - it will not
    align pixel-perfectly across every client/device.
    """
    raw_desc = str(ad.get('description') or '')
    desc = raw_desc[:200] + '...' if len(raw_desc) > 200 else raw_desc
    category, name = matches[0]

    first_line = f"📂 <b>{html.escape(str(category))}</b> | 🔍 <i>{html.escape(str(name))}</i>"
    ad_id = ad.get('id')
    if ad_id:
        first_line += f"\u2003\u2003<code>#{html.escape(str(ad_id))}</code>"

    header_lines = [first_line]
    if len(matches) > 1:
        extra_names = ", ".join(html.escape(str(other_name)) for _, other_name in matches[1:])
        header_lines.append(t("also_found_by", lang, names=extra_names))

    title_safe = html.escape(str(ad.get('title') or ''))
    price_safe = html.escape(str(ad.get('price') or ''))
    location_safe = html.escape(str(ad.get('location') or ''))
    desc_safe = html.escape(desc)

    base_msg = (
        "\n".join(header_lines) + "\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{title_safe}</b>\n"
        f"{t('ad_price_label', lang, price=price_safe)}\n"
        f"{t('ad_location_label', lang, location=location_safe)}\n\n"
        f"📝 "
    )

    # Guarantee total message length stays within Telegram's 1024-char photo caption
    # limit without ever slicing inside HTML tags or HTML entities.
    max_total_len = 1000
    overhead = len(base_msg) + len("<i></i>")
    if overhead + len(desc_safe) > max_total_len:
        available = max(0, max_total_len - overhead - 3)
        raw_truncated = desc[:available]
        desc_safe = html.escape(raw_truncated) + "..."

    return f"{base_msg}<i>{desc_safe}</i>"

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

    tg.broadcast_system("system_online", show_delete=False)
    
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

        # Auto-delete ad notification messages older than the hardcoded retention
        # window (report item 5). See TelegramUI.AD_NOTIFICATION_RETENTION_DAYS.
        tg.purge_expired_ad_notifications()

        searches = db.get_all_searches()

        # Cross-search dedup (report item 1): keyed by (chat_id, ad_id) and reset every
        # scan pass. If a second search belonging to the SAME user matches an ad that
        # was already notified earlier in this same pass, the existing message is
        # edited in place (see format_message's "Also found by" line) instead of
        # sending a duplicate notification for the same ad.
        seen_this_pass = {}

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
            lang = s.get('language') or DEFAULT_LANGUAGE

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
                        t("subscription_expired_notice", lang, days=days_valid)
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
                        pass_key = (chat_id, ad_id)
                        existing = seen_this_pass.get(pass_key)
                        if existing:
                            # Same ad already notified for this user earlier in this pass
                            # (a different search matched it too): edit that message to
                            # list the additional search instead of re-notifying.
                            existing["matches"].append((category, name))
                            new_text = format_message(existing["matches"], existing["ad"], lang)
                            tg.edit_ad_message(
                                chat_id,
                                existing["message_id"],
                                new_text,
                                existing["is_photo"],
                                item_url=existing["ad"].get("link"),
                                lang=lang
                            )
                        else:
                            img = scraper.download_image(ad['image_url'])
                            matches = [(category, name)]
                            msg = format_message(matches, ad, lang)
                            result = tg.send_ad(chat_id, msg, image_bytes=img, item_url=ad['link'], lang=lang)
                            if result:
                                seen_this_pass[pass_key] = {
                                    "message_id": result["message_id"],
                                    "is_photo": result["is_photo"],
                                    "matches": matches,
                                    "ad": ad,
                                }

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
        tg.broadcast_system("system_offline", show_delete=False)
    except Exception as e:
        log(f"Teardown error: {e}", Colors.FAIL)
    finally:
        log("Teardown complete.", Colors.BOLD)
        sys.exit(0)

if __name__ == "__main__":
    main()