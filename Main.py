from flask import Flask
from threading import Thread
from telegram.ext import Application, CommandHandler
import os
import asyncio

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "PayLock Bot is running!"

async def start(update, context):
    await update.message.reply_text("PayLock Bot is live! ✅")

def run_telegram():
    # Fix for threading on Render
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    print("Starting Telegram polling...")
    application.run_polling()

if __name__ == "__main__":
    # Telegram in background
    tg_thread = Thread(target=run_telegram)
    tg_thread.daemon = True
    tg_thread.start()
    
    # Flask in main thread (Render needs this)
    print(f"Starting Flask on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)
