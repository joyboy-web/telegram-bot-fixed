"""
PayLock Escrow — Flask Backend (app.py)
Production-grade, security-hardened backend for Telegram Mini App escrow.
Stack: Flask + Flask-CORS + Flask-Limiter + python-dotenv + supabase-py + Paystack REST API
Deployed on Render.
"""

import os
import hmac
import hashlib
import json
import time
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from collections import defaultdict

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from supabase import create_client, Client
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")

PAYSTACK_BASE = "https://api.paystack.co"

if not all([BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY, PAYSTACK_SECRET_KEY, PAYSTACK_PUBLIC_KEY]):
    raise RuntimeError("Missing required environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("paylock")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": FRONTEND_ORIGIN}}, supports_credentials=True)

limiter = Limiter(get_remote_address, app=app, storage_uri="memory://", default_limits=["200 per minute"])

BUCKET_COVERS = "product-covers"
BUCKET_FILES = "product-files"

MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_PRODUCT_FILE_BYTES = 50 * 1024 * 1024

ALLOWED_COVER_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_FILE_MIME = {
    "application/zip",
    "application/x-zip-compressed",
    "application/pdf",
    "video/mp4",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_bank_cache = {"data": None, "fetched_at": 0}
BANK_CACHE_TTL = 60 * 60 * 6

# In-memory funding attempt throttle: {telegram_id: [timestamps]}
_fund_attempts = defaultdict(list)
FUND_ATTEMPT_WINDOW_SECONDS = 60
FUND_ATTEMPT_MAX = 5

MIN_WITHDRAWAL_KOBO = 100000  # 1,000 NGN
MIN_FUNDING_KOBO = 10000      # 100 NGN


# ============================================================
# REQUIRED SCHEMA ADDITION
# ============================================================
#   ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_hash text;
#
# Transaction PIN is required for withdrawal confirmation. Must be set
# via /api/wallet/set-pin before a user can withdraw. Enforced below.


# ============================================================
# ROUNDING — must match frontend Math.round() behavior exactly
# (Python's built-in round() uses banker's rounding; we need
#  round-half-away-from-zero to stay consistent with JS.)
# ============================================================
def round_half_up(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ============================================================
# FEE MODEL — LOCKED BUSINESS LOGIC
# ============================================================
PLATFORM_FEE_KOBO = 20000  # 200 NGN clean profit


def paystack_fee_kobo(price_kobo: int) -> int:
    if price_kobo <= 250000:
        fee = round_half_up(price_kobo * 0.015)
    else:
        fee = round_half_up(price_kobo * 0.015 + 10000)
    return min(200000, fee)


def calc_fees(price_kobo: int) -> dict:
    pf = paystack_fee_kobo(price_kobo)
    total = pf + PLATFORM_FEE_KOBO
    split = round_half_up(total / 2)
    return {
        "price_kobo": price_kobo,
        "paystack_fee_kobo": pf,
        "platform_fee_kobo": PLATFORM_FEE_KOBO,
        "total_extra_kobo": total,
        "split_kobo": split,
        "buyer_pays_kobo": price_kobo + split,
        "seller_gets_kobo": price_kobo - split,
    }


# ============================================================
# TELEGRAM INITDATA VALIDATION (Feature 1) — CORRECT HMAC IMPLEMENTATION
# ============================================================
def verify_init_data(init_data: str):
    """
    Validates Telegram WebApp initData per official spec:
      secret_key = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)
      computed_hash = HMAC_SHA256(key=secret_key, msg=data_check_string)
    Returns parsed Telegram user dict if valid, else None.
    """
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = int(parsed.get("auth_date", 0))
    if auth_date and (time.time() - auth_date) > 86400:
        return None

    user_json = parsed.get("user")
    if not user_json:
        return None

    try:
        user = json.loads(user_json)
    except json.JSONDecodeError:
        return None

    return user


def get_or_create_user(tg_user: dict) -> dict:
    telegram_id = tg_user["id"]
    existing = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()

    if existing.data:
        user_row = existing.data[0]
        updates = {}
        for field in ("first_name", "last_name", "username"):
            if tg_user.get(field) and tg_user[field] != user_row.get(field):
                updates[field] = tg_user[field]
        if updates:
            supabase.table("users").update(updates).eq("telegram_id", telegram_id).execute()
            user_row.update(updates)
        return user_row

    new_user = {
        "telegram_id": telegram_id,
        "first_name": tg_user.get("first_name", ""),
        "last_name": tg_user.get("last_name", ""),
        "username": tg_user.get("username", ""),
        "balance_kobo": 0,
    }
    inserted = supabase.table("users").insert(new_user).execute()
    return inserted.data[0]


def get_user_from_header():
    """
    SECURITY CRITICAL: Every protected endpoint must derive the acting
    user's telegram_id from this function — NEVER from client-supplied
    query params, JSON body fields, or form fields.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("tma "):
        return None

    init_data = auth_header[4:]
    tg_user = verify_init_data(init_data)
    if not tg_user:
        return None

    return get_or_create_user(tg_user)


def require_auth():
    """Returns (user, None) on success, or (None, error_response) on failure."""
    user = get_user_from_header()
    if not user:
        return None, (jsonify({"error": "Unauthorized — invalid or missing Telegram session"}), 401)
    return user, None


@app.route("/api/auth/validate", methods=["POST"])
@limiter.limit("30 per minute")
def auth_validate():
    body = request.get_json(silent=True) or {}
    init_data = body.get("initData")

    if not init_data:
        return jsonify({"error": "initData required"}), 400

    tg_user = verify_init_data(init_data)
    if not tg_user:
        return jsonify({"error": "invalid_signature"}), 401

    user_row = get_or_create_user(tg_user)

    return jsonify({
        "user": {
            "telegram_id": user_row["telegram_id"],
            "first_name": user_row.get("first_name", ""),
            "last_name": user_row.get("last_name", ""),
            "username": user_row.get("username", ""),
            "balance_kobo": user_row.get("balance_kobo", 0),
        }
    })


# ============================================================
# PRODUCTS (Feature 2) — seller_id derived from auth, never client input
# ============================================================
def upload_to_bucket(bucket: str, path: str, file_bytes: bytes, content_type: str) -> str:
    supabase.storage.from_(bucket).upload(path, file_bytes, {"content-type": content_type, "upsert": "true"})
    return supabase.storage.from_(bucket).get_public_url(path)


@app.route("/api/products", methods=["POST"])
@limiter.limit("20 per hour")
def create_product():
    user, err = require_auth()
    if err:
        return err

    seller_id = user["telegram_id"]  # never trust client-sent seller_id

    try:
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        price_naira = request.form.get("price_naira")

        if not name or not price_naira:
            return jsonify({"error": "Missing required fields"}), 400

        try:
            price_kobo = int(round_half_up(float(price_naira) * 100))
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid price"}), 400

        if price_kobo <= 0:
            return jsonify({"error": "Price must be greater than zero"}), 400
            Is this the result
def get_held_kobo(telegram_id: int) -> int:
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Try escrow_transactions table first (if it exists in this deployment)
        result = (
            supabase.table("escrow_transactions")
            .select("seller_gets_kobo")
            .eq("seller_id", telegram_id)
            .eq("status", "held")
            .gt("release_at", now_iso)
            .execute()
        )
        if result.data:
            return sum(r.get("seller_gets_kobo", 0) for r in result.data)
        return 0
    except Exception as e:
        # escrow_transactions table does not exist yet in V7.1 schema —
        # fall back to 0 held so withdrawals are never incorrectly blocked.
        logger.warning(
            "get_held_kobo: escrow_transactions lookup failed for telegram_id=%s (%s). "
            "Defaulting held_kobo=0.",
            telegram_id, e
        )
        return 0


@app.route("/api/wallet/balance", methods=["GET"])
@limiter.limit("60 per minute")
def wallet_balance():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]

    try:
        balance_kobo = int(user.get("balance_kobo", 0) or 0)
    except Exception:
        logger.exception("wallet_balance: failed to read balance_kobo for telegram_id=%s", telegram_id)
        balance_kobo = 0

    try:
        held_kobo = get_held_kobo(telegram_id)
    except Exception as e:
        logger.warning(
            "wallet_balance: get_held_kobo raised unexpectedly for telegram_id=%s (%s). "
            "Defaulting held_kobo=0.",
            telegram_id, e
        )
        held_kobo = 0

    return jsonify({
        "balance_kobo": balance_kobo,
        "held_kobo": held_kobo
    }), 200
            if product_file.mimetype not in ALLOWED_FILE_MIME:
                return jsonify({"error": "Product file must be ZIP, PDF, MP4, or DOCX"}), 400

            file_bytes = product_file.read()
            if len(file_bytes) > MAX_PRODUCT_FILE_BYTES:
                return jsonify({"error": "Product file exceeds 50MB limit"}), 400

            ext = os.path.splitext(product_file.filename)[1].lower()
            file_path = f"{seller_id}/{uuid.uuid4().hex}{ext}"
            file_url = upload_to_bucket(BUCKET_FILES, file_path, file_bytes, product_file.mimetype)

        fees = calc_fees(price_kobo)

        record = {
            "seller_id": seller_id,
            "name": name,
            "description": description,
            "category": category,
            "price_kobo": fees["price_kobo"],
            "buyer_pays_kobo": fees["buyer_pays_kobo"],
            "seller_gets_kobo": fees["seller_gets_kobo"],
            "platform_fee_kobo": fees["platform_fee_kobo"],
            "paystack_fee_kobo": fees["paystack_fee_kobo"],
            "image_url": image_url,
            "file_url": file_url,
            "is_public": True,
        }

        inserted = supabase.table("products").insert(record).execute()
        return jsonify({"product": inserted.data[0]}), 201

    except Exception:
        logger.exception("create_product failed")
        return jsonify({"error": "Failed to create product"}), 500


@app.route("/api/products", methods=["GET"])
def list_products():
    try:
        result = (
            supabase.table("products")
            .select("*")
            .eq("is_public", True)
            .order("created_at", desc=True)
            .execute()
        )
        return jsonify({"products": result.data})
    except Exception:
        logger.exception("list_products failed")
        return jsonify({"products": []})


# ============================================================
# WALLET — BALANCE / TRANSACTIONS — auth-derived telegram_id only
# ============================================================
def get_held_kobo(telegram_id: int) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    result = (
        supabase.table("products")
        .select("seller_gets_kobo")
        .eq("seller_id", telegram_id)
        .not_.is_("escrow_release_at", "null")
        .gt("escrow_release_at", now_iso)
        .execute()
    )
    return sum(row["seller_gets_kobo"] for row in result.data) if result.data else 0


@app.route("/api/wallet/balance", methods=["GET"])
def wallet_balance():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]
    fresh = supabase.table("users").select("balance_kobo").eq("telegram_id", telegram_id).execute()
    balance_kobo = fresh.data[0]["balance_kobo"] if fresh.data else 0
    held_kobo = get_held_kobo(telegram_id)

    return jsonify({"balance_kobo": balance_kobo, "held_kobo": held_kobo})


@app.route("/api/wallet/transactions", methods=["GET"])
def wallet_transactions():
    user, err = require_auth()
    if err:
        return err

    result = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("user_id", user["telegram_id"])
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return jsonify({"transactions": result.data})


# ============================================================
# WALLET — FUNDING (Feature 3A)
# ============================================================
def paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}


def check_fund_rate_limit(telegram_id: int) -> bool:
    """Returns True if allowed, False if rate-limited."""
    now = time.time()
    attempts = _fund_attempts[telegram_id]
    attempts[:] = [t for t in attempts if now - t < FUND_ATTEMPT_WINDOW_SECONDS]
    if len(attempts) >= FUND_ATTEMPT_MAX:
        return False
    attempts.append(now)
    return True


def _credit_wallet_if_not_already(reference: str) -> dict:
    """
    Idempotent credit. Returns dict with keys: credited(bool), telegram_id, amount_kobo.
    Ownership (telegram_id) is read from our own DB record created at
    initialize time — NEVER from client input or Paystack metadata directly.
    """
    existing = supabase.table("wallet_transactions").select("*").eq("reference", reference).execute()
    if not existing.data:
        return {"credited": False, "telegram_id": None, "amount_kobo": 0, "reason": "unknown_reference"}

    row = existing.data[0]
    telegram_id = row["user_id"]
    amount_kobo = row["amount_kobo"]

    if row["reason"] == "wallet_funding":
        return {"credited": False, "telegram_id": telegram_id, "amount_kobo": amount_kobo, "reason": "already_credited"}

    supabase.table("wallet_transactions").update({"reason": "wallet_funding"}).eq("reference", reference).execute()

    user_row = supabase.table("users").select("balance_kobo").eq("telegram_id", telegram_id).execute()
    current = user_row.data[0]["balance_kobo"] if user_row.data else 0
    supabase.table("users").update({"balance_kobo": current + amount_kobo}).eq("telegram_id", telegram_id).execute()

    return {"credited": True, "telegram_id": telegram_id, "amount_kobo": amount_kobo, "reason": "credited"}


def _save_card_from_authorization(telegram_id: int, auth: dict):
    try:
        existing = (
            supabase.table("saved_cards")
            .select("id")
            .eq("user_id", telegram_id)
            .eq("authorization_code", auth["authorization_code"])
            .execute()
        )
        if existing.data:
            return
        supabase.table("saved_cards").insert({
            "user_id": telegram_id,
            "authorization_code": auth["authorization_code"],
            "last4": auth.get("last4", ""),
            "exp_month": auth.get("exp_month", ""),
            "exp_year": auth.get("exp_year", ""),
            "brand": auth.get("brand", ""),
            "is_default": False,
        }).execute()
    except Exception:
        logger.exception("Failed to save card")


@app.route("/api/wallet/fund/initialize", methods=["POST"])
@limiter.limit("10 per minute")
def fund_initialize():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]

    if not check_fund_rate_limit(telegram_id):
        return jsonify({"error": "Too many funding attempts. Please wait a minute and try again."}), 429

    body = request.get_json(silent=True) or {}
    amount_kobo = body.get("amount_kobo")
    email = body.get("email")
    save_card = bool(body.get("save_card"))
    card_id = body.get("card_id")  # references saved_cards.id — NEVER a raw authorization_code

    if not amount_kobo or not email:
        return jsonify({"error": "amount_kobo and email are required"}), 400

    if amount_kobo < MIN_FUNDING_KOBO:
        return jsonify({"error": "Minimum funding amount is 100 NGN"}), 400

    reference = f"PLF-{uuid.uuid4().hex[:16]}"

    # Create authoritative pending record — this is the ONLY source of
    # truth for who owns this reference at verify/webhook time.
    supabase.table("wallet_transactions").insert({
        "user_id": telegram_id,
        "type": "credit",
        "amount_kobo": amount_kobo,
        "reason": "wallet_funding_pending",
        "reference": reference,
    }).execute()

    if card_id:
        # SECURITY: look up the authorization_code server-side, scoped to
        # the authenticated user only. Client never supplies a raw code.
        card_row = (
            supabase.table("saved_cards")
            .select("*")
            .eq("id", card_id)
            .eq("user_id", telegram_id)
            .execute()
        )
        if not card_row.data:
            return jsonify({"error": "Saved card not found"}), 404

        authorization_code = card_row.data[0]["authorization_code"]

        payload = {
            "authorization_code": authorization_code,
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "metadata": {"telegram_id": telegram_id, "purpose": "wallet_funding"},
        }
        resp = requests.post(f"{PAYSTACK_BASE}/transaction/charge_authorization",
                              headers=paystack_headers(), json=payload, timeout=20)
        data = resp.json()

        if not data.get("status") or data.get("data", {}).get("status") != "success":
            return jsonify({"error": "Card charge failed. Please try a different method."}), 402

        result = _credit_wallet_if_not_already(reference)
        return jsonify({"charge_immediately": True, "reference": reference, "amount_kobo": amount_kobo})

    return jsonify({"charge_immediately": False, "reference": reference, "public_key": PAYSTACK_PUBLIC_KEY})


@app.route("/api/wallet/verify-transaction", methods=["POST"])
@limiter.limit("20 per minute")
def verify_transaction():
    user, err = require_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    reference = body.get("reference")

    if not reference:
        return jsonify({"error": "reference required"}), 400

    pending = supabase.table("wallet_transactions").select("*").eq("reference", reference).execute()
    if not pending.data:
        return jsonify({"error": "Unknown transaction reference"}), 404

    owner_telegram_id = pending.data[0]["user_id"]
    if owner_telegram_id != user["telegram_id"]:
        return jsonify({"error": "This transaction does not belong to your account"}), 403

    resp = requests.get(f"{PAYSTACK_BASE}/transaction/verify/{reference}", headers=paystack_headers(), timeout=20)
    data = resp.json()

    if not data.get("status") or data.get("data", {}).get("status") != "success":
        return jsonify({"status": "failed"}), 200

    tx = data["data"]
    result = _credit_wallet_if_not_already(reference)

    auth = tx.get("authorization", {})
    if auth.get("reusable"):
        _save_card_from_authorization(owner_telegram_id, auth)

    return jsonify({"status": "success", "amount_kobo": tx["amount"], "already_credited": not result["credited"]})


@app.route("/api/webhook/paystack", methods=["POST"])
def paystack_webhook():
    """
    Paystack signs webhooks with HMAC-SHA512 of the raw request body using
    the secret key. This must be verified before trusting any payload.
    """
    signature = request.headers.get("x-paystack-signature", "")
    raw_body = request.get_data()

    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()

    if not hmac.compare_digest(computed, signature):
        logger.warning("Invalid Paystack webhook signature")
        return jsonify({"error": "invalid signature"}), 401

    event = json.loads(raw_body)

    if event.get("event") == "charge.success":
        tx = event["data"]
        reference = tx["reference"]

        # Ownership resolved from OUR pending record, not from metadata.
        pending = supabase.table("wallet_transactions").select("*").eq("reference", reference).execute()
        if pending.data:
            result = _credit_wallet_if_not_already(reference)
            telegram_id = pending.data[0]["user_id"]

            auth = tx.get("authorization", {})
            if auth.get("reusable"):
                _save_card_from_authorization(telegram_id, auth)
        else:
            logger.warning(f"Webhook received for unknown reference: {reference}")

    return jsonify({"status": "ok"}), 200


@app.route("/api/wallet/cards", methods=["GET"])
def wallet_cards():
    user, err = require_auth()
    if err:
        return err

    result = (
        supabase.table("saved_cards")
        .select("*")
        .eq("user_id", user["telegram_id"])
        .order("is_default", desc=True)
        .execute()
    )
    return jsonify({"cards": result.data})


# ============================================================
# WALLET — PIN MANAGEMENT
# ============================================================
@app.route("/api/wallet/set-pin", methods=["POST"])
@limiter.limit("5 per hour")
def set_pin():
    user, err = require_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    pin = body.get("pin")

    if not pin or not str(pin).isdigit() or len(str(pin)) != 4:
        return jsonify({"error": "Valid 4-digit PIN required"}), 400

    pin_hash = generate_password_hash(str(pin))
    supabase.table("users").update({"pin_hash": pin_hash}).eq("telegram_id", user["telegram_id"]).execute()

    return jsonify({"status": "ok"})


def verify_user_pin(telegram_id: int, pin: str) -> bool:
    row = supabase.table("users").select("pin_hash").eq("telegram_id", telegram_id).execute()
    if not row.data or not row.data[0].get("pin_hash"):
        logger.warning(f"Withdrawal blocked: user {telegram_id} has not set a PIN")
        return False
    return check_password_hash(row.data[0]["pin_hash"], str(pin))


# ============================================================
# BANKS (Feature 3B)
# ============================================================
@app.route("/api/banks", methods=["GET"])
@limiter.limit("60 per minute")
def list_banks():
    now = time.time()
    if _bank_cache["data"] and (now - _bank_cache["fetched_at"]) < BANK_CACHE_TTL:
        return jsonify({"banks": _bank_cache["data"]})

    resp = requests.get(f"{PAYSTACK_BASE}/bank?country=nigeria&use_cursor=false&perPage=100",
                         headers=paystack_headers(), timeout=20)
    data = resp.json()

    if not data.get("status"):
        return jsonify({"banks": _bank_cache["data"] or []})

    banks = [{"name": b["name"], "code": b["code"]} for b in data["data"]]
    _bank_cache["data"] = banks
    _bank_cache["fetched_at"] = now

    return jsonify({"banks": banks})


@app.route("/api/banks/resolve", methods=["POST"])
@limiter.limit("30 per minute")
def resolve_bank_account():
    body = request.get_json(silent=True) or {}
    bank_code = body.get("bank_code")
    account_number = body.get("account_number")

    if not bank_code or not account_number or len(str(account_number)) != 10 or not str(account_number).isdigit():
        return jsonify({"error": "Valid bank_code and 10-digit account_number required"}), 400

    resp = requests.get(f"{PAYSTACK_BASE}/bank/resolve",
                         params={"account_number": account_number, "bank_code": bank_code},
                         headers=paystack_headers(), timeout=20)
    data = resp.json()

    if not data.get("status"):
        return jsonify({"error": "Could not resolve account. Check details and try again."}), 422

    return jsonify({"account_name": data["data"]["account_name"]})


# ============================================================
# WITHDRAWALS (Feature 3B core)
# ============================================================
@app.route("/api/wallet/withdraw", methods=["POST"])
@limiter.limit("10 per hour")
def request_withdrawal():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]
    body = request.get_json(silent=True) or {}

    amount_kobo = body.get("amount_kobo")
    bank_name = body.get("bank_name")
    bank_code = body.get("bank_code")
    account_number = body.get("account_number")
    account_name = body.get("account_name")
    pin = body.get("pin")

    if not all([amount_kobo, bank_name, bank_code, account_number, account_name, pin]):
        return jsonify({"error": "All withdrawal fields are required"}), 400

    if amount_kobo < MIN_WITHDRAWAL_KOBO:
        return jsonify({"error": "Minimum withdrawal is 1,000 NGN"}), 400

    if not verify_user_pin(telegram_id, pin):
        return jsonify({"error": "Incorrect PIN, or PIN not yet set. Set a PIN before withdrawing."}), 401

    fresh = supabase.table("users").select("balance_kobo").eq("telegram_id", telegram_id).execute()
    if not fresh.data:
        return jsonify({"error": "User not found"}), 404

    balance_kobo = fresh.data[0]["balance_kobo"]
    held_kobo = get_held_kobo(telegram_id)
    available_kobo = balance_kobo - held_kobo

    if amount_kobo > available_kobo:
        return jsonify({"error": "Insufficient available balance. Some funds may be held in escrow for 24 hours."}), 400

    reference = f"PLW-{uuid.uuid4().hex[:16]}"

    withdrawal = {
        "user_id": telegram_id,
        "amount_kobo": amount_kobo,
        "bank_name": bank_name,
        "bank_code": bank_code,
        "account_number": account_number,
        "account_name": account_name,
        "status": "pending",
        "reference": reference,
    }

    inserted = supabase.table("withdrawals").insert(withdrawal).execute()

    supabase.table("users").update({"balance_kobo": balance_kobo - amount_kobo}).eq("telegram_id", telegram_id).execute()

    supabase.table("wallet_transactions").insert({
        "user_id": telegram_id,
        "type": "debit",
        "amount_kobo": amount_kobo,
        "reason": "withdrawal_request",
        "reference": reference,
    }).execute()

    return jsonify({"withdrawal": inserted.data[0]}), 201


@app.route("/api/wallet/withdrawals", methods=["GET"])
def list_withdrawals():
    user, err = require_auth()
    if err:
        return err

    result = (
        supabase.table("withdrawals")
        .select("*")
        .eq("user_id", user["telegram_id"])
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify({"withdrawals": result.data})


# ============================================================
# HEALTH CHECK
# ============================================================
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


# ============================================================
# ERROR HANDLERS
# ============================================================
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    logger.exception("Unhandled server error")
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Too many requests. Please slow down."}), 429


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
