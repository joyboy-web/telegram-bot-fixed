# =============================================================================
# PayLock — Production Flask backend
# Deploy: gunicorn app:app --bind 0.0.0.0:$PORT   (Procfile: web: gunicorn app:app)
#
# Required env vars:
#   SUPABASE_URL, SUPABASE_SERVICE_KEY
#   PAYSTACK_SECRET_KEY, PAYSTACK_PUBLIC_KEY
#   BOT_TOKEN
#   PAYSTACK_BASE (optional, defaults to https://api.paystack.co)
#
# Required Supabase migrations:
#   users.pin_hash, users.pin_fail_count, users.pin_locked_until
#   escrow_orders table
#   products.owner_id (NOT NULL) — must be populated alongside seller_id
# =============================================================================

import os
import re
import io
import json
import time
import math
import uuid
import hmac
import hashlib
import logging
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import requests
import bcrypt
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from supabase import create_client, Client
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Env + clients
# -----------------------------------------------------------------------------

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY")
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
PAYSTACK_BASE = os.environ.get("PAYSTACK_BASE", "https://api.paystack.co")

logging.basicConfig(level=logging.INFO)

if not all([SUPABASE_URL, SUPABASE_SERVICE_KEY, PAYSTACK_SECRET_KEY, PAYSTACK_PUBLIC_KEY, BOT_TOKEN]):
    logging.warning("One or more required environment variables are missing.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

PAYSTACK_HEADERS = {
    "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
    "Content-Type": "application/json",
}

# -----------------------------------------------------------------------------
# App init
# -----------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)
CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

PLATFORM_FEE_KOBO = 20000          # NGN 200 clean platform profit per sale
PAYSTACK_FEE_CAP_KOBO = 200000     # NGN 2000 cap

WITHDRAWAL_MIN_KOBO = 100000       # NGN 1000 minimum withdrawal
PIN_FAIL_LIMIT = 5
PIN_LOCK_MINUTES = 15

ESCROW_HOLD_DAYS = 3

ALLOWED_COVER_EXT = {"jpg", "jpeg", "png", "webp"}
ALLOWED_PRODUCT_EXT = {"zip", "pdf", "mp4", "docx"}
MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_PRODUCT_BYTES = 50 * 1024 * 1024

COVER_BUCKET = "product-covers"
PRODUCT_FILE_BUCKET = "product-files"

INIT_DATA_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days — soft/warn-only check

# -----------------------------------------------------------------------------
# Fee model (LOCKED — do not alter)
# -----------------------------------------------------------------------------

def round_half_up(value):
    return int(math.floor(value + 0.5))


def paystack_fee_kobo(price_kobo):
    if price_kobo <= 250000:
        fee = round_half_up(price_kobo * 0.015)
    else:
        fee = round_half_up(price_kobo * 0.015 + 10000)
    return min(fee, PAYSTACK_FEE_CAP_KOBO)


def calc_fees(price_kobo):
    ps_fee = paystack_fee_kobo(price_kobo)
    total_extra = PLATFORM_FEE_KOBO + ps_fee
    split = round_half_up(total_extra / 2)
    return {
        "paystack_fee_kobo": ps_fee,
        "platform_fee_kobo": PLATFORM_FEE_KOBO,
        "split_kobo": split,
        "buyer_pays_kobo": price_kobo + split,
        "seller_gets_kobo": price_kobo - split,
    }

# -----------------------------------------------------------------------------
# Helpers — generic
# -----------------------------------------------------------------------------

def secure_name(filename):
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename or "file")
    return name[-120:]


def _allowed_file(filename, allowed_exts):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in allowed_exts


def extract_init_data():
    """
    Looks for Telegram initData across every transport method the frontend may
    use, in priority order:
      1. X-Telegram-Init-Data header
      2. Authorization: tma <initData> header
      3. JSON body key: init_data
      4. JSON body key: initData
      5. Form data key: init_data or initData
    Returns the raw initData string, or '' if none found.
    """
    header_val = request.headers.get("X-Telegram-Init-Data", "").strip()
    if header_val:
        return header_val

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("tma "):
        candidate = auth_header[4:].strip()
        if candidate:
            return candidate

    data = request.get_json(silent=True) or {}
    if data.get("init_data"):
        return str(data["init_data"]).strip()
    if data.get("initData"):
        return str(data["initData"]).strip()

    form_val = request.form.get("init_data") or request.form.get("initData")
    if form_val:
        return form_val.strip()

    return ""


