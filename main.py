import os, json, uuid, shutil
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
DB_FILE = os.path.join(UPLOAD_DIR, "products_db.json")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE) as f: return json.load(f)
        except: return {}
    return {}
def save_db(db):
    with open(DB_FILE, "w") as f: json.dump(db, f)

PRODUCTS = load_db()

@app.get("/")
async def root(): return FileResponse(os.path.join(BASE_DIR, "index.html"))

@app.post("/api/upload")
async def upload(product_name: str = Form(...), price: str = Form(...), description: str = Form(...), product_type: str = Form(...), file: UploadFile = File(...), cover_image: UploadFile = File(...)):
    tx = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    fp = os.path.join(UPLOAD_DIR, f"{tx}_file_{file.filename}")
    cp = os.path.join(UPLOAD_DIR, f"{tx}_cover_{cover_image.filename}")
    with open(fp, "wb") as b: shutil.copyfileobj(file.file, b)
    with open(cp, "wb") as b: shutil.copyfileobj(cover_image.file, b)
    PRODUCTS[tx] = {"tx":tx,"name":product_name,"price":int(price),"desc":description,"prodType":product_type,"file":file.filename,"cover_url":f"/uploads/{tx}_cover_{cover_image.filename}","file_url":f"/uploads/{tx}_file_{file.filename}","salesCount":0,"orders":[]}
    save_db(PRODUCTS)
    return {"transaction_id":tx,"cover_url":PRODUCTS[tx]["cover_url"],"preview_url":PRODUCTS[tx]["file_url"]}

@app.post("/api/purchase")
async def purchase(req: Request):
    j = await req.json(); tx=j.get("tx"); order=j.get("order")
    if tx in PRODUCTS:
        PRODUCTS[tx]["orders"].append(order); PRODUCTS[tx]["salesCount"]+=1; save_db(PRODUCTS)
    return {"ok":True}

@app.get("/api/products")
async def list_products(): return PRODUCTS

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

@app.get("/setwebhook")
async def set_webhook(req: Request):
    url = str(req.base_url).rstrip("/") + "/webhook"
    async with httpx.AsyncClient() as c:
        r = await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
        return r.json()

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    async with httpx.AsyncClient() as client:
        if "message" in data:
            msg=data["message"]; chat_id=msg["chat"]["id"]; text=msg.get("text","")
            if text.startswith("/start"):
                param=text.split(" ",1)[1] if " " in text else ""
                if param.startswith("PAY-") and param in PRODUCTS:
                    p=PRODUCTS[param]
                    txt=f"🔒 Confirm Purchase\n\nName: {p['name']}\nGenre/Type: {p['prodType']}\nDescription: {p['desc'][:400]}\nPrice: ₦{p['price']:,}\n\n24hrs escrow hold. Continue?"
                    kb={"inline_keyboard":[[{"text":f"💳 Buy - ₦{p['price']:,}", "callback_data":f"buy_{param}"}]]}
                    await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":chat_id,"text":txt,"reply_markup":kb})
                else:
                    kb={"inline_keyboard":[[{"text":"🛍️ Open PayLock","web_app":{"url":str(req.base_url)}}]]}
                    await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":chat_id,"text":"Welcome to PayLock 🔒","reply_markup":kb})
        if "callback_query" in data:
            cb=data["callback_query"]; chat_id=cb["message"]["chat"]["id"]; d=cb["data"]; cid=cb["id"]
            if d.startswith("buy_"):
                tx=d.replace("buy_","")
                if tx in PRODUCTS:
                    PRODUCTS[tx]["salesCount"]+=1; PRODUCTS[tx]["orders"].append({"id":f"ORD-{uuid.uuid4().hex[:4].upper()}","status":"held"}); save_db(PRODUCTS)
                    await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id":cid,"text":"✅ Paid"})
                    await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":chat_id,"text":f"✅ Paid {PRODUCTS[tx]['name']} - ₦{PRODUCTS[tx]['price']:,}"})
    return JSONResponse({"ok":True})
