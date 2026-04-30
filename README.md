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

* **Interactive Telegram UI:** Fully manage your searches, categories, and keywords directly from the chat using inline keyboards and guided setup flows.
* **Asynchronous Polling:** Separated scraping and Telegram API threads for zero-latency command processing.
* **WAF Resilience & Exponential Backoff:** Uses a dedicated Chromium-spoofed `requests.Session` exclusively for querying Subito.it. In case of temporary IP bans (HTTP 403), the system automatically performs up to 3 retries, doubling the wait time at each failure, before proceeding.
* **Long Polling Telegram:** Reduces network traffic by keeping HTTP connections open for 60 seconds to Telegram servers.
* **Instant Graceful Teardown:** The main loop utilizes a 1-second interruptible sleep cycle. Upon receiving a `SIGINT` (Ctrl+C) or `SIGTERM`, the daemon immediately halts the sleep cycle, safely saves the state, and broadcasts an offline message without waiting for the refresh delay to naturally expire.
* **Atomic Writes:** JSON file I/O is managed via `os.replace` on temporary files, ensuring immunity to data corruption during crashes or power losses.
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

## 📁 Search Configuration (`searches.json`)

**The bot automatically generates and manages the `searches.json` file through the interactive Telegram commands (`/add` and `/rm`). You do not need to create or edit this file manually.**

However, if you prefer manual configuration, the structure must strictly follow the JSON format with two levels of nesting: `{"Search_Name": {"Keyword": "Full_URL"}}`.

```json
{
  "Electronics": {
    "Macbook Air M1": "[https://www.subito.it/annunci-italia/vendita/usato/?q=macbook+air+m1](https://www.subito.it/annunci-italia/vendita/usato/?q=macbook+air+m1)"
  }
}
```

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
* `/unsub`: Unsubscribes the user and removes the Chat ID from the database.
* `/search`: Prints the hierarchical list of active searches currently in the database.
* `/add <link>`: Starts an interactive flow to add a new search. You will be guided to select/create a category and set a keyword.+
  **Note:** The link must be a valid Subito.it URL strictly pointing to the `/annunci-italia/vendita/` path.
* `/rm <keyword>`: Removes an existing search via an interactive confirmation menu. Empty categories are automatically cleaned up.
* `/status`: Displays the number of active users and the system uptime formatted in years, months, days, hours, and minutes.
* `🛑 /cancel`: Instantly aborts the current action (e.g., waiting for keyword input) and resets the user's state.
* `/help`: Displays the command guide.

## ⏱ Internal Parameters (System Mechanics)

The program implements automatic maintenance logic with static parameters defined in the source code.

* **`MAX_SUBSCRIBERS` (15):** To prevent Telegram API rate-limiting during broadcasts, the system accepts a maximum of 15 simultaneous users.
* **`TIMEOUT_SECONDS` (96 Hours):** Subscriptions have a Time-To-Live of 4 days (`4 * 24 * 3600`). Once this threshold is reached, the user is automatically unsubscribed if they do not renew by sending `/sub` again. This prevents spamming inactive accounts.
* **Memory Pruning (30 Minutes):** To prevent memory leaks, abandoned inline-keyboard callbacks and incomplete user interaction states are automatically purged from RAM every 30 minutes.
* **Database Trimming:** To prevent storage bloat, the Garbage Collector (`trim_tracked_items()`) automatically limits the saved history to the last 30 items per active search category/keyword. Orphaned links from deleted categories are automatically pruned during the regular database trim cycle. Empty categories are immediately deleted from the JSON state upon keyword removal.

## License
This project is licensed under the **GNU General Public License v3 (GPLv3)**. 
See the `LICENSE` file for full details.
