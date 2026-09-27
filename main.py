"""
Filename: main.py
Description: Thin entrypoint for the Subito.it Telegram Bot daemon. The actual
             implementation lives in bot/daemon.py (part of the `bot` package,
             which also hosts bot/scrapers/ for future non-Subito site
             scrapers). Kept at the repo root so `python3 main.py` keeps
             working unchanged.

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

from bot.daemon import main

if __name__ == "__main__":
    main()
