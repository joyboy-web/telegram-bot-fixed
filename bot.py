# PayLock Bot - thin wrapper, all logic in app.py
# requirements: python-telegram-bot>=20.0, python-dotenv
# Run (Render worker): python bot.py   (long polling, no webhook)

import os
import re
import sys
import logging

from dotenv import load_dotenv
from telegram import (
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


def _is_admin(update):
    user = update.effective_user
    return bool(ADMIN_ID) and user is not None and str(user.id) == ADMIN_ID


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


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    if not _is_admin(update):
        await msg.reply_text("Forbidden.")
        return
    await msg.reply_text(
        "PayLock admin panel. Open the app and use Admin Panel in the Wallet tab.",
        reply_markup=open_app_markup(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No callback buttons are used; answer any stray one so no spinner hangs.
    if update.callback_query is not None:
        await update.callback_query.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error while processing an update", exc_info=context.error)


async def post_init(application: Application):
    me = await application.bot.get_me()
    log.info("PayLock bot started as @%s", me.username)


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is not set. Not starting the bot.")
        sys.exit(1)

    if not WEBAPP_URL:
        log.warning(
            "WEBAPP_URL is not set; falling back to %s. Set WEBAPP_URL to your "
            "deployed https URL so 'Open PayLock' opens the Mini App.",
            SUPPORT_URL,
        )

    try:
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(post_init)
            .build()
        )
        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(CommandHandler("help", cmd_help))
        application.add_handler(CommandHandler("app", cmd_app))
        application.add_handler(CommandHandler("support", cmd_support))
        application.add_handler(CommandHandler("balance", cmd_balance))
        application.add_handler(CommandHandler("admin", cmd_admin))
        application.add_handler(CallbackQueryHandler(on_callback))
        application.add_error_handler(on_error)

        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        log.exception("PayLock bot crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