def verify_init_data(init_data):
    """
    Validates Telegram WebApp initData per Telegram's documented HMAC scheme.

    auth_date expiry is a SOFT check (warns only, never blocks) — hard-blocking
    on age caused false "Verification failed" errors for legitimate sessions
    carrying slightly older initData (e.g. across BOT_TOKEN rotation/revoke,
    backgrounded WebApp resumes). Only a hash mismatch blocks authentication.
    """
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        logging.warning(
            "verify_init_data: parse_qsl failed, init_data length=%s",
            len(init_data or ""),
        )
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        logging.warning("verify_init_data: no hash field present in initData")
        return None

    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        logging.warning(
            "verify_init_data: HASH MISMATCH. received_hash=%s computed_hash=%s bot_token_len=%s",
            received_hash, computed_hash, len(BOT_TOKEN or ""),
        )
        return None

    auth_date = parsed.get("auth_date")
    if auth_date:
        try:
            age_seconds = time.time() - int(auth_date)
            if age_seconds > INIT_DATA_MAX_AGE_SECONDS:
                logging.warning(
                    "verify_init_data: initData is %.0f seconds old (>7 days) — allowing, warn-only",
                    age_seconds,
                )
        except ValueError:
            pass

    user_json = parsed.get("user")
    if not user_json:
        logging.warning("verify_init_data: no 'user' field present in initData")
        return None

    try:
        return json.loads(user_json)
    except (TypeError, ValueError):
        logging.warning("verify_init_data: failed to json.loads the 'user' field")
        return None


def get_or_create_user(tg_user):
    telegram_id = int(tg_user.get("id"))
    try:
        res = supabase.table("users").select("*").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        if rows:
            return rows[0]

        insert_res = supabase.table("users").insert({
            "telegram_id": telegram_id,
            "first_name": tg_user.get("first_name") or "",
            "last_name": tg_user.get("last_name") or "",
            "username": tg_user.get("username") or "",
            "balance_kobo": 0,
        }).execute()
        rows = insert_res.data or []
        return rows[0] if rows else None
    except Exception:
        logging.exception("get_or_create_user failed for telegram_id=%s", telegram_id)
        return None


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        init_data = extract_init_data()
        if not init_data:
            return jsonify({"error": "Unauthorized"}), 401

        tg_user = verify_init_data(init_data)
        if not tg_user:
            return jsonify({"error": "Invalid Telegram session"}), 401

        user = get_or_create_user(tg_user)
        if not user:
            return jsonify({"error": "Could not load user"}), 401

        return f(user, *args, **kwargs)
    return wrapper


def _save_card_from_authorization(telegram_id, authorization):
    auth_code = authorization.get("authorization_code")
    if not auth_code:
        return

    existing = (
        supabase.table("saved_cards")
        .select("id")
        .eq("user_id", telegram_id)
        .eq("authorization_code", auth_code)
        .limit(1)
        .execute()
    )
    if existing.data:
        return

    has_any = supabase.table("saved_cards").select("id").eq("user_id", telegram_id).limit(1).execute()
    is_first = not bool(has_any.data)

    supabase.table("saved_cards").insert({
        "user_id": telegram_id,
        "authorization_code": auth_code,
        "last4": authorization.get("last4"),
        "exp_month": authorization.get("exp_month"),
        "exp_year": authorization.get("exp_year"),
        "brand": authorization.get("brand") or authorization.get("card_type"),
        "is_default": is_first,
    }).execute()


