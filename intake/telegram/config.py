"""
config.py — Telegram Carding Bot configuration.

Holds:
  - The Telegram bot token (read from a .env file / environment).
  - Per-mill carding server base URLs (CARDING_BASE), copied verbatim from
    the WhatsApp bot's config.js. The bot routes each photo to the selected
    mill's carding server, which already handles its own machine-master
    validation (per-plant database) and its own SAP push.
  - Where the authorized Telegram user-ID whitelist lives.

This is a STANDALONE project. Nothing here reads from, writes to, or depends
on the WhatsApp bot.
"""

import os
from pathlib import Path

# Optionally load a .env file sitting next to this one (BOT_TOKEN=...).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except Exception:
    pass

# ── Telegram bot token ───────────────────────────────────────────────────────
# Create a bot with @BotFather in Telegram, copy the token it gives you, and
# put it in a .env file next to this one as:  BOT_TOKEN=123456789:AAxxxxxxxx
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ── Mills → carding server ───────────────────────────────────────────────────
# IMPORTANT: there is only ONE carding server (app.py, listening on 0.0.0.0:5001
# on this machine). It serves BOTH mills and decides the mill automatically:
#   - machine numbers written with an o/n prefix -> 031/032 -> MILL_B
#   - plain numbers                              -> 030     -> MILL_A
# The bot also sends the mill you pick as `plant`, so the preview opens on the
# right mill and MILL_B sheets flag any number missing its o/n prefix.
# Because it is one server, both mills point to the SAME URL — if the machine's
# LAN IP changes, update it here in one place.
_CARDING_SERVER = os.environ.get("CARDING_BASE", "http://127.0.0.1:5001").rstrip("/")

MILLS = {
    "MILL_A": {"label": "Mill A",   "carding_base": _CARDING_SERVER},
    "MILL_B": {"label": "Mill B (two sheds)", "carding_base": _CARDING_SERVER},
    "MILL_C": {"label": "Mill C",      "carding_base": _CARDING_SERVER},
}

# Order the mill buttons appear in. Remove a mill here to hide it.
MILL_ORDER = ["MILL_A", "MILL_B", "MILL_C"]

# ── Authorization ────────────────────────────────────────────────────────────
# Only Telegram numeric user IDs listed (and active) in this file may use the
# bot. A user can learn their own ID by sending /whoami to the bot.
AUTHORIZED_USERS_FILE = Path(__file__).with_name("authorized_users.json")

# Where python-telegram-bot persists per-user state (the chosen mill), so a
# restart doesn't forget everyone's mill.
PERSISTENCE_FILE = Path(__file__).with_name("bot_state.pkl")

# Network timeout (seconds) for calls to the carding server. OCR uses
# Claude/Gemini and can take a while, so keep this generous.
REQUEST_TIMEOUT = 180
