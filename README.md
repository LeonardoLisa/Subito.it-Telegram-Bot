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

* **Interactive Telegram UI:** Fully manage your searches, categories, and **exclusion keywords** directly from the chat using inline keyboards and guided setup flows.
* **Per-User Architecture:** Searches and notifications are strictly personal. The bot manages independent tracking queues for each user, allowing personalized configurations and targeted messaging instead of global broadcasting.
* **Relational Database (SQLite):** State is persistently and efficiently managed via a robust, thread-safe SQLite database (`subito_bot.db`) with cascading deletes, ensuring data integrity and zero RAM overhead.
* **Asynchronous Polling:** Separated scraping and Telegram API threads for zero-latency command processing.
* **WAF Resilience & Backoff:** Uses a dedicated Chromium-spoofed `requests.Session` exclusively for querying Subito.it. In case of temporary IP bans (HTTP 403), the session is reset with randomized delays. For rate limiting (HTTP 429), the system performs up to 3 retries, doubling the wait time at each failure.
* **Long Polling Telegram:** Reduces network traffic by keeping HTTP connections open for 60 seconds to Telegram servers.
* **Instant Graceful Teardown:** The main loop utilizes a 1-second interruptible sleep cycle. Upon receiving a `SIGINT` (Ctrl+C) or `SIGTERM`, the daemon immediately halts the sleep cycle, safely saves the state, and broadcasts an offline message without waiting for the refresh delay to naturally expire.
* **Native Photo Broadcasting:** Dynamically resolves Subito's `cdnBaseUrl` via query string API rules (`?rule=gallery-desktop-1x-auto`) to download the highest quality JPEG directly into RAM and broadcast it using Telegram's native `sendPhoto` `multipart/form-data` endpoint.
* **Hybrid Error Handling:** Verifies network connectivity via TCP DNS sockets (1.1.1.1:53) before querying the target, halting execution during outages without generating exception loops.

## 🛠 Installation

1. Clone the repository and navigate to the directory.
2. Install the required dependencies (using `uv` is recommended):
   ```bash
   uv pip install requests beautifulsoup4 python-dotenv curl_cffi pillow
   ```
3. Create a `.env` file in the project root and insert the token provided by BotFather:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCDEF_ghijklmnopqrstuvwxyz
   ```

## 📁 Database Configuration (SQLite)

**The bot automatically generates and manages the `subito_bot.db` SQLite database through interactive Telegram commands (`/add` and `/rm`). You do not need to create or edit configuration files manually.**

All user subscriptions, active searches, exclusion keywords, and tracked ads histories are securely stored in relational tables with cascading deletion policies.

## 🚀 CLI Usage

The program runs continuously in daemon mode by default. Its behavior is governed by the following command-line arguments:

* `-r`, `--refreshrate [SECONDS]`: Sets the wait time between two complete scraping cycles. Default is `120`.
* `-d`, `--debug`: Enables verbose logging for HTTP request tracking, CDN schema dumps, and Telegram API payloads.
* `-s`, `--skip`: Skips sending notifications for pre-existing ads during the very first startup scan.

**Startup Examples:**
```bash
# Standard startup with a check every 2 minutes
python3 main.py

# Startup with a check every 60 seconds
python3 main.py -r 60

# Startup with a 30-second delay, debug output enabled, skipping old ads
python3 main.py --refreshrate 30 --debug --skip
```

## 📱 Telegram Commands

User interaction occurs via direct chat with the bot.

* `/sub` or `/start`: Subscribes the user and starts receiving notifications.
* `/unsub`: Unsubscribes the user and removes the Chat ID and all related data from the database.
* `/search`: Prints the hierarchical list of active searches currently in the database.
* `/add <link>`: Starts an interactive flow to add a new search. You will be guided to select/create a category, set a search name, and optionally define up to 3 **exclusion keywords** to filter out unwanted ads.
  **Note:** The link must be a valid Subito.it URL strictly pointing to the `/annunci-italia/vendita/` path.
* `/rm <Search Name>`: Removes an existing search via an interactive confirmation menu. Associated ads history is automatically cleaned up.
* `/status`: Displays the number of active users, your active searches limit, and the system uptime formatted in years, months, days, hours, and minutes.
* `🛑 /cancel`: Instantly aborts the current action (e.g., waiting for keyword input) and resets the user's state.
* `/help`: Displays the command guide.

## ⏱ Internal Parameters (System Mechanics)

The program implements automatic maintenance logic with static parameters defined in the source code.

* **`MAX_SUBSCRIBERS` (default is 3):** To prevent resource exhaustion and scraping overload, the system accepts a maximum of 5 registered users. New subscriptions are blocked once this limit is reached. See telegram_ui.py, line 31
* **`MAX_REGULAR_SEARCHES` (default is 15):** To prevent Telegram API rate-limiting and server overload, a single user can have a maximum of 15 simultaneous active searches. See telegram_ui.py, line 30
* **`TIMEOUT_SECONDS` (default is 96h):** Subscriptions have an active Time-To-Live of 4 days (`4 * 24 * 3600`). Once this threshold is reached, the bot suspends scanning for that user until they renew by sending `/sub` again. This prevents spamming inactive accounts. See telegram_ui.py, line 28
* **`CACHE_PRUNE_INTERVAL` (default is 30m):** To prevent memory leaks, abandoned inline-keyboard callbacks and incomplete user interaction states are automatically purged from RAM every 30 minutes. See telegram_ui.py, line 29
* **Deep Clean Retention (34 Days):** Users who remain inactive (without renewing their subscription) for 34 days are permanently purged from the database, along with all their searches and tracking history.
* **Database Trimming:** To prevent storage bloat, the Garbage Collector (`trim_tracked_items()`) utilizes SQL subqueries to automatically limit the saved history to the newest 150 items per search ID. 

## License
This project is licensed under the **GNU General Public License v3 (GPLv3)**. 
See the `LICENSE` file for full details.