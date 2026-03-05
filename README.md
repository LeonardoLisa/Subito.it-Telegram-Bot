---
Filename: README.md
Version: 2.1.0
Date: 2026-03-05
Author: Leonardo Lisa
Description: Documentation for the Subito.it Scraper Daemon and Telegram Bot.
Requirements: Python 3.8+, requests, beautifulsoup4, python-dotenv
License: GNU GPLv3
---

# Subito.it Telegram Bot & Scraper Daemon

An advanced scraping daemon for Subito.it with native Telegram integration. Designed for continuous (24/7) execution on servers or Raspberry Pi, it implements resilient architectures against Web Application Firewalls (WAF) and autonomously manages memory and subscription states.

## ⚙️ Core Features

* **Asynchronous Polling:** Separated scraping and Telegram API threads for zero-latency command processing.
* **Long Polling Telegram:** Reduces network traffic by keeping HTTP connections open for 60 seconds to Telegram servers.
* **WAF Resilience:** Uses Chromium headers, Connection Pooling (HTTP Sessions), and Exponential Backoff to mitigate IP bans (HTTP 403/429).
* **Hybrid Error Handling:** Verifies network connectivity via TCP DNS sockets (1.1.1.1:53) before querying the target, halting execution during outages without generating exception loops.
* **Atomic Writes:** JSON file I/O is managed via `os.replace` on temporary files, ensuring immunity to data corruption during crashes or power losses.
* **Rich Media:** Native broadcasting of listing images via `sendPhoto` and use of `InlineKeyboardMarkup` for embedded UI buttons.
* **Garbage Collection:** Automatic memory and local database cleanup based on Time-To-Live (TTL).

## 🛠 Installation

1. Clone the repository and navigate to the directory.
2. Install the required dependencies:
   ```bash
   pip install requests beautifulsoup4 python-dotenv
   ```
3. Create a `.env` file in the project root and insert the token provided by BotFather:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCDEF_ghijklmnopqrstuvwxyz
   ```

## 📁 Search Configuration (`searches.json`)

Create a `searches.json` file in the project root. The structure must strictly follow the JSON format with two levels of nesting: `{"Search_Name": {"Keyword": "Full_URL"}}`.

```json
{
  "Electronics": {
    "Macbook Air M1": "[https://www.subito.it/annunci-italia/vendita/usato/?q=macbook+air+m1](https://www.subito.it/annunci-italia/vendita/usato/?q=macbook+air+m1)",
    "iPhone 13 Pro": "[https://www.subito.it/annunci-italia/vendita/usato/?q=iphone+13+pro](https://www.subito.it/annunci-italia/vendita/usato/?q=iphone+13+pro)"
  },
  "Vehicles": {
    "Honda SH 150": "[https://www.subito.it/annunci-italia/vendita/moto-e-scooter/?q=honda+sh+150](https://www.subito.it/annunci-italia/vendita/moto-e-scooter/?q=honda+sh+150)"
  }
}
```
*Note: Modifications to this file are read at runtime during the next scraping cycle. Restarting the daemon is not required.*

## 🚀 CLI Usage

The script's behavior is governed by command-line arguments.

* `--daemon` (or `-d`): Starts the program in a continuous loop.
* `--refresh` (or `-r`): Executes a single scraping iteration and terminates. Useful for testing or external cronjobs.
* `--delay [SECONDS]`: Sets the wait time between two complete scraping cycles. Default is `120`.
* `--activeHour [HOUR]` (or `-ah`): Defines the start hour of the activity (24h format, e.g., `8`).
* `--pauseHour [HOUR]` (or `-ph`): Defines the end hour of the activity (24h format, e.g., `23`).

**Startup Examples:**
```bash
# Standard startup with a check every 2 minutes
python subito_telegram_bot.py --daemon

# Startup with a check every 60 seconds, active only between 08:00 and 23:00
python subito_telegram_bot.py -d --delay 60 -ah 8 -ph 23
```

**Graceful Teardown:** Send `SIGINT` (Ctrl+C) or `SIGTERM`. The daemon will complete the current cycle, safely save the state, and broadcast an offline message to all active subscribers.

## 📱 Telegram Commands

User interaction occurs via direct chat with the bot.

* `/sub` or `/start`: Subscribes the user and starts receiving notifications.
* `/unsub`: Unsubscribes the user and removes the Chat ID from the database.
* `/search`: Prints the hierarchical list of active searches currently in the JSON file.
* `/status`: Displays the number of active users and the system uptime (hours, minutes, seconds).
* `/help`: Displays the command guide.

## ⏱ Internal Parameters (System Mechanics)

The program implements automatic maintenance logic with static parameters defined in the source code. You can find and modify these variables to tune the bot's behavior:

* **`retention_period` (47 Hours):** Found inside the `manage_message_deletions()` function (`47 * 60 * 60`). The Telegram API prevents the deletion of messages older than 48 hours. The bot tracks sent `message_id`s and automatically deletes them after exactly 47 hours to keep the chat history clean and avoid `400 Bad Request` exceptions.
* **`MAX_SUBSCRIBERS` (15):** To prevent Telegram API rate-limiting during broadcasts, the system accepts a maximum of 15 simultaneous users.
* **`WARNING_SECONDS` (48 Hours):** Defines the elapsed time (`2 * 24 * 3600`) after which the system sends a 1-day expiration warning to the subscriber.
* **`TIMEOUT_SECONDS` (72 Hours):** Subscriptions have a Time-To-Live of 3 days (`3 * 24 * 3600`). Once this threshold is reached, the user is automatically unsubscribed if they do not renew by sending `/sub` again. This prevents spamming inactive accounts.
* **`TTL_30_DAYS` (30 Days):** Processed listing URLs are saved in `tracked_items.json` to avoid duplicate alerts. To prevent memory leaks, the Garbage Collector (`garbage_collect_tracking()`) uses this threshold (`30 * 24 * 3600`) to automatically purge records older than 30 days from the local database.
* **`MAX_BACKOFF` (32x):** Found inside `run_scraper()`. If the IP is blocked by Subito.it (HTTP 403/429), the standard `--delay` is progressively multiplied (x2, x4, x8, x16) up to a maximum of 32 times. This Exponential Backoff allows the IP to restore its reputation on the target's firewalls.
* **`is_new_query` (Zero-notification initialization):** A boolean flag logic inside `run_scraper()`. When a new URL is added to `searches.json`, the very first scan of that link silently populates the database with existing listings, bypassing the broadcast block (`if notify and not is_new_query:`) to avoid flooding users with old alerts.

## ⚖️ License

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 3.