import os, json, uuid, base64, io, requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from supabase import create_client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

SUPABASE_URL=os.getenv("SUPABASE_URL")
SUPABASE_KEY=os.getenv("SUPABASE_KEY")
BUCKET=os.getenv("SUPABASE_BUCKET","paylock-files")
FOLDER_ID=os.getenv("GOOGLE_DRIVE_FOLDER_ID") or os.getenv("GDRIVE_FOLDER_ID")
CREDS=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_CREDENTIALS")
BOT_TOKEN=os.getenv("BOT_TOKEN")
RENDER_URL=os.getenv("RENDER_EXTERNAL_URL","https://telegram-bot-fixed-uqmt.onrender.com").rstrip("/")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

def get_drive():
    if not CREDS or not FOLDER_ID: return None
    try:
        s=CREDS.strip()
        info=json.loads(s) if s.startswith("{") else json.loads(base64.b64decode(s).decode())
        creds=service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
        return build('drive','v3',credentials=creds)
    except Exception as e:
        print(f"Drive init error: {e}"); return None

async def upload_core(b:bytes, filename:str, mime:str):
    tid=f"PAY-{uuid.uuid4().hex[:8].upper()}"
    supa_url=None; drive_url=None
    try:
        path=f"{tid}/{filename}"
        supabase.storage.from_(BUCKET).upload(path,b,file_options={"content-type":mime,"upsert":"true"})
        supa_url=supabase.storage.from_(BUCKET).get_public_url(path)
    except Exception as e: print(f"supa {e}")
    try:
        svc=get_drive()
        if svc:
            meta={'name':f"{tid}_{filename}",'parents':[FOLDER_ID]}
            media=MediaIoBaseUpload(io.BytesIO(b),mimetype=mime)
            up=svc.files().create(body=meta,media_body=media,fields='id,webViewLink').execute()
            drive_url=up.get('webViewLink')
            try: svc.permissions().create(fileId=up['id'],body={'role':'reader','type':'anyone'}).execute()
            except: pass
    except Exception as e: print(f"drive {e}")
    return tid,supa_url,drive_url

# Telegram setup
telegram_app = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None

async def tg_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton("🔓 Open PayLock App", web_app=WebAppInfo(url=f"{RENDER_URL}/"))]]
    await update.message.reply_text(f"Welcome {update.effective_user.first_name}!\nSecure • Held • Protected", reply_markup=InlineKeyboardMarkup(kb))

async def tg_file(update:Update, context:ContextTypes.DEFAULT_TYPE):
    try:
        if update.message.document:
            f=await context.bot.get_file(update.message.document.file_id); fname=update.message.document.file_name
        elif update.message.photo:
            f=await context.bot.get_file(update.message.photo[-1].file_id); fname=f"photo_{uuid.uuid4().hex[:4]}.jpg"
        elif update.message.video:
            f=await context.bot.get_file(update.message.video.file_id); fname="video.mp4"
        else: return
        await update.message.reply_text("⏳ Uploading to PayLock...")
        b=bytes(await f.download_as_bytearray())
        tid,supa,drive=await upload_core(b,fname,"application/octet-stream")
        msg=f"✅ Uploaded!\nID: `{tid}`\n"
        if supa: msg+=f"\n☁️ {supa}"
        if drive: msg+=f"\n📁 {drive}"
        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"Error {e}")

if telegram_app:
    telegram_app.add_handler(CommandHandler("start", tg_start))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, tg_file))

@asynccontextmanager
async def lifespan(app: FastAPI):
    if telegram_app:
        await telegram_app.initialize()
        await telegram_app.start()
    yield
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

app = FastAPI(title="PayLock FINAL", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
def serve_root():
    for p in ["index.html","static/index.html"]:
        if os.path.exists(p):
            with open(p) as f: return f.read()
    return "<h1>PayLock Live - index.html not found</h1>"

@app.get("/app", response_class=HTMLResponse)
def serve_app(): return serve_root()

@app.get("/health")
def health(): return {"status":"ok","drive":get_drive() is not None,"supabase":supabase is not None,"bucket":BUCKET}

@app.post("/api/upload")
async def api_upload(file: UploadFile=File(...), product_name:str=Form(""), price:str=Form(""), description:str=Form("")):
    data=await file.read()
    tid,supa,drive=await upload_core(data,file.filename,file.content_type or "application/octet-stream")
    return {"status":"success","transaction_id":tid,"supabase_url":supa,"preview_url":supa or drive,"drive_url":drive}

@app.post("/webhook")
async def webhook(req:Request):
    if not telegram_app: return {"error":"no token"}
    data=await req.json()
    update=Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok":True}

@app.get("/setwebhook")
def sethook():
    if not BOT_TOKEN: return {"error":"BOT_TOKEN missing"}
    r=requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={RENDER_URL}/webhook")
    return r.json()
