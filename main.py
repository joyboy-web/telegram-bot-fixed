import os
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()  # e.g https://your-app.onrender.com

app = FastAPI()

# Prevent /start spam
started_users = set()

async def clear_user_later(uid):
    await asyncio.sleep(120)
    started_users.discard(uid)

# Telegram handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in started_users:
        return
    started_users.add(uid)
    asyncio.create_task(clear_user_later(uid))
    
    url = WEBAPP_URL if WEBAPP_URL else "https://t.me"
    keyboard = {
        "inline_keyboard": [[
            {"text": "🚀 Open PayLock", "web_app": {"url": url}}
        ]]
    }
    await update.message.reply_text(
        "Welcome to PayLock 🔒\nEscrow Secured\n\nTap to open app:",
        reply_markup=keyboard
    )

# Build bot app
telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_cmd))

@app.get("/")
async def serve_index():
    return FileResponse("index.html")

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()
    # Only set webhook if URL is valid and contains https
    if WEBAPP_URL and WEBAPP_URL.startswith("https://"):
        try:
            await telegram_app.bot.set_webhook(f"{WEBAPP_URL}/webhook")
            print(f"Webhook set to {WEBAPP_URL}/webhook")
        except Exception as e:
            print(f"Webhook failed (will use polling fallback): {e}")
    else:
        print("WEBAPP_URL not set or invalid - skipping webhook, using polling mode")

@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()

# Render needs this to bind port
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
