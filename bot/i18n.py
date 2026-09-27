"""
Filename: bot/i18n.py
Description: Minimal localization layer for the Subito.it Telegram Bot.
             Supports English (default) and Italian. Every user-facing string
             sent by bot/telegram_ui.py and bot/daemon.py is keyed here rather
             than hardcoded inline, so a user's stored `language` preference
             (see the `users.language` column in bot/database.py) determines
             which variant is sent.

             Superuser-authored free text (e.g. the /broadcast message body)
             is NOT translated - only the surrounding UI labels are.

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "it")

# Each entry maps a translation key to {"en": ..., "it": ...}. Placeholders use
# str.format() syntax (e.g. "{name}") and are filled in by t()'s **kwargs.
TRANSLATIONS = {
    # --- Subscription lifecycle ---
    "action_cancelled": {
        "en": "❌ <b>Action cancelled.</b>",
        "it": "❌ <b>Azione annullata.</b>",
    },
    "registrations_closed": {
        "en": "⚠️ <b>Registrations Closed.</b>\nThe bot has reached its maximum user capacity.",
        "it": "⚠️ <b>Registrazioni Chiuse.</b>\nIl bot ha raggiunto il numero massimo di utenti.",
    },
    "sub_active_days": {
        "en": "✅ Subscription active for {days} days.",
        "it": "✅ Abbonamento attivo per {days} giorni.",
    },
    "sub_renewed_days": {
        "en": "✅ Subscription renewed for {days} days.",
        "it": "✅ Abbonamento rinnovato per {days} giorni.",
    },
    "unsubscribed": {
        "en": "❌ Unsubscribed. All your data has been deleted.",
        "it": "❌ Disiscritto. Tutti i tuoi dati sono stati eliminati.",
    },
    "not_subscribed": {
        "en": "⚠️ Not subscribed.",
        "it": "⚠️ Non sei iscritto.",
    },
    "must_subscribe_first": {
        "en": "⚠️ <b>Action Denied.</b>\nYou must subscribe first using /sub.",
        "it": "⚠️ <b>Azione Negata.</b>\nDevi prima iscriverti con /sub.",
    },

    # --- /search ---
    "no_active_searches": {
        "en": "📂 <b>Active Searches:</b>\n\nNo active searches.",
        "it": "📂 <b>Ricerche Attive:</b>\n\nNessuna ricerca attiva.",
    },
    "select_category": {
        "en": "📂 <b>Select a category:</b>",
        "it": "📂 <b>Seleziona una categoria:</b>",
    },
    "category_select_search": {
        "en": "📂 <b>{cat}</b>\nSelect a search:",
        "it": "📂 <b>{cat}</b>\nSeleziona una ricerca:",
    },
    "category_auto_deleted": {
        "en": "⚠️ <b>{cat}</b> has been deleted because it no longer contains any searches.",
        "it": "⚠️ <b>{cat}</b> è stata eliminata perché non contiene più ricerche.",
    },

    # --- Add-search link entry (started via the "+ Add Category"/"+ Add Search" buttons) ---
    "limit_reached": {
        "en": "⚠️ <b>Limit Reached.</b>\nYou cannot have more than {max} active searches.",
        "it": "⚠️ <b>Limite Raggiunto.</b>\nNon puoi avere più di {max} ricerche attive.",
    },
    "send_link_prompt": {
        "en": "🔗 Send the Subito.it search link:",
        "it": "🔗 Invia il link di ricerca Subito.it:",
    },
    "invalid_link_path": {
        "en": "⚠️ <b>Error:</b> Invalid Subito.it link. Must start with /annunci-italia/vendita/.",
        "it": "⚠️ <b>Errore:</b> Link Subito.it non valido. Deve iniziare con /annunci-italia/vendita/.",
    },
    "price_sort_unsupported": {
        "en": (
            "⚠️ <b>Error:</b> Price-based sorting (<code>order=priceasc</code>/<code>order=pricedesc</code>) is not supported.\n"
            "Searches are always saved sorted by <b>most recent</b>. Remove the 'order' parameter from the link and try again."
        ),
        "it": (
            "⚠️ <b>Errore:</b> L'ordinamento per prezzo (<code>order=priceasc</code>/<code>order=pricedesc</code>) non è supportato.\n"
            "Le ricerche vengono sempre salvate ordinate per <b>più recenti</b>. Rimuovi il parametro 'order' dal link e riprova."
        ),
    },
    "verifying_link": {
        "en": "🔎 <i>Verifying link, please wait...</i>",
        "it": "🔎 <i>Verifica del link in corso, attendi...</i>",
    },

    # --- Background link validation (_validate_link_and_proceed / _validate_and_update_url) ---
    "link_unreachable": {
        "en": "⚠️ <b>Error:</b> Link unreachable (HTTP {code}).",
        "it": "⚠️ <b>Errore:</b> Link non raggiungibile (HTTP {code}).",
    },
    "link_unreachable_not_modified": {
        "en": "⚠️ <b>Error:</b> Link unreachable (HTTP {code}). The search was NOT modified.",
        "it": "⚠️ <b>Errore:</b> Link non raggiungibile (HTTP {code}). La ricerca NON è stata modificata.",
    },
    "not_valid_search_page": {
        "en": "⚠️ <b>Error:</b> Not a valid Subito.it search page.",
        "it": "⚠️ <b>Errore:</b> Non è una pagina di ricerca Subito.it valida.",
    },
    "not_valid_search_page_not_modified": {
        "en": "⚠️ <b>Error:</b> Not a valid Subito.it search page. The search was NOT modified.",
        "it": "⚠️ <b>Errore:</b> Non è una pagina di ricerca Subito.it valida. La ricerca NON è stata modificata.",
    },
    "validation_network_error": {
        "en": "⚠️ <b>Network Error:</b> Validation failed. Try again.",
        "it": "⚠️ <b>Errore di Rete:</b> Verifica fallita. Riprova.",
    },
    "validation_network_error_not_modified": {
        "en": "⚠️ <b>Network Error:</b> Validation failed. The search was NOT modified.",
        "it": "⚠️ <b>Errore di Rete:</b> Verifica fallita. La ricerca NON è stata modificata.",
    },
    "url_updated": {
        "en": "✅ URL updated for search <b>{name}</b>.",
        "it": "✅ URL aggiornato per la ricerca <b>{name}</b>.",
    },

    # --- /broadcast ---
    "superuser_only": {
        "en": "⚠️ <b>Action Denied.</b>\nOnly superusers can use this command.",
        "it": "⚠️ <b>Azione Negata.</b>\nSolo i superuser possono usare questo comando.",
    },
    "usage_broadcast": {
        "en": "⚠️ <b>Usage:</b> /broadcast &lt;message&gt;",
        "it": "⚠️ <b>Uso:</b> /broadcast &lt;messaggio&gt;",
    },
    "announcement_label": {
        "en": "📢 <b>Announcement</b>",
        "it": "📢 <b>Annuncio</b>",
    },
    "broadcast_sent": {
        "en": "✅ Broadcast sent to all users.",
        "it": "✅ Messaggio inviato a tutti gli utenti.",
    },

    # --- /help ---
    "help_text_base": {
        "en": (
            "🤖 <b>Available Commands</b>\n\n"
            "✅ <b>/sub</b> - Subscribe to receive alerts.\n"
            "❌ <b>/unsub</b> - Unsubscribe and delete your data.\n"
            "🔎 <b>/search</b> - View, add and manage your searches.\n"
            "📊 <b>/status</b> - View system status.\n"
            "🌐 <b>/language</b> - Change the bot's language.\n"
            "🛑 <b>/cancel</b> - Abort current action."
        ),
        "it": (
            "🤖 <b>Comandi Disponibili</b>\n\n"
            "✅ <b>/sub</b> - Iscriviti per ricevere notifiche.\n"
            "❌ <b>/unsub</b> - Disiscriviti ed elimina i tuoi dati.\n"
            "🔎 <b>/search</b> - Visualizza, aggiungi e gestisci le tue ricerche.\n"
            "📊 <b>/status</b> - Visualizza lo stato del sistema.\n"
            "🌐 <b>/language</b> - Cambia la lingua del bot.\n"
            "🛑 <b>/cancel</b> - Annulla l'azione corrente."
        ),
    },
    "help_broadcast_addition": {
        "en": "\n📢 <b>/broadcast &lt;message&gt;</b> - Send a message to all users (superuser only).",
        "it": "\n📢 <b>/broadcast &lt;messaggio&gt;</b> - Invia un messaggio a tutti gli utenti (solo superuser).",
    },

    # --- /status ---
    "status_header": {
        "en": "📊 <b>System Status</b>\nUptime: {uptime}\n",
        "it": "📊 <b>Stato del Sistema</b>\nUptime: {uptime}\n",
    },
    "status_users_line": {
        "en": "👥 Active Users: {active}/{max}\n😴 Inactive Users: {inactive}\n\n",
        "it": "👥 Utenti Attivi: {active}/{max}\n😴 Utenti Inattivi: {inactive}\n\n",
    },
    "status_account_header": {
        "en": "👤 <b>Your Account</b>\nActive Searches: {count}",
        "it": "👤 <b>Il Tuo Account</b>\nRicerche Attive: {count}",
    },
    "status_max_suffix": {
        "en": "/{max}",
        "it": "/{max}",
    },
    "status_expires_in": {
        "en": "\nSubscription expires in: {duration}",
        "it": "\nAbbonamento in scadenza tra: {duration}",
    },
    "status_expired": {
        "en": "\nSubscription: expired (send /sub to renew)",
        "it": "\nAbbonamento: scaduto (invia /sub per rinnovare)",
    },
    "status_unlimited": {
        "en": "\nSubscription: Unlimited (superuser)",
        "it": "\nAbbonamento: Illimitato (superuser)",
    },

    # --- Add-search flow (category / name / exclusion / include steps) ---
    "cat_name_too_long": {
        "en": "⚠️ Category name too long (max 30).",
        "it": "⚠️ Nome categoria troppo lungo (max 30).",
    },
    "search_name_too_long": {
        "en": "⚠️ Search name too long (max {max}).",
        "it": "⚠️ Nome ricerca troppo lungo (max {max}).",
    },
    "invalid_chars": {
        "en": "⚠️ Invalid characters. Use alphanumeric, spaces, hyphens.",
        "it": "⚠️ Caratteri non validi. Usa alfanumerici, spazi, trattini.",
    },
    "category_set_write_name": {
        "en": "Category '<b>{cat}</b>' set. Write the Search Name:",
        "it": "Categoria '<b>{cat}</b>' impostata. Scrivi il Nome della Ricerca:",
    },
    "search_name_set_exclusion_prompt": {
        "en": (
            "Search Name set to '<b>{name}</b>'.\n\n"
            "Optional: Send up to {max} <b>exclusion keywords</b> separated by commas (e.g. broken, parts, not working).\n"
            "Send <b>NONE</b> to skip this step."
        ),
        "it": (
            "Nome Ricerca impostato su '<b>{name}</b>'.\n\n"
            "Opzionale: Invia fino a {max} <b>parole chiave di esclusione</b> separate da virgole (es. rotto, parti, non funzionante).\n"
            "Invia <b>NONE</b> per saltare questo passaggio."
        ),
    },
    "include_kw_prompt": {
        "en": (
            "Optional: Send up to {max} <b>include keywords</b> separated by commas: "
            "an ad is only notified if its title contains at least one of them (e.g. new, sealed).\n"
            "Send <b>NONE</b> to skip this step and match any title."
        ),
        "it": (
            "Opzionale: Invia fino a {max} <b>parole chiave di inclusione</b> separate da virgole: "
            "un annuncio viene notificato solo se il titolo contiene almeno una di esse (es. nuovo, sigillato).\n"
            "Invia <b>NONE</b> per saltare questo passaggio e includere qualsiasi titolo."
        ),
    },
    "added_confirmation": {
        "en": (
            "✅ <b>Added!</b>\n📂 Category: {cat}\n🔑 Search Name: {name}\n"
            "🚫 Exclusions: {excl}\n✅ Includes: {incl}"
        ),
        "it": (
            "✅ <b>Aggiunta!</b>\n📂 Categoria: {cat}\n🔑 Nome Ricerca: {name}\n"
            "🚫 Esclusioni: {excl}\n✅ Inclusioni: {incl}"
        ),
    },
    "category_unsupported_note": {
        "en": "\n\nℹ️ This category isn't fully supported by the internal filter: results for this search may be broader than expected.",
        "it": "\n\nℹ️ Questa categoria non è completamente supportata dal filtro interno: i risultati di questa ricerca potrebbero essere più ampi del previsto.",
    },
    "invalid_subito_link": {
        "en": "⚠️ <b>Error:</b> Invalid Subito.it link.",
        "it": "⚠️ <b>Errore:</b> Link Subito.it non valido.",
    },
    "price_sort_unsupported_short": {
        "en": "⚠️ <b>Error:</b> Price-based sorting is not supported. Remove 'order' from the link and try again.",
        "it": "⚠️ <b>Errore:</b> L'ordinamento per prezzo non è supportato. Rimuovi 'order' dal link e riprova.",
    },
    "verifying_new_link": {
        "en": "🔎 <i>Verifying new link, please wait...</i>",
        "it": "🔎 <i>Verifica del nuovo link in corso, attendi...</i>",
    },
    "exclusion_updated": {
        "en": "✅ Exclusion keywords updated: {kw}",
        "it": "✅ Parole chiave di esclusione aggiornate: {kw}",
    },
    "include_updated": {
        "en": "✅ Include keywords updated: {kw}",
        "it": "✅ Parole chiave di inclusione aggiornate: {kw}",
    },

    # --- Callbacks / menus ---
    "session_expired": {
        "en": "Session expired.",
        "it": "Sessione scaduta.",
    },
    "operation_cancelled": {
        "en": "❌ <b>Operation cancelled.</b>",
        "it": "❌ <b>Operazione annullata.</b>",
    },
    "editing_what": {
        "en": "✏️ <b>Editing:</b> {name}\nWhat do you want to change?",
        "it": "✏️ <b>Modifica:</b> {name}\nCosa vuoi modificare?",
    },
    "edit_prompt_url": {
        "en": "🔗 Send the new Subito.it link for this search:",
        "it": "🔗 Invia il nuovo link Subito.it per questa ricerca:",
    },
    "edit_prompt_exclusion": {
        "en": "🚫 Send up to {max} exclusion keywords separated by commas, or NONE to clear them:",
        "it": "🚫 Invia fino a {max} parole chiave di esclusione separate da virgole, oppure NONE per cancellarle:",
    },
    "edit_prompt_include": {
        "en": "✅ Send up to {max} include keywords separated by commas, or NONE to clear them:",
        "it": "✅ Invia fino a {max} parole chiave di inclusione separate da virgole, oppure NONE per cancellarle:",
    },
    "send_new_value": {
        "en": "Send the new value:",
        "it": "Invia il nuovo valore:",
    },
    "write_new_category_name": {
        "en": "Write the name of the new category (max 30 chars, alphanumeric):",
        "it": "Scrivi il nome della nuova categoria (max 30 caratteri, alfanumerico):",
    },
    "category_selected_write_name": {
        "en": "Category '<b>{cat}</b>' selected. Write the Search Name:",
        "it": "Categoria '<b>{cat}</b>' selezionata. Scrivi il Nome della Ricerca:",
    },
    "search_deleted": {
        "en": "✅ Search <b>{name}</b> deleted.",
        "it": "✅ Ricerca <b>{name}</b> eliminata.",
    },
    "category_removed_no_searches": {
        "en": "\n🗑️ Category <b>{cat}</b> has been removed because it no longer contains any searches.",
        "it": "\n🗑️ La categoria <b>{cat}</b> è stata rimossa perché non contiene più ricerche.",
    },

    # --- Search detail menu ---
    "search_not_found": {
        "en": "⚠️ Search not found.",
        "it": "⚠️ Ricerca non trovata.",
    },
    "search_menu_header": {
        "en": "📂 <b>Category:</b> {cat}\n🔍 <b>Search:</b> {name}\n",
        "it": "📂 <b>Categoria:</b> {cat}\n🔍 <b>Ricerca:</b> {name}\n",
    },
    "status_active": {
        "en": "▶️ <b>Status:</b> Active",
        "it": "▶️ <b>Stato:</b> Attiva",
    },
    "status_paused": {
        "en": "⏸️ <b>Status:</b> Paused",
        "it": "⏸️ <b>Stato:</b> In pausa",
    },
    "exclusions_line": {
        "en": "🚫 <b>Exclusions:</b> {excl}\n",
        "it": "🚫 <b>Esclusioni:</b> {excl}\n",
    },
    "includes_line": {
        "en": "✅ <b>Includes:</b> {incl}\n",
        "it": "✅ <b>Inclusioni:</b> {incl}\n",
    },
    "direct_link_line": {
        "en": "\n🔗 <b>Direct Link:</b>\n<code>{url}</code>",
        "it": "\n🔗 <b>Link Diretto:</b>\n<code>{url}</code>",
    },

    # --- Buttons (shared across menus) ---
    "btn_close": {"en": "❌ Close", "it": "❌ Chiudi"},
    "btn_cancel": {"en": "❌ Cancel", "it": "❌ Annulla"},
    "btn_back": {"en": "🔙 Back", "it": "🔙 Indietro"},
    "btn_pause": {"en": "⏸️ Pause", "it": "⏸️ Pausa"},
    "btn_resume": {"en": "▶️ Resume", "it": "▶️ Riprendi"},
    "btn_edit": {"en": "✏️ Edit", "it": "✏️ Modifica"},
    "btn_delete": {"en": "🗑️ Delete", "it": "🗑️ Elimina"},
    "btn_copy_link": {"en": "🔗 Copy Link", "it": "🔗 Copia Link"},
    "btn_open_link": {"en": "🌐 Open Link", "it": "🌐 Apri Link"},
    "btn_edit_url": {"en": "🔗 Edit URL", "it": "🔗 Modifica URL"},
    "btn_edit_exclusions": {"en": "🚫 Edit exclusions", "it": "🚫 Modifica esclusioni"},
    "btn_edit_includes": {"en": "✅ Edit includes", "it": "✅ Modifica inclusioni"},
    "btn_add_category": {"en": "➕ Add Category", "it": "➕ Aggiungi Categoria"},
    "btn_add_search": {"en": "➕ Add Search", "it": "➕ Aggiungi Ricerca"},
    "btn_go_to_ad": {"en": "🛒 Go to Ad", "it": "🛒 Vai all'Annuncio"},

    # --- Daemon system broadcasts (bot/daemon.py) ---
    "system_online": {"en": "🟢 <b>System Online</b>", "it": "🟢 <b>Sistema Online</b>"},
    "system_offline": {"en": "🔴 <b>System Offline</b>", "it": "🔴 <b>Sistema Offline</b>"},

    # --- Ad notification message (format_message in bot/daemon.py) ---
    "ad_price_label": {"en": "💶 Price: {price}", "it": "💶 Prezzo: {price}"},
    "ad_location_label": {"en": "📍 Location: {location}", "it": "📍 Posizione: {location}"},
    "also_found_by": {
        "en": "🔍 <i>Also found by:</i> {names}",
        "it": "🔍 <i>Trovato anche da:</i> {names}",
    },

    # --- Generic labels ---
    "label_none": {"en": "None", "it": "Nessuna"},

    # --- Daemon per-user notices (bot/daemon.py) ---
    "subscription_expired_notice": {
        "en": "⚠️ <b>Subscription Expired</b>\nYour {days}-day access has ended. Send /sub to renew.",
        "it": "⚠️ <b>Abbonamento Scaduto</b>\nIl tuo accesso di {days} giorni è terminato. Invia /sub per rinnovare.",
    },

    # --- /language ---
    "language_prompt": {
        "en": "🌐 Select your language:",
        "it": "🌐 Seleziona la tua lingua:",
    },
    # Not a translation pair of one sentence: each variant is the confirmation
    # shown in the language the user just switched TO.
    "language_set": {
        "en": "✅ Language set to English 🇬🇧",
        "it": "✅ Lingua impostata su Italiano 🇮🇹",
    },
}


def t(key, lang=DEFAULT_LANGUAGE, **kwargs):
    """
    Looks up `key` in TRANSLATIONS for `lang`, falling back to English and then
    to the raw key if either the key or the language variant is missing.
    Any `kwargs` are applied via str.format(); formatting errors (e.g. a typo
    in a placeholder name) fall back to the unformatted string rather than
    raising, since a slightly-wrong message is preferable to a crash.
    """
    entry = TRANSLATIONS.get(key)
    if entry is None:
        return key

    text = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or key
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError):
        return text
