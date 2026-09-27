"""
Filename: bot/superuser_cli.py
Version: 1.1.0
Description: Interactive terminal (curses) menu to promote/demote users to/from
             superuser status and ban/unban chat_ids (report item 8), launched via
             `main.py --superuser`. Uses only the Python standard library (curses)
             so it introduces no new dependency.

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

import curses
from datetime import datetime

import requests


def _fetch_display_name(token, chat_id):
    """
    Best-effort lookup of a human-readable name for a chat_id via the Telegram
    Bot API. Falls back to a placeholder if the call fails (e.g. the user
    blocked the bot, or there's no network connectivity at this point).
    """
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{token}/getChat",
            json={"chat_id": chat_id},
            timeout=5,
        )
        data = res.json()
        if data.get("ok"):
            result = data.get("result", {})
            username = result.get("username")
            first_name = result.get("first_name")
            if username:
                return f"@{username}"
            if first_name:
                return first_name
    except Exception:
        pass
    return "N/D"


def _draw_menu(stdscr, rows, selected_idx, dirty):
    stdscr.erase()
    curses.curs_set(0)

    stdscr.addstr(0, 0, "Gestione Superuser/Ban - Subito.it Telegram Bot", curses.A_BOLD)
    title = " (modifiche non salvate)" if dirty else ""
    if title:
        stdscr.addstr(0, 49, title, curses.A_DIM)

    header = f"   {'Chat ID':<14}{'Nome':<20}{'Iscritto il':<18}{'Superuser':<11}{'Bannato'}"
    stdscr.addstr(2, 0, header, curses.A_UNDERLINE)

    max_y, _ = stdscr.getmaxyx()
    visible_rows = max(1, max_y - 6)

    if not rows:
        stdscr.addstr(4, 2, "Nessun utente registrato.")
    else:
        for idx, row in enumerate(rows[:visible_rows]):
            y = 3 + idx
            marker = ">" if idx == selected_idx else " "
            joined_str = (
                datetime.fromtimestamp(row["joined_at"]).strftime("%Y-%m-%d %H:%M")
                if row["joined_at"] else "-"
            )
            attr = curses.A_REVERSE if idx == selected_idx else 0

            line = f"{marker}  {str(row['chat_id']):<14}{row['name']:<20}{joined_str:<18}"
            stdscr.addstr(y, 0, line, attr)

            # Unregistered chat_ids (banned but never subscribed, or banned then
            # /unsub'd) can't be superusers - shown as "-" instead of Si/No so it's
            # clear the toggle is a no-op for them.
            if row["is_registered"]:
                su_text = "Si" if row["is_superuser"] else "No"
                su_color = curses.color_pair(1) if row["is_superuser"] else curses.color_pair(2)
            else:
                su_text = "-"
                su_color = curses.color_pair(0)
            stdscr.addstr(y, len(line), f"{su_text:<11}", su_color | attr | curses.A_BOLD)

            ban_text = "Si" if row["is_banned"] else "No"
            ban_color = curses.color_pair(2) if row["is_banned"] else curses.color_pair(1)
            stdscr.addstr(y, len(line) + 11, ban_text, ban_color | attr | curses.A_BOLD)

    footer_y = max_y - 2
    stdscr.addstr(footer_y, 0, "-" * min(78, max(1, curses.COLS - 1)))
    stdscr.addstr(
        footer_y + 1, 0,
        "Frecce su/giu: naviga   SPACE/INVIO: alterna superuser   b: alterna ban   s: salva ed esci   q/ESC: annulla ed esci"
    )
    stdscr.refresh()


def _curses_main(stdscr, rows):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)

    selected_idx = 0
    dirty = False

    while True:
        _draw_menu(stdscr, rows, selected_idx, dirty)
        key = stdscr.getch()

        if not rows:
            if key in (ord('q'), ord('Q'), 27):
                return False
            continue

        if key == curses.KEY_UP:
            selected_idx = (selected_idx - 1) % len(rows)
        elif key == curses.KEY_DOWN:
            selected_idx = (selected_idx + 1) % len(rows)
        elif key in (ord(' '), curses.KEY_ENTER, 10, 13, curses.KEY_LEFT, curses.KEY_RIGHT):
            # Toggling superuser status is meaningless for a chat_id with no `users`
            # row (banned but unregistered) - ignored rather than silently creating
            # a fake account on save.
            if rows[selected_idx]["is_registered"]:
                rows[selected_idx]["is_superuser"] = not rows[selected_idx]["is_superuser"]
                dirty = True
        elif key in (ord('b'), ord('B')):
            rows[selected_idx]["is_banned"] = not rows[selected_idx]["is_banned"]
            dirty = True
        elif key in (ord('s'), ord('S')):
            return True
        elif key in (ord('q'), ord('Q'), 27):
            return False


def run_superuser_menu(db, token):
    """
    Entry point called from `main.py --superuser`. Shows the interactive menu,
    then either persists the pending toggles to the database ('s' to save) or
    discards them ('q'/ESC to cancel) before returning control to main.py, which
    continues with the normal bot startup either way.

    Report item 8: the list merges `users` with `banned_users` so that a chat_id
    banned after /unsub'ing (or banned before ever subscribing) still shows up and
    can be unbanned, instead of only being visible via raw DB access.
    """
    users = db.get_all_users()
    banned = db.get_all_banned()
    banned_by_chat_id = {b["chat_id"]: b for b in banned}

    if not users and not banned:
        print("\033[93m[superuser] Nessun utente registrato: niente da gestire.\033[0m")
        return

    print("\033[96m[superuser] Recupero informazioni utenti da Telegram...\033[0m")
    rows = [
        {
            "chat_id": u["chat_id"],
            "name": _fetch_display_name(token, u["chat_id"]),
            "joined_at": u["joined_at"],
            "is_superuser": bool(u["is_superuser"]),
            "is_banned": u["chat_id"] in banned_by_chat_id,
            "is_registered": True,
        }
        for u in users
    ]

    registered_ids = {u["chat_id"] for u in users}
    rows.extend(
        {
            "chat_id": b["chat_id"],
            "name": _fetch_display_name(token, b["chat_id"]),
            "joined_at": None,
            "is_superuser": False,
            "is_banned": True,
            "is_registered": False,
        }
        for b in banned
        if b["chat_id"] not in registered_ids
    )

    initial_ban_state = {row["chat_id"]: row["is_banned"] for row in rows}

    try:
        save = curses.wrapper(_curses_main, rows)
    except Exception as e:
        # Any curses failure (e.g. non-interactive terminal, unsupported TERM)
        # must not prevent the bot from starting normally.
        print(f"\033[91m[superuser] Impossibile avviare l'interfaccia a schermo: {e}\033[0m")
        print("\033[93m[superuser] Nessuna modifica applicata. Avvio del bot in corso...\033[0m")
        return

    if save:
        for row in rows:
            if row["is_registered"]:
                db.set_superuser(row["chat_id"], row["is_superuser"])
            # Only touch the ban table when the state actually changed, so
            # unrelated rows don't get their banned_at timestamp reset on every save.
            if row["is_banned"] != initial_ban_state[row["chat_id"]]:
                if row["is_banned"]:
                    db.ban_user(row["chat_id"], reason="manual")
                else:
                    db.unban_user(row["chat_id"])
        print("\033[92m[superuser] Modifiche salvate.\033[0m")
    else:
        print("\033[93m[superuser] Modifiche scartate.\033[0m")
