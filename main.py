import os, json, uuid, shutil
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
DB_FILE = os.path.join(UPLOAD_DIR, "products_db.json")

app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f: return json.load(f)
        except: return {}
    return {}
def save_db(db):
    with open(DB_FILE, "w") as f: json.dump(db, f)

PRODUCTS = load_db()

@app.get("/")
async def root():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

# THIS IS THE ROUTE WEY DEY MISS - NOW FIXED
@app.post("/api/upload")
async def upload(
    product_name: str = Form(...),
    price: str = Form(...),
    description: str = Form(...),
    product_type: str = Form(...),
    file: UploadFile = File(...),
    cover_image: UploadFile = File(...)
):
    tx = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    file_path = os.path.join(UPLOAD_DIR, f"{tx}_FILE_{file.filename}")
    cover_path = os.path.join(UPLOAD_DIR, f"{tx}_COVER_{cover_image.filename}")
    with open(file_path, "wb") as b: shutil.copyfileobj(file.file, b)
    with open(cover_path, "wb") as b: shutil.copyfileobj(cover_image.file, b)
    PRODUCTS[tx] = {
        "tx": tx, "name": product_name, "price": int(float(price)),
        "desc": description, "prodType": product_type,
        "file": file.filename, "cover_url": f"/uploads/{tx}_COVER_{cover_image.filename}",
        "file_url": f"/uploads/{tx}_FILE_{file.filename}",
        "salesCount": 0, "orders": []
    }
    save_db(PRODUCTS)
    return {"transaction_id": tx, "cover_url": PRODUCTS[tx]["cover_url"]}

@app.post("/api/purchase")
async def purchase(req: Request):
    j = await req.json()
    tx = j.get("tx")
    order = j.get("order")
    if tx in PRODUCTS:
        PRODUCTS[tx]["orders"].append(order)
        PRODUCTS[tx]["salesCount"] += 1
        save_db(PRODUCTS)
    return {"ok": True}

@app.get("/api/products")
async def get_products():
    return PRODUCTS

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

@app.get("/setwebhook")
async def set_webhook(request: Request):
    base = str(request.base_url).rstrip("/")
    url = f"{base}/webhook"
    async with httpx.AsyncClient() as c:
        r = await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
        return r.json()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    async with httpx.AsyncClient() as client:
        if "message" in data:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"].get("text","")
            if text.startswith("/start"):
                kb = {"inline_keyboard": [[{"text": "🛍️ Open PayLock", "web_app": {"url": str(request.base_url)}}]]}
                await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": "Welcome to PayLock 🔒\nSecure • Held • Protected", "reply_markup": kb})
    return JSONResponse({"ok": True})
