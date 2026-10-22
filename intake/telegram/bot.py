"""
bot.py — Telegram Carding Bot (standalone).

A WhatsApp-free INTAKE channel for the carding OCR system. A supervisor sends
(or scans and sends) the carding sheet photo to this Telegram bot; the bot
forwards it to the correct mill's carding server and replies with the review
link — exactly what the WhatsApp bot did, minus WhatsApp.

ONE bot handles multiple mills (MILL_A / MILL_B): the user picks a mill once, and
the bot routes photos to that mill's carding server. Each mill's server handles
its own machine-master validation and SAP push.

This project does NOT touch, import, or depend on the WhatsApp bot.

Run:  python bot.py    (see README.md for full setup)
"""

import asyncio
import json
import logging
from datetime import datetime

import httpx
import requests
from telegram.error import NetworkError, TimedOut

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    PicklePersistence,
    filters,
)

import config
import carding_client

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("carding-bot")
# httpx logs every request URL at INFO -- those URLs contain the bot token and
# a getUpdates line every 10s. Keep only warnings/errors.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Transient network failures worth retrying (dropped/reset connections, timeouts).
_TRANSIENT = (NetworkError, TimedOut, httpx.TransportError, OSError)
_RETRY_DELAYS = (2, 5, 10)  # seconds between attempts -> 4 attempts total


