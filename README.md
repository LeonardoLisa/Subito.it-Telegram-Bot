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

A highly optimized, automatic tool that monitors Subito.it for the items you want to buy. Whenever a new ad matches your search, the bot instantly sends you a Telegram message with the price, location, details, and high-quality native photos. Powered by a reverse-engineered Mobile API engine, it runs continuously in the background, ensuring you never miss a good deal while bypassing modern anti-bot protections.

📖 **Are you a beginner?** Read the [step-by-step english guide](https://github.com/LeonardoLisa/Subito.it-Telegram-Bot/wiki#english-version) on the Wiki for easy installation instructions.

---

<a name="descrizione-italiana"></a>
 **🇮🇹**

Monitora automaticamente le tue ricerche su Subito.it e ricevi aggiornamenti via messaggio. Alimentato da un motore API Mobile analizzato tramite reverse engineering, il bot aggira le moderne protezioni anti-bot. Quando viene pubblicato un nuovo annuncio che corrisponde alla tua ricerca, ti invia immediatamente un messaggio Telegram con prezzo, luogo, dettagli e foto native in alta qualità. Funziona in background, aiutandoti a non farti scappare l'affare.

📖 Segui la [guida passo-passo in italiano](https://github.com/LeonardoLisa/Subito.it-Telegram-Bot/wiki#versione-italiana) sulla Wiki.

---

## ⚙️ Core Features

* **Mobile API Engine & DataDome WAF Evasion:** Replaces legacy HTML scraping with direct queries to the `hades.subito.it` internal mobile API. It utilizes `curl_cffi` for advanced TLS impersonation, completely neutralizing JavaScript challenges and canvas fingerprinting.
* **Deep Hardware Spoofing:** Emulates an Android 13 environment masking as a Google Pixel 7 ("panther") by hardcoding specific `user-agent` and `x-subito-fingerprint` headers to bypass ThreatMetrix behavioral anomaly detection.
* **Dynamic State Machine Simulation:** Mathematically replicates the native application's lifecycle. It generates and persists a static UUIDv4 (`.subito_env.json`) to simulate the device installation footprint, while rotating a volatile Session UUID upon cold starts or HTTP 403 blocks to evade anti-rotation rules.
* **Adaptive Rate Limiting & Backoff:** Dual-layer jittered pacing. In case of WAF blocks (HTTP 403), the session is suspended and re-initialized with new temporal identifiers. For standard rate limits (HTTP 429), it applies randomized exponential backoff (up to 3 retries) without crashing the daemon.
* **Interactive Telegram UI:** Fully manage your searches, categories, and **exclusion/include keywords** directly from the chat using inline keyboards and guided setup flows — no text commands to memorize for adding or removing searches.
* **Multi-language (English/Italian):** Every bot message is localized per-user via the `/language` command; English is the default, with Italian selectable via an inline flag button.
* **Cross-Search Deduplication:** If the same ad matches more than one of your searches within a single scan pass, you get a single notification message (edited in place to list every matching search) instead of duplicates.
* **Per-User Architecture:** Searches and notifications are strictly personal. The bot manages independent tracking queues for each user, allowing personalized configurations and targeted messaging instead of global broadcasting.
* **Relational Database (SQLite):** State is persistently and efficiently managed via a robust, thread-safe SQLite database (`subito_bot.db`) with cascading deletes, ensuring data integrity and zero RAM overhead.
* **Native Photo Broadcasting:** Dynamically resolves internal CDN URNs (via sharding logic to `s.sbito.it`) and forces the highest quality JPEG extraction directly into RAM, broadcasting it using Telegram's native `multipart/form-data` endpoint.
* **Abuse Protection & Permanent Bans:** Conservative, hardcoded heuristics (flood guard, oversized-input guard) automatically ban chat_ids exhibiting suspicious behavior; superusers can also ban/unban manually from the terminal menu.
* **Instant Graceful Teardown:** The main loop utilizes a 1-second interruptible sleep cycle. Upon receiving a `SIGINT` (Ctrl+C) or `SIGTERM`, the daemon immediately halts the sleep cycle, safely saves the state, and broadcasts an offline message without waiting for the refresh delay to naturally expire.

## 📂 Project Layout

The codebase lives under the `bot/` package, so additional scraper sites can be added alongside `bot/scrapers/subito.py` in the future without restructuring:
```
main.py                  # thin launcher: python3 main.py ...
bot/
  daemon.py               # scan loop, CLI arg parsing, message formatting
  telegram_ui.py          # Telegram command/callback handling, add/edit flows
  database.py             # SQLite schema, migrations, queries
  i18n.py                 # translation strings and the t() lookup helper
  superuser_cli.py        # curses-based superuser/ban terminal menu
  scrapers/
    base.py               # BaseScraper abstract interface
    subito.py             # Subito.it Mobile API scraper
```

## 🛠 Installation

1. Clone the repository and navigate to the directory.
2. Install the required, version-pinned dependencies from `requirements.txt` (using `uv` is strictly recommended for environment isolation):
   ```bash
   uv pip install -r requirements.txt
   ```
3. Create a `.env` file in the project root and insert the token provided by BotFather:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCDEF_ghijklmnopqrstuvwxyz
   ```

## 📁 State & Database Configuration

**No manual configuration files are needed.** The bot automatically generates and manages its state files:
* **`subito_bot.db` (SQLite):** Managed entirely through the interactive Telegram UI (the **➕ Add Category**/**➕ Add Search** buttons under `/search`, plus each search's Delete button). Securely stores user subscriptions, active searches, exclusion keywords, tracked ads histories, and permanent bans in relational tables.
* **`.subito_env.json` (State Tracker):** Automatically created at first launch. Stores the cryptographic `environment-id` required to maintain persistent WAF trust. Do not delete this file unless you intend to simulate a full app reinstallation.

## 🚀 CLI Usage

The program runs continuously in daemon mode by default. Its behavior is governed by the following command-line arguments:

* `-r`, `--refreshrate [SECONDS]`: Sets the wait time between two complete scraping cycles. Default is `120`.
* `-d`, `--debug`: Enables verbose logging for HTTP request tracking, CDN schema dumps, and Telegram API payloads.
* `-s`, `--skip`: Skips sending notifications for pre-existing ads during the very first startup scan.
* `-su`, `--superuser`: Opens an interactive, terminal-based (curses) menu listing every registered user plus any banned-but-unregistered chat_ids, letting you promote/demote superusers (`SPACE`/`ENTER`) and ban/unban chat_ids (`b`) with arrow-key navigation before the bot starts. Changes are only applied if you save (`s`); either way the bot then starts normally.

**Startup Examples:**
```bash
# Standard startup with a check every 2 minutes
python3 main.py

# Startup with a check every 60 seconds
python3 main.py -r 60

# Startup with a 30-second delay, debug output enabled, skipping old ads
python3 main.py --refreshrate 30 --debug --skip

# Manage superusers interactively, then start normally
python3 main.py --superuser
```

## 📱 Telegram Commands

User interaction occurs via direct chat with the bot.

* `/sub` or `/start`: Subscribes the user and starts receiving notifications.
* `/unsub`: Unsubscribes the user and removes the Chat ID and all related data from the database.
* `/search`: Prints the hierarchical list of active searches, with a **➕ Add Category** button. Selecting a category shows its searches with a **➕ Add Search** button (category pre-set); selecting a search opens a menu to **Pause/Resume** it, **Edit** it (URL, exclusion keywords, or include keywords, in place — the tracked-ads history is preserved), delete it, or open/copy its link.
  Adding a search (via either button) prompts for a Subito.it link, then a search name, then optionally up to `MAX_EXCLUSION_KEYWORDS` **exclusion keywords** and up to `MAX_INCLUDE_KEYWORDS` **include keywords** (if set, an ad is only notified when its title contains at least one of them). Deleting a search's last remaining item automatically removes its category.
  **Note:** The link must be a valid Subito.it URL strictly pointing to the `/annunci-italia/vendita/` path.
* `/language`: Shows flag buttons to switch the bot's UI language (🇬🇧 English, the default, or 🇮🇹 Italiano). All bot messages are sent in the selected language.
* `/status`: Displays active/inactive user counts (`Active Users: X/MAX_SUBSCRIBERS`, `Inactive Users: Y`), your active searches limit, the system uptime, and the remaining time before your subscription expires (or "Unlimited" for superusers).
* `/broadcast <message>` (superusers only): Sends a message to every registered user.
* `🛑 /cancel`: Instantly aborts the current action (e.g., waiting for keyword input) and resets the user's state.
* `/help`: Displays the command guide.

## ⚙️ Configurazione avanzata via .env

All values below have sensible defaults and are entirely optional — set them in `.env` only if you need to change the bot's behavior.

| Variable | Default | Description |
|---|---|---|
| `MAX_SUBSCRIBERS` | `3` | Maximum number of registered users. New `/sub` requests are rejected once reached. |
| `MAX_REGULAR_SEARCHES` | `15` | Maximum simultaneous active searches per non-superuser. |
| `SUBSCRIPTION_TTL_DAYS` | `4` | Soft subscription Time-To-Live, in days. Once exceeded, scanning is paused for that user (searches are **not** deleted) until they send `/sub` again. |
| `CACHE_PRUNE_INTERVAL_SECONDS` | `1800` | How often abandoned inline-keyboard callbacks and incomplete interaction states are purged from RAM. |
| `MAX_EXCLUSION_KEYWORDS` | `3` | Maximum exclusion keywords per search. |
| `MAX_INCLUDE_KEYWORDS` | `3` | Maximum include keywords per search. |
| `DEEP_CLEAN_RETENTION_DAYS` | `30` | Hard-delete retention, in days. Regular users inactive for longer than this are permanently purged, along with their searches and tracking history. **Always much longer than `SUBSCRIPTION_TTL_DAYS`, and superusers are always exempt.** |
| `TRACKED_ADS_MAX_ITEMS` | `150` | Maximum tracked-ad history kept per search; older entries are trimmed every cycle. |
| `DB_PATH` | `subito_bot.db` | Path to the SQLite database file. |

### Scadenza dell'abbonamento: i due stadi
La scadenza di un abbonamento regolare avviene in due stadi indipendenti, entrambi configurabili:
1. **Soft (`SUBSCRIPTION_TTL_DAYS`, default 4gg):** lo scan si ferma per l'utente scaduto, che riceve un avviso; **le sue ricerche non vengono toccate**.
2. **Hard (`DEEP_CLEAN_RETENTION_DAYS`, default 30gg):** solo dopo questo periodo, molto più lungo, l'utente e le sue ricerche vengono cancellati definitivamente. I superuser non sono mai soggetti a questa cancellazione.

## 🔒 Parametri di sicurezza e manutenzione hardcoded

A differenza della tabella `.env` sopra, i seguenti parametri sono **intenzionalmente hardcoded** nel codice (non configurabili via `.env`), perché renderli modificabili per singola installazione indebolirebbe la garanzia che offrono. Sono comunque documentati qui per trasparenza; per modificarli è necessario cambiare la costante nella classe `TelegramUI` (`bot/telegram_ui.py`).

| Costante | Default | Descrizione |
|---|---|---|
| `AD_NOTIFICATION_RETENTION_DAYS` | `5` | I messaggi di notifica nuovo annuncio più vecchi di questo numero di giorni vengono cancellati automaticamente ad ogni ciclo di scan. |
| `MAX_SEARCH_NAME_LENGTH` | `60` | Lunghezza massima del nome di una ricerca. |
| `MAX_KEYWORD_LENGTH` | `40` | Lunghezza massima di ogni singola parola chiave di esclusione/inclusione (le parole più lunghe vengono troncate). |
| `FLOOD_WINDOW_SECONDS` / `FLOOD_MAX_UPDATES` | `10` / `20` | Finestra scorrevole anti-flood: un chat_id che invia più di `FLOOD_MAX_UPDATES` messaggi/callback in `FLOOD_WINDOW_SECONDS` secondi viene bannato automaticamente. |
| `OVERSIZED_INPUT_THRESHOLD` | `500` | Soglia (in caratteri) oltre la quale un valore inserito in uno dei passaggi "brevi" del flusso di aggiunta/modifica ricerca (categoria, nome, parole chiave, link) viene considerato sospetto e causa un ban immediato. Non è un limite generico su tutti i messaggi (es. `/broadcast` resta illimitato). |

I superuser sono sempre esentati dai controlli anti-flood e oversized-input, così un amministratore non può mai auto-bannarsi accidentalmente.

### Gestione ban/unban da terminale
Il menu `--superuser` (vedi sezione CLI Usage) mostra sia gli utenti registrati sia i chat_id bannati non registrati (bannati dopo `/unsub`, o mai iscritti), permettendo di alternarne lo stato con il tasto `b`. Un ban è indipendente dalla tabella `users`: sopravvive a `/unsub`, così un utente non può aggirarlo disiscrivendosi e reiscrivendosi.

## License
This project is licensed under the **GNU General Public License v3 (GPLv3)**. 
See the `LICENSE` file for full details.
