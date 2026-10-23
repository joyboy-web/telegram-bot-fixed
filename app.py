import os
import re
import time
import uuid
import hmac
import json
import hashlib
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from collections import defaultdict
from urllib.parse import parse_qsl

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("paylock")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")
PORT = int(os.environ.get("PORT", 5000))

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is not set — Telegram auth will fail.")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logger.warning("SUPABASE_URL / SUPABASE_SERVICE_KEY not set — Supabase calls will fail.")
if not PAYSTACK_SECRET_KEY:
    logger.warning("PAYSTACK_SECRET_KEY is not set — Paystack calls will fail.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": FRONTEND_ORIGIN}}, supports_credentials=True)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_HEADERS = {
    "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
    "Content-Type": "application/json",
}

COVER_BUCKET = "product-covers"
FILE_BUCKET = "product-files"

ALLOWED_COVER_MIMES = {"image/jpeg", "image/png", "image/webp"}
MAX_COVER_BYTES = 5 * 1024 * 1024

ALLOWED_FILE_MIMES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/pdf",
    "video/mp4",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_FILE_BYTES = 50 * 1024 * 1024

_fund_attempts = defaultdict(list)
_bank_cache = {"data": None, "fetched_at": 0}
BANK_CACHE_TTL = 6 * 60 * 60  # 6 hours


# ---------------------------------------------------------------------------
# Fee model (LOCKED — do not alter)
# ---------------------------------------------------------------------------

PLATFORM_FEE_KOBO = 20000


