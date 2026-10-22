import logging
import uuid
from typing import Dict, Optional
from supabase import Client
from .fee_service import calculate_fees

logger = logging.getLogger(__name__)

def _detect_mime(b: bytes) -> str:
    if b.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if b.startswith(b"\x89PNG"): return "image/png"
    if b.startswith(b"RIFF") and b"WEBP" in b[:12]: return "image/webp"
    return "image/jpeg"

def create_product(db: Client, seller_id: str, name: str, price_kobo: int, desc: str = "", image_bytes: Optional[bytes] = None) -> Dict:
    fees = calculate_fees(price_kobo)
    pid = str(uuid.uuid4())
    image_url = None
    if image_bytes:
        try:
            mime = _detect_mime(image_bytes)
            path = f"{seller_id}/{pid}.jpg"
            db.storage.from_("product-covers").upload(path, image_bytes, {"content-type": mime, "upsert": "true"})
            image_url = db.storage.from_("product-covers").get_public_url(path)
        except Exception as e:
            logger.warning(f"image upload failed {pid}: {e}")
    row = {
        "id": pid, "seller_id": seller_id, "name": name.strip(),
        "price": price_kobo, "description": desc.strip(),
        "image_url": image_url, "buyer_pays": fees["buyer_pays"],
        "seller_gets": fees["seller_gets"], "total_fee": fees["total_fee"],
        "status": "active"
    }
    res = db.table("products").insert(row).execute()
    return {"ok": True, "data": res.data[0] if res.data else row, "fees": fees}

def list_active(db: Client) -> Dict:
    res = db.table("products").select("*").eq("status", "active").order("created_at", desc=True).execute()
    return {"ok": True, "data": res.data or []}
