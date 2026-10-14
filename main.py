import os, time, logging
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://telegram-bot-fixed-uqmt.onrender.com").rstrip("/")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
logging.basicConfig(level=logging.INFO)

@app.get("/")
async def serve_index(request: Request):
    try:
        path = "index.html"
        if not os.path.exists(path):
            path = os.path.join(os.path.dirname(__file__), "index.html")
        resp = FileResponse(path, media_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    except Exception as e:
        logging.error(f"serve error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/health")
async def health():
    return {"status":"ok","ts":int(time.time())}

# Only init bot if token exists
tg_app = None
if BOT_TOKEN:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from telegram.ext import Application, CommandHandler, ContextTypes
    tg_app = Application.builder().token(BOT_TOKEN).build()
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        version = int(time.time())
        webapp_url = f"{WEBHOOK_URL}/?v={version}&uid={update.effective_user.id}"
        kb = [[InlineKeyboardButton("🔒 Open PayLock", web_app=WebAppInfo(url=webapp_url))]]
        await update.message.reply_text(f"🔒 PayLock — Secure Escrow\n\nHi {update.effective_user.first_name}!\nTap below (v{version}):", reply_markup=InlineKeyboardMarkup(kb))
    tg_app.add_handler(CommandHandler("start", start))

    @app.post("/webhook")
    async def webhook(request: Request):
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}

    @app.on_event("startup")
    async def on_startup():
        await tg_app.initialize()
        await tg_app.start()
        if WEBHOOK_URL:
            try:
                await tg_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
                logging.info("Webhook set")
            except Exception as e:
                logging.error(f"webhook error {e}")

    @app.on_event("shutdown")
    async def on_shutdown():
        await tg_app.stop()
        await tg_app.shutdown()
else:
    @app.post("/webhook")
    async def webhook(request: Request):
        return {"error":"BOT_TOKEN not set"}