async def download_with_retry(bot, file_id) -> bytes:
    """Download a Telegram file, retrying on dropped connections.

    Re-calls get_file on each attempt so a stale file path is never reused."""
    last = None
    for attempt, delay in enumerate((0,) + _RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            tg_file = await bot.get_file(file_id)
            return bytes(await tg_file.download_as_bytearray())
        except _TRANSIENT as e:
            last = e
            log.warning("Photo download attempt %d failed: %r", attempt, e)
    raise last


async def safe_edit(msg, text, **kw):
    """Edit a status message; never let a Telegram blip crash the handler."""
    for delay in (0,) + _RETRY_DELAYS[:2]:
        if delay:
            await asyncio.sleep(delay)
        try:
            return await msg.edit_text(text, **kw)
        except _TRANSIENT as e:
            log.warning("edit_text failed: %r", e)
        except Exception as e:  # noqa: BLE001  (e.g. 'message is not modified')
            log.warning("edit_text error: %r", e)
            return None
    return None


# ── Authorization ─────────────────────────────────────────────────────────────
def load_authorized_ids():
    """Read the whitelist fresh each time, so edits apply without a restart."""
    try:
        with open(config.AUTHORIZED_USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(u["id"]) for u in data if u.get("active", True)}
    except FileNotFoundError:
        return set()
    except Exception as e:  # noqa: BLE001
        log.error("Could not read authorized_users.json: %s", e)
        return set()


def is_authorized(user_id) -> bool:
    return user_id in load_authorized_ids()


# ── Mill selection keyboard ───────────────────────────────────────────────────
def mill_keyboard():
    rows = [
        [InlineKeyboardButton(config.MILLS[m]["label"], callback_data=f"mill:{m}")]
        for m in config.MILL_ORDER
        if m in config.MILLS
    ]
    return InlineKeyboardMarkup(rows)


# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text(
            "🚫 You're not authorized to use this bot yet.\n\n"
            f"Your Telegram ID is: `{user.id}`\n"
            "Send that ID to your admin to be added.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    mill = context.user_data.get("mill")
    current = (
        f"\n\n🏭 Current mill: *{config.MILLS[mill]['label']}*"
        if mill in config.MILLS
        else ""
    )
    await update.message.reply_text(
        "👋 *Carding OCR Bot*\n\n"
        "Send a photo of the carding sheet — or scan it and send the image — "
        "and I'll process it and give you the review link.\n\n"
        "💡 *Tip:* for the sharpest OCR, send the scan as a *File* "
        "(paperclip → File) rather than a compressed Photo.\n\n"
        "Pick your mill below, then just send sheets." + current,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=mill_keyboard(),
    )


async def cmd_mill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "🏭 Choose the mill for your sheets:",
        reply_markup=mill_keyboard(),
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"Your Telegram ID: `{u.id}`\nUsername: @{u.username or '—'}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Mill selection callback ───────────────────────────────────────────────────
async def on_mill_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_authorized(query.from_user.id):
        await query.edit_message_text("🚫 Not authorized.")
        return
    mill = query.data.split(":", 1)[1]
    if mill not in config.MILLS:
        await query.edit_message_text("Unknown mill.")
        return
    context.user_data["mill"] = mill
    await query.edit_message_text(
        f"✅ Mill set to *{config.MILLS[mill]['label']}*.\n\n"
        "Now send a carding sheet photo.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Photo / image handler ─────────────────────────────────────────────────────
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text(
            f"🚫 Not authorized. Your ID: `{user.id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    mill = context.user_data.get("mill")
    if mill not in config.MILLS:
        await update.message.reply_text(
            "🏭 Which mill is this sheet for? Pick one, then re-send the photo:",
            reply_markup=mill_keyboard(),
        )
        return

    carding_base = config.MILLS[mill]["carding_base"]

    # Accept both a compressed Photo and an image sent as a File (document).
    file_id = None
    file_name = f"carding_{datetime.now():%Y%m%d_%H%M%S}.jpg"
    if update.message.photo:
        file_id = update.message.photo[-1].file_id  # highest resolution
    elif update.message.document and (
        update.message.document.mime_type or ""
    ).startswith("image/"):
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name or file_name
    if not file_id:
        await update.message.reply_text("Please send an image (a photo or an image file).")
        return

    status = await update.message.reply_text(
        f"⏳ Processing for *{config.MILLS[mill]['label']}*…",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Download the image into memory (no temp files left behind).
    try:
        image_bytes = await download_with_retry(context.bot, file_id)
    except Exception as e:  # noqa: BLE001
        log.exception("Photo download failed after retries")
        await safe_edit(
            status,
            "❌ Couldn't download the photo from Telegram after 4 tries "
            f"({type(e).__name__}).\n\n"
            "This machine's internet is dropping connections — check Wi-Fi / "
            "network, then send the photo again.",
        )
        return

    jid = f"telegram:{user.id}"
    label = config.MILLS[mill]["label"]
    try:
        # Run the blocking HTTP calls in a worker thread so the bot keeps
        # polling and answering buttons while OCR runs (30-120 s).
        summary, link = await asyncio.to_thread(
            carding_client.process_photo,
            carding_base, image_bytes, file_name, jid, config.REQUEST_TIMEOUT,
            plant=mill,
        )
    except carding_client.CardingError as e:
        await safe_edit(status, f"❌ OCR failed: {e}")
        return
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = ""
        if e.response is not None:
            try:
                body = e.response.json().get("error") or ""
            except Exception:  # noqa: BLE001
                body = (e.response.text or "")[:300]
        log.error("Carding server HTTP %s: %s", code, body or e)
        await safe_edit(
            status,
            f"❌ The {label} carding server returned an error (HTTP {code}).\n\n"
            + (f"Reason: {body}\n\n" if body else "")
            + "The server is running — check its console for the traceback, "
            "then send the photo again.",
        )
        return
    except requests.exceptions.Timeout:
        log.error("Carding server timed out after %ss", config.REQUEST_TIMEOUT)
        await safe_edit(
            status,
            f"⌛ The {label} carding server took longer than "
            f"{config.REQUEST_TIMEOUT}s. OCR may be slow or the OCR API "
            "unreachable — please send the photo again.",
        )
        return
    except requests.exceptions.ConnectionError as e:
        log.error("Carding server unreachable: %s", e)
        await safe_edit(
            status,
            f"❌ Couldn't reach the {label} carding server ({carding_base}).\n\n"
            "Check app.py is running on this machine.",
        )
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Carding processing error")
        await safe_edit(status, f"❌ Unexpected error: {type(e).__name__}: {e}")
        return

    await safe_edit(
        status,
        "✅ *Carding OCR complete*\n\n"
        f"🏭 Mill: *{config.MILLS[mill]['label']}*\n"
        f"📅 Date: *{summary.get('date') or '—'}*\n"
        f"🔄 Shift: *{summary.get('shift') or '—'}*\n"
        f"👤 Supervisor: *{summary.get('supervisor') or '—'}*\n\n"
        "👇 Review, edit & push to SAP here:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.reply_text(link)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "Send a carding sheet *photo* to process it.\n"
        "/mill — change mill   ·   /start — help",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catch any handler exception (e.g. a transient Telegram/network timeout)
    so the bot logs it cleanly and keeps running instead of dumping a traceback,
    and nudges the user to retry."""
    log.warning("Handler error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Network hiccup talking to Telegram — please send that again."
            )
    except Exception:
        pass  # if Telegram is unreachable, nothing more we can do


def main():
    if not config.BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Create a bot with @BotFather, then put\n"
            "  BOT_TOKEN=...\n"
            "in a .env file next to bot.py (copy .env.example)."
        )

    persistence = PicklePersistence(filepath=str(config.PERSISTENCE_FILE))
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .persistence(persistence)
        # Generous timeouts so a brief internet slowdown doesn't instantly
        # time out when talking to Telegram (mill/office links can be flaky).
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(40)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("mill", cmd_mill))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CallbackQueryHandler(on_mill_selected, pattern=r"^mill:"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Carding Telegram bot starting (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
