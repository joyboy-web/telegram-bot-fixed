import os
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- Flask keepalive for Render ---
app = Flask(__name__)
@app.route('/')
def home():
    return "PayLock Bot is live! ✅"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- UI Builders (Matches your screenshot) ---
def get_home_screen(user_name="Alex"):
    text = f"""
🔒 **PayLock**

**Good morning, {user_name}**
Secure • Held • Protected

━━━━━━━━━━━━━━━━━━━━
**Funds in Escrow**
**₦3,850.00**
🔒 Protected until release
━━━━━━━━━━━━━━━━━━━━

**Active Escrows**

📷 **Sony A7IV Camera — ₦2,200.00**
`Awaiting Shipment`
Seller: @coastmedia • Created 2 days ago

💻 **MacBook Pro M3 — ₦1,850.00**
`Funds Held in Escrow`
Seller: @designlab • Created 5 days ago

**Recommended for You**

⌚ **Apple Watch Ultra 2 — ₦750.00**
Tap to start escrow →
"""

    keyboard = [
        [InlineKeyboardButton("➕ New", callback_data="new_escrow")],
        [
            InlineKeyboardButton("📷 Sony A7IV - Details", callback_data="escrow_sony"),
            InlineKeyboardButton("💻 MacBook - Details", callback_data="escrow_mac")
        ],
        [InlineKeyboardButton("⌚ Start Apple Watch Escrow", callback_data="escrow_watch")],
        [
            InlineKeyboardButton("🏠 Home", callback_data="home"),
            InlineKeyboardButton("🏪 Sell", callback_data="sell"),
            InlineKeyboardButton("🛍️ Buy", callback_data="buy"),
            InlineKeyboardButton("👛 Wallet", callback_data="wallet")
        ]
    ]
    return text, InlineKeyboardMarkup(keyboard)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Alex"
    text, markup = get_home_screen(name)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    name = query.from_user.first_name or "Alex"

    if data == "home":
        text, markup = get_home_screen(name)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    
    elif data == "new_escrow":
        text = "➕ **Create New Escrow**\n\nWhat do you want to escrow?\nSend item name and amount like:\n`Sony Camera - ₦2,200.00`"
        kb = [[InlineKeyboardButton("⬅️ Back to Home", callback_data="home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data == "sell":
        text = "🏪 **Sell**\n\nList an item for escrow.\nYour buyers will pay securely to PayLock.\n\nTap New to start."
        kb = [
            [InlineKeyboardButton("➕ New Listing", callback_data="new_escrow")],
            [InlineKeyboardButton("⬅️ Home", callback_data="home"), InlineKeyboardButton("👛 Wallet", callback_data="wallet")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data == "buy":
        text = "🛍️ **Buy**\n\nBrowse secure escrow deals.\n\n**Recommended:** Apple Watch Ultra 2 — ₦750.00"
        kb = [
            [InlineKeyboardButton("⌚ Buy Apple Watch", callback_data="escrow_watch")],
            [InlineKeyboardButton("⬅️ Home", callback_data="home")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data == "wallet":
        text = """👛 **Wallet**

**Funds in Escrow**
**₦3,850.00**
🔒 Protected until release

**Available Balance:** ₦0.00

Your funds are safe until both parties confirm."""
        kb = [[InlineKeyboardButton("⬅️ Back to Home", callback_data="home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data.startswith("escrow_"):
        if "sony" in data:
            text = "📷 **Sony A7IV Camera — ₦2,200.00**\nStatus: `Awaiting Shipment`\nSeller: @coastmedia\n\nFunds are protected until you confirm delivery."
        elif "mac" in data:
            text = "💻 **MacBook Pro M3 — ₦1,850.00**\nStatus: `Funds Held in Escrow`\nSeller: @designlab"
        else:
            text = "⌚ **Apple Watch Ultra 2 — ₦750.00**\nTap Confirm to create escrow."
        kb = [
            [InlineKeyboardButton("✅ Confirm Release", callback_data="confirm"), InlineKeyboardButton("❌ Dispute", callback_data="dispute")],
            [InlineKeyboardButton("⬅️ Home", callback_data="home")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# --- Main ---
if __name__ == "__main__":
    # Start Flask in background
    Thread(target=run_flask, daemon=True).start()
    
    print("Starting Telegram polling...")
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(button_handler))
    tg_app.run_polling()