def get_held_kobo(telegram_id):
    """
    Sum of seller_gets_kobo currently sitting in escrow (status='held') for this
    user as a seller. Wrapped defensively: if escrow_orders doesn't exist yet
    (migration not run), this returns 0 instead of crashing wallet/balance.
    """
    try:
        res = (
            supabase.table("escrow_orders")
            .select("seller_gets_kobo")
            .eq("seller_id", telegram_id)
            .eq("status", "held")
            .execute()
        )
        return sum(int(r.get("seller_gets_kobo") or 0) for r in (res.data or []))
    except Exception:
        logging.exception("get_held_kobo: escrow_orders lookup failed for telegram_id=%s", telegram_id)
        return 0


# =============================================================================
# Health
# =============================================================================

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()}), 200


# =============================================================================
# Auth
# =============================================================================

@app.route("/api/auth/validate", methods=["POST"])
@limiter.limit("30 per minute")
def auth_validate():
    init_data = extract_init_data()

    if not init_data:
        return jsonify({"error": "init_data is required"}), 400

    tg_user = verify_init_data(init_data)
    if not tg_user:
        return jsonify({"error": "Invalid Telegram session"}), 401

    user = get_or_create_user(tg_user)
    if not user:
        return jsonify({"error": "Could not load user"}), 500

    return jsonify({
        "ok": True,
        "user": {
            "telegram_id": user["telegram_id"],
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "username": user.get("username"),
        },
    }), 200


# =============================================================================
# Products
# =============================================================================

