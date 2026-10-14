import os, time, logging, asyncio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://telegram-bot-fixed-uqmt.onrender.com").rstrip("/")
BOT_USERNAME = os.getenv("BOT_USERNAME", "PayLockOfficialBot").replace("@","")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
logging.basicConfig(level=logging.INFO)
processed_updates = set()
update_lock = asyncio.Lock()

@app.get("/")
async def index():
    for p in ["index.html", os.path.join(os.path.dirname(__file__), "index.html")]:
        if os.path.exists(p):
            r = FileResponse(p, media_type="text/html")
            r.headers["Cache-Control"] = "no-store"
            return r
    return JSONResponse({"error":"index.html missing"})

@app.get("/health")
async def health(): return {"status":"ok","has_token":bool(BOT_TOKEN),"webhook":WEBHOOK_URL,"bot":BOT_USERNAME}

@app.get("/api/config")
async def config():
    return {"botUsername": BOT_USERNAME or "PayLockOfficialBot", "webhookUrl": WEBHOOK_URL}

tg_app = None
try:
    if BOT_TOKEN:
        from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        from telegram.ext import Application, CommandHandler, ContextTypes
        tg_app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            async with update_lock:
                if update.update_id in processed_updates: return
                processed_updates.add(update.update_id)
            v=int(time.time())
            kb=[[InlineKeyboardButton("🔒 Open PayLock", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/?v={v}"))]]
            await update.message.reply_text(f"PayLock • Secure Escrow • v{v}", reply_markup=InlineKeyboardMarkup(kb))
        tg_app.add_handler(CommandHandler("start", start))
        @app.post("/webhook")
        async def webhook(req: Request):
            data=await req.json()
            update=Update.de_json(data, tg_app.bot)
            await tg_app.process_update(update)
            return {"ok":True}
        @app.on_event("startup")
        async def startup():
            await tg_app.initialize(); await tg_app.start()
            await tg_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook", drop_pending_updates=True)
        @app.on_event("shutdown")
        async def shutdown():
            await tg_app.stop(); await tg_app.shutdown()
except Exception as e:
    logging.error(e)
