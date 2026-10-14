import os, json, uuid, shutil
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN", "123456:ABC")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://telegram-bot-fixed-uqmt.onrender.com")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
DB_FILE = os.path.join(UPLOAD_DIR, "db.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE) as f: return json.load(f)
        except: return {}
    return {}
def save_db(d):
    with open(DB_FILE, "w") as f: json.dump(d, f)

PRODUCTS = load_db()

# --- WELCOME MESSAGE FOR FIRST TIME USERS ---
WELCOME_TEXT = (
    "Welcome to PayLock 🔒\n\n"
    "Secure Escrow Marketplace for Naija 🇳🇬\n\n"
    "🛒 Buy safely — money dey hold until you confirm\n"
    "💰 Sell fast — get paid instantly after buyer confirms\n"
    "🛡️ 100% escrow protection • 24hrs delivery\n\n"
    "Tap 🛒 Open PayLock below to start!"
)

async def send_telegram_message(chat_id, text, reply_markup=None):
    if not BOT_TOKEN or BOT_TOKEN.startswith("123456"): return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json=payload)

async def handle_telegram_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg: return
    chat_id = msg["chat"]["id"]
    text = msg.get("text","")
    if text.startswith("/start"):
        parts = text.split()
        tx = parts[1] if len(parts)>1 else None
        if tx and tx in PRODUCTS:
            p = PRODUCTS[tx]
            prod_text = f"🔒 <b>{p['name']}</b>\n\n💰 ₦{p['price']:,}\n📦 {p['prodType']}\n\nTap below to buy securely 👇"
            kb = {"inline_keyboard": [[{"text": "🛒 Open & Buy Now", "web_app": {"url": f"{WEBHOOK_URL}/"}}]]}
            await send_telegram_message(chat_id, prod_text, kb)
        else:
            kb = {"inline_keyboard": [[{"text": "🛒 Open PayLock", "web_app": {"url": f"{WEBHOOK_URL}/"}}]]}
            await send_telegram_message(chat_id, WELCOME_TEXT, kb)

@app.post("/webhook")
@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(req: Request):
    try:
        update = await req.json()
        await handle_telegram_update(update)
    except: pass
    return {"ok": True}

@app.get("/health")
async def health(): return {"ok": True, "products": len(PRODUCTS)}

@app.post("/api/upload")
async def upload(product_name: str = Form(...), price: str = Form(...), description: str = Form(...), product_type: str = Form(...), file: UploadFile = File(...), cover_image: UploadFile = File(...)):
    tx = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    fp = os.path.join(UPLOAD_DIR, f"{tx}_FILE_{file.filename}")
    cp = os.path.join(UPLOAD_DIR, f"{tx}_COVER_{cover_image.filename}")
    with open(fp, "wb") as b: shutil.copyfileobj(file.file, b)
    with open(cp, "wb") as b: shutil.copyfileobj(cover_image.file, b)
    PRODUCTS[tx] = {"tx":tx,"name":product_name,"price":int(float(price)),"desc":description,"prodType":product_type,"file":file.filename,"cover_url":f"/uploads/{tx}_COVER_{cover_image.filename}","file_url":f"/uploads/{tx}_FILE_{file.filename}","salesCount":0,"orders":[],"sellerContact":f"SELL-{uuid.uuid4().hex[:6].upper()}","sellerUsername":f"seller_{tx[-4:]}"}
    save_db(PRODUCTS)
    return {"transaction_id":tx,"cover_url":PRODUCTS[tx]["cover_url"]}

@app.post("/api/purchase")
async def purchase(req: Request):
    j = await req.json()
    tx=j.get("tx"); order=j.get("order")
    if tx in PRODUCTS:
        PRODUCTS[tx]["orders"].append(order); PRODUCTS[tx]["salesCount"]+=1; save_db(PRODUCTS)
    return {"ok":True, "sellerContact": PRODUCTS.get(tx,{}).get("sellerContact"), "file_url": PRODUCTS.get(tx,{}).get("file_url")}

@app.get("/api/products")
async def prods(): return PRODUCTS

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

@app.get("/")
async def root():
    if os.path.exists(INDEX_FILE): return FileResponse(INDEX_FILE)
    return JSONResponse({"error":"index.html NOT FOUND"})

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("uploads/") or full_path.startswith("webhook"): return JSONResponse({"detail":"Not Found"}, status_code=404)
    if os.path.exists(INDEX_FILE): return FileResponse(INDEX_FILE)
    return JSONResponse({"detail":"index.html missing"})
