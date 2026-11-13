# PayLock Bot - thin wrapper, all logic in app.py
# requirements: python-telegram-bot>=20.0, python-dotenv
#
# ARCHIVED: Polling disabled - app.py webhook handles updates now.
# This file is NOT part of the Render deployment. The only live bot is the
# /webhook route in app.py (Render Start Command: gunicorn main:app).
# Running this file never polls. Only the one-shot helper below is available:
#     python bot_polling_backup.py set-commands

import os
import re
import sys
import asyncio
import logging

from dotenv import load_dotenv
from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("paylock-bot")

# -----------------------------------------------------------------------------
# Config (env only, nothing hardcoded except safe defaults)
# -----------------------------------------------------------------------------

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
BOT_USERNAME = (os.environ.get("BOT_USERNAME") or "PayLockOfficialBot").strip().lstrip("@")
ADMIN_ID = str(os.environ.get("ADMIN_ID", "") or "").strip()   # empty = nobody is admin
WEBAPP_URL = (os.environ.get("WEBAPP_URL") or "").strip()

SUPPORT_URL = f"https://t.me/{BOT_USERNAME}"
WEBAPP_BASE = WEBAPP_URL or SUPPORT_URL   # fallback keeps the WebApp button buildable

# Accepts prod_X7K9Q2, ref_123, inv_123. Case is preserved exactly.
START_PARAM_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

WELCOME_TEXT = (
    "Welcome to PayLock - Buy & sell digital products safely with escrow "
    "protection. Tap below to open the app."
)

# Menu shown to EVERYONE. /admin is deliberately not in this list.
PUBLIC_COMMANDS = [
    BotCommand("start", "Open PayLock"),
    BotCommand("support", "Support"),
    BotCommand("balance", "Balance"),
    BotCommand("help", "Help"),
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def webapp_url(start_param=None):
    """WEBAPP_URL, plus ?startapp=<start_param> when a valid param is given."""
    if not start_param:
        return WEBAPP_BASE
    sep = "&" if "?" in WEBAPP_BASE else "?"
    return f"{WEBAPP_BASE}{sep}startapp={start_param}"


def open_app_markup(start_param=None):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            text="Open PayLock",
            web_app=WebAppInfo(url=webapp_url(start_param)),
        )
    ]])


# -----------------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    param = (context.args[0] if context.args else "").strip()
    if param and not START_PARAM_RE.fullmatch(param):
        param = ""

    if param.startswith("prod_") and len(param) > len("prod_"):
        await msg.reply_text(
            "You opened a PayLock product link. Tap below to view it and pay "
            "safely with escrow protection.",
            reply_markup=open_app_markup(param),
        )
        return

    if param.startswith(("ref_", "inv_")):
        # Referral is handled by app.py via initData.start_param, not here.
        await msg.reply_text(WELCOME_TEXT, reply_markup=open_app_markup(param))
        return

    await msg.reply_text(WELCOME_TEXT, reply_markup=open_app_markup())


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(WELCOME_TEXT, reply_markup=open_app_markup())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "PayLock commands:\n"
        "/start - Open PayLock\n"
        "/app - Open PayLock\n"
        "/support - Contact support\n"
        "/balance - Check your balance (in the app)\n"
        "/help - Show this message"
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "Your balance is shown inside the PayLock app. Tap below to open it.",
        reply_markup=open_app_markup(),
    )


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        f"Support: @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(text="Contact support", url=SUPPORT_URL)
        ]]),
    )


async def _bot_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only guard: ADMIN_ID comes from env; empty ADMIN_ID means nobody passes.
    if update.effective_user is None or str(update.effective_user.id) != str(ADMIN_ID):
        if update.effective_message is not None:
            await update.effective_message.reply_text("Admin only")
        return
    await update.effective_message.reply_text(
        "Admin Panel - Daily activities",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(text="Admin Panel", web_app=WebAppInfo(url=WEBAPP_BASE))
        ]]),
    )


# Kept so the earlier handler name still resolves.
cmd_admin = _bot_admin


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No callback buttons are used; answer any stray one so no spinner hangs.
    if update.callback_query is not None:
        await update.callback_query.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error while processing an update", exc_info=context.error)


# -----------------------------------------------------------------------------
# Private command menu (only the admin chat sees /admin)
# -----------------------------------------------------------------------------

async def setup_private_commands(bot):
    # Default scope (all users): NO /admin.
    await bot.set_my_commands(PUBLIC_COMMANDS)
    # Admin chat only: same list plus /admin. Never set globally.
    if ADMIN_ID.isdigit():
        await bot.set_my_commands(
            PUBLIC_COMMANDS + [BotCommand("admin", "Admin Panel")],
            scope=BotCommandScopeChat(chat_id=int(ADMIN_ID)),
        )
    else:
        log.warning("ADMIN_ID is not set/numeric; no admin command menu was registered.")


# -----------------------------------------------------------------------------
# Application wiring (builds handlers only; nothing here ever polls)
# -----------------------------------------------------------------------------

def build_application():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("app", cmd_app))
    application.add_handler(CommandHandler("support", cmd_support))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("admin", _bot_admin))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_error_handler(on_error)
    return application


async def _set_commands_once():
    async with Bot(BOT_TOKEN) as bot:
        await setup_private_commands(bot)
    log.info("Command menus registered (public + admin-only).")


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

def main():
    # Polling disabled - app.py webhook handles updates now.
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is not set.")
        sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1] == "set-commands":
        try:
            asyncio.run(_set_commands_once())
        except Exception:
            log.exception("Could not register command menus")
            sys.exit(1)
        return

    log.warning(
        "Polling disabled - app.py webhook handles updates now. Nothing to run here. "
        "Use 'python bot_polling_backup.py set-commands' for a one-shot menu update."
    )


if __name__ == "__main__":
    main()
