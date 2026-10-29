"""
services/product_service.py

Rewritten against the LOCKED products schema:

  id, seller_id, name, description, price_kobo, buyer_pays_kobo,
  seller_gets_kobo, platform_fee_kobo, paystack_fee_kobo,
  image_url, file_url, category, created_at

Never inserts/selects: price, status, total_fee, is_public.

Call signature of create_product() is kept backward-compatible with
the previous version (db, seller_id, name, price_kobo, desc, image_bytes)
— file_url and category are added as optional trailing kwargs so any
existing positional caller keeps working unchanged.
"""

import logging
import uuid
from typing import Dict, Optional
from supabase import Client
from .fee_service import calculate_fees

logger = logging.getLogger(__name__)

# Explicit column list. Never '*', never 'is_public', never 'status'.
_PRODUCT_COLUMNS_FULL = (
    "id,seller_id,name,description,price_kobo,buyer_pays_kobo,"
    "seller_gets_kobo,platform_fee_kobo,paystack_fee_kobo,"
    "image_url,file_url,category,created_at"
)

# Fallback used only if category/file_url aren't present in some environment.
_PRODUCT_COLUMNS_FALLBACK = (
    "id,seller_id,name,description,price_kobo,buyer_pays_kobo,"
    "seller_gets_kobo,platform_fee_kobo,paystack_fee_kobo,image_url,created_at"
)


def _detect_mime(b: bytes) -> str:
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b.startswith(b"\x89PNG"):
        return "image/png"
    if b.startswith(b"RIFF") and b"WEBP" in b[:12]:
        return "image/webp"
    return "image/jpeg"


def create_product(
    db: Client,
    seller_id: str,
    name: str,
    price_kobo: int,
    desc: str = "",
    image_bytes: Optional[bytes] = None,
    file_url: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict:
    """
    Create a product row using the locked schema only.

    Computes buyer_pays_kobo / seller_gets_kobo / platform_fee_kobo /
    paystack_fee_kobo via fee_service.calculate_fees(). Never writes
    price, status, total_fee, or is_public.
    """
    fees = calculate_fees(price_kobo)
    pid = str(uuid.uuid4())

    image_url = None
    if image_bytes:
        try:
            mime = _detect_mime(image_bytes)
            path = f"{seller_id}/{pid}.jpg"
            db.storage.from_("product-covers").upload(
                path, image_bytes, {"content-type": mime, "upsert": "true"}
            )
            image_url = db.storage.from_("product-covers").get_public_url(path)
        except Exception as e:
            logger.warning(f"image upload failed {pid}: {e}")

    row = {
        "id": pid,
        "seller_id": seller_id,
        "name": name.strip(),
        "description": desc.strip(),
        "price_kobo": fees["price_kobo"],
        "buyer_pays_kobo": fees["buyer_pays_kobo"],
        "seller_gets_kobo": fees["seller_gets_kobo"],
        "platform_fee_kobo": fees["platform_fee_kobo"],
        "paystack_fee_kobo": fees["paystack_fee_kobo"],
        "image_url": image_url,
        "file_url": file_url,
        "category": category,
    }

    try:
        res = db.table("products").insert(row).execute()
        return {"ok": True, "data": res.data[0] if res.data else row, "fees": fees}
    except Exception as e:
        logger.error(f"create_product insert failed for {pid}: {e}")
        return {"ok": False, "error": "Failed to create product"}


def list_active(db: Client) -> Dict:
    """
    List products newest-first. Never selects '*', never filters on
    'status' or 'is_public' (those columns don't exist in the locked
    schema). Falls back to a reduced column set if category/file_url
    aren't present, and never raises — returns {"ok": False, "data": []}
    on unrecoverable failure instead of propagating an exception.
    """
    try:
        res = (
            db.table("products")
            .select(_PRODUCT_COLUMNS_FULL)
            .order("created_at", desc=True)
            .execute()
        )
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        logger.warning(f"list_active: full column select failed ({e}), retrying fallback")

    try:
        res = (
            db.table("products")
            .select(_PRODUCT_COLUMNS_FALLBACK)
            .order("created_at", desc=True)
            .execute()
        )
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        logger.error(f"list_active: fallback column select also failed ({e})")
        return {"ok": False, "data": []}