@app.route("/api/products", methods=["GET"])
def products_list():
    # FIXED is_public removed — this route must NEVER reference the
    # is_public column again, in any form: no .eq("is_public", ...) filter,
    # no select("*") where is_public could leak in, no try/except
    # detect-and-retry fallback. Explicit column list only. Any failure of
    # any kind returns an empty list with HTTP 200 — never a 500.
    try:
        res = (
            supabase.table("products")
            .select(
                "id,name,description,price_kobo,buyer_pays_kobo,"
                "seller_gets_kobo,image_url,file_url,seller_id,"
                "created_at,category"
            )
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        return jsonify({"products": res.data or []}), 200
    except Exception:
        logging.exception("products_list: failed to load products")
        return jsonify({"products": []}), 200


@app.route("/api/products/<product_id>", methods=["GET"])
def products_get(product_id):
    try:
        res = (
            supabase.table("products")
            .select(
                "id,name,description,price_kobo,buyer_pays_kobo,"
                "seller_gets_kobo,platform_fee_kobo,paystack_fee_kobo,"
                "image_url,file_url,seller_id,created_at,category"
            )
            .eq("id", product_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return jsonify({"error": "Product not found"}), 404
        return jsonify({"product": rows[0]}), 200
    except Exception:
        logging.exception("products_get: failed for product_id=%s", product_id)
        return jsonify({"error": "Product not found"}), 404


@app.route("/api/products", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def products_create(user):
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    category = (request.form.get("category") or "").strip()

    price_raw = request.form.get("price") or request.form.get("price_naira")
    price_kobo_raw = request.form.get("price_kobo")

    price_kobo = None

    if price_kobo_raw:
        try:
            price_kobo = int(round(float(price_kobo_raw)))
        except ValueError:
            return jsonify({"error": "Invalid price_kobo"}), 400
    elif price_raw:
        try:
            price_naira = float(price_raw)
        except ValueError:
            return jsonify({"error": "Invalid price"}), 400
        price_kobo = round_half_up(price_naira * 100)

    if not name or not price_kobo:
        return jsonify({"error": "Name and price are required"}), 400

    if price_kobo <= 0:
        return jsonify({"error": "Price must be greater than zero"}), 400

    fees = calc_fees(price_kobo)

    cover = request.files.get("cover")
    if not cover or cover.filename == "":
        return jsonify({"error": "Cover image is required"}), 400
    if not _allowed_file(cover.filename, ALLOWED_COVER_EXT):
        return jsonify({"error": "Cover must be JPG, PNG, or WEBP"}), 400

    cover_bytes = cover.read()
    if len(cover_bytes) > MAX_COVER_BYTES:
        return jsonify({"error": "Cover image must be under 5MB"}), 400

    cover_path = f"{user['telegram_id']}/{uuid.uuid4().hex}_{secure_name(cover.filename)}"
    supabase.storage.from_(COVER_BUCKET).upload(
        path=cover_path,
        file=cover_bytes,
        file_options={"content-type": cover.mimetype or "application/octet-stream"},
    )
    image_url = supabase.storage.from_(COVER_BUCKET).get_public_url(cover_path)

    file_url = None
    product_file = request.files.get("product_file")
    if product_file and product_file.filename:
        if not _allowed_file(product_file.filename, ALLOWED_PRODUCT_EXT):
            return jsonify({"error": "Product file must be ZIP, PDF, MP4, or DOCX"}), 400

        pf_bytes = product_file.read()
        if len(pf_bytes) > MAX_PRODUCT_BYTES:
            return jsonify({"error": "Product file must be under 50MB"}), 400

        file_path = f"{user['telegram_id']}/{uuid.uuid4().hex}_{secure_name(product_file.filename)}"
        supabase.storage.from_(PRODUCT_FILE_BUCKET).upload(
            path=file_path,
            file=pf_bytes,
            file_options={"content-type": product_file.mimetype or "application/octet-stream"},
        )
        file_url = supabase.storage.from_(PRODUCT_FILE_BUCKET).get_public_url(file_path)

    # FIXED owner_id NOT NULL violation (23502) — products.owner_id has a
    # NOT NULL constraint but was never being populated, only seller_id was.
    # Both are now written as the same value, explicitly cast to string so
    # the payload can never silently send an int where Postgres/PostgREST
    # expects text (and vice versa never sends None for owner_id again).
    insert_payload = {
        "owner_id": str(user["telegram_id"]),
        "seller_id": str(user["telegram_id"]),
        "name": name,
        "description": description,
        "price_kobo": price_kobo,
        "buyer_pays_kobo": fees["buyer_pays_kobo"],
        "seller_gets_kobo": fees["seller_gets_kobo"],
        "platform_fee_kobo": fees["platform_fee_kobo"],
        "paystack_fee_kobo": fees["paystack_fee_kobo"],
        "image_url": image_url,
        "file_url": file_url,
        "category": category or None,
        # FIXED is_public removed — this key is never inserted, under any
        # condition, regardless of whether the column exists in Postgres.
        # A stale PostgREST schema cache on this column must never be able
        # to break product creation again.
    }

    try:
        insert_res = supabase.table("products").insert(insert_payload).execute()
    except Exception:
        logging.exception("products_create: insert failed")
        return jsonify({"error": "Could not create product. Please try again."}), 500

    return jsonify({"product": (insert_res.data or [None])[0]}), 201


# =============================================================================
# Wallet — balance / history
# =============================================================================

@app.route("/api/wallet/balance", methods=["GET"])
@require_auth
def wallet_balance(user):
    res = supabase.table("users").select("balance_kobo").eq("telegram_id", user["telegram_id"]).limit(1).execute()
    rows = res.data or []
    balance_kobo = int(rows[0].get("balance_kobo") or 0) if rows else 0
    held_kobo = get_held_kobo(user["telegram_id"])

    return jsonify({
        "balance_kobo": balance_kobo,
        "held_kobo": held_kobo,
        "available_kobo": balance_kobo,
    }), 200


@app.route("/api/wallet/transactions", methods=["GET"])
@require_auth
def wallet_transactions(user):
    res = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("user_id", user["telegram_id"])
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return jsonify({"transactions": res.data or []}), 200


# =============================================================================
# Wallet — funding
# =============================================================================

@app.route("/api/wallet/fund/initialize", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def wallet_fund_initialize(user):
    data = request.get_json(silent=True) or {}
    amount_kobo = int(data.get("amount_kobo") or 0)
    email = (data.get("email") or "").strip()

    if amount_kobo < 10000:
        return jsonify({"error": "Minimum funding amount is NGN100"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "A valid email is required"}), 400

    reference = f"PLK-{uuid.uuid4().hex[:20]}"

    supabase.table("wallet_transactions").insert({
        "user_id": user["telegram_id"],
        "type": "credit",
        "amount_kobo": amount_kobo,
        "reason": "wallet_funding_pending",
        "reference": reference,
    }).execute()

    return jsonify({
        "public_key": PAYSTACK_PUBLIC_KEY,
        "reference": reference,
        "amount_kobo": amount_kobo,
        "email": email,
    }), 200


@app.route("/api/wallet/verify-transaction", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def wallet_verify_transaction(user):
    data = request.get_json(silent=True) or {}
    reference = (data.get("reference") or "").strip()

    if not reference:
        return jsonify({"error": "reference is required"}), 400

    tx_res = supabase.table("wallet_transactions").select("*").eq("reference", reference).limit(1).execute()
    tx_rows = tx_res.data or []

    if not tx_rows:
        return jsonify({"error": "Transaction not found"}), 404

    tx = tx_rows[0]

    if int(tx.get("user_id") or 0) != int(user["telegram_id"]):
        return jsonify({"error": "Forbidden"}), 403

    ps_resp = requests.get(
        f"{PAYSTACK_BASE}/transaction/verify/{reference}",
        headers=PAYSTACK_HEADERS,
        timeout=15,
    )
    ps_data = ps_resp.json()

    if not ps_resp.ok or not ps_data.get("status"):
        return jsonify({"status": "failed"}), 400

    ps_tx = ps_data.get("data") or {}

    if ps_tx.get("status") != "success":
        return jsonify({"status": "failed"}), 400

    amount_kobo = int(ps_tx.get("amount") or tx.get("amount_kobo") or 0)

    if tx.get("reason") == "wallet_funding":
        return jsonify({
            "status": "success",
            "credited": True,
            "already_credited": True,
            "amount_kobo": tx.get("amount_kobo"),
            "reference": reference,
        }), 200

    user_res = supabase.table("users").select("balance_kobo").eq("telegram_id", user["telegram_id"]).limit(1).execute()
    current_balance = int((user_res.data or [{}])[0].get("balance_kobo") or 0)
    new_balance = current_balance + amount_kobo

    supabase.table("users").update({"balance_kobo": new_balance}).eq("telegram_id", user["telegram_id"]).execute()
    supabase.table("wallet_transactions").update(
        {"reason": "wallet_funding", "amount_kobo": amount_kobo}
    ).eq("reference", reference).execute()

    authorization = ps_tx.get("authorization") or {}
    if authorization.get("reusable"):
        _save_card_from_authorization(user["telegram_id"], authorization)

    return jsonify({
        "status": "success",
        "credited": True,
        "amount_kobo": amount_kobo,
        "reference": reference,
    }), 200


# =============================================================================
# Wallet — saved cards
# =============================================================================

@app.route("/api/wallet/cards", methods=["GET"])
@require_auth
def wallet_cards(user):
    res = (
        supabase.table("saved_cards")
        .select("id,last4,brand,exp_month,exp_year,is_default")
        .eq("user_id", user["telegram_id"])
        .order("is_default", desc=True)
        .execute()
    )
    return jsonify({"cards": res.data or []}), 200


@app.route("/api/wallet/cards/<card_id>", methods=["DELETE"])
@require_auth
def wallet_card_delete(user, card_id):
    supabase.table("saved_cards").delete().eq("id", card_id).eq("user_id", user["telegram_id"]).execute()
    return jsonify({"status": "success"}), 200


# =============================================================================
# Wallet — PIN
# =============================================================================

@app.route("/api/wallet/pin-status", methods=["GET"])
@require_auth
def wallet_pin_status(user):
    res = supabase.table("users").select("pin_hash").eq("telegram_id", user["telegram_id"]).limit(1).execute()
    rows = res.data or []
    has_pin = bool(rows and rows[0].get("pin_hash"))
    return jsonify({"has_pin": has_pin}), 200


@app.route("/api/wallet/set-pin", methods=["POST"])
@require_auth
@limiter.limit("5 per minute")
def wallet_set_pin(user):
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin") or "").strip()
    confirm_pin = str(data.get("confirm_pin") or "").strip()

    if not re.fullmatch(r"\d{4}", pin):
        return jsonify({"error": "PIN must be exactly 4 digits"}), 400
    if pin != confirm_pin:
        return jsonify({"error": "PINs do not match"}), 400

    existing = supabase.table("users").select("pin_hash").eq("telegram_id", user["telegram_id"]).limit(1).execute()
    rows = existing.data or []
    if rows and rows[0].get("pin_hash"):
        return jsonify({"error": "A PIN is already set for this account", "code": "PIN_ALREADY_SET"}), 409

    pin_hash = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()

    supabase.table("users").update({
        "pin_hash": pin_hash,
        "pin_fail_count": 0,
        "pin_locked_until": None,
    }).eq("telegram_id", user["telegram_id"]).execute()

    return jsonify({"status": "success", "has_pin": True}), 200


# =============================================================================
# Banks
# =============================================================================

@app.route("/api/banks", methods=["GET"])
@require_auth
def list_banks(user):
    resp = requests.get(
        f"{PAYSTACK_BASE}/bank",
        headers=PAYSTACK_HEADERS,
        params={"currency": "NGN"},
        timeout=15,
    )
    data = resp.json()

    if not resp.ok or not data.get("status"):
        return jsonify({"error": "Could not load banks"}), 502

    banks = [{"name": b["name"], "code": b["code"]} for b in data.get("data", [])]
    return jsonify({"banks": banks}), 200


@app.route("/api/banks/resolve", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def resolve_bank(user):
    data = request.get_json(silent=True) or {}
    account_number = (data.get("account_number") or "").strip()
    bank_code = (data.get("bank_code") or "").strip()

    if not account_number or not bank_code:
        return jsonify({"error": "account_number and bank_code are required"}), 400

    # FIXED: added detailed logging of the outbound Paystack request/response
    # and now surface Paystack's real error message to the frontend instead
    # of a generic "Could not resolve account" 400 on every failure — this
    # was making the real cause (invalid bank_code, Paystack rate limit,
    # Paystack 5xx, auth header issue, etc.) impossible to diagnose from logs.
    logging.info(
        "banks/resolve: requesting Paystack resolve account_number=%s bank_code=%s",
        account_number, bank_code,
    )

    try:
        resp = requests.get(
            f"{PAYSTACK_BASE}/bank/resolve",
            headers=PAYSTACK_HEADERS,
            params={"account_number": account_number, "bank_code": bank_code},
            timeout=15,
        )
    except requests.RequestException:
        logging.exception("banks/resolve: request to Paystack failed (network/timeout)")
        return jsonify({"error": "Could not reach Paystack. Please try again."}), 502

    logging.info(
        "banks/resolve: Paystack response status=%s body=%s",
        resp.status_code, resp.text,
    )

    try:
        resp_data = resp.json()
    except ValueError:
        logging.warning("banks/resolve: Paystack response was not valid JSON: %s", resp.text)
        return jsonify({"error": resp.text or "Could not resolve account"}), 400

    if not resp.ok or not resp_data.get("status"):
        error_message = resp_data.get("message") or resp.text or "Could not resolve account"
        return jsonify({"error": error_message}), 400

    account_name = (resp_data.get("data") or {}).get("account_name")
    return jsonify({"account_name": account_name}), 200


# =============================================================================
# Wallet — withdrawals
# =============================================================================

@app.route("/api/wallet/withdraw", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def wallet_withdraw(user):
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin") or "").strip()
    amount_kobo = int(data.get("amount_kobo") or 0)
    bank_code = (data.get("bank_code") or "").strip()
    bank_name = (data.get("bank_name") or "").strip()
    account_number = (data.get("account_number") or "").strip()
    account_name = (data.get("account_name") or "").strip()

    if amount_kobo < WITHDRAWAL_MIN_KOBO:
        return jsonify({
            "error": f"Minimum withdrawal is NGN{WITHDRAWAL_MIN_KOBO // 100}",
            "code": "AMOUNT_TOO_LOW",
        }), 400

    if not bank_code or not account_number or not account_name:
        return jsonify({"error": "Bank details are required", "code": "BANK_REQUIRED"}), 400

    if not re.fullmatch(r"\d{4}", pin):
        return jsonify({"error": "A valid 4-digit PIN is required", "code": "PIN_INVALID"}), 400

    user_res = (
        supabase.table("users")
        .select("balance_kobo,pin_hash,pin_fail_count,pin_locked_until")
        .eq("telegram_id", user["telegram_id"])
        .limit(1)
        .execute()
    )
    rows = user_res.data or []
    if not rows:
        return jsonify({"error": "User not found"}), 404

    u = rows[0]

    if not u.get("pin_hash"):
        return jsonify({"error": "Set your withdrawal PIN first", "code": "PIN_NOT_SET"}), 403

    locked_until = u.get("pin_locked_until")
    if locked_until:
        try:
            lu = datetime.fromisoformat(str(locked_until).replace("Z", "+00:00"))
            if lu > datetime.now(timezone.utc):
                return jsonify({
                    "error": "Too many attempts. Try again in 15 minutes.",
                    "code": "RATE_LIMITED",
                }), 429
        except ValueError:
            pass

    if not bcrypt.checkpw(pin.encode(), u["pin_hash"].encode()):
        fail_count = int(u.get("pin_fail_count") or 0) + 1
        update_payload = {"pin_fail_count": fail_count}

        if fail_count >= PIN_FAIL_LIMIT:
            update_payload["pin_locked_until"] = (
                datetime.now(timezone.utc) + timedelta(minutes=PIN_LOCK_MINUTES)
            ).isoformat()
            update_payload["pin_fail_count"] = 0

        supabase.table("users").update(update_payload).eq("telegram_id", user["telegram_id"]).execute()

        if fail_count >= PIN_FAIL_LIMIT:
            return jsonify({
                "error": "Too many attempts. Try again in 15 minutes.",
                "code": "RATE_LIMITED",
            }), 429

        return jsonify({"error": "Incorrect PIN", "code": "PIN_INVALID"}), 401

    supabase.table("users").update(
        {"pin_fail_count": 0, "pin_locked_until": None}
    ).eq("telegram_id", user["telegram_id"]).execute()

    balance_kobo = int(u.get("balance_kobo") or 0)
    if amount_kobo > balance_kobo:
        return jsonify({"error": "Insufficient available balance", "code": "INSUFFICIENT_BALANCE"}), 400

    new_balance = balance_kobo - amount_kobo
    reference = f"WD-{uuid.uuid4().hex[:20]}"

    supabase.table("users").update({"balance_kobo": new_balance}).eq("telegram_id", user["telegram_id"]).execute()

    supabase.table("wallet_transactions").insert({
        "user_id": user["telegram_id"],
        "type": "debit",
        "amount_kobo": amount_kobo,
        "reason": "withdrawal",
        "reference": reference,
    }).execute()

    supabase.table("withdrawals").insert({
        "user_id": user["telegram_id"],
        "amount_kobo": amount_kobo,
        "bank_name": bank_name,
        "bank_code": bank_code,
        "account_number": account_number,
        "account_name": account_name,
        "status": "pending",
        "reference": reference,
    }).execute()

    return jsonify({"status": "success", "reference": reference, "amount_kobo": amount_kobo}), 200


@app.route("/api/wallet/withdrawals", methods=["GET"])
@require_auth
def wallet_withdrawals(user):
    res = (
        supabase.table("withdrawals")
        .select("*")
        .eq("user_id", user["telegram_id"])
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return jsonify({"withdrawals": res.data or []}), 200


# =============================================================================
# Buy / Purchase — escrow
# =============================================================================

@app.route("/api/wallet/buy/<product_id>", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def wallet_buy(user, product_id):
    # FIXED is_public removed — product selection below uses an explicit
    # column list (no "*") and this route no longer references is_public
    # anywhere, including the old fail-open availability check.
    prod_res = (
        supabase.table("products")
        .select(
            "id,name,price_kobo,buyer_pays_kobo,seller_gets_kobo,"
            "platform_fee_kobo,seller_id"
        )
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    rows = prod_res.data or []
    if not rows:
        return jsonify({"error": "Product not found"}), 404

    product = rows[0]

    if int(product["seller_id"]) == int(user["telegram_id"]):
        return jsonify({"error": "You cannot buy your own product"}), 400

    buyer_pays_kobo = int(product["buyer_pays_kobo"])

    user_res = supabase.table("users").select("balance_kobo").eq("telegram_id", user["telegram_id"]).limit(1).execute()
    u_rows = user_res.data or []
    if not u_rows:
        return jsonify({"error": "User not found"}), 404

    balance_kobo = int(u_rows[0].get("balance_kobo") or 0)

    if buyer_pays_kobo > balance_kobo:
        return jsonify({
            "error": "Insufficient balance. Please fund your wallet.",
            "code": "INSUFFICIENT_BALANCE",
        }), 400

    new_balance = balance_kobo - buyer_pays_kobo
    reference = f"ESC-{uuid.uuid4().hex[:20]}"
    release_at = (datetime.now(timezone.utc) + timedelta(days=ESCROW_HOLD_DAYS)).isoformat()

    supabase.table("users").update({"balance_kobo": new_balance}).eq("telegram_id", user["telegram_id"]).execute()

    supabase.table("wallet_transactions").insert({
        "user_id": user["telegram_id"],
        "type": "debit",
        "amount_kobo": buyer_pays_kobo,
        "reason": "escrow_purchase",
        "reference": reference,
    }).execute()

    escrow_res = supabase.table("escrow_orders").insert({
        "product_id": product_id,
        "buyer_id": user["telegram_id"],
        "seller_id": product["seller_id"],
        "price_kobo": product["price_kobo"],
        "buyer_pays_kobo": buyer_pays_kobo,
        "seller_gets_kobo": product["seller_gets_kobo"],
        "platform_fee_kobo": product["platform_fee_kobo"],
        "status": "held",
        "reference": reference,
        "release_at": release_at,
    }).execute()

    return jsonify({
        "status": "success",
        "reference": reference,
        "order": (escrow_res.data or [None])[0],
    }), 201


# =============================================================================
# Paystack webhook
# =============================================================================

@app.route("/api/webhook/paystack", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body = request.get_data()

    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(computed, signature):
        return jsonify({"error": "Invalid signature"}), 401

    event = request.get_json(silent=True) or {}
    event_type = event.get("event")
    data = event.get("data") or {}

    if event_type == "charge.success":
        reference = data.get("reference")
        if reference:
            tx_res = supabase.table("wallet_transactions").select("*").eq("reference", reference).limit(1).execute()
            tx_rows = tx_res.data or []

            if tx_rows and tx_rows[0].get("reason") != "wallet_funding":
                tx = tx_rows[0]
                amount_kobo = int(data.get("amount") or tx.get("amount_kobo") or 0)

                user_res = supabase.table("users").select("balance_kobo").eq("telegram_id", tx["user_id"]).limit(1).execute()
                current_balance = int((user_res.data or [{}])[0].get("balance_kobo") or 0)

                supabase.table("users").update(
                    {"balance_kobo": current_balance + amount_kobo}
                ).eq("telegram_id", tx["user_id"]).execute()

                supabase.table("wallet_transactions").update(
                    {"reason": "wallet_funding", "amount_kobo": amount_kobo}
                ).eq("reference", reference).execute()

                authorization = data.get("authorization") or {}
                if authorization.get("reusable"):
                    _save_card_from_authorization(tx["user_id"], authorization)

    return jsonify({"status": "received"}), 200


# =============================================================================
# Static frontend (single-service deployment) — must stay before error handlers
# =============================================================================

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path.startswith("api/"):
        abort(404)

    full_path = os.path.join(app.root_path, path)
    if path and os.path.isfile(full_path):
        return send_from_directory(app.root_path, path)

    return send_from_directory(app.root_path, "index.html")


# =============================================================================
# Error handlers
# =============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Too many requests", "code": "RATE_LIMITED"}), 429


@app.errorhandler(500)
def server_error(e):
    logging.exception("Unhandled server error")
    return jsonify({"error": "Internal server error"}), 500


# =============================================================================
# Entrypoint
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
