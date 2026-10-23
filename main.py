"""PayLock API: Telegram initData validation + Feature 2 Escrow."""
from __future__ import annotations
import hashlib, hmac, json, logging, os, sys, time, uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qsl
from flask import Flask, Response, g, jsonify, request, send_from_directory
from flask_cors import CORS
from supabase import Client, create_client

from services.fee_service import calculate_fees
from services.wallet_service import get_balance, apply_transaction, fund_mock
from services.product_service import create_product as svc_create_product, list_active

logging.basicConfig(stream=sys.stdout, level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("paylock")

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
ENABLE_MOCK = os.getenv("ENABLE_MOCK_FUNDING", "false").lower() == "true"

_missing = [name for name, value in (("BOT_TOKEN", BOT_TOKEN),("SUPABASE_URL", SUPABASE_URL),("SUPABASE_SERVICE_KEY", SUPABASE_SERVICE_KEY),("ALLOWED_ORIGINS", ALLOWED_ORIGINS)) if not value]
if _missing: raise RuntimeError(f"Missing required env: {', '.join(_missing)}")

INIT_DATA_MAX_AGE_SECONDS = 24*60*60
CLOCK_SKEW_SECONDS = 60
MAX_INIT_DATA_LENGTH = 8192
MAX_FIELD_LENGTH = 256
_SECRET_KEY: bytes = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 10*1024*1024
app.json.sort_keys = False
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, allow_headers=["Content-Type","Authorization"], methods=["GET","POST","OPTIONS"], supports_credentials=False, max_age=600)

@app.after_request
def set_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response

def _reject(reason: str) -> None:
    logger.warning("initData rejected: %s", reason)
    return None

def validate_init_data(init_data: str) -> dict[str, Any] | None:
    if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_INIT_DATA_LENGTH: return _reject("missing_or_oversized")
    try: pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError: return _reject("malformed_query_string")
    fields = dict(pairs)
    if len(fields) != len(pairs): return _reject("duplicate_keys")
    received_hash = fields.pop("hash", None)
    if not received_hash: return _reject("missing_hash")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    expected_hash = hmac.new(_SECRET_KEY, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash.encode(), received_hash.lower().encode()): return _reject("hash_mismatch")
    try: auth_date = int(fields["auth_date"])
    except (KeyError, ValueError): return _reject("invalid_auth_date")
    age = int(time.time()) - auth_date
    if age > INIT_DATA_MAX_AGE_SECONDS: return _reject("expired")
    if age < -CLOCK_SKEW_SECONDS: return _reject("auth_date_in_future")
    try: user = json.loads(fields.get("user", ""))
    except json.JSONDecodeError: return _reject("invalid_user_json")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0: return _reject("invalid_user_object")
    if not isinstance(user.get("first_name"), str) or not user["first_name"].strip(): return _reject("invalid_user_name")
    return {"user": user, "auth_date": auth_date, "query_id": fields.get("query_id"), "start_param": fields.get("start_param")}

F = TypeVar("F", bound=Callable[..., Any])
def _error(code: str, status: int) -> tuple[Response, int]: return jsonify({"ok": False, "error": code}), status
def require_telegram_auth(view: F) -> F:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "tma" or not token: return _error("unauthorized", 401)
        parsed = validate_init_data(token)
        if parsed is None: return _error("invalid_init_data", 401)
        g.tg_user = parsed["user"]; g.auth_date = parsed["auth_date"]
        return view(*args, **kwargs)
    return wrapper

def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str): return None
    value = value.strip()[:MAX_FIELD_LENGTH]
    return value or None
def _clean_photo_url(value: Any) -> str | None:
    url = _clean_text(value)
    return url if url and url.startswith("https://") else None
def upsert_user(user: dict[str, Any], auth_date: int) -> dict[str, Any]:
    row = {"telegram_id": user["id"], "first_name": _clean_text(user.get("first_name")) or "User", "last_name": _clean_text(user.get("last_name")), "username": _clean_text(user.get("username")), "photo_url": _clean_photo_url(user.get("photo_url")), "auth_date": datetime.fromtimestamp(auth_date, tz=timezone.utc).isoformat()}
    result = supabase.table("users").upsert(row, on_conflict="telegram_id").execute()
    if not result.data: raise RuntimeError("Upsert returned no rows")
    return result.data[0]
def _public_user(row: dict[str, Any]) -> dict[str, Any]: return {"telegram_id": row["telegram_id"], "first_name": row["first_name"], "last_name": row.get("last_name"), "username": row.get("username"), "photo_url": row.get("photo_url")}

# ===== ONLY THIS FUNCTION FIXED TO SERVE index.html FROM ROOT =====
@app.get("/")
@app.get("/app")
def root():
    # your index.html is in root, not static/
    if os.path.exists("index.html"):
        return send_from_directory(".", "index.html")
    if os.path.exists(os.path.join(app.static_folder, "index.html")):
        return send_from_directory(app.static_folder, "index.html")
    return jsonify({"ok": True, "service": "PayLock API live"})

@app.get("/health")
def health(): return jsonify({"ok": True, "status": "live"})
@app.get("/healthz")
def healthz(): return jsonify({"ok": True})

