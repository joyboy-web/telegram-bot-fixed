import os, json, uuid, shutil
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN", "123456:ABC")

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

# --- API ROUTES FIRST ---
@app.get("/health")
async def health():
    return {"ok": True, "has_index": os.path.exists(INDEX_FILE), "products": len(PRODUCTS)}

@app.post("/api/upload")
async def upload(product_name: str = Form(...), price: str = Form(...), description: str = Form(...), product_type: str = Form(...), file: UploadFile = File(...), cover_image: UploadFile = File(...)):
    tx = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    fp = os.path.join(UPLOAD_DIR, f"{tx}_FILE_{file.filename}")
    cp = os.path.join(UPLOAD_DIR, f"{tx}_COVER_{cover_image.filename}")
    with open(fp, "wb") as b: shutil.copyfileobj(file.file, b)
    with open(cp, "wb") as b: shutil.copyfileobj(cover_image.file, b)
    PRODUCTS[tx] = {"tx":tx,"name":product_name,"price":int(float(price)),"desc":description,"prodType":product_type,"file":file.filename,"cover_url":f"/uploads/{tx}_COVER_{cover_image.filename}","file_url":f"/uploads/{tx}_FILE_{file.filename}","salesCount":0,"orders":[]}
    save_db(PRODUCTS)
    return {"transaction_id":tx,"cover_url":PRODUCTS[tx]["cover_url"]}

@app.post("/api/purchase")
async def purchase(req: Request):
    j = await req.json()
    tx=j.get("tx"); order=j.get("order")
    if tx in PRODUCTS:
        PRODUCTS[tx]["orders"].append(order); PRODUCTS[tx]["salesCount"]+=1; save_db(PRODUCTS)
    return {"ok":True}

@app.get("/api/products")
async def prods(): return PRODUCTS

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- SERVE INDEX.HTML FOR ALL OTHER ROUTES ---
@app.get("/")
async def root():
    if os.path.exists(INDEX_FILE):
        return FileResponse(INDEX_FILE)
    return JSONResponse({"error":"index.html NOT FOUND in root","base_dir":BASE_DIR,"files":os.listdir(BASE_DIR)})

@app.get("/{full_path:path}")
async def catch_all(full_path: str, request: Request):
    # if api path not found, return real 404
    if full_path.startswith("api/") or full_path.startswith("uploads/") or full_path.startswith("docs"):
        return JSONResponse({"detail":"Not Found - API route missing"}, status_code=404)
    # else return index.html (fixes your screenshot)
    if os.path.exists(INDEX_FILE):
        return FileResponse(INDEX_FILE)
    return JSONResponse({"detail":"index.html missing","path":full_path})
