from flask import Flask
from threading import Thread
from telegram.ext import Application, CommandHandler
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

async def start(update, context):
    await update.message.reply_text("Bot is live!")

def run_telegram():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.run_polling()

if __name__ == "__main__":
    Thread(target=run_telegram).start()
    flask_app.run(host="0.0.0.0", port=PORT)