def round_half_up(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def paystack_fee_kobo(price_kobo: int) -> int:
    if price_kobo <= 250000:
        fee = round_half_up(Decimal(price_kobo) * Decimal("0.015"))
    else:
        fee = round_half_up(Decimal(price_kobo) * Decimal("0.015") + Decimal(10000))
    return min(200000, fee)


def calc_fees(price_kobo: int) -> dict:
    price_kobo = int(price_kobo)
    ps_fee = paystack_fee_kobo(price_kobo)
    total_extra = PLATFORM_FEE_KOBO + ps_fee
    split = round_half_up(Decimal(total_extra) / Decimal(2))
    buyer_pays = price_kobo + split
    seller_gets = price_kobo - split
    return {
        "price_kobo": price_kobo,
        "paystack_fee_kobo": ps_fee,
        "platform_fee_kobo": PLATFORM_FEE_KOBO,
        "total_extra_kobo": total_extra,
        "split_kobo": split,
        "buyer_pays_kobo": buyer_pays,
        "seller_gets_kobo": seller_gets,
    }


# ---------------------------------------------------------------------------
# Telegram initData verification
# ---------------------------------------------------------------------------

def verify_init_data(init_data: str):
    """
    secret_key = HMAC_SHA256(key=b"WebAppData", msg=BOT_TOKEN)
    computed_hash = HMAC_SHA256(key=secret_key, msg=data_check_string)
    data_check_string = sorted k=v pairs (excluding hash) joined by \n
    Rejects stale auth_date (> 86400s old).
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        logger.warning("verify_init_data: hash mismatch")
        return None

    try:
        auth_date_int = int(pairs.get("auth_date"))
    except (TypeError, ValueError):
        return None

    if int(time.time()) - auth_date_int > 86400:
        logger.warning("verify_init_data: stale auth_date")
        return None

    user_json = pairs.get("user")
    if not user_json:
        return None

    try:
        return json.loads(user_json)
    except Exception:
        return None


def get_or_create_user(tg_user: dict):
    telegram_id = tg_user.get("id")
    if telegram_id is None:
        return None

    first_name = tg_user.get("first_name", "") or ""
    last_name = tg_user.get("last_name", "") or ""
    username = tg_user.get("username", "") or ""

    existing = (
        supabase.table("users")
        .select("*")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )

    if existing.data:
        row = existing.data[0]
        updates = {}
        if row.get("first_name") != first_name:
            updates["first_name"] = first_name
        if row.get("last_name") != last_name:
            updates["last_name"] = last_name
        if row.get("username") != username:
            updates["username"] = username
        if updates:
            supabase.table("users").update(updates).eq("telegram_id", telegram_id).execute()
            row.update(updates)
        return row

    new_row = {
        "telegram_id": telegram_id,
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "balance_kobo": 0,
    }
    inserted = supabase.table("users").insert(new_row).execute()
    return inserted.data[0] if inserted.data else new_row


def get_user_from_header():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("tma "):
        return None
    tg_user = verify_init_data(auth_header[4:].strip())
    if not tg_user:
        return None
    return get_or_create_user(tg_user)


def require_auth():
    user = get_user_from_header()
    if not user:
        return None, (jsonify({"error": "Unauthorized — invalid or missing Telegram session."}), 401)
    return user, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def upload_to_bucket(bucket: str, path: str, file_bytes: bytes, content_type: str) -> str:
    supabase.storage.from_(bucket).upload(
        path, file_bytes, {"content-type": content_type, "upsert": "true"}
    )
    return supabase.storage.from_(bucket).get_public_url(path)


def verify_user_pin(user: dict, pin: str) -> bool:
    pin_hash = user.get("pin_hash")
    if not pin_hash:
        logger.warning("verify_user_pin: no pin_hash set for telegram_id=%s", user.get("telegram_id"))
        return False
    try:
        return check_password_hash(pin_hash, pin)
    except Exception:
        logger.exception("verify_user_pin: check_password_hash failed")
        return False


def check_fund_rate_limit(telegram_id: int) -> bool:
    now = time.time()
    attempts = _fund_attempts[telegram_id]
    attempts[:] = [t for t in attempts if now - t < 60]
    if len(attempts) >= 5:
        return False
    attempts.append(now)
    return True


def get_held_kobo(telegram_id: int) -> int:
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
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
        logger.warning(
            "get_held_kobo: escrow_transactions lookup failed for telegram_id=%s (%s). Defaulting held_kobo=0.",
            telegram_id, e
        )
        return 0


def _save_card_from_authorization(telegram_id: int, authorization: dict):
    if not authorization:
        return
    auth_code = authorization.get("authorization_code")
    if not auth_code or not authorization.get("reusable"):
        return
    try:
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
        supabase.table("saved_cards").insert({
            "user_id": telegram_id,
            "authorization_code": auth_code,
            "last4": authorization.get("last4", ""),
            "exp_month": authorization.get("exp_month", ""),
            "exp_year": authorization.get("exp_year", ""),
            "brand": authorization.get("brand", authorization.get("card_type", "")),
            "is_default": False,
        }).execute()
    except Exception:
        logger.exception("_save_card_from_authorization: failed for telegram_id=%s", telegram_id)


def _credit_wallet_if_not_already(reference: str) -> bool:
    pending = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("reference", reference)
        .limit(1)
        .execute()
    )
    if not pending.data:
        logger.warning("_credit_wallet_if_not_already: no row for reference=%s", reference)
        return False

    row = pending.data[0]
    if row.get("reason") == "wallet_funding":
        return True

    telegram_id = row["user_id"]
    amount_kobo = row["amount_kobo"]

    user_result = (
        supabase.table("users")
        .select("balance_kobo")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    if not user_result.data:
        logger.error("_credit_wallet_if_not_already: user not found telegram_id=%s", telegram_id)
        return False

    current_balance = int(user_result.data[0].get("balance_kobo", 0) or 0)
    new_balance = current_balance + int(amount_kobo)

    supabase.table("users").update({"balance_kobo": new_balance}).eq("telegram_id", telegram_id).execute()
    supabase.table("wallet_transactions").update({"reason": "wallet_funding"}).eq("reference", reference).execute()
    return True


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/api/auth/validate", methods=["POST"])
@limiter.limit("30 per minute")
def auth_validate():
    body = request.get_json(silent=True) or {}
    tg_user = verify_init_data(body.get("initData", ""))
    if not tg_user:
        return jsonify({"error": "Invalid Telegram session."}), 401

    user = get_or_create_user(tg_user)
    if not user:
        return jsonify({"error": "Failed to create or load user."}), 500

    return jsonify({"user": user}), 200


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@app.route("/api/products", methods=["POST"])
@limiter.limit("20 per hour")
def create_product():
    user, err = require_auth()
    if err:
        return err

    seller_id = user["telegram_id"]

    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    category = (request.form.get("category") or "").strip()
    price_naira_raw = request.form.get("price_naira", "")

    if not name or not price_naira_raw:
        return jsonify({"error": "Product name and price are required."}), 400

    try:
        price_naira = Decimal(str(price_naira_raw))
        if price_naira <= 0:
            raise ValueError
    except Exception:
        return jsonify({"error": "Invalid price."}), 400

    price_kobo = round_half_up(price_naira * 100)

    cover_file = request.files.get("cover")
    if not cover_file or cover_file.filename == "":
        return jsonify({"error": "Cover image is required."}), 400

    cover_bytes = cover_file.read()
    if len(cover_bytes) > MAX_COVER_BYTES:
        return jsonify({"error": "Cover image exceeds 5MB limit."}), 400

    cover_mime = cover_file.mimetype or ""
    if cover_mime not in ALLOWED_COVER_MIMES:
        return jsonify({"error": "Cover must be JPG, PNG, or WEBP."}), 400

    cover_ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[cover_mime]
    cover_path = f"{seller_id}/{uuid.uuid4().hex}.{cover_ext}"

    try:
        image_url = upload_to_bucket(COVER_BUCKET, cover_path, cover_bytes, cover_mime)
    except Exception:
        logger.exception("create_product: cover upload failed for seller_id=%s", seller_id)
        return jsonify({"error": "Failed to upload cover image."}), 500

    file_url = None
    product_file = request.files.get("product_file")
    if product_file and product_file.filename != "":
        file_bytes = product_file.read()
        if len(file_bytes) > MAX_FILE_BYTES:
            return jsonify({"error": "Product file exceeds 50MB limit."}), 400

        file_mime = product_file.mimetype or ""
        if file_mime not in ALLOWED_FILE_MIMES:
            return jsonify({"error": "Product file must be ZIP, PDF, MP4, or DOCX."}), 400

        file_ext = {
            "application/zip": "zip",
            "application/x-zip-compressed": "zip",
            "application/pdf": "pdf",
            "video/mp4": "mp4",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        }[file_mime]
        file_path = f"{seller_id}/{uuid.uuid4().hex}.{file_ext}"

        try:
            file_url = upload_to_bucket(FILE_BUCKET, file_path, file_bytes, file_mime)
        except Exception:
            logger.exception("create_product: product file upload failed for seller_id=%s", seller_id)
            return jsonify({"error": "Failed to upload product file."}), 500

    fees = calc_fees(price_kobo)

    insert_row = {
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

    try:
        inserted = supabase.table("products").insert(insert_row).execute()
    except Exception:
        logger.exception("create_product: insert failed for seller_id=%s", seller_id)
        return jsonify({"error": "Failed to create product."}), 500

    product = inserted.data[0] if inserted.data else insert_row
    return jsonify({"product": product}), 201


@app.route("/api/products", methods=["GET"])
@limiter.limit("120 per minute")
def list_products():
    try:
        result = (
            supabase.table("products")
            .select("*")
            .eq("is_public", True)
            .order("created_at", desc=True)
            .execute()
        )
        return jsonify({"products": result.data or []}), 200
    except Exception:
        logger.exception("list_products: query failed")
        return jsonify({"products": []}), 200


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

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
            "wallet_balance: get_held_kobo raised unexpectedly for telegram_id=%s (%s). Defaulting held_kobo=0.",
            telegram_id, e
        )
        held_kobo = 0
    return jsonify({
        "balance_kobo": balance_kobo,
        "held_kobo": held_kobo
    }), 200


@app.route("/api/wallet/transactions", methods=["GET"])
@limiter.limit("60 per minute")
def wallet_transactions():
    user, err = require_auth()
    if err:
        return err
    telegram_id = user["telegram_id"]
    try:
        result = (
            supabase.table("wallet_transactions")
            .select("*")
            .eq("user_id", telegram_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        return jsonify({"transactions": result.data or []}), 200
    except Exception:
        logger.exception("wallet_transactions: query failed for telegram_id=%s", telegram_id)
        return jsonify({"transactions": []}), 200


@app.route("/api/wallet/fund/initialize", methods=["POST"])
@limiter.limit("10 per minute")
def wallet_fund_initialize():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]

    if not check_fund_rate_limit(telegram_id):
        return jsonify({"error": "Too many funding attempts. Please wait a moment and try again."}), 429

    body = request.get_json(silent=True) or {}
    amount_kobo = body.get("amount_kobo")
    email = (body.get("email") or "").strip()
    save_card = bool(body.get("save_card", False))
    card_id = body.get("card_id")

    try:
        amount_kobo = int(amount_kobo)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount."}), 400

    if amount_kobo < 10000:
        return jsonify({"error": "Minimum funding amount is ₦100."}), 400
    if not email:
        return jsonify({"error": "Email is required."}), 400

    reference = f"PLF-{uuid.uuid4().hex[:16]}"

    try:
        supabase.table("wallet_transactions").insert({
            "user_id": telegram_id,
            "type": "credit",
            "amount_kobo": amount_kobo,
            "reason": "wallet_funding_pending",
            "reference": reference,
        }).execute()
    except Exception:
        logger.exception("wallet_fund_initialize: failed to create pending record for telegram_id=%s", telegram_id)
        return jsonify({"error": "Failed to initialize funding."}), 500

    if card_id:
        try:
            card_result = (
                supabase.table("saved_cards")
                .select("*")
                .eq("id", card_id)
                .eq("user_id", telegram_id)
                .limit(1)
                .execute()
            )
        except Exception:
            logger.exception("wallet_fund_initialize: saved_cards lookup failed for telegram_id=%s", telegram_id)
            return jsonify({"error": "Failed to load saved card."}), 500

        if not card_result.data:
            return jsonify({"error": "Saved card not found."}), 404

        auth_code = card_result.data[0]["authorization_code"]

        try:
            resp = requests.post(
                f"{PAYSTACK_BASE_URL}/transaction/charge_authorization",
                headers=PAYSTACK_HEADERS,
                json={
                    "authorization_code": auth_code,
                    "email": email,
                    "amount": amount_kobo,
                    "reference": reference,
                },
                timeout=30,
            )
            data = resp.json()
        except Exception:
            logger.exception("wallet_fund_initialize: paystack charge_authorization failed for telegram_id=%s", telegram_id)
            return jsonify({"error": "Payment provider error."}), 502

        if data.get("status") and data.get("data", {}).get("status") == "success":
            _credit_wallet_if_not_already(reference)
            if save_card:
                _save_card_from_authorization(telegram_id, data["data"].get("authorization"))
            return jsonify({"charge_immediately": True, "status": "success", "reference": reference}), 200

        return jsonify({
            "charge_immediately": True,
            "status": "failed",
            "reference": reference,
            "message": data.get("message", "Charge failed."),
        }), 400

    return jsonify({
        "charge_immediately": False,
        "reference": reference,
        "public_key": PAYSTACK_PUBLIC_KEY,
    }), 200


@app.route("/api/wallet/verify-transaction", methods=["POST"])
@limiter.limit("20 per minute")
def wallet_verify_transaction():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]
    body = request.get_json(silent=True) or {}
    reference = (body.get("reference") or "").strip()

    if not reference:
        return jsonify({"error": "Reference is required."}), 400

    pending = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("reference", reference)
        .limit(1)
        .execute()
    )
    if not pending.data:
        return jsonify({"error": "Transaction reference not found."}), 404

    row = pending.data[0]
    if row["user_id"] != telegram_id:
        return jsonify({"error": "Forbidden."}), 403

    try:
        resp = requests.get(
            f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
            headers=PAYSTACK_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except Exception:
        logger.exception("wallet_verify_transaction: paystack verify failed for reference=%s", reference)
        return jsonify({"error": "Payment provider error."}), 502

    tx_data = data.get("data", {})
    if not data.get("status") or tx_data.get("status") != "success":
        return jsonify({"status": "failed", "message": data.get("message", "Verification failed.")}), 400

    credited = _credit_wallet_if_not_already(reference)

    authorization = tx_data.get("authorization")
    if authorization and authorization.get("reusable"):
        _save_card_from_authorization(telegram_id, authorization)

    return jsonify({
        "status": "success",
        "credited": credited,
        "amount_kobo": row["amount_kobo"],
        "reference": reference,
        "paid_at": tx_data.get("paid_at"),
    }), 200


@app.route("/api/webhook/paystack", methods=["POST"])
def webhook_paystack():
    raw_body = request.get_data()
    signature = request.headers.get("x-paystack-signature", "")

    computed_signature = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()

    if not hmac.compare_digest(computed_signature, signature):
        logger.warning("webhook_paystack: signature mismatch")
        return jsonify({"error": "Invalid signature."}), 401

    event = request.get_json(silent=True) or {}

    if event.get("event") == "charge.success":
        data = event.get("data", {})
        reference = data.get("reference")
        if reference:
            _credit_wallet_if_not_already(reference)
            authorization = data.get("authorization")
            if authorization and authorization.get("reusable"):
                pending = (
                    supabase.table("wallet_transactions")
                    .select("user_id")
                    .eq("reference", reference)
                    .limit(1)
                    .execute()
                )
                if pending.data:
                    _save_card_from_authorization(pending.data[0]["user_id"], authorization)

    return jsonify({"status": "ok"}), 200


@app.route("/api/wallet/cards", methods=["GET"])
@limiter.limit("60 per minute")
def wallet_cards():
    user, err = require_auth()
    if err:
        return err
    telegram_id = user["telegram_id"]
    try:
        result = (
            supabase.table("saved_cards")
            .select("*")
            .eq("user_id", telegram_id)
            .order("is_default", desc=True)
            .execute()
        )
        return jsonify({"cards": result.data or []}), 200
    except Exception:
        logger.exception("wallet_cards: query failed for telegram_id=%s", telegram_id)
        return jsonify({"cards": []}), 200


@app.route("/api/wallet/set-pin", methods=["POST"])
@limiter.limit("5 per hour")
def wallet_set_pin():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]
    body = request.get_json(silent=True) or {}
    pin = str(body.get("pin", "")).strip()

    if not re.fullmatch(r"\d{4}", pin):
        return jsonify({"error": "PIN must be exactly 4 digits."}), 400

    pin_hash = generate_password_hash(pin)

    try:
        supabase.table("users").update({"pin_hash": pin_hash}).eq("telegram_id", telegram_id).execute()
    except Exception:
        logger.exception("wallet_set_pin: update failed for telegram_id=%s", telegram_id)
        return jsonify({"error": "Failed to set PIN."}), 500

    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Banks
# ---------------------------------------------------------------------------

@app.route("/api/banks", methods=["GET"])
@limiter.limit("60 per minute")
def list_banks():
    now = time.time()
    if _bank_cache["data"] and (now - _bank_cache["fetched_at"] < BANK_CACHE_TTL):
        return jsonify({"banks": _bank_cache["data"]}), 200

    try:
        resp = requests.get(
            f"{PAYSTACK_BASE_URL}/bank",
            headers=PAYSTACK_HEADERS,
            params={"country": "nigeria", "use_cursor": "false", "perPage": 100},
            timeout=30,
        )
        data = resp.json()
    except Exception:
        logger.exception("list_banks: paystack fetch failed")
        if _bank_cache["data"]:
            return jsonify({"banks": _bank_cache["data"]}), 200
        return jsonify({"error": "Failed to load banks."}), 502

    banks = data.get("data", [])
    _bank_cache["data"] = banks
    _bank_cache["fetched_at"] = now
    return jsonify({"banks": banks}), 200


@app.route("/api/banks/resolve", methods=["POST"])
@limiter.limit("30 per minute")
def resolve_bank_account():
    body = request.get_json(silent=True) or {}
    bank_code = (body.get("bank_code") or "").strip()
    account_number = (body.get("account_number") or "").strip()

    if not bank_code or not re.fullmatch(r"\d{10}", account_number):
        return jsonify({"error": "Valid bank_code and 10-digit account_number are required."}), 400

    try:
        resp = requests.get(
            f"{PAYSTACK_BASE_URL}/bank/resolve",
            headers=PAYSTACK_HEADERS,
            params={"account_number": account_number, "bank_code": bank_code},
            timeout=30,
        )
        data = resp.json()
    except Exception:
        logger.exception("resolve_bank_account: paystack resolve failed")
        return jsonify({"error": "Failed to resolve account."}), 502

    if not data.get("status"):
        return jsonify({"error": data.get("message", "Could not resolve account.")}), 400

    account_data = data.get("data", {})
    return jsonify({
        "account_name": account_data.get("account_name"),
        "account_number": account_data.get("account_number"),
    }), 200


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------

@app.route("/api/wallet/withdraw", methods=["POST"])
@limiter.limit("10 per hour")
def wallet_withdraw():
    user, err = require_auth()
    if err:
        return err

    telegram_id = user["telegram_id"]
    body = request.get_json(silent=True) or {}

    amount_kobo = body.get("amount_kobo")
    bank_name = (body.get("bank_name") or "").strip()
    bank_code = (body.get("bank_code") or "").strip()
    account_number = (body.get("account_number") or "").strip()
    account_name = (body.get("account_name") or "").strip()
    pin = str(body.get("pin", "")).strip()

    try:
        amount_kobo = int(amount_kobo)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount."}), 400

    if amount_kobo < 100000:
        return jsonify({"error": "Minimum withdrawal amount is ₦1,000."}), 400

    if not (bank_name and bank_code and account_number and account_name):
        return jsonify({"error": "Bank details are incomplete."}), 400

    if not verify_user_pin(user, pin):
        return jsonify({"error": "Incorrect PIN, or PIN not yet set. Set a PIN before withdrawing."}), 401

    try:
        balance_kobo = int(user.get("balance_kobo", 0) or 0)
    except Exception:
        balance_kobo = 0

    held_kobo = get_held_kobo(telegram_id)
    available_kobo = balance_kobo - held_kobo

    if amount_kobo > available_kobo:
        return jsonify({"error": "Insufficient available balance."}), 400

    reference = f"PLW-{uuid.uuid4().hex[:16]}"

    try:
        supabase.table("withdrawals").insert({
            "user_id": telegram_id,
            "amount_kobo": amount_kobo,
            "bank_name": bank_name,
            "bank_code": bank_code,
            "account_number": account_number,
            "account_name": account_name,
            "status": "pending",
            "reference": reference,
        }).execute()

        new_balance = balance_kobo - amount_kobo
        supabase.table("users").update({"balance_kobo": new_balance}).eq("telegram_id", telegram_id).execute()

        supabase.table("wallet_transactions").insert({
            "user_id": telegram_id,
            "type": "debit",
            "amount_kobo": amount_kobo,
            "reason": "withdrawal_request",
            "reference": reference,
        }).execute()
    except Exception:
        logger.exception("wallet_withdraw: failed to process withdrawal for telegram_id=%s", telegram_id)
        return jsonify({"error": "Failed to process withdrawal."}), 500

    return jsonify({"status": "pending", "reference": reference}), 201


@app.route("/api/wallet/withdrawals", methods=["GET"])
@limiter.limit("60 per minute")
def wallet_withdrawals():
    user, err = require_auth()
    if err:
        return err
    telegram_id = user["telegram_id"]
    try:
        result = (
            supabase.table("withdrawals")
            .select("*")
            .eq("user_id", telegram_id)
            .order("created_at", desc=True)
            .execute()
        )
        return jsonify({"withdrawals": result.data or []}), 200
    except Exception:
        logger.exception("wallet_withdrawals: query failed for telegram_id=%s", telegram_id)
        return jsonify({"withdrawals": []}), 200


# ---------------------------------------------------------------------------
# Health & error handlers
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()}), 200


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Too many requests. Please slow down."}), 429


@app.errorhandler(500)
def server_error(e):
    logger.exception("Unhandled server error")
    return jsonify({"error": "Internal server error."}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
