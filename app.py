# =============================================================================
# PayLock — Production Flask backend
# Deploy: gunicorn main:app --bind 0.0.0.0:$PORT   (Procfile: web: gunicorn main:app)
#
# Required env vars:
#   SUPABASE_URL, SUPABASE_SERVICE_KEY
#   PAYSTACK_SECRET_KEY, PAYSTACK_PUBLIC_KEY
#   BOT_TOKEN
#   PAYSTACK_BASE (optional, defaults to https://api.paystack.co)
#
# Required Supabase migrations:
#   users.pin_hash, users.pin_fail_count, users.pin_locked_until
#   users.deleted_at, users.email, users.phone, users.account_name
#   products.is_hidden (see supabase_migration.sql)
#   escrow_orders table
#   products.owner_id (NOT NULL) — must be populated alongside seller_id
#   products.title (NOT NULL) — must be populated alongside name
#   products.price (NOT NULL) — must be populated alongside price_kobo
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
from werkzeug.middleware.proxy_fix import ProxyFix

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

# Boot safety: a missing SUPABASE_URL / SUPABASE_SERVICE_KEY must not crash the
# import. It logs a clear error instead; requests that need the database will
# fail individually until the variables are set.
supabase: Client = None
try:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_URL and/or SUPABASE_SERVICE_KEY is not set")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
except Exception as exc:
    logging.error(
        "Supabase client was NOT created (%s). Set SUPABASE_URL and "
        "SUPABASE_SERVICE_KEY; database features are unavailable until then.",
        exc,
    )
    supabase = None

PAYSTACK_HEADERS = {
    "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
    "Content-Type": "application/json",
}

# -----------------------------------------------------------------------------
# App init
# -----------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)

# Render terminates TLS at its proxy. Trust exactly one proxy hop so that
# get_remote_address (rate limiter) sees the real client IP, not the proxy's.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

CORS(app)

# NOTE: memory:// storage is per-process. It is NOT shared across gunicorn
# workers. With more than 1 worker, switch to a Redis storage_uri.
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

ESCROW_HOLD_HOURS = 48

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


