from flask import Flask
from threading import Thread
from telegram.ext import Application, CommandHandler
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "PayLock Bot is running!"

async def start(update, context):
    await update.message.reply_text("PayLock Bot is live! ✅")

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    # Flask in background thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Telegram in MAIN thread (fixes set_wakeup_fd error)
    print("Starting Telegram polling...")
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.run_polling()
