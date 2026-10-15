import os
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")  # your Render URL e.g https://yourapp.onrender.com

app = FastAPI()

# Track users who already got /start to prevent spam
started_users = set()

@app.get("/")
async def serve_index():
    return FileResponse("index.html")

# --- Telegram Bot ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in started_users:
        return  # block spam - reply only once
    started_users.add(user_id)
    
    # auto-clear after 2 minutes to allow again if needed
    asyncio.create_task(clear_user_later(user_id))
    
    await update.message.reply_text(
        "Welcome to PayLock 🔒\nYour escrow is secured.\n\nTap below to open app:",
        reply_markup={
            "inline_keyboard": [[
                {"text": "🚀 Open PayLock", "web_app": {"url": WEBAPP_URL}}
            ]]
        }
    )

async def clear_user_later(user_id):
    await asyncio.sleep(120)
    started_users.discard(user_id)

telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_cmd))

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()
    # set webhook or polling based on your setup
    if os.getenv("RENDER"):
        await telegram_app.bot.set_webhook(f"{WEBAPP_URL}/webhook")
    else:
        asyncio.create_task(telegram_app.updater.start_polling())

@app.post("/webhook")
async def webhook(update: dict):
    await telegram_app.process_update(Update.de_json(update, telegram_app.bot))
    return {"ok": True}