def _lock_seconds_remaining(locked_until):
    """
    Seconds left on a PIN lock (0 if there is no lock, it has expired, or the
    stored value cannot be parsed).
    """
    if not locked_until:
        return 0
    try:
        lu = datetime.fromisoformat(str(locked_until).replace("Z", "+00:00"))
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        remaining = (lu - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            return int(math.ceil(remaining))
    except ValueError:
        pass
    return 0


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

        if user.get("deleted_at"):
            return jsonify({"error": "Account deleted", "code": "ACCOUNT_DELETED"}), 403

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


# -----------------------------------------------------------------------------
# Helpers — wallet
# -----------------------------------------------------------------------------

def _amount_kobo_from(data):
    """
    Reads an amount from a JSON body. Priority: amount_kobo (kobo),
    amount_naira (naira), amount (naira). Returns int kobo, or None if invalid.
    """
    try:
        if data.get("amount_kobo") not in (None, ""):
            return int(round_half_up(float(data["amount_kobo"])))
        if data.get("amount_naira") not in (None, ""):
            return int(round_half_up(float(data["amount_naira"]) * 100))
        if data.get("amount") not in (None, ""):
            return int(round_half_up(float(data["amount"]) * 100))
    except (TypeError, ValueError):
        return None
    return None


def _restore_balance(telegram_id, amount_kobo):
    """Adds a previously debited amount back onto the user's current balance."""
    res = supabase.table("users").select("balance_kobo").eq("telegram_id", telegram_id).limit(1).execute()
    rows = res.data or []
    current = int(rows[0].get("balance_kobo") or 0) if rows else 0
    supabase.table("users").update({"balance_kobo": current + int(amount_kobo)}).eq("telegram_id", telegram_id).execute()


def _wallet_funding_already_credited(reference):
    """True if a wallet_transactions row for this reference is already marked wallet_funding."""
    res = (
        supabase.table("wallet_transactions")
        .select("id")
        .eq("reference", reference)
        .eq("reason", "wallet_funding")
        .limit(1)
        .execute()
    )
    return bool(res.data)


def _credit_wallet_funding(tx, paid_kobo):
    """
    Credits a verified Paystack payment to the wallet exactly once.
    Shared by /api/wallet/verify-transaction and /api/webhook/paystack.

    Returns the new balance (int) if this call credited the wallet, or None if
    the payment was already credited (nothing is credited again).
    """
    reference = tx["reference"]
    user_id = tx["user_id"]

    # Re-check right before crediting (verify + webhook race guard).
    if _wallet_funding_already_credited(reference):
        return None

    # Atomic claim: only one caller can flip pending -> wallet_funding.
    claim = (
        supabase.table("wallet_transactions")
        .update({"reason": "wallet_funding", "amount_kobo": paid_kobo})
        .eq("reference", reference)
        .eq("user_id", user_id)
        .eq("reason", "wallet_funding_pending")
        .execute()
    )
    if not claim.data:
        return None

    try:
        user_res = supabase.table("users").select("balance_kobo").eq("telegram_id", user_id).limit(1).execute()
        rows = user_res.data or []
        current_balance = int(rows[0].get("balance_kobo") or 0) if rows else 0
        new_balance = current_balance + int(paid_kobo)
        supabase.table("users").update({"balance_kobo": new_balance}).eq("telegram_id", user_id).execute()
    except Exception:
        # Credit failed: release the claim so verify/webhook can retry.
        try:
            (
                supabase.table("wallet_transactions")
                .update({"reason": "wallet_funding_pending"})
                .eq("reference", reference)
                .eq("user_id", user_id)
                .eq("reason", "wallet_funding")
                .execute()
            )
        except Exception:
            logging.exception("_credit_wallet_funding: failed to release claim for %s", reference)
        raise

    return new_balance


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

    if user and user.get("deleted_at"):
        return jsonify(error="Account deleted", code="ACCOUNT_DELETED"), 403

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

PRODUCT_PUBLIC_COLUMNS = (
    "id,name,description,price_kobo,buyer_pays_kobo,"
    "seller_gets_kobo,image_url,file_url,seller_id,"
    "created_at,category"
)


@app.route("/api/products", methods=["GET"])
def products_list():
    # Explicit columns only. Never select("*"), never reference is_public.
    # Hidden products (deleted sellers) are excluded via is_hidden.
    try:
        res = (
            supabase.table("products")
            .select(PRODUCT_PUBLIC_COLUMNS)
            .eq("is_hidden", False)
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
    res = (
        supabase.table("products")
        .select(PRODUCT_PUBLIC_COLUMNS + ",platform_fee_kobo,paystack_fee_kobo,is_hidden")
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows or rows[0].get("is_hidden"):
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404

    product = rows[0]
    product.pop("is_hidden", None)
    return jsonify({"product": product}), 200


@app.route("/api/products", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def products_create(user):
    tid = user["telegram_id"]

    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    category = (request.form.get("category") or "").strip()

    if not name:
        return jsonify({"error": "Product name is required"}), 400

    price_kobo = None
    try:
        if request.form.get("price_kobo"):
            price_kobo = int(request.form.get("price_kobo"))
        else:
            naira_raw = request.form.get("price_naira") or request.form.get("price")
            if naira_raw:
                price_kobo = round_half_up(float(naira_raw) * 100)
    except (TypeError, ValueError):
        price_kobo = None

    if not price_kobo or price_kobo <= 0:
        return jsonify({"error": "A valid price is required"}), 400

    fees = calc_fees(price_kobo)
    if fees["seller_gets_kobo"] <= 0:
        return jsonify({"error": "Price is too low to cover fees"}), 400

    cover = request.files.get("cover")
    if not cover or not cover.filename:
        return jsonify({"error": "Cover image is required"}), 400
    if not _allowed_file(cover.filename, ALLOWED_COVER_EXT):
        return jsonify({"error": "Cover must be JPG, PNG or WEBP"}), 400

    cover_bytes = cover.read()
    if not cover_bytes:
        return jsonify({"error": "Cover image is empty"}), 400
    if len(cover_bytes) > MAX_COVER_BYTES:
        return jsonify({"error": "Cover image must be 5MB or less"}), 400

    product_file = request.files.get("product_file")
    file_bytes = None
    if product_file and product_file.filename:
        if not _allowed_file(product_file.filename, ALLOWED_PRODUCT_EXT):
            return jsonify({"error": "Product file must be ZIP, PDF, MP4 or DOCX"}), 400
        file_bytes = product_file.read()
        if not file_bytes:
            return jsonify({"error": "Product file is empty"}), 400
        if len(file_bytes) > MAX_PRODUCT_BYTES:
            return jsonify({"error": "Product file must be 50MB or less"}), 400

    image_url = None
    try:
        cover_path = f"{tid}/{uuid.uuid4().hex}_{secure_name(cover.filename)}"
        supabase.storage.from_(COVER_BUCKET).upload(
            path=cover_path,
            file=cover_bytes,
            file_options={"content-type": cover.mimetype or "image/jpeg"},
        )
        image_url = supabase.storage.from_(COVER_BUCKET).get_public_url(cover_path)
    except Exception:
        logging.exception("products_create: cover upload failed for telegram_id=%s", tid)
        return jsonify({"error": "Cover upload failed"}), 500

    file_url = None
    if file_bytes is not None:
        try:
            file_path = f"{tid}/{uuid.uuid4().hex}_{secure_name(product_file.filename)}"
            supabase.storage.from_(PRODUCT_FILE_BUCKET).upload(
                path=file_path,
                file=file_bytes,
                file_options={"content-type": product_file.mimetype or "application/octet-stream"},
            )
            file_url = supabase.storage.from_(PRODUCT_FILE_BUCKET).get_public_url(file_path)
        except Exception:
            logging.exception("products_create: product file upload failed for telegram_id=%s", tid)
            return jsonify({"error": "Product file upload failed"}), 500

    # NEVER insert is_public. owner_id/title/price are NOT NULL columns and must
    # be populated alongside seller_id/name/price_kobo.
    insert_payload = {
        "owner_id": str(tid),
        "seller_id": str(tid),
        "title": name,
        "name": name,
        "price": price_kobo,
        "price_kobo": price_kobo,
        "buyer_pays_kobo": fees["buyer_pays_kobo"],
        "seller_gets_kobo": fees["seller_gets_kobo"],
        "platform_fee_kobo": fees["platform_fee_kobo"],
        "paystack_fee_kobo": fees["paystack_fee_kobo"],
        "description": description,
        "image_url": image_url,
        "file_url": file_url,
        "category": category,
    }

    try:
        res = supabase.table("products").insert(insert_payload).execute()
        rows = res.data or []
    except Exception as exc:
        # If the products table has no category column, the insert fails on that
        # key alone. Retry once WITHOUT category so listings keep working.
        if "category" in str(exc).lower():
            logging.warning(
                "products_create: insert failed on category column, retrying without category for telegram_id=%s",
                tid,
            )
            fallback_payload = {k: v for k, v in insert_payload.items() if k != "category"}
            try:
                res = supabase.table("products").insert(fallback_payload).execute()
                rows = res.data or []
                insert_payload = fallback_payload
            except Exception:
                logging.exception("products_create: fallback insert (no category) failed for telegram_id=%s", tid)
                return jsonify({"error": "Could not create product"}), 500
        else:
            logging.exception("products_create: insert failed for telegram_id=%s", tid)
            return jsonify({"error": "Could not create product"}), 500

    return jsonify({"product": rows[0] if rows else insert_payload}), 201


# =============================================================================
# Wallet — balance + transactions
# =============================================================================

@app.route("/api/wallet/balance", methods=["GET"])
@require_auth
def wallet_balance(user):
    tid = user["telegram_id"]
    balance = int(user.get("balance_kobo") or 0)
    return jsonify({
        "balance_kobo": balance,
        "held_kobo": get_held_kobo(tid),
        "available_kobo": balance,
    }), 200


@app.route("/api/wallet/transactions", methods=["GET"])
@require_auth
def wallet_transactions(user):
    tid = user["telegram_id"]
    res = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("user_id", tid)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return jsonify({"transactions": res.data or []}), 200


# =============================================================================
# Wallet — funding (Paystack)
# =============================================================================

@app.route("/api/wallet/fund/initialize", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def wallet_fund_initialize(user):
    tid = user["telegram_id"]
    data = request.get_json(silent=True) or {}

    amount_kobo = _amount_kobo_from(data)
    if amount_kobo is None or amount_kobo < 10000:
        return jsonify({"error": "Minimum funding amount is ₦100"}), 400

    email = (data.get("email") or "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "A valid email is required"}), 400

    reference = "PLK-" + uuid.uuid4().hex[:20]

    supabase.table("wallet_transactions").insert({
        "user_id": tid,
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
    tid = user["telegram_id"]
    data = request.get_json(silent=True) or {}
    reference = (data.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "reference is required"}), 400

    tx_res = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("reference", reference)
        .limit(1)
        .execute()
    )
    rows = tx_res.data or []
    if not rows:
        return jsonify({"error": "Transaction not found"}), 404

    tx = rows[0]
    if int(tx.get("user_id")) != int(tid):
        return jsonify({"error": "Forbidden"}), 403

    if tx.get("reason") == "wallet_funding":
        return jsonify({
            "status": "success",
            "credited": True,
            "already_credited": True,
            "amount_kobo": int(tx.get("amount_kobo") or 0),
            "reference": reference,
        }), 200

    if tx.get("reason") != "wallet_funding_pending":
        return jsonify({"error": "Not a funding reference"}), 400

    try:
        ps_res = requests.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=PAYSTACK_HEADERS,
            timeout=20,
        )
        ps_body = ps_res.json()
    except (requests.RequestException, ValueError):
        logging.exception("wallet_verify_transaction: Paystack verify failed for %s", reference)
        return jsonify({"error": "Could not reach Paystack"}), 502

    pdata = ps_body.get("data") or {}
    if not ps_body.get("status") or pdata.get("status") != "success":
        return jsonify({"status": "failed", "error": "Payment was not successful"}), 400

    try:
        paid_kobo = int(pdata.get("amount") or 0)
    except (TypeError, ValueError):
        paid_kobo = 0
    if paid_kobo <= 0:
        return jsonify({"status": "failed", "error": "Invalid payment amount"}), 400

    new_balance = _credit_wallet_funding(tx, paid_kobo)
    if new_balance is None:
        return jsonify({
            "status": "success",
            "credited": True,
            "already_credited": True,
            "amount_kobo": paid_kobo,
            "reference": reference,
        }), 200

    authorization = pdata.get("authorization") or {}
    if authorization.get("reusable"):
        try:
            _save_card_from_authorization(tid, authorization)
        except Exception:
            logging.exception("wallet_verify_transaction: saving card failed for %s", reference)

    return jsonify({
        "status": "success",
        "credited": True,
        "amount_kobo": paid_kobo,
        "reference": reference,
    }), 200


# =============================================================================
# Wallet — saved cards
# =============================================================================

@app.route("/api/wallet/cards", methods=["GET"])
@require_auth
def wallet_cards(user):
    tid = user["telegram_id"]
    res = (
        supabase.table("saved_cards")
        .select("id,last4,exp_month,exp_year,brand,is_default")
        .eq("user_id", tid)
        .execute()
    )
    return jsonify({"cards": res.data or []}), 200


@app.route("/api/wallet/cards/<card_id>", methods=["DELETE"])
@require_auth
def wallet_cards_delete(user, card_id):
    tid = user["telegram_id"]
    supabase.table("saved_cards").delete().eq("id", card_id).eq("user_id", tid).execute()
    return jsonify({"status": "success"}), 200


# =============================================================================
# Wallet — PIN
# =============================================================================

@app.route("/api/wallet/pin-status", methods=["GET"])
@require_auth
def wallet_pin_status(user):
    tid = user["telegram_id"]
    res = supabase.table("users").select("pin_hash").eq("telegram_id", tid).limit(1).execute()
    rows = res.data or []
    has_pin = bool(rows and rows[0].get("pin_hash"))
    return jsonify({"has_pin": has_pin}), 200


@app.route("/api/wallet/set-pin", methods=["POST"])
@require_auth
@limiter.limit("5 per minute")
def wallet_set_pin(user):
    tid = user["telegram_id"]
    data = request.get_json(silent=True) or {}

    pin = str(data.get("pin") or "")
    confirm_pin = str(data.get("confirm_pin") or data.get("pin_confirm") or data.get("confirm") or "")

    if not re.fullmatch(r"\d{4}", pin):
        return jsonify({"error": "PIN must be exactly 4 digits"}), 400
    if pin != confirm_pin:
        return jsonify({"error": "PINs do not match"}), 400

    res = supabase.table("users").select("pin_hash").eq("telegram_id", tid).limit(1).execute()
    rows = res.data or []
    if rows and rows[0].get("pin_hash"):
        return jsonify({"error": "PIN already set", "code": "PIN_ALREADY_SET"}), 409

    pin_hash = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()
    supabase.table("users").update({
        "pin_hash": pin_hash,
        "pin_fail_count": 0,
        "pin_locked_until": None,
    }).eq("telegram_id", tid).execute()

    return jsonify({"status": "success", "success": True, "has_pin": True}), 200


# =============================================================================
# Banks
# =============================================================================

@app.route("/api/banks", methods=["GET"])
@require_auth
def banks_list(user):
    banks = []
    next_cursor = None
    try:
        for _ in range(5):
            params = {"currency": "NGN", "perPage": 100, "use_cursor": "true"}
            if next_cursor:
                params["next"] = next_cursor
            ps_res = requests.get(
                f"{PAYSTACK_BASE}/bank",
                headers=PAYSTACK_HEADERS,
                params=params,
                timeout=20,
            )
            body = ps_res.json()
            if not body.get("status"):
                logging.warning("banks_list: Paystack error: %s", body)
                if not banks:
                    return jsonify({"error": body.get("message") or "Could not load banks"}), 502
                break
            for b in (body.get("data") or []):
                if b.get("active") is False:
                    continue
                banks.append({"name": b.get("name"), "code": b.get("code")})
            next_cursor = (body.get("meta") or {}).get("next")
            if not next_cursor:
                break
    except (requests.RequestException, ValueError):
        logging.exception("banks_list: request to Paystack failed")
        if not banks:
            return jsonify({"error": "Could not reach Paystack"}), 502

    banks.sort(key=lambda b: (b.get("name") or "").lower())
    return jsonify({"banks": banks}), 200


@app.route("/api/banks/resolve", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def banks_resolve(user):
    data = request.get_json(silent=True) or {}
    account_number = str(data.get("account_number") or "").strip()
    bank_code = str(data.get("bank_code") or "").strip()

    if not account_number or not bank_code:
        return jsonify({"error": "account_number and bank_code are required"}), 400

    logging.info(
        "banks_resolve: request bank_code=%s account_number=******%s",
        bank_code, account_number[-4:],
    )

    try:
        ps_res = requests.get(
            f"{PAYSTACK_BASE}/bank/resolve",
            headers=PAYSTACK_HEADERS,
            params={"account_number": account_number, "bank_code": bank_code},
            timeout=20,
        )
        body = ps_res.json()
    except (requests.RequestException, ValueError) as exc:
        logging.exception("banks_resolve: request to Paystack failed")
        return jsonify({"error": f"Could not reach Paystack: {exc}"}), 502

    logging.info("banks_resolve: http=%s status=%s", ps_res.status_code, body.get("status"))

    if not body.get("status"):
        return jsonify({"error": body.get("message") or "Could not resolve account"}), 400

    info = body.get("data") or {}
    return jsonify({"account_name": info.get("account_name")}), 200


# =============================================================================
# Wallet — withdrawals
# =============================================================================

# Transfer recipients already created in this process, keyed by
# (telegram_id, bank_code, account_number). In-process only: it resets on
# restart and is not shared across gunicorn workers.
_RECIPIENT_CACHE = {}

# Paystack transfer statuses that mean the money will NOT be sent.
_TRANSFER_FAILED_STATUSES = {"failed", "reversed", "rejected", "blocked", "abandoned"}


def _paystack_post(path, payload):
    """POSTs JSON to Paystack. Returns (http_status, body_dict)."""
    ps_res = requests.post(
        f"{PAYSTACK_BASE}{path}",
        headers=PAYSTACK_HEADERS,
        json=payload,
        timeout=30,
    )
    try:
        body = ps_res.json()
    except ValueError:
        body = {}
    return ps_res.status_code, body


def _get_recipient_code(tid, bank_code, account_number, account_name):
    """
    Returns a Paystack transfer recipient_code for this user + account, reusing
    one created earlier in this process instead of creating a duplicate.
    Raises RuntimeError if Paystack does not return a recipient.
    """
    key = (int(tid), bank_code, account_number)
    cached = _RECIPIENT_CACHE.get(key)
    if cached:
        return cached

    http_status, body = _paystack_post("/transferrecipient", {
        "type": "nuban",
        "name": account_name,
        "account_number": account_number,
        "bank_code": bank_code,
        "currency": "NGN",
    })
    code = (body.get("data") or {}).get("recipient_code")
    logging.info(
        "wallet_withdraw: transferrecipient http=%s status=%s",
        http_status, body.get("status"),
    )
    if not body.get("status") or not code:
        raise RuntimeError(f"Paystack transferrecipient failed: {body.get('message')}")

    _RECIPIENT_CACHE[key] = code
    return code


@app.route("/api/wallet/withdraw", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def wallet_withdraw(user):
    tid = user["telegram_id"]
    data = request.get_json(silent=True) or {}

    amount_kobo = _amount_kobo_from(data)
    if amount_kobo is None or amount_kobo < WITHDRAWAL_MIN_KOBO:
        return jsonify({"error": "Minimum withdrawal is ₦1,000", "code": "AMOUNT_TOO_LOW"}), 400

    bank_code = str(data.get("bank_code") or "").strip()
    bank_name = str(data.get("bank_name") or "").strip()
    account_number = str(data.get("account_number") or "").strip()
    account_name = str(data.get("account_name") or "").strip()
    if not bank_code or not account_number or not account_name:
        return jsonify({"error": "Bank details are required"}), 400

    pin = str(data.get("pin") or "")
    if not re.fullmatch(r"\d{4}", pin):
        return jsonify({"error": "A 4-digit PIN is required", "code": "PIN_INVALID"}), 400

    user_res = (
        supabase.table("users")
        .select("balance_kobo,pin_hash,pin_fail_count,pin_locked_until")
        .eq("telegram_id", tid)
        .limit(1)
        .execute()
    )
    rows = user_res.data or []
    if not rows:
        return jsonify({"error": "User not found"}), 404
    u = rows[0]

    pin_hash = u.get("pin_hash")
    if not pin_hash:
        return jsonify({"error": "Set a PIN before withdrawing", "code": "PIN_NOT_SET"}), 403

    remaining = _lock_seconds_remaining(u.get("pin_locked_until"))
    if remaining > 0:
        return jsonify({
            "error": "Too many wrong PIN attempts. Try again later.",
            "code": "RATE_LIMITED",
            "retry_after": remaining,
        }), 429

    try:
        pin_ok = bcrypt.checkpw(pin.encode(), str(pin_hash).encode())
    except ValueError:
        pin_ok = False

    if not pin_ok:
        fails = int(u.get("pin_fail_count") or 0) + 1
        if fails >= PIN_FAIL_LIMIT:
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=PIN_LOCK_MINUTES)).isoformat()
            supabase.table("users").update({
                "pin_fail_count": 0,
                "pin_locked_until": locked_until,
            }).eq("telegram_id", tid).execute()
            return jsonify({
                "error": "Too many wrong PIN attempts. Try again later.",
                "code": "RATE_LIMITED",
                "retry_after": PIN_LOCK_MINUTES * 60,
            }), 429
        supabase.table("users").update({"pin_fail_count": fails}).eq("telegram_id", tid).execute()
        return jsonify({
            "error": "Incorrect PIN",
            "code": "PIN_INVALID",
            "attempts_left": PIN_FAIL_LIMIT - fails,
        }), 401

    if int(u.get("pin_fail_count") or 0) != 0 or u.get("pin_locked_until"):
        supabase.table("users").update({
            "pin_fail_count": 0,
            "pin_locked_until": None,
        }).eq("telegram_id", tid).execute()

    balance = int(u.get("balance_kobo") or 0)
    if amount_kobo > balance:
        return jsonify({"error": "Insufficient balance", "code": "INSUFFICIENT_BALANCE"}), 400

    # Debit (guarded so a concurrent balance change cannot be overwritten).
    debit = (
        supabase.table("users")
        .update({"balance_kobo": balance - amount_kobo})
        .eq("telegram_id", tid)
        .eq("balance_kobo", balance)
        .execute()
    )
    if not debit.data:
        return jsonify({"error": "Balance changed, please retry"}), 409

    # Paystack requires transfer references to be lowercase letters, digits,
    # "-" or "_" (16-50 chars), so the reference is generated lowercase.
    reference = "wd-" + uuid.uuid4().hex[:20]

    # Balance is now debited. If either insert fails, revert the debit before
    # returning the error so no money is lost.
    tx_inserted = False
    try:
        supabase.table("wallet_transactions").insert({
            "user_id": tid,
            "type": "debit",
            "amount_kobo": amount_kobo,
            "reason": "withdrawal",
            "reference": reference,
        }).execute()
        tx_inserted = True

        supabase.table("withdrawals").insert({
            "user_id": tid,
            "amount_kobo": amount_kobo,
            "bank_code": bank_code,
            "bank_name": bank_name,
            "account_number": account_number,
            "account_name": account_name,
            "status": "pending",
            "reference": reference,
        }).execute()
    except Exception:
        logging.exception("wallet_withdraw: insert failed after debit, reverting balance for telegram_id=%s", tid)
        try:
            _restore_balance(tid, amount_kobo)
        except Exception:
            logging.exception("wallet_withdraw: CRITICAL balance revert failed for telegram_id=%s amount=%s", tid, amount_kobo)
        if tx_inserted:
            try:
                supabase.table("wallet_transactions").delete().eq("reference", reference).execute()
            except Exception:
                logging.exception("wallet_withdraw: could not remove orphan transaction %s", reference)
        return jsonify({"error": "Withdrawal could not be created. Your balance was not changed."}), 500

    # Debit + both rows exist. Now actually pay the user through Paystack.
    transfer_status = "pending"
    try:
        recipient_code = _get_recipient_code(tid, bank_code, account_number, account_name)

        http_status, body = _paystack_post("/transfer", {
            "source": "balance",
            "amount": amount_kobo,
            "recipient": recipient_code,
            "reference": reference,
            "reason": "PayLock withdrawal",
        })
        logging.info(
            "wallet_withdraw: transfer http=%s status=%s reference=%s",
            http_status, body.get("status"), reference,
        )
        if not body.get("status"):
            raise RuntimeError(f"Paystack transfer failed: {body.get('message')}")

        transfer_status = str((body.get("data") or {}).get("status") or "pending").lower()
        if transfer_status in _TRANSFER_FAILED_STATUSES:
            raise RuntimeError(f"Paystack transfer returned status={transfer_status}")
    except Exception:
        logging.exception(
            "wallet_withdraw: Paystack payout failed, reverting for telegram_id=%s reference=%s",
            tid, reference,
        )
        # A stale cached recipient must not be reused on the next attempt.
        _RECIPIENT_CACHE.pop((int(tid), bank_code, account_number), None)
        try:
            _restore_balance(tid, amount_kobo)
        except Exception:
            logging.exception("wallet_withdraw: CRITICAL balance revert failed for telegram_id=%s amount=%s", tid, amount_kobo)
        try:
            supabase.table("wallet_transactions").delete().eq("reference", reference).execute()
        except Exception:
            logging.exception("wallet_withdraw: could not remove orphan transaction %s", reference)
        try:
            supabase.table("withdrawals").delete().eq("reference", reference).execute()
        except Exception:
            logging.exception("wallet_withdraw: could not remove orphan withdrawal %s", reference)
        return jsonify({"error": "Withdrawal could not be created. Your balance was not changed."}), 500

    # Transfer accepted by Paystack: record its status. Never revert from here,
    # the money is already on its way.
    try:
        supabase.table("withdrawals").update({"status": transfer_status}).eq("reference", reference).execute()
    except Exception:
        logging.exception("wallet_withdraw: could not update withdrawal status for %s", reference)

    return jsonify({
        "status": "success",
        "reference": reference,
        "amount_kobo": amount_kobo,
    }), 200


@app.route("/api/wallet/withdrawals", methods=["GET"])
@require_auth
def wallet_withdrawals(user):
    tid = user["telegram_id"]
    res = (
        supabase.table("withdrawals")
        .select("*")
        .eq("user_id", tid)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return jsonify({"withdrawals": res.data or []}), 200


# =============================================================================
# Wallet — buy (escrow)
# =============================================================================

@app.route("/api/wallet/buy/<product_id>", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def wallet_buy(user, product_id):
    tid = user["telegram_id"]

    prod_res = (
        supabase.table("products")
        .select("id,seller_id,price_kobo,buyer_pays_kobo,seller_gets_kobo,platform_fee_kobo,is_hidden")
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    prod_rows = prod_res.data or []
    if not prod_rows:
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404

    product = prod_rows[0]
    if product.get("is_hidden"):
        return jsonify({"error": "Product is no longer available", "code": "PRODUCT_UNAVAILABLE"}), 410

    if str(product.get("seller_id")) == str(tid):
        return jsonify({"error": "You cannot buy your own product"}), 400

    try:
        seller_id_int = int(product.get("seller_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Product has no valid seller"}), 400

    buyer_pays = int(product.get("buyer_pays_kobo") or 0)
    if buyer_pays <= 0:
        return jsonify({"error": "Product price is invalid"}), 400

    balance = int(user.get("balance_kobo") or 0)
    if balance < buyer_pays:
        return jsonify({"error": "Insufficient balance", "code": "INSUFFICIENT_BALANCE"}), 400

    # Debit (guarded so a concurrent balance change cannot be overwritten).
    debit = (
        supabase.table("users")
        .update({"balance_kobo": balance - buyer_pays})
        .eq("telegram_id", tid)
        .eq("balance_kobo", balance)
        .execute()
    )
    if not debit.data:
        return jsonify({"error": "Balance changed, please retry"}), 409

    reference = "ESC-" + uuid.uuid4().hex[:20]
    release_at = (datetime.now(timezone.utc) + timedelta(hours=ESCROW_HOLD_HOURS)).isoformat()

    # Balance is now debited. On any failure below, revert the debit and remove
    # the orphan wallet transaction before returning the error.
    try:
        supabase.table("wallet_transactions").insert({
            "user_id": tid,
            "type": "debit",
            "amount_kobo": buyer_pays,
            "reason": "escrow_purchase",
            "reference": reference,
        }).execute()

        order_payload = {
            "product_id": product.get("id"),
            "buyer_id": int(tid),
            "seller_id": seller_id_int,
            "price_kobo": int(product.get("price_kobo") or 0),
            "buyer_pays_kobo": buyer_pays,
            "seller_gets_kobo": int(product.get("seller_gets_kobo") or 0),
            "platform_fee_kobo": int(product.get("platform_fee_kobo") or PLATFORM_FEE_KOBO),
            "status": "held",
            "reference": reference,
            "release_at": release_at,
        }
        order_res = supabase.table("escrow_orders").insert(order_payload).execute()
        order_rows = order_res.data or []
    except Exception:
        logging.exception("wallet_buy: failed after debit, reverting balance for telegram_id=%s", tid)
        try:
            _restore_balance(tid, buyer_pays)
        except Exception:
            logging.exception("wallet_buy: CRITICAL balance revert failed for telegram_id=%s amount=%s", tid, buyer_pays)
        try:
            supabase.table("wallet_transactions").delete().eq("reference", reference).execute()
        except Exception:
            logging.exception("wallet_buy: could not remove orphan transaction %s", reference)
        return jsonify({"error": "Purchase failed. Your balance was not changed."}), 500

    return jsonify({
        "status": "success",
        "reference": reference,
        "order": order_rows[0] if order_rows else order_payload,
    }), 201


# =============================================================================
# Paystack webhook (NO auth decorator — authenticated by HMAC signature)
# =============================================================================

@app.route("/api/webhook/paystack", methods=["POST"])
def paystack_webhook():
    if not PAYSTACK_SECRET_KEY:
        logging.error("paystack_webhook: PAYSTACK_SECRET_KEY is not set")
        return jsonify({"error": "Webhook not configured"}), 500

    raw_body = request.get_data()
    signature = request.headers.get("x-paystack-signature", "")
    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(computed, signature):
        return jsonify({"error": "Invalid signature"}), 401

    try:
        event = json.loads(raw_body)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid payload"}), 400

    if event.get("event") != "charge.success":
        return jsonify({"status": "received"}), 200

    try:
        pdata = event.get("data") or {}
        reference = str(pdata.get("reference") or "").strip()
        if not reference:
            return jsonify({"status": "received"}), 200

        tx_res = (
            supabase.table("wallet_transactions")
            .select("*")
            .eq("reference", reference)
            .limit(1)
            .execute()
        )
        tx_rows = tx_res.data or []
        if not tx_rows:
            return jsonify({"status": "received"}), 200

        tx = tx_rows[0]
        if tx.get("reason") == "wallet_funding":
            return jsonify({"status": "received"}), 200

        try:
            paid_kobo = int(pdata.get("amount") or 0)
        except (TypeError, ValueError):
            paid_kobo = 0
        if paid_kobo <= 0:
            return jsonify({"status": "received"}), 200

        new_balance = _credit_wallet_funding(tx, paid_kobo)
        if new_balance is None:
            return jsonify({"status": "received"}), 200

        authorization = pdata.get("authorization") or {}
        if authorization.get("reusable"):
            try:
                _save_card_from_authorization(tx["user_id"], authorization)
            except Exception:
                logging.exception("paystack_webhook: saving card failed for %s", reference)
    except Exception:
        logging.exception("paystack_webhook: processing failed")
        return jsonify({"error": "Processing failed"}), 500

    return jsonify({"status": "received"}), 200


# =============================================================================
# Account deletion
# =============================================================================

@app.route("/api/account", methods=["DELETE"])
@require_auth
@limiter.limit("3 per hour")
def account_delete(user):
    tid = user["telegram_id"]
    data = request.get_json(silent=True) or {}

    user_res = (
        supabase.table("users")
        .select("balance_kobo,pin_hash,pin_fail_count,pin_locked_until,deleted_at")
        .eq("telegram_id", tid)
        .limit(1)
        .execute()
    )
    rows = user_res.data or []
    if not rows:
        return jsonify({"error": "User not found"}), 404
    u = rows[0]

    if u.get("deleted_at"):
        return jsonify({"error": "Account deleted", "code": "ACCOUNT_DELETED"}), 403

    pin_hash = u.get("pin_hash")

    if pin_hash:
        remaining = _lock_seconds_remaining(u.get("pin_locked_until"))
        if remaining > 0:
            return jsonify({
                "error": "Too many wrong PIN attempts. Try again later.",
                "code": "RATE_LIMITED",
                "retry_after": remaining,
            }), 429

    balance = int(u.get("balance_kobo") or 0)
    if balance > 0:
        return jsonify({
            "error": "Withdraw your wallet balance before deleting your account.",
            "code": "FUNDS_PENDING",
        }), 409

    # Fail-closed: if open escrow cannot be verified, do not delete.
    try:
        open_res = (
            supabase.table("escrow_orders")
            .select("id")
            .in_("status", ["held", "disputed"])
            .or_(f"buyer_id.eq.{tid},seller_id.eq.{tid}")
            .limit(1)
            .execute()
        )
        has_open_escrow = bool(open_res.data)
    except Exception:
        logging.exception("account_delete: escrow check failed for telegram_id=%s", tid)
        return jsonify({"error": "Could not delete account. Please try again.", "code": "DELETE_FAILED"}), 500

    if has_open_escrow:
        return jsonify({
            "error": "You have open escrow orders. Complete or resolve them first.",
            "code": "FUNDS_PENDING",
        }), 409

    if pin_hash:
        pin = str(data.get("pin") or "")
        if not re.fullmatch(r"\d{4}", pin):
            return jsonify({"error": "A 4-digit PIN is required", "code": "PIN_REQUIRED"}), 400

        try:
            pin_ok = bcrypt.checkpw(pin.encode(), str(pin_hash).encode())
        except ValueError:
            pin_ok = False

        if not pin_ok:
            fails = int(u.get("pin_fail_count") or 0) + 1
            if fails >= PIN_FAIL_LIMIT:
                locked_until = (datetime.now(timezone.utc) + timedelta(minutes=PIN_LOCK_MINUTES)).isoformat()
                supabase.table("users").update({
                    "pin_fail_count": 0,
                    "pin_locked_until": locked_until,
                }).eq("telegram_id", tid).execute()
                return jsonify({
                    "error": "Too many wrong PIN attempts. Try again later.",
                    "code": "RATE_LIMITED",
                    "retry_after": PIN_LOCK_MINUTES * 60,
                }), 429
            supabase.table("users").update({"pin_fail_count": fails}).eq("telegram_id", tid).execute()
            return jsonify({
                "error": "Incorrect PIN",
                "code": "PIN_INVALID",
                "attempts_left": PIN_FAIL_LIMIT - fails,
            }), 401

        supabase.table("users").update({
            "pin_fail_count": 0,
            "pin_locked_until": None,
        }).eq("telegram_id", tid).execute()
    else:
        if data.get("confirm") is not True:
            return jsonify({"error": "Confirmation required", "code": "CONFIRM_REQUIRED"}), 400

    # Hide the user's products by BOTH seller_id and owner_id.
    try:
        supabase.table("products").update({"is_hidden": True}).eq("seller_id", str(tid)).execute()
        supabase.table("products").update({"is_hidden": True}).eq("owner_id", str(tid)).execute()
    except Exception:
        logging.exception("account_delete: hiding products failed for telegram_id=%s", tid)
        return jsonify({"error": "Account could not be deleted. Please try again.", "code": "DELETE_FAILED"}), 500

    # Soft-delete + anonymize.
    try:
        supabase.table("users").update({
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "email": "deleted_" + str(tid) + "@deleted.local",
            "phone": "",
            "account_name": "DELETED",
        }).eq("telegram_id", tid).execute()
    except Exception:
        logging.exception("account_delete: soft-delete failed for telegram_id=%s", tid)
        return jsonify({"error": "Account could not be deleted. Please try again.", "code": "DELETE_FAILED"}), 500

    return jsonify({"deleted": True}), 200


# =============================================================================
# Frontend (serves ONLY index.html)
# =============================================================================

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path.startswith("api/"):
        abort(404)
    if path and path != "index.html":
        abort(404)
    return send_from_directory(app.root_path, "index.html")


# =============================================================================
# Error handlers
# =============================================================================

@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(429)
def handle_429(e):
    return jsonify({"error": "Too many requests", "code": "RATE_LIMITED"}), 429


@app.errorhandler(500)
def handle_500(e):
    original = getattr(e, "original_exception", None)
    logging.error("Internal server error: %r", original or e, exc_info=original)
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 5000)
    app.run(host="0.0.0.0", port=port)
```