@app.post("/api/auth/validate")
def auth_validate():
    payload = request.get_json(silent=True)
    init_data = payload.get("initData") if isinstance(payload, dict) else None
    if not isinstance(init_data, str): return _error("bad_request", 400)
    parsed = validate_init_data(init_data)
    if parsed is None: return _error("invalid_init_data", 401)
    try: row = upsert_user(parsed["user"], parsed["auth_date"])
    except Exception as exc:
        logger.error("user upsert failed: %s", type(exc).__name__)
        return _error("service_unavailable", 503)
    logger.info("auth ok telegram_id=%s", row["telegram_id"])
    return jsonify({"ok": True, "user": _public_user(row)})

@app.get("/api/me")
@require_telegram_auth
def me():
    result = supabase.table("users").select("telegram_id, first_name, last_name, username, photo_url, balance").eq("telegram_id", g.tg_user["id"]).limit(1).execute()
    if not result.data: return _error("user_not_found", 404)
    return jsonify({"ok": True, "user": result.data[0]})

# ============ FEATURE 2: WALLET + PRODUCTS ============

@app.get("/api/wallet/balance")
@require_telegram_auth
def wallet_balance():
    bal = get_balance(supabase, str(g.tg_user["id"]))
    return jsonify({"ok": True, "data": {"balance": bal, "balance_naira": bal/100}})

@app.post("/api/wallet/fund/mock")
@require_telegram_auth
def wallet_fund_mock():
    if not ENABLE_MOCK: return _error("not_found", 404)
    payload = request.get_json(silent=True) or {}
    amount = payload.get("amount_kobo") or 500000
    try: amount = int(amount)
    except: return _error("bad_request", 400)
    if amount < 10000: return _error("bad_request", 400)
    res = fund_mock(supabase, str(g.tg_user["id"]), amount)
    if not res["ok"]: return jsonify(res), 400
    return jsonify({"ok": True, "data": res["data"]})

@app.get("/api/products")
@require_telegram_auth
def products_list():
    res = list_active(supabase)
    return jsonify(res)

@app.get("/api/products/me")
@require_telegram_auth
def products_mine():
    res = supabase.table("products").select("*").eq("seller_id", g.tg_user["id"]).order("created_at", desc=True).execute()
    return jsonify({"ok": True, "data": res.data or []})

@app.post("/api/products")
@require_telegram_auth
def products_create():
    try:
        name = request.form.get("name","").strip()
        desc = request.form.get("description","").strip()
        price_raw = request.form.get("price_kobo") or request.form.get("price") or "0"
        price_kobo = int(float(price_raw)*100) if "." in str(price_raw) else int(price_raw))
        if not name: return _error("bad_request", 400)
        f = request.files.get("cover")
        img_bytes = f.read() if f else None
        if img_bytes and len(img_bytes) > 5*1024*1024: return _error("payload_too_large", 413)
        result = svc_create_product(supabase, str(g.tg_user["id"]), name, price_kobo, desc, img_bytes)
        return jsonify(result)
    except ValueError as ve:
        return jsonify({"ok": False, "error": str(ve)}), 400
    except Exception as exc:
        logger.error("create product failed: %s", exc, exc_info=True)
        return _error("internal_error", 500)

@app.post("/api/wallet/buy/<product_id>")
@require_telegram_auth
def wallet_buy(product_id: str):
    try: uuid.UUID(product_id)
    except: return _error("bad_request", 400)
    prod = supabase.table("products").select("*").eq("id", product_id).single().execute()
    if not prod.data: return _error("not_found", 404)
    if str(prod.data["seller_id"]) == str(g.tg_user["id"]): return _error("cannot_buy_own", 400)
    fees = calculate_fees(int(prod.data["price"]))
    buyer_id = str(g.tg_user["id"])
    seller_id = str(prod.data["seller_id"])
    buy_ref = f"buy_{buyer_id}_{product_id[:8]}_{uuid.uuid4().hex[:6]}"
    debit = apply_transaction(supabase, buyer_id, -fees["buyer_pays"], "escrow_buy", buy_ref, {"product_id": product_id})
    if not debit["ok"]: return jsonify(debit), 400
    credit_ref = f"sell_{seller_id}_{product_id[:8]}_{uuid.uuid4().hex[:6]}"
    credit = apply_transaction(supabase, seller_id, fees["seller_gets"], "escrow_sell", credit_ref, {"product_id": product_id, "buyer_id": buyer_id})
    return jsonify({"ok": True, "data": {"product": prod.data, "fees": fees, "buyer_balance": get_balance(supabase, buyer_id)}})

@app.errorhandler(404)
def not_found(_: Exception): return _error("not_found", 404)
@app.errorhandler(405)
def method_not_allowed(_: Exception): return _error("method_not_allowed", 405)
@app.errorhandler(413)
def payload_too_large(_: Exception): return _error("payload_too_large", 413)
@app.errorhandler(Exception)
def unhandled(exc: Exception):
    logger.error("unhandled error: %s", type(exc).__name__, exc_info=True)
    return _error("internal_error", 500)
