<div align="center">
  <h1>Subito.it Telegram Bot</h1>
  <img src="bot.jpg" alt="Subito.it Telegram Bot" width="200">
  <br><br>
      <b>If you find this project useful, please consider giving it a Star! It helps the repository grow and keeps you updated.</b> ⭐
</div>

<br>

---

## Project Overview
<a name="english-description"></a>
 **🇬🇧**

A simple and automatic tool that monitors Subito.it for the items you want to buy. Whenever a new ad matches your search, the bot instantly sends you a Telegram message with the price, location, details, and high-quality photos. It runs continuously in the background, making sure you never miss a good deal.

📖 **Are you a beginner?** Read the [step-by-step english guide](https://github.com/LeonardoLisa/Subito.it-Telegram-Bot/wiki#english-version) on the Wiki for easy installation instructions.

---

<a name="descrizione-italiana"></a>
 **🇮🇹**

Monitora automaticamente le tue ricerche su Subito.it e ricevi aggiornamenti via messaggio. Quando viene pubblicato un nuovo annuncio che corrisponde alla tua ricerca, il bot ti invia immediatamente un messaggio su Telegram con prezzo, luogo, dettagli e foto native in alta qualità. Funziona continuamente in background, aiutandoti a non farti scappare l'affare.

📖 Segui la [guida passo-passo in italiano](https://github.com/LeonardoLisa/Subito.it-Telegram-Bot/wiki#versione-italiana) sulla Wiki.

---

## ⚙️ Core Features

* **Asynchronous Polling:** Separated scraping and Telegram API threads for zero-latency command processing.
* **Long Polling Telegram:** Reduces network traffic by keeping HTTP connections open for 60 seconds to Telegram servers.
* **WAF Resilience & Session Isolation:** Uses a dedicated Chromium-spoofed `requests.Session` exclusively for querying Subito.it to mitigate IP bans (HTTP 403/429), while using pure `requests` for the Telegram API and CDN image fetching to prevent header-based rejections (HTTP 400).
* **Instant Graceful Teardown:** The main loop utilizes a 1-second interruptible sleep cycle. Upon receiving a `SIGINT` (Ctrl+C) or `SIGTERM`, the daemon immediately halts the sleep cycle, safely saves the state, and broadcasts an offline message without waiting for the refresh delay to naturally expire.
* **Atomic Writes:** JSON file I/O is managed via `os.replace` on temporary files, ensuring immunity to data corruption during crashes or power losses.
* **Native Photo Broadcasting:** Dynamically resolves Subito's `cdnBaseUrl` via query string API rules (`?rule=gallery-desktop-1x-auto`) to download the highest quality JPEG directly into RAM and broadcast it using Telegram's native `sendPhoto` `multipart/form-data` endpoint.
* **Hybrid Error Handling:** Verifies network connectivity via TCP DNS sockets (1.1.1.1:53) before querying the target, halting execution during outages without generating exception loops.

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
    "Macbook Air M1": "https://www.subito.it/annunci-italia/vendita/usato/?q=macbook+air+m1",
    "iPhone 13 Pro": "https://www.subito.it/annunci-italia/vendita/usato/?q=iphone+13+pro"
  },
  "Vehicles": {
    "Honda SH 150": "https://www.subito.it/annunci-italia/vendita/moto-e-scooter/?q=honda+sh+150"
  }
}
```
*Note: Modifications to this file are read at runtime during the next scraping cycle. Restarting the daemon is not required.*

## 🚀 CLI Usage

The program runs continuously in daemon mode by default. Its behavior is governed by the following command-line arguments:

* `-r`, `--refreshrate [SECONDS]`: Sets the wait time between two complete scraping cycles. Default is `120`.
* `-d`, `--debug`: Enables verbose logging for HTTP request tracking, CDN schema dumps, and Telegram API payloads.

**Startup Examples:**
```bash
# Standard startup with a check every 2 minutes
python subito_telegram_bot.py

# Startup with a check every 60 seconds
python subito_telegram_bot.py -r 60

# Startup with a 30-second delay and debug output enabled
python subito_telegram_bot.py --refreshrate 30 --debug
```

## 📱 Telegram Commands

User interaction occurs via direct chat with the bot.

* `/sub` or `/start`: Subscribes the user and starts receiving notifications.
* `/unsub`: Unsubscribes the user and removes the Chat ID from the database.
* `/search`: Prints the hierarchical list of active searches currently in the JSON file.
* `/status`: Displays the number of active users and the system uptime (hours, minutes, seconds).
* `/help`: Displays the command guide.

## ⏱ Internal Parameters (System Mechanics)

The program implements automatic maintenance logic with static parameters defined in the source code.

* **`retention_period` (36 Hours):** Found inside the `manage_message_deletions()` function (`36 * 60 * 60`). The bot tracks sent `message_id`s and automatically deletes them after 36 hours to keep the chat history clean.
* **`MAX_SUBSCRIBERS` (15):** To prevent Telegram API rate-limiting during broadcasts, the system accepts a maximum of 15 simultaneous users.
* **`WARNING_SECONDS` (48 Hours):** Defines the elapsed time (`2 * 24 * 3600`) after which the system sends a 1-day expiration warning to the subscriber.
* **`TIMEOUT_SECONDS` (72 Hours):** Subscriptions have a Time-To-Live of 3 days (`3 * 24 * 3600`). Once this threshold is reached, the user is automatically unsubscribed if they do not renew by sending `/sub` again. This prevents spamming inactive accounts.
* **`TTL_30_DAYS` (30 Days):** Processed listing URLs are saved in `tracked_items.json` to avoid duplicate alerts. To prevent memory leaks, the Garbage Collector (`garbage_collect_tracking()`) uses this threshold (`30 * 24 * 3600`) to automatically purge records older than 30 days from the local database.
* **`MAX_BACKOFF` (32x):** Found inside `run_scraper()`. If the IP is blocked by Subito.it (HTTP 403/429), the standard `--refreshrate` is progressively multiplied (x2, x4, x8, x16) up to a maximum of 32 times. This Exponential Backoff allows the IP to restore its reputation on the target's firewalls.
* **`is_new_query` (Zero-notification initialization):** A boolean flag logic inside `run_scraper()`. When a new URL is added to `searches.json`, the very first scan of that link silently populates the database with existing listings, bypassing the broadcast block to avoid flooding users with old alerts.

## License
This project is licensed under the **GNU Affero General Public License v3 (AGPLv3)**. 
See the `LICENSE` file for full details.
