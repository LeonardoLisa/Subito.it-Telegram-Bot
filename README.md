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

* **Interactive Telegram UI:** Fully manage your searches, categories, and keywords directly from the chat using inline keyboards and guided setup flows[cite: 7].
* **Asynchronous Polling:** Separated scraping and Telegram API threads for zero-latency command processing[cite: 7].
* **WAF Resilience & Backoff:** Uses a dedicated Chromium-spoofed `requests.Session` exclusively for querying Subito.it[cite: 6, 7]. In case of temporary IP bans (HTTP 403), the session is reset with randomized delays[cite: 6]. For rate limiting (HTTP 429), the system performs up to 3 retries, doubling the wait time at each failure[cite: 6].
* **Long Polling Telegram:** Reduces network traffic by keeping HTTP connections open for 60 seconds to Telegram servers[cite: 7].
* **Instant Graceful Teardown:** The main loop utilizes a 1-second interruptible sleep cycle[cite: 7]. Upon receiving a `SIGINT` (Ctrl+C) or `SIGTERM`, the daemon immediately halts the sleep cycle, safely saves the state, and broadcasts an offline message without waiting for the refresh delay to naturally expire[cite: 7].
* **Atomic Writes:** JSON file I/O is managed via `os.replace` on temporary files, ensuring immunity to data corruption during crashes or power losses[cite: 7].
* **Native Photo Broadcasting:** Dynamically resolves Subito's `cdnBaseUrl` via query string API rules (`?rule=gallery-desktop-1x-auto`) to download the highest quality JPEG directly into RAM and broadcast it using Telegram's native `sendPhoto` `multipart/form-data` endpoint[cite: 7].
* **Hybrid Error Handling:** Verifies network connectivity via TCP DNS sockets (1.1.1.1:53) before querying the target, halting execution during outages without generating exception loops[cite: 7].

## 🛠 Installation

1. Clone the repository and navigate to the directory[cite: 7].
2. Install the required dependencies (using `uv` is recommended)[cite: 7]:
   ```bash
   uv pip install requests beautifulsoup4 python-dotenv curl_cffi pillow
   ```
3. Create a `.env` file in the project root and insert the token provided by BotFather[cite: 7]:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCDEF_ghijklmnopqrstuvwxyz
   ```

## 📁 Search Configuration (`searches.json`)

**The bot automatically generates and manages the `searches.json` file through the interactive Telegram commands (`/add` and `/rm`)[cite: 7]. You do not need to create or edit this file manually[cite: 7].**

However, if you prefer manual configuration, the structure must strictly follow the JSON format with two levels of nesting: `{"Search_Name": {"Keyword": "Full_URL"}}`[cite: 7].
```json
{
  "Electronics": {
    "Macbook Air M1": "https://www.subito.it/annunci-italia/vendita/usato/?q=macbook+air+m1"
  }
}
```

## 🚀 CLI Usage

The program runs continuously in daemon mode by default[cite: 7]. Its behavior is governed by the following command-line arguments:

* `-r`, `--refreshrate [SECONDS]`: Sets the wait time between two complete scraping cycles[cite: 7]. Default is `120`[cite: 7].
* `-d`, `--debug`: Enables verbose logging for HTTP request tracking, CDN schema dumps, and Telegram API payloads[cite: 7].
* `-s`, `--skip`: Skips sending notifications for pre-existing ads during the very first startup scan[cite: 7].

**Startup Examples:**
```bash
# Standard startup with a check every 2 minutes
uv run main.py

# Startup with a check every 60 seconds
uv run main.py -r 60

# Startup with a 30-second delay, debug output enabled, skipping old ads
uv run main.py --refreshrate 30 --debug --skip
```

## 📱 Telegram Commands

User interaction occurs via direct chat with the bot[cite: 7].

* `/sub` or `/start`: Subscribes the user and starts receiving notifications[cite: 7].
* `/unsub`: Unsubscribes the user and removes the Chat ID from the database[cite: 7].
* `/search`: Prints the hierarchical list of active searches currently in the database[cite: 7].
* `/add <link>`: Starts an interactive flow to add a new search[cite: 7]. You will be guided to select/create a category and set a keyword[cite: 7]. **Note:** The link must be a valid Subito.it URL strictly pointing to the `/annunci-italia/vendita/` path[cite: 7].
* `/rm <keyword>`: Removes an existing search via an interactive confirmation menu[cite: 7]. Empty categories are automatically cleaned up[cite: 7].
* `/status`: Displays the number of active users and the system uptime formatted in years, months, days, hours, and minutes[cite: 7].
* `🛑 /cancel`: Instantly aborts the current action (e.g., waiting for keyword input) and resets the user's state[cite: 7].
* `/help`: Displays the command guide[cite: 7].

## ⏱ Internal Parameters (System Mechanics)

The program implements automatic maintenance logic with static parameters defined in the source code[cite: 7].

* **`MAX_SUBSCRIBERS` (15):** To prevent Telegram API rate-limiting during broadcasts, the system accepts a maximum of 15 simultaneous users[cite: 7].
  * *Reference:* `telegram_ui.py`, line 36 (parameter `max_subs`)[cite: 4].
* **`TIMEOUT_SECONDS` (96 Hours):** Subscriptions have a Time-To-Live of 4 days (`4 * 24 * 3600`)[cite: 7]. Once this threshold is reached, the user is automatically unsubscribed if they do not renew by sending `/sub` again[cite: 7]. This prevents spamming inactive accounts[cite: 7].
  * *Reference:* `telegram_ui.py`, line 33[cite: 4].
* **Memory Pruning (30 Minutes):** To prevent memory leaks, abandoned inline-keyboard callbacks and incomplete user interaction states are automatically purged from RAM every 30 minutes[cite: 7].
  * *Reference:* `telegram_ui.py`, line 34 (`CACHE_PRUNE_INTERVAL`)[cite: 4].
* **Database Trimming:** To prevent storage bloat, the Garbage Collector (`trim_tracked_items()`) automatically limits the saved history to the last 30 items per active search category/keyword[cite: 7].
  * *Reference:* `database.py`, line 91 (`max_items=30`)[cite: 5].

## License
This project is licensed under the **GNU General Public License v3 (GPLv3)**[cite: 7]. 
See the `LICENSE` file for full details[cite: 7].
