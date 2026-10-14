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
    path = "index.html"
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(__file__), "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "index.html not found", "dir": os.listdir(".")})
    resp = FileResponse(path, media_type="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

@app.get("/health")
async def health():
    return {"status": "ok", "has_token": bool(BOT_TOKEN), "webhook": WEBHOOK_URL}

# ---- TELEGRAM SETUP WITH TRY/EXCEPT (CRASH-PROOF) ----
tg_app = None
try:
    if BOT_TOKEN:
        from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        from telegram.ext import Application, CommandHandler, ContextTypes

        tg_app = Application.builder().token(BOT_TOKEN).build()

        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            version = int(time.time())
            webapp_url = f"{WEBHOOK_URL}/?v={version}"
            kb = [[InlineKeyboardButton("🔒 Open PayLock", web_app=WebAppInfo(url=webapp_url))]]
            await update.message.reply_text(f"PayLock v{version} - Tap to open", reply_markup=InlineKeyboardMarkup(kb))

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
            await tg_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
            logging.info(f"Webhook set to {WEBHOOK_URL}/webhook")

        @app.on_event("shutdown")
        async def on_shutdown():
            await tg_app.stop()
            await tg_app.shutdown()
    else:
        @app.post("/webhook")
        async def webhook(request: Request):
            return JSONResponse({"error": "BOT_TOKEN not set"})

except ModuleNotFoundError as e:
    logging.error(f"python-telegram-bot not installed: {e} - App will still run without bot")
    
    @app.post("/webhook")
    async def webhook(request: Request):
        return JSONResponse({"error": "python-telegram-bot not installed", "fix": "Add python-telegram-bot to requirements.txt and redeploy"}, status_code=500)
