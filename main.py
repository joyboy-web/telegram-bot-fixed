import os, json, uuid, base64, io, requests, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

BOT_USERNAME = "PayLockOfficialBot"
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL","https://telegram-bot-fixed-uqmt.onrender.com").rstrip("/")
SUPABASE_URL=os.getenv("SUPABASE_URL"); SUPABASE_KEY=os.getenv("SUPABASE_KEY")
BUCKET=os.getenv("SUPABASE_BUCKET","paylock-files")
FOLDER_ID=os.getenv("GOOGLE_DRIVE_FOLDER_ID") or os.getenv("GDRIVE_FOLDER_ID")
CREDS=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_CREDENTIALS")
BOT_TOKEN=os.getenv("BOT_TOKEN")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
ORDERS_DB = {}

def get_drive():
    if not CREDS or not FOLDER_ID: return None
    try:
        s=CREDS.strip(); info=json.loads(s) if s.startswith("{") else json.loads(base64.b64decode(s).decode())
        creds=service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
        return build('drive','v3',credentials=creds)
    except Exception as e: print(e); return None

async def upload_core(b:bytes, filename:str, mime:str):
    tid=f"PAY-{uuid.uuid4().hex[:8].upper()}"; supa_url=None; drive_url=None
    try:
        if supabase:
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

telegram_app = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None

async def tg_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    product_id = context.args[0] if context.args else None
    url = f"{RENDER_URL}/?product={product_id}" if product_id else f"{RENDER_URL}/"
    text = f"🔒 Product `{product_id}` found!\nOpening dashboard..." if product_id else f"Welcome {update.effective_user.first_name} to PayLock!\nSecure • Held • Protected"
    kb=[[InlineKeyboardButton("🔓 Open PayLock App", web_app=WebAppInfo(url=url))]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def tg_file(update:Update, context:ContextTypes.DEFAULT_TYPE):
    try:
        if update.message.document: f=await context.bot.get_file(update.message.document.file_id); fname=update.message.document.file_name
        elif update.message.photo: f=await context.bot.get_file(update.message.photo[-1].file_id); fname=f"photo_{uuid.uuid4().hex[:4]}.jpg"
        else: return
        await update.message.reply_text("⏳ Uploading...")
        b=bytes(await f.download_as_bytearray()); tid,supa,drive=await upload_core(b,fname,"application/octet-stream")
        await update.message.reply_text(f"✅ Uploaded!\nID: `{tid}`\nLink: https://t.me/{BOT_USERNAME}?start={tid}", parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e: await update.message.reply_text(f"Error {e}")

if telegram_app:
    telegram_app.add_handler(CommandHandler("start", tg_start))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, tg_file))

@asynccontextmanager
async def lifespan(app: FastAPI):
    if telegram_app: await telegram_app.initialize(); await telegram_app.start()
    yield
    if telegram_app: await telegram_app.stop(); await telegram_app.shutdown()

app = FastAPI(title="PayLock FINAL BOSS", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
def serve_root():
    for p in ["index.html","static/index.html"]:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f: return f.read()
    return "<h1>PayLock Live</h1>"

@app.get("/app", response_class=HTMLResponse)
def serve_app(): return serve_root()
@app.get("/p/{tx_id}", response_class=HTMLResponse)
def pay_page(tx_id:str): return HTMLResponse(f'<html><head><meta http-equiv="refresh" content="0; url=https://t.me/{BOT_USERNAME}?start={tx_id}"></head><body>Redirecting to @PayLockOfficialBot...</body></html>')
@app.get("/health")
def health(): return {"status":"ok","bot":BOT_USERNAME,"drive":get_drive() is not None,"supabase":supabase is not None,"bucket":BUCKET,"solver":"active"}

@app.post("/api/upload")
async def api_upload(file: UploadFile=File(...), cover_image: UploadFile=File(None), product_name:str=Form(""), price:str=Form(""), description:str=Form(""), product_type:str=Form("")):
    data=await file.read(); tid,supa,drive=await upload_core(data,file.filename,file.content_type or "application/octet-stream")
    cover_url=None
    if cover_image: cdata=await cover_image.read(); _,cover_url,_=await upload_core(cdata,cover_image.filename,cover_image.content_type or "image/jpeg")
    now=int(time.time())
    return {"status":"success","transaction_id":tid,"supabase_url":supa,"preview_url":supa or drive,"drive_url":drive,"cover_url":cover_url,"paylink":f"https://t.me/{BOT_USERNAME}?start={tid}","webapp_link":f"https://t.me/{BOT_USERNAME}/app?startapp={tid}","created_at":now,"release_at":now+86400,"escrow_status":"held","escrow_policy":"Funds held 24hrs for refund"}

@app.post("/api/purchase")
async def purchase(req:Request):
    data=await req.json(); tx=data.get("tx"); order=data.get("order")
    if not tx: return {"error":"missing tx"}
    ORDERS_DB.setdefault(tx, []).append(order)
    total=len(ORDERS_DB[tx])
    return {"status":"success","sales_count":total,"orders":ORDERS_DB[tx]}

@app.get("/api/stats/{tx_id}")
def stats(tx_id:str): return {"tx":tx_id,"sales_count":len(ORDERS_DB.get(tx_id,[])),"orders":ORDERS_DB.get(tx_id,[])}

@app.post("/webhook")
async def webhook(req:Request):
    if not telegram_app: return JSONResponse({"error":"no token"}, status_code=500)
    data=await req.json(); update=Update.de_json(data, telegram_app.bot); await telegram_app.process_update(update); return {"ok":True}

@app.get("/setwebhook")
def sethook():
    if not BOT_TOKEN: return {"error":"BOT_TOKEN missing"}
    r=requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={RENDER_URL}/webhook"); return r.json()
