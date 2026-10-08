import os
from flask import Flask, send_from_directory
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL") or "https://telegram-bot-fixed-uqmt.onrender.com"

app = Flask(__name__, static_folder=".")

@app.route('/')
def root():
    return send_from_directory('.', 'index.html')

@app.route('/app')
def mini_app():
    return send_from_directory('.', 'index.html')

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    text = f"""
🔒 *PayLock — Secure Escrow on Telegram*

Hello {name},

*Connecting buyers and sellers, bridging the gap with trust. Building confidence one transaction at a time.*

PayLock protects your money until you get what you paid for. No more scams, no more stories.

*Why PayLock?*
• 🔒 Bank-grade escrow protection
• 🤝 Verified buyers & sellers
• ⚡ Instant release after delivery
• 🇳🇬 100% Naira (₦) transactions

*How it works:*
1. Buyer funds escrow
2. Seller ships item
3. Buyer confirms delivery
4. Funds released securely

Tap Home to enter your secure dashboard 👇
"""
    keyboard = [
        [InlineKeyboardButton("🏠 Open Home Dashboard", web_app=WebAppInfo(url=f"{RENDER_URL}/app"))],
        [
            InlineKeyboardButton("🏪 Sell", callback_data="sell"),
            InlineKeyboardButton("🛍️ Buy", callback_data="buy"),
            InlineKeyboardButton("👛 Wallet", callback_data="wallet")
        ]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "sell":
        await query.message.reply_text("🏪 Sell: Open Home Dashboard and tap + New to list your item for escrow.")
    elif query.data == "buy":
        await query.message.reply_text("🛍️ Buy: All verified deals are inside your Home Dashboard.")
    elif query.data == "wallet":
        await query.message.reply_text("👛 Wallet: Your protected balance is ₦3,850.00 - Open Home for full breakdown.")

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(button_handler))
    tg_app.run_polling()
