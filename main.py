import os, json, uuid, shutil
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from supabase import create_client, Client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = FastAPI(title="PayLock Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ENV
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "paylock-files")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY else None

drive_service = None
try:
    creds_str = os.getenv("GOOGLE_DRIVE_CREDENTIALS_JSON")
    if creds_str:
        info = json.loads(creds_str)
        creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive"])
        drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    print("Drive init error:", e)

# ===== PERMANENT CONFLICT SOLVER (YOUR OLD ENGINE - PRESERVED) =====
transaction_memory = set()
def permanent_conflict_solver(transaction_id, amount, reference):
    key = f"{transaction_id}_{amount}_{reference}".lower().strip()
    if key in transaction_memory:
        return {"status": "conflict", "msg": "Duplicate blocked by solver"}
    if supabase:
        try:
            res = supabase.table("paylock_transactions").select("id").eq("unique_key", key).execute()
            if res.data: return {"status": "conflict", "msg": "Already exists in DB"}
        except: pass
    transaction_memory.add(key)
    return {"status": "clear", "unique_key": key}

def save_record(data):
    if supabase:
        try: supabase.table("paylock_transactions").insert(data).execute()
        except Exception as e: print("DB save fail:", e)

@app.get("/health")
def health():
    return {"drive": bool(drive_service), "supabase": bool(supabase), "bucket": SUPABASE_BUCKET, "solver": "active"}

# MAIN UPLOAD ENDPOINT FOR YOUR UX
@app.post("/api/upload")
async def upload_api(
    file: UploadFile = File(...),
    product_name: str = Form(...),
    price: str = Form(...),
    description: str = Form("")
):
    transaction_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    reference = product_name
    
    solver = permanent_conflict_solver(transaction_id, price, reference)
    if solver["status"] == "conflict":
        raise HTTPException(status_code=409, detail=solver["msg"])
    
    tmp = f"/tmp/{uuid.uuid4()}_{file.filename}"
    with open(tmp, "wb") as b: shutil.copyfileobj(file.file, b)

    drive_link = None
    supabase_link = None

    # Drive
    try:
        if drive_service and GOOGLE_DRIVE_FOLDER_ID:
            meta = {'name': f"{transaction_id}_{file.filename}", 'parents': [GOOGLE_DRIVE_FOLDER_ID]}
            media = MediaFileUpload(tmp, resumable=True)
            up = drive_service.files().create(body=meta, media_body=media, fields='id, webViewLink').execute()
            drive_link = up.get('webViewLink')
            drive_service.permissions().create(fileId=up.get('id'), body={'type':'anyone','role':'reader'}).execute()
    except Exception as e: print("Drive err:", e)

    # Supabase
    try:
        if supabase:
            path = f"{transaction_id}/{file.filename}"
            with open(tmp, "rb") as f:
                supabase.storage.from_(SUPABASE_BUCKET).upload(path, f, {"content-type": file.content_type, "upsert":"true"})
            supabase_link = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path)
    except Exception as e: print("Supabase err:", e)

    if os.path.exists(tmp): os.remove(tmp)

    save_record({
        "unique_key": solver["unique_key"],
        "transaction_id": transaction_id,
        "product_name": product_name,
        "amount": price,
        "reference": reference,
        "description": description,
        "filename": file.filename,
        "drive_link": drive_link,
        "supabase_link": supabase_link,
        "created_at": datetime.utcnow().isoformat()
    })

    return {"status":"success", "transaction_id": transaction_id, "drive_url": drive_link, "supabase_url": supabase_link, "preview_url": supabase_link or drive_link}

# Serve index.html
@app.get("/")
def serve_index():
    if os.path.exists("index.html"): return FileResponse("index.html")
    return {"message": "PayLock API Live - index.html not found in root"}
