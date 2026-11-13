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
#
# Optional env vars (additive): ADMIN_ID (Telegram id of the admin; empty = every
#   /api/admin/* route returns 403), BOT_USERNAME (default PayLockOfficialBot).
#
# Additive schema this file expects (every use is guarded: a missing table or
# column makes that one feature answer 501 NOT_READY / fall back, never crash):
#   products.code, sales_count, avg_rating, is_boosted, boosted_until, visible
#   users.banned, banned_permanent, referred_by
#   tables: ratings (unique order_id), disputes, product_reports, appeals,
#           referrals, webhook_logs
# =============================================================================

import os
import re
import io
import json
import time
import math
import uuid
import secrets
import hmac
import hashlib
import logging
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import requests
import bcrypt
from flask import Flask, request, jsonify, send_from_directory, abort, Response
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
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")

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

# --- CONFIG (additive) --------------------------------------------------------
BOT_USERNAME = os.environ.get("BOT_USERNAME", "PayLockOfficialBot")
# Leave empty until you fill it in (here or via the ADMIN_ID env var).
# While empty, EVERY /api/admin/* route returns 403.
ADMIN_ID = str(os.environ.get("ADMIN_ID", "") or "").strip()

BAN_THRESHOLD = 5                  # CONFIRMED reports/disputes before auto-ban
BOOST_FEE_KOBO = 50000             # NGN 500 per boost
BOOST_HOURS = 24
DAILY_LISTING_LIMIT = 10
PRODUCT_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
REPORT_REASONS = {"scam": "Scam", "fake": "Fake", "wrong file": "Wrong file", "spam": "Spam"}

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
# Helpers — additions (admin, ban, products, escrow, logs, PDF)
# =============================================================================

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value):
    """Parses a Supabase/ISO timestamp to an aware datetime, or None."""
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    # Python < 3.11 only accepts 3 or 6 fractional digits; normalise to 6.
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_schema_missing(exc):
    s = str(exc).lower()
    return any(m in s for m in (
        "does not exist", "schema cache", "pgrst205", "pgrst204", "42p01", "42703",
    ))


def _missing_column_in(exc, col):
    s = str(exc).lower()
    return (f"'{col}' column" in s) or (f'column "{col}"' in s) or (f"products.{col}" in s)


def _not_ready():
    return jsonify({"error": "This feature is not available yet", "code": "NOT_READY"}), 501


def _db_error(exc, log_msg):
    """501 NOT_READY when a table/column is missing, otherwise a logged 500."""
    if _is_schema_missing(exc):
        logging.warning("%s: schema not ready (%s)", log_msg, exc)
        return _not_ready()
    logging.exception(log_msg)
    return jsonify({"error": "Something went wrong"}), 500


def _is_admin(user):
    return bool(ADMIN_ID) and str(user.get("telegram_id")) == ADMIN_ID


def admin_only(f):
    """Use BELOW @require_auth. Empty ADMIN_ID means nobody is admin (403)."""
    @wraps(f)
    def wrapper(user, *args, **kwargs):
        if not _is_admin(user):
            return jsonify({"error": "Forbidden", "code": "ADMIN_ONLY"}), 403
        return f(user, *args, **kwargs)
    return wrapper


def _auth_user_or_none():
    """Same checks as require_auth, for routes that are public unless asked."""
    init_data = extract_init_data()
    if not init_data:
        return None
    tg_user = verify_init_data(init_data)
    if not tg_user:
        return None
    user = get_or_create_user(tg_user)
    if not user or user.get("deleted_at"):
        return None
    return user


def _banned_response(user):
    if user.get("banned"):
        return jsonify({"error": "Account temporarily banned", "code": "ACCOUNT_BANNED"}), 403
    return None


def _redact_payload(obj, depth=0):
    """Strips card/customer data before anything is written to webhook_logs."""
    sensitive = {"authorization", "authorization_code", "customer", "card", "bin"}
    if depth > 6:
        return None
    if isinstance(obj, dict):
        return {k: _redact_payload(v, depth + 1) for k, v in obj.items() if k not in sensitive}
    if isinstance(obj, list):
        return [_redact_payload(v, depth + 1) for v in obj[:50]]
    return obj


def _log_webhook(reference, event, payload, status):
    try:
        supabase.table("webhook_logs").insert({
            "reference": reference or None,
            "event": event,
            "payload": _redact_payload(payload),
            "status": status,
            "created_at": _now_iso(),
        }).execute()
    except Exception as exc:
        logging.warning("_log_webhook: insert failed (%s)", exc)


def _fetch_all(build, page=1000, max_pages=20):
    """build(start, end) -> query; pages past Supabase's 1000-row default cap."""
    rows = []
    for i in range(max_pages):
        chunk = build(i * page, i * page + page - 1).execute().data or []
        rows.extend(chunk)
        if len(chunk) < page:
            break
    return rows


# ---- products ----------------------------------------------------------------

PRODUCT_EXTRA_COLUMNS = "code,sales_count,avg_rating,is_boosted,boosted_until"


def _new_product_code():
    """6 chars A-Z0-9, unique when the code column exists."""
    code = ""
    for _ in range(10):
        code = "".join(secrets.choice(PRODUCT_CODE_ALPHABET) for _ in range(6))
        try:
            res = supabase.table("products").select("id").eq("code", code).limit(1).execute()
            if not res.data:
                return code
        except Exception:
            return code  # column missing: cannot check; insert fallback handles it
    return code


def _ensure_code(row):
    """Backfills a missing code on an old product. No-op if the column is absent."""
    if "code" not in row or row.get("code") or row.get("id") is None:
        return
    try:
        code = _new_product_code()
        supabase.table("products").update({"code": code}).eq("id", row["id"]).execute()
        row["code"] = code
    except Exception:
        logging.exception("_ensure_code: backfill failed for product %s", row.get("id"))


def _products_select(apply, with_fees=False):
    """
    apply(columns) -> executed query. Tries the extended column list first and
    falls back to the original columns if the new product columns are not
    migrated yet, so the marketplace never goes blank.
    """
    tail = ",platform_fee_kobo,paystack_fee_kobo" if with_fees else ""
    base = PRODUCT_PUBLIC_COLUMNS + ",is_hidden" + tail
    try:
        return apply(base + "," + PRODUCT_EXTRA_COLUMNS).data or []
    except Exception:
        logging.warning("_products_select: extended columns unavailable, using base columns")
    return apply(base).data or []


def _boost_active(row):
    until = _parse_ts(row.get("boosted_until"))
    if until:
        return until > datetime.now(timezone.utc)
    return bool(row.get("is_boosted"))


def _normalize_product(row):
    row["visible"] = not bool(row.pop("is_hidden", False))
    try:
        row["sales_count"] = int(row.get("sales_count") or 0)
    except (TypeError, ValueError):
        row["sales_count"] = 0
    try:
        row["avg_rating"] = round(float(row.get("avg_rating") or 0), 2)
    except (TypeError, ValueError):
        row["avg_rating"] = 0
    row["is_boosted"] = _boost_active(row)
    row.setdefault("code", None)
    if row.get("code"):
        row["share_link"] = f"https://t.me/{BOT_USERNAME}?start=prod_{row['code']}"
    return row


def _find_product(key, mode="any", include_hidden=False):
    """
    Finds one PUBLIC product by code or id (hidden products return None unless
    include_hidden=True, in which case the row comes back with visible=False).
    mode='code' looks at the code column only (GET /api/products?code=).
    """
    key = str(key or "").strip()
    if not key:
        return None
    code = (key[5:] if key.lower().startswith("prod_") else key).upper()
    code_ok = bool(re.fullmatch(r"[A-Z0-9]{6}", code))

    if mode == "code" or key.lower().startswith("prod_"):
        order = [("code", code)] if code_ok else []
    elif key.isdigit():
        order = [("id", key)] + ([("code", code)] if code_ok else [])
    else:
        order = ([("code", code)] if code_ok else []) + [("id", key)]

    for col, val in order:
        try:
            rows = _products_select(
                lambda cols: supabase.table("products").select(cols).eq(col, val).limit(1).execute(),
                with_fees=True,
            )
        except Exception:
            continue  # bad id format for the column type, or column missing
        if not rows:
            continue
        row = rows[0]
        if row.get("is_hidden") and not include_hidden:
            return None
        _ensure_code(row)
        return _normalize_product(row)
    return None


MAX_EXTRA_IMAGES = 3


def _save_extra_images(tid, product_id):
    """
    Reads extra_images (repeatable), extra_image_1..3 from the current multipart
    request; keeps at most 3 valid JPG/PNG/WEBP files of <= 5MB; uploads them to
    COVER_BUCKET and records them in extra_images. Invalid files are skipped and
    a missing extra_images table is skipped silently. Returns the saved count.
    """
    candidates = list(request.files.getlist("extra_images"))
    for key in ("extra_image_1", "extra_image_2", "extra_image_3"):
        candidates.extend(request.files.getlist(key))

    saved = 0
    for f in candidates:
        if saved >= MAX_EXTRA_IMAGES:
            break
        try:
            if not f or not f.filename or not _allowed_file(f.filename, ALLOWED_COVER_EXT):
                continue
            blob = f.read()
            if not blob or len(blob) > MAX_COVER_BYTES:
                continue
            position = saved + 1
            path = f"{tid}/{uuid.uuid4().hex}_extra_{position}_{secure_name(f.filename)}"
            supabase.storage.from_(COVER_BUCKET).upload(
                path=path,
                file=blob,
                file_options={"content-type": f.mimetype or "image/jpeg"},
            )
            url = supabase.storage.from_(COVER_BUCKET).get_public_url(path)
            try:
                supabase.table("extra_images").insert({
                    "product_id": product_id,
                    "image_url": url,
                    "position": position,
                    "created_at": _now_iso(),
                }).execute()
            except Exception as exc:
                if _is_schema_missing(exc):
                    return saved  # table not created yet: skip silently
                logging.warning("_save_extra_images: insert failed for product %s", product_id, exc_info=True)
                continue
            saved += 1
        except Exception:
            logging.warning("_save_extra_images: one image skipped for product %s", product_id, exc_info=True)
    return saved


def _refresh_product_sales(product_id):
    try:
        res = (
            supabase.table("escrow_orders")
            .select("id", count="exact")
            .eq("product_id", product_id)
            .eq("status", "released")
            .execute()
        )
        supabase.table("products").update({"sales_count": int(res.count or 0)}).eq("id", product_id).execute()
    except Exception:
        logging.warning("_refresh_product_sales: skipped for product %s", product_id, exc_info=True)


def _refresh_product_rating(product_id):
    try:
        rows = _fetch_all(lambda a, b: (
            supabase.table("ratings").select("stars").eq("product_id", product_id).order("id").range(a, b)
        ))
        stars = [int(r["stars"]) for r in rows if r.get("stars")]
        avg = round(sum(stars) / len(stars), 2) if stars else 0
        supabase.table("products").update({"avg_rating": avg}).eq("id", product_id).execute()
    except Exception:
        logging.warning("_refresh_product_rating: skipped for product %s", product_id, exc_info=True)


# ---- escrow ------------------------------------------------------------------

def _credit_balance(telegram_id, amount_kobo):
    """Adds money to a wallet with a compare-and-set so concurrent changes are not lost."""
    for _ in range(5):
        res = supabase.table("users").select("balance_kobo").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        if not rows:
            raise RuntimeError("user not found")
        current = int(rows[0].get("balance_kobo") or 0)
        upd = (
            supabase.table("users")
            .update({"balance_kobo": current + int(amount_kobo)})
            .eq("telegram_id", telegram_id)
            .eq("balance_kobo", current)
            .execute()
        )
        if upd.data:
            return current + int(amount_kobo)
    raise RuntimeError("balance kept changing, credit not applied")


def _escrow_get(order_id):
    try:
        res = supabase.table("escrow_orders").select("*").eq("id", order_id).limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        logging.warning("_escrow_get: lookup failed for %s", order_id, exc_info=True)
        return None


def _mark_referral_rewarded(*user_ids):
    """Flags (does not pay) the referral reward on a referred user's first release."""
    for uid in user_ids:
        try:
            (
                supabase.table("referrals")
                .update({"rewarded": True})
                .eq("referred_id", int(uid))
                .eq("rewarded", False)
                .execute()
            )
        except Exception:
            logging.warning("_mark_referral_rewarded: skipped for %s", uid, exc_info=True)


def _release_escrow(order_id, from_statuses=("held",)):
    """
    Atomically moves the order to 'released' and pays the seller.
    Returns the order row, or None if it was not in a releasable state.
    Raises only if the seller could not be credited (status is put back first).
    """
    order = _escrow_get(order_id)
    if not order or order.get("status") not in from_statuses:
        return None
    prior = order["status"]
    claim = (
        supabase.table("escrow_orders")
        .update({"status": "released"})
        .eq("id", order_id)
        .eq("status", prior)
        .execute()
    )
    if not claim.data:
        return None

    seller_id = int(order["seller_id"])
    amount = int(order.get("seller_gets_kobo") or 0)
    try:
        _credit_balance(seller_id, amount)
    except Exception:
        logging.exception("_release_escrow: crediting seller failed for order %s", order_id)
        try:
            supabase.table("escrow_orders").update({"status": prior}).eq("id", order_id).eq("status", "released").execute()
        except Exception:
            logging.exception("_release_escrow: CRITICAL could not restore status for order %s", order_id)
        raise

    try:
        supabase.table("wallet_transactions").insert({
            "user_id": seller_id,
            "type": "credit",
            "amount_kobo": amount,
            "reason": "escrow_release",
            "reference": f"{order.get('reference') or order_id}-REL",
        }).execute()
    except Exception:
        logging.exception("_release_escrow: seller credited but transaction row failed for order %s", order_id)

    _refresh_product_sales(order.get("product_id"))
    _mark_referral_rewarded(order.get("buyer_id"), seller_id)
    order["status"] = "released"
    return order


def _refund_escrow(order_id, from_statuses=("held", "disputed")):
    """
    Auto-refund helper: pays buyer_pays_kobo back to the buyer, sets the order to
    'refunded' and logs an escrow_refund transaction. Returns the order or None.
    """
    order = _escrow_get(order_id)
    if not order or order.get("status") not in from_statuses:
        return None
    prior = order["status"]
    claim = (
        supabase.table("escrow_orders")
        .update({"status": "refunded"})
        .eq("id", order_id)
        .eq("status", prior)
        .execute()
    )
    if not claim.data:
        return None

    buyer_id = int(order["buyer_id"])
    amount = int(order.get("buyer_pays_kobo") or 0)
    try:
        _credit_balance(buyer_id, amount)
    except Exception:
        logging.exception("_refund_escrow: crediting buyer failed for order %s", order_id)
        try:
            supabase.table("escrow_orders").update({"status": prior}).eq("id", order_id).eq("status", "refunded").execute()
        except Exception:
            logging.exception("_refund_escrow: CRITICAL could not restore status for order %s", order_id)
        raise

    try:
        supabase.table("wallet_transactions").insert({
            "user_id": buyer_id,
            "type": "credit",
            "amount_kobo": amount,
            "reason": "escrow_refund",
            "reference": f"{order.get('reference') or order_id}-REF",
        }).execute()
    except Exception:
        logging.exception("_refund_escrow: buyer credited but transaction row failed for order %s", order_id)

    order["status"] = "refunded"
    return order


def _open_dispute(user, order_id, reason, require_reason=False):
    """Shared by /api/dispute and /api/escrow/dispute. Returns (response, status)."""
    tid = int(user["telegram_id"])
    order_id = str(order_id or "").strip()
    reason = str(reason or "").strip()[:1000]
    if not order_id:
        return jsonify({"error": "order_id required"}), 400
    if require_reason and not reason:
        return jsonify({"error": "reason is required"}), 400

    order = _escrow_get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    if tid not in (int(order.get("buyer_id") or 0), int(order.get("seller_id") or 0)):
        return jsonify({"error": "Forbidden"}), 403
    if order.get("status") != "held":
        return jsonify({"error": "Only held orders can be disputed", "code": "ORDER_NOT_HELD"}), 400

    # Freeze first: this is what protects the money.
    claim = (
        supabase.table("escrow_orders")
        .update({"status": "disputed"})
        .eq("id", order_id)
        .eq("status", "held")
        .execute()
    )
    if not claim.data:
        return jsonify({"error": "Order changed, please retry"}), 409

    try:
        supabase.table("disputes").insert({
            "order_id": order.get("id"),
            "product_id": order.get("product_id"),
            "opener_id": tid,
            "buyer_id": order.get("buyer_id"),
            "seller_id": order.get("seller_id"),
            "reason": reason,
            "status": "open",
            "created_at": _now_iso(),
        }).execute()
    except Exception:
        logging.exception("_open_dispute: escrow frozen but dispute record failed for order %s", order_id)
    return jsonify({"ok": True, "status": "success"}), 200


# ---- ban / appeal ------------------------------------------------------------

def _update_user_flags(telegram_id, fields):
    try:
        supabase.table("users").update(fields).eq("telegram_id", telegram_id).execute()
    except Exception as exc:
        if "banned_permanent" in fields and _is_schema_missing(exc):
            reduced = {k: v for k, v in fields.items() if k != "banned_permanent"}
            supabase.table("users").update(reduced).eq("telegram_id", telegram_id).execute()
        else:
            raise


def _ban_user(seller_id, permanent=False):
    fields = {"banned": True}
    if permanent:
        fields["banned_permanent"] = True
    _update_user_flags(int(seller_id), fields)
    try:
        supabase.table("products").update({"is_hidden": True}).eq("seller_id", str(seller_id)).execute()
    except Exception:
        logging.exception("_ban_user: hiding products failed for %s", seller_id)


def _unban_user(seller_id):
    _update_user_flags(int(seller_id), {"banned": False, "banned_permanent": False})
    try:
        res = supabase.table("users").select("deleted_at").eq("telegram_id", int(seller_id)).limit(1).execute()
        rows = res.data or []
        if rows and not rows[0].get("deleted_at"):  # never unhide a deleted account's products
            supabase.table("products").update({"is_hidden": False}).eq("seller_id", str(seller_id)).execute()
    except Exception:
        logging.exception("_unban_user: unhiding products failed for %s", seller_id)


def _check_auto_ban(seller_id):
    """Bans the seller once BAN_THRESHOLD (5) CONFIRMED reports+disputes exist. Never on 3."""
    try:
        seller_id = int(seller_id)
    except (TypeError, ValueError):
        return False
    total = 0
    for table in ("product_reports", "disputes"):
        try:
            res = (
                supabase.table(table)
                .select("id", count="exact")
                .eq("seller_id", seller_id)
                .eq("status", "confirmed")
                .execute()
            )
            total += int(res.count or 0)
        except Exception:
            logging.warning("_check_auto_ban: could not count %s", table, exc_info=True)
    if total < BAN_THRESHOLD:
        return False
    try:
        _ban_user(seller_id)
        return True
    except Exception:
        logging.exception("_check_auto_ban: ban failed for %s", seller_id)
        return False


# ---- referral ----------------------------------------------------------------

def _handle_referral(user, init_data):
    """ref_<id> / inv_<id> start_param -> users.referred_by + referrals row. prod_* is ignored."""
    try:
        sp = dict(parse_qsl(init_data, keep_blank_values=True)).get("start_param") or ""
        m = re.fullmatch(r"(?:ref|inv)_(\d{1,15})", sp)
        if not m:
            return
        inviter = int(m.group(1))
        me = int(user["telegram_id"])
        if inviter == me or user.get("referred_by"):
            return
        inv = supabase.table("users").select("telegram_id,deleted_at").eq("telegram_id", inviter).limit(1).execute()
        rows = inv.data or []
        if not rows or rows[0].get("deleted_at"):
            return
        upd = (
            supabase.table("users")
            .update({"referred_by": inviter})
            .eq("telegram_id", me)
            .is_("referred_by", "null")
            .execute()
        )
        if not upd.data:
            return
        user["referred_by"] = inviter
        supabase.table("referrals").insert({
            "referrer_id": inviter,
            "referred_id": me,
            "created_at": _now_iso(),
            "rewarded": False,
        }).execute()
    except Exception:
        logging.warning("_handle_referral: skipped", exc_info=True)


# ---- minimal PDF writer (no extra dependency) --------------------------------

def _pdf_text(value, limit=90):
    s = re.sub(r"[\r\n\t]", " ", str(value if value is not None else "-"))[:limit]
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_simple_pdf(title, rows):
    """One-page A4 text PDF (Helvetica). rows = [(label, value), ...]."""
    ops = [f"BT /F2 20 Tf 50 790 Td ({_pdf_text(title)}) Tj ET"]
    y = 750
    for label, value in rows:
        ops.append(f"BT /F2 11 Tf 50 {y} Td ({_pdf_text(label, 40)}) Tj ET")
        ops.append(f"BT /F1 11 Tf 210 {y} Td ({_pdf_text(value)}) Tj ET")
        y -= 22
    stream = "\n".join(ops).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


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

    _handle_referral(user, init_data)

    return jsonify({
        "ok": True,
        "user": {
            "telegram_id": user["telegram_id"],
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "username": user.get("username"),
            "banned": bool(user.get("banned")),
            "banned_permanent": bool(user.get("banned_permanent")),
        },
    }), 200


# =============================================================================
# Products
# =============================================================================

PRODUCT_PUBLIC_COLUMNS = (
    "id,name,title,description,price,price_kobo,buyer_pays_kobo,"
    "seller_gets_kobo,image_url,file_url,seller_id,owner_id,"
    "created_at,category"

)


@app.route("/api/products", methods=["GET"])
def products_list():
    # Explicit columns only. Never select("*"), never reference is_public.
    # Hidden products (deleted sellers, owner-hidden, banned) are excluded via is_hidden.

    # ?code=XXXX -> a single product (deep link)
    code = (request.args.get("code") or "").strip()
    if code:
        product = _find_product(code, mode="code")
        if not product:
            return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404
        return jsonify({"product": product}), 200

    # ?mine=1 -> only the signed-in seller's products (hidden ones included)
    if (request.args.get("mine") or "").strip().lower() in ("1", "true", "yes"):
        user = _auth_user_or_none()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            rows = _products_select(lambda cols: (
                supabase.table("products")
                .select(cols)
                .eq("seller_id", str(user["telegram_id"]))
                .order("created_at", desc=True)
                .limit(200)
                .execute()
            ))
            return jsonify({"products": [_normalize_product(r) for r in rows]}), 200
        except Exception:
            logging.exception("products_list: failed to load own products")
            return jsonify({"products": []}), 200

    try:
        rows = _products_select(lambda cols: (
            supabase.table("products")
            .select(cols)
            .eq("is_hidden", False)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        ))
        backfilled = 0
        for r in rows:  # old products get a code gradually (25 per request)
            if "code" in r and not r.get("code") and backfilled < 25:
                _ensure_code(r)
                backfilled += 1
        products = [_normalize_product(r) for r in rows]
        # boosted first, then most sales; created_at desc is kept for ties (stable sort)
        products.sort(key=lambda p: (0 if p["is_boosted"] else 1, -p["sales_count"]))
        return jsonify({"products": products}), 200
    except Exception:
        logging.exception("products_list: failed to load products")
        return jsonify({"products": []}), 200


@app.route("/api/products/<product_id>/images", methods=["GET"])
def products_images(product_id):
    # Public for visible products; a hidden product's images are only shown to its owner.
    product = _find_product(product_id, mode="any", include_hidden=True)
    if not product:
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404
    if not product.get("visible", True):
        user = _auth_user_or_none()
        if not user or str(user["telegram_id"]) not in (str(product.get("seller_id")), str(product.get("owner_id"))):
            return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404
    try:
        rows = (
            supabase.table("extra_images")
            .select("image_url,position")
            .eq("product_id", product["id"])
            .order("position")
            .limit(10)
            .execute()
        ).data or []
    except Exception:
        return jsonify({"images": []}), 200  # table missing or lookup failed
    return jsonify({"images": [
        {"image_url": r.get("image_url"), "position": r.get("position")} for r in rows
    ]}), 200


@app.route("/api/products/<product_id>", methods=["GET"])
def products_get(product_id):
    # <product_id> may be a product id OR its 6-char / prod_XXXXXX code.
    product = _find_product(product_id, mode="any")
    if not product:
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404
    return jsonify({"product": product}), 200


@app.route("/api/products", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def products_create(user):
    tid = user["telegram_id"]

    blocked = _banned_response(user)
    if blocked:
        return blocked

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

    # Duplicate + daily-limit guards (rolling 24h). A failed lookup never blocks listing.
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent = (
            supabase.table("products")
            .select("id,name,price_kobo")
            .eq("seller_id", str(tid))
            .gte("created_at", since)
            .limit(100)
            .execute()
        ).data or []
    except Exception:
        logging.exception("products_create: duplicate/daily lookup failed for telegram_id=%s", tid)
        recent = []
    for r in recent:
        if str(r.get("name") or "").strip().lower() == name.lower() and int(r.get("price_kobo") or 0) == price_kobo:
            return jsonify({"error": "Duplicate product", "code": "DUPLICATE_PRODUCT"}), 409
    if len(recent) >= DAILY_LISTING_LIMIT:
        return jsonify({"error": "Daily limit reached (10/day)", "code": "DAILY_LIMIT"}), 429

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
    product_code = _new_product_code()
    if product_code:
        insert_payload["code"] = product_code

    try:
        res = supabase.table("products").insert(insert_payload).execute()
        rows = res.data or []
    except Exception as exc:
        # If the products table has no category (or code) column, the insert fails
        # on that key alone. Retry once WITHOUT it so listings keep working.
        drop = set()
        if "category" in str(exc).lower():
            drop.add("category")
        if _missing_column_in(exc, "code"):
            drop.add("code")
        if drop:
            logging.warning(
                "products_create: insert failed on column(s) %s, retrying without them for telegram_id=%s",
                sorted(drop), tid,
            )
            fallback_payload = {k: v for k, v in insert_payload.items() if k not in drop}
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

    # Optional extra images (max 3). Runs only AFTER the product exists and can never fail it.
    try:
        if rows and rows[0].get("id") is not None:
            _save_extra_images(tid, rows[0]["id"])
    except Exception:
        logging.warning("products_create: extra images skipped for telegram_id=%s", tid, exc_info=True)

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
        _log_webhook(reference, "verify-transaction", {"error": "paystack unreachable"}, "error")
        return jsonify({"error": "Could not reach Paystack"}), 502

    pdata = ps_body.get("data") or {}
    _log_webhook(
        reference, "verify-transaction", ps_body,
        "success" if (ps_body.get("status") and pdata.get("status") == "success") else "failed",
    )
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

    # <product_id> may be a numeric/uuid id, a 6-char code, or prod_<code> (deep link).
    # Hidden products are included here so they still answer 410 (as before), not 404.
    product = _find_product(product_id, include_hidden=True)
    if not product:
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404

    if not product.get("visible", True):  # _find_product turns is_hidden into visible
        return jsonify({"error": "Product is no longer available", "code": "PRODUCT_UNAVAILABLE"}), 410

    if str(product.get("seller_id")) == str(tid):
        return jsonify({"error": "You cannot buy your own product"}), 400

    try:
        seller_id_int = int(product.get("seller_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Product has no valid seller"}), 400

    # A banned or deleted seller cannot be paid: refuse BEFORE the buyer is debited.
    try:
        seller_rows = (
            supabase.table("users")
            .select("telegram_id,banned,deleted_at")
            .eq("telegram_id", seller_id_int)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:
        if not _missing_column_in(exc, "banned") and "banned" not in str(exc).lower():
            logging.exception("wallet_buy: seller lookup failed for product %s", product.get("id"))
            return jsonify({"error": "Could not verify the seller. Please try again."}), 500
        # users.banned not migrated yet: check deletion only
        try:
            seller_rows = (
                supabase.table("users").select("telegram_id,deleted_at")
                .eq("telegram_id", seller_id_int).limit(1).execute()
            ).data or []
        except Exception:
            logging.exception("wallet_buy: seller lookup failed for product %s", product.get("id"))
            return jsonify({"error": "Could not verify the seller. Please try again."}), 500
    if not seller_rows or seller_rows[0].get("banned") or seller_rows[0].get("deleted_at"):
        return jsonify({"error": "Product is no longer available", "code": "PRODUCT_UNAVAILABLE"}), 410

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

    # Logged only AFTER the signature check, so unauthenticated callers cannot fill the table.
    _log_webhook(
        str((event.get("data") or {}).get("reference") or "").strip(),
        event.get("event"), event, "received",
    )

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
        _log_webhook(reference, "charge.success", {"amount": paid_kobo}, "credited")

        authorization = pdata.get("authorization") or {}
        if authorization.get("reusable"):
            try:
                _save_card_from_authorization(tx["user_id"], authorization)
            except Exception:
                logging.exception("paystack_webhook: saving card failed for %s", reference)
    except Exception:
        logging.exception("paystack_webhook: processing failed")
        _log_webhook(str((event.get("data") or {}).get("reference") or ""), event.get("event"), {}, "error")
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
# Ratings — Block E only, Rating Alone
# =============================================================================

@app.route("/api/rate", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def rate_seller(user):
    tid = user["telegram_id"]
    data = request.get_json(silent=True) or {}

    order_id = str(data.get("order_id") or "").strip()
    stars = data.get("stars")
    complaint_text = data.get("complaint_text")

    try:
        stars = int(stars)
    except (TypeError, ValueError):
        return jsonify({"error": "stars 1-5 required"}), 400
    if stars < 1 or stars > 5:
        return jsonify({"error": "stars 1-5 required"}), 400
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    if complaint_text is not None:
        complaint_text = str(complaint_text).strip()[:500]
        if stars >= 3:
            complaint_text = None
        if not complaint_text:
            complaint_text = None

    try:
        existing = supabase.table("ratings").select("id,stars").eq("order_id", order_id).limit(1).execute()
        if existing.data:
            return jsonify({
                "error": "already_rated", "already_rated": True,
                "stars": existing.data[0].get("stars"),
            }), 409
    except Exception:
        pass

    order = _escrow_get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    # buyer_id / seller_id / product_id from the request body are ignored on purpose:
    # everything is taken from the order itself.
    if int(order.get("buyer_id")) != int(tid):
        return jsonify({"error": "Forbidden - only buyer can rate"}), 403
    order_status = order.get("status")
    if order_status not in ("held", "released"):
        return jsonify({"error": "Can only rate a held or released order"}), 400

    seller_id = int(order.get("seller_id"))
    real_product_id = str(order.get("product_id"))

    try:
        res = supabase.table("ratings").insert({
            "order_id": order_id,
            "product_id": real_product_id,
            "buyer_id": int(tid),
            "seller_id": seller_id,
            "stars": stars,
            "complaint_text": complaint_text,
        }).execute()
    except Exception as exc:
        # A unique(order_id) constraint turns a double-submit race into a 409.
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            return jsonify({"error": "already_rated", "already_rated": True}), 409
        logging.exception("rate_seller failed order_id=%s", order_id)
        return jsonify({"error": "Could not save rating"}), 500

    _refresh_product_rating(order.get("product_id"))

    # 3+ stars on a still-held order: release to the seller (same logic as /api/escrow/confirm).
    # 1-2 stars: complaint saved, escrow stays held; the buyer then chooses refund or release.
    released = False
    if stars >= 3 and order_status == "held":
        try:
            released = _release_escrow(order_id) is not None
        except Exception:
            logging.exception("rate_seller: auto-release failed for order %s", order_id)

    return jsonify({
        "ok": True,
        "status": "success",
        "released": released,
        "rating": res.data[0] if res.data else {},
    }), 201
# =============================================================================
# Escrow — list / confirm / dispute / refund
# (these three routes were missing from the supplied file; the frontend calls them)
# =============================================================================

@app.route("/api/escrow/list", methods=["GET"])
@require_auth
def escrow_list(user):
    tid = user["telegram_id"]
    try:
        def mine():  # fresh builder each time (builders are mutable)
            return supabase.table("escrow_orders").select("*").or_(f"buyer_id.eq.{tid},seller_id.eq.{tid}")
        try:
            orders = mine().order("created_at", desc=True).limit(100).execute().data or []
        except Exception:
            orders = mine().limit(100).execute().data or []
    except Exception:
        logging.exception("escrow_list: lookup failed for telegram_id=%s", tid)
        return jsonify({"orders": []}), 200

    # Hold period over and not disputed: release to the seller (there is no background job).
    now = datetime.now(timezone.utc)
    for o in orders:
        due = _parse_ts(o.get("release_at"))
        if o.get("status") == "held" and due and due <= now:
            try:
                if _release_escrow(o["id"]):
                    o["status"] = "released"
            except Exception:
                logging.exception("escrow_list: auto-release failed for order %s", o.get("id"))

    ids = list({o.get("product_id") for o in orders if o.get("product_id") is not None})
    names = {}
    if ids:
        try:
            pr = supabase.table("products").select("id,name,title").in_("id", ids).execute()
            for p in pr.data or []:
                names[str(p["id"])] = p.get("name") or p.get("title")
        except Exception:
            logging.warning("escrow_list: product names unavailable", exc_info=True)
    for o in orders:
        o["product_name"] = names.get(str(o.get("product_id")))
    return jsonify({"orders": orders}), 200


@app.route("/api/escrow/confirm", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def escrow_confirm(user):
    tid = int(user["telegram_id"])
    data = request.get_json(silent=True) or {}
    order_id = str(data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    order = _escrow_get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    if int(order.get("buyer_id") or 0) != tid:
        return jsonify({"error": "Only the buyer can confirm delivery"}), 403
    if order.get("status") == "released":  # e.g. a 3+ star rating already released it
        return jsonify({"ok": True, "status": "success", "already_released": True}), 200
    if order.get("status") != "held":
        return jsonify({"error": "Order is not held", "code": "ORDER_NOT_HELD"}), 400

    try:
        released = _release_escrow(order_id)
    except Exception:
        return jsonify({"error": "Could not release funds. Please try again."}), 500
    if not released:
        return jsonify({"error": "Order changed, please retry"}), 409
    return jsonify({"ok": True, "status": "success"}), 200


@app.route("/api/escrow/dispute", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def escrow_dispute(user):
    data = request.get_json(silent=True) or {}
    return _open_dispute(user, data.get("order_id"), data.get("reason"), require_reason=False)


@app.route("/api/dispute", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def dispute_create(user):
    data = request.get_json(silent=True) or {}
    return _open_dispute(user, data.get("order_id"), data.get("reason"), require_reason=True)


@app.route("/api/escrow/refund", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def escrow_refund(user):
    """Buyer's 'Yes, Refund Me' after a 1-2 star rating. Only valid for a held order the buyer rated low."""
    tid = int(user["telegram_id"])
    data = request.get_json(silent=True) or {}
    order_id = str(data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    order = _escrow_get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    if int(order.get("buyer_id") or 0) != tid:
        return jsonify({"error": "Only the buyer can request this refund"}), 403
    if order.get("status") != "held":
        return jsonify({"error": "Order is not held", "code": "ORDER_NOT_HELD"}), 400

    try:
        rated = supabase.table("ratings").select("stars").eq("order_id", order_id).limit(1).execute().data or []
    except Exception as exc:
        return _db_error(exc, "escrow_refund: rating lookup failed")
    if not rated or int(rated[0].get("stars") or 0) > 2:
        return jsonify({"error": "A rating of 1-2 stars is required for an instant refund", "code": "RATING_REQUIRED"}), 400

    try:
        refunded = _refund_escrow(order_id, from_statuses=("held",))
    except Exception:
        return jsonify({"error": "Could not refund. Please try again."}), 500
    if not refunded:
        return jsonify({"error": "Order changed, please retry"}), 409
    return jsonify({"ok": True, "status": "success"}), 200


# =============================================================================
# Receipt PDF
# =============================================================================

def _user_label(telegram_id):
    try:
        res = supabase.table("users").select("first_name,username").eq("telegram_id", int(telegram_id)).limit(1).execute()
        rows = res.data or []
        if rows:
            name = rows[0].get("first_name") or "User"
            return f"{name} (@{rows[0]['username']})" if rows[0].get("username") else name
    except Exception:
        logging.warning("_user_label: lookup failed for %s", telegram_id, exc_info=True)
    return f"User {telegram_id}"


@app.route("/api/receipt/<order_id>", methods=["GET"])
@require_auth
def receipt_pdf(user, order_id):
    tid = int(user["telegram_id"])
    order = _escrow_get(str(order_id))
    if not order:
        return jsonify({"error": "Order not found"}), 404
    try:
        buyer_id, seller_id = int(order.get("buyer_id")), int(order.get("seller_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Order not found"}), 404
    if tid not in (buyer_id, seller_id):
        return jsonify({"error": "Forbidden"}), 403

    product_name = "-"
    try:
        pr = supabase.table("products").select("name,title").eq("id", order.get("product_id")).limit(1).execute().data or []
        if pr:
            product_name = pr[0].get("name") or pr[0].get("title") or "-"
    except Exception:
        logging.warning("receipt_pdf: product lookup failed", exc_info=True)

    def naira(kobo):
        return "NGN " + format(int(kobo or 0) / 100, ",.2f")

    price = int(order.get("price_kobo") or 0)
    buyer_pays = int(order.get("buyer_pays_kobo") or 0)
    seller_gets = int(order.get("seller_gets_kobo") or 0)
    when = _parse_ts(order.get("created_at"))
    reference = str(order.get("reference") or order.get("id"))

    rows = [
        ("Reference", reference),
        ("Product", product_name),
        ("Product price", naira(price)),
        ("Buyer fee share", naira(buyer_pays - price)),
        ("Seller fee share", naira(price - seller_gets)),
        ("Platform fee (in fees)", naira(order.get("platform_fee_kobo") or PLATFORM_FEE_KOBO)),
        ("Buyer paid", naira(buyer_pays)),
        ("Seller received", naira(seller_gets)),
        ("Buyer", _user_label(buyer_id)),
        ("Seller", _user_label(seller_id)),
        ("Date", when.strftime("%Y-%m-%d %H:%M UTC") if when else "-"),
        ("Status", str(order.get("status") or "-").capitalize()),
    ]
    pdf = _build_simple_pdf("PayLock Receipt", rows)
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="paylock-receipt-{secure_name(reference)}.pdf"',
        "Cache-Control": "no-store",
    })


# =============================================================================
# Report + blacklist
# =============================================================================

@app.route("/api/report", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def report_product(user):
    tid = int(user["telegram_id"])
    data = request.get_json(silent=True) or {}
    product_id = str(data.get("product_id") or "").strip()
    reason = REPORT_REASONS.get(str(data.get("reason") or "").strip().lower())
    if not product_id:
        return jsonify({"error": "product_id required"}), 400
    if not reason:
        return jsonify({"error": "reason must be one of: Scam, Fake, Wrong file, Spam"}), 400

    try:
        prod = supabase.table("products").select("id,seller_id,owner_id").eq("id", product_id).limit(1).execute().data or []
    except Exception:
        prod = []
    if not prod:
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404
    prod = prod[0]
    try:
        seller_id = int(prod.get("seller_id") or prod.get("owner_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Product has no valid seller"}), 400
    if seller_id == tid:
        return jsonify({"error": "You cannot report your own product"}), 400

    try:
        dup = (
            supabase.table("product_reports").select("id")
            .eq("product_id", prod["id"]).eq("reporter_id", tid).limit(1).execute()
        ).data
        if dup:
            return jsonify({"error": "You already reported this product", "code": "ALREADY_REPORTED"}), 409
        supabase.table("product_reports").insert({
            "product_id": prod["id"],
            "seller_id": seller_id,
            "reporter_id": tid,
            "reason": reason,
            "status": "pending",
            "created_at": _now_iso(),
        }).execute()
    except Exception as exc:
        return _db_error(exc, "report_product: save failed")
    return jsonify({"ok": True}), 201


# =============================================================================
# Visibility, analytics, boost
# =============================================================================

def _owns_product(prod, tid):
    return str(prod.get("seller_id")) == str(tid) or str(prod.get("owner_id")) == str(tid)


@app.route("/api/toggle-visibility", methods=["POST"])
@require_auth
@limiter.limit("30 per minute")
def toggle_visibility(user):
    tid = user["telegram_id"]
    blocked = _banned_response(user)
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    product_id = str(data.get("product_id") or "").strip()
    visible = data.get("visible")
    if isinstance(visible, str) and visible.strip().lower() in ("true", "false"):
        visible = visible.strip().lower() == "true"
    if not product_id or not isinstance(visible, bool):
        return jsonify({"error": "product_id and visible (true/false) are required"}), 400

    try:
        rows = supabase.table("products").select("id,seller_id,owner_id").eq("id", product_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404
    if not _owns_product(rows[0], tid):
        return jsonify({"error": "Forbidden"}), 403

    try:
        # is_hidden is what the marketplace and the buy route enforce; `visible` is kept in step when it exists.
        try:
            supabase.table("products").update({"is_hidden": not visible, "visible": visible}).eq("id", rows[0]["id"]).execute()
        except Exception as exc:
            if not _is_schema_missing(exc):
                raise
            supabase.table("products").update({"is_hidden": not visible}).eq("id", rows[0]["id"]).execute()
    except Exception:
        logging.exception("toggle_visibility: update failed for product %s", product_id)
        return jsonify({"error": "Could not update visibility"}), 500
    return jsonify({"ok": True, "visible": visible}), 200


@app.route("/api/analytics", methods=["GET"])
@require_auth
def analytics(user):
    tid = int(user["telegram_id"])
    total_sales, revenue_kobo, avg_rating = 0, 0, 0
    try:
        rows = _fetch_all(lambda a, b: (
            supabase.table("escrow_orders").select("id,seller_gets_kobo")
            .eq("seller_id", tid).eq("status", "released").order("id").range(a, b)
        ))
        total_sales = len(rows)
        revenue_kobo = sum(int(r.get("seller_gets_kobo") or 0) for r in rows)
    except Exception:
        logging.warning("analytics: orders unavailable", exc_info=True)
    try:
        rows = _fetch_all(lambda a, b: (
            supabase.table("ratings").select("stars").eq("seller_id", tid).order("id").range(a, b)
        ))
        stars = [int(r["stars"]) for r in rows if r.get("stars")]
        avg_rating = round(sum(stars) / len(stars), 2) if stars else 0
    except Exception:
        logging.warning("analytics: ratings unavailable", exc_info=True)
    return jsonify({"analytics": {
        "total_sales": total_sales,
        "total_revenue_kobo": revenue_kobo,
        "avg_rating": avg_rating,
    }}), 200


@app.route("/api/boost", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def boost_product(user):
    tid = user["telegram_id"]
    blocked = _banned_response(user)
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    product_id = str(data.get("product_id") or "").strip()
    if not product_id:
        return jsonify({"error": "product_id required"}), 400

    try:
        rows = (
            supabase.table("products")
            .select("id,seller_id,owner_id,is_hidden,is_boosted,boosted_until")
            .eq("id", product_id).limit(1).execute()
        ).data or []
    except Exception as exc:
        return _db_error(exc, "boost_product: product lookup failed")
    if not rows:
        return jsonify({"error": "Product not found", "code": "PRODUCT_NOT_FOUND"}), 404
    prod = rows[0]
    if not _owns_product(prod, tid):
        return jsonify({"error": "Forbidden"}), 403
    if prod.get("is_hidden"):
        return jsonify({"error": "Make the product visible before boosting it"}), 400
    if _boost_active(prod):
        return jsonify({"error": "This product is already boosted", "code": "ALREADY_BOOSTED"}), 409

    balance = int(user.get("balance_kobo") or 0)
    if balance < BOOST_FEE_KOBO:
        return jsonify({"error": "Insufficient balance for the ₦500 boost fee", "code": "INSUFFICIENT_BALANCE"}), 400

    debit = (
        supabase.table("users")
        .update({"balance_kobo": balance - BOOST_FEE_KOBO})
        .eq("telegram_id", tid)
        .eq("balance_kobo", balance)
        .execute()
    )
    if not debit.data:
        return jsonify({"error": "Balance changed, please retry"}), 409

    reference = "BST-" + uuid.uuid4().hex[:20]
    until = (datetime.now(timezone.utc) + timedelta(hours=BOOST_HOURS)).isoformat()
    try:
        supabase.table("wallet_transactions").insert({
            "user_id": tid,
            "type": "debit",
            "amount_kobo": BOOST_FEE_KOBO,
            "reason": "boost_fee",
            "reference": reference,
        }).execute()
        supabase.table("products").update({"is_boosted": True, "boosted_until": until}).eq("id", prod["id"]).execute()
    except Exception as exc:
        try:
            _credit_balance(tid, BOOST_FEE_KOBO)
        except Exception:
            logging.exception("boost_product: CRITICAL balance revert failed for telegram_id=%s", tid)
        try:
            supabase.table("wallet_transactions").delete().eq("reference", reference).execute()
        except Exception:
            logging.exception("boost_product: could not remove orphan transaction %s", reference)
        return _db_error(exc, "boost_product: failed after debit, fee refunded")
    return jsonify({"ok": True, "boosted_until": until, "fee_kobo": BOOST_FEE_KOBO}), 200


# =============================================================================
# Appeals + admin
# =============================================================================

@app.route("/api/appeal", methods=["POST"])
@require_auth
@limiter.limit("5 per hour")
def appeal_submit(user):
    tid = int(user["telegram_id"])
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()[:1000]
    if not message:
        return jsonify({"error": "message is required"}), 400
    if not user.get("banned"):
        return jsonify({"error": "Your account is not banned", "code": "NOT_BANNED"}), 400
    if user.get("banned_permanent"):
        return jsonify({"error": "This ban is permanent", "code": "BAN_PERMANENT"}), 403
    try:
        pending = (
            supabase.table("appeals").select("id")
            .eq("user_id", tid).eq("status", "pending").limit(1).execute()
        ).data
        if pending:
            return jsonify({"error": "You already have a pending appeal", "code": "APPEAL_PENDING"}), 409
        supabase.table("appeals").insert({
            "user_id": tid,
            "message": message,
            "status": "pending",
            "created_at": _now_iso(),
        }).execute()
    except Exception as exc:
        return _db_error(exc, "appeal_submit: save failed")
    return jsonify({"ok": True}), 201


def _admin_list(table, key, default_status="pending"):
    status = (request.args.get("status") or default_status).strip().lower()
    def build():  # fresh builder each time (builders are mutable)
        q = supabase.table(table).select("*")
        return q if status == "all" else q.eq("status", status)

    try:
        try:
            rows = build().order("created_at", desc=True).limit(100).execute().data or []
        except Exception:
            rows = build().limit(100).execute().data or []
    except Exception as exc:
        if _is_schema_missing(exc):
            return jsonify({key: []}), 200
        logging.exception("_admin_list: %s lookup failed", table)
        return jsonify({key: []}), 200
    return jsonify({key: rows}), 200


@app.route("/api/admin/appeals", methods=["GET"])
@require_auth
@admin_only
def admin_appeals(user):
    return _admin_list("appeals", "appeals")


@app.route("/api/admin/appeals/action", methods=["POST"])
@require_auth
@admin_only
@limiter.limit("30 per minute")
def admin_appeal_action(user):
    data = request.get_json(silent=True) or {}
    appeal_id = str(data.get("appeal_id") or "").strip()
    action = str(data.get("action") or "").strip().lower()
    if not appeal_id or action not in ("unban", "permanent"):
        return jsonify({"error": "appeal_id and action (unban/permanent) are required"}), 400
    try:
        rows = supabase.table("appeals").select("*").eq("id", appeal_id).limit(1).execute().data or []
    except Exception as exc:
        return _db_error(exc, "admin_appeal_action: lookup failed")
    if not rows:
        return jsonify({"error": "Appeal not found"}), 404
    appeal = rows[0]
    if appeal.get("status") != "pending":
        return jsonify({"error": "Appeal already handled", "code": "ALREADY_HANDLED"}), 409
    try:
        if action == "unban":
            _unban_user(appeal["user_id"])
            new_status = "approved"
        else:
            _ban_user(appeal["user_id"], permanent=True)
            new_status = "rejected"
        supabase.table("appeals").update({"status": new_status}).eq("id", appeal_id).execute()
    except Exception as exc:
        return _db_error(exc, "admin_appeal_action: update failed")
    return jsonify({"ok": True, "status": new_status}), 200


@app.route("/api/admin/reports", methods=["GET"])
@require_auth
@admin_only
def admin_reports(user):
    return _admin_list("product_reports", "reports")


@app.route("/api/admin/reports/action", methods=["POST"])
@require_auth
@admin_only
@limiter.limit("60 per minute")
def admin_report_action(user):
    data = request.get_json(silent=True) or {}
    report_id = str(data.get("report_id") or "").strip()
    action = str(data.get("action") or "").strip().lower()
    if not report_id or action not in ("confirm", "dismiss"):
        return jsonify({"error": "report_id and action (confirm/dismiss) are required"}), 400
    try:
        rows = supabase.table("product_reports").select("*").eq("id", report_id).limit(1).execute().data or []
        if not rows:
            return jsonify({"error": "Report not found"}), 404
        if rows[0].get("status") != "pending":
            return jsonify({"error": "Report already handled", "code": "ALREADY_HANDLED"}), 409
        new_status = "confirmed" if action == "confirm" else "dismissed"
        supabase.table("product_reports").update({"status": new_status}).eq("id", report_id).execute()
    except Exception as exc:
        return _db_error(exc, "admin_report_action: failed")
    banned = _check_auto_ban(rows[0].get("seller_id")) if action == "confirm" else False
    return jsonify({"ok": True, "status": new_status, "banned": banned}), 200


@app.route("/api/admin/disputes", methods=["GET"])
@require_auth
@admin_only
def admin_disputes(user):
    return _admin_list("disputes", "disputes", default_status="open")


@app.route("/api/admin/disputes/resolve", methods=["POST"])
@require_auth
@admin_only
@limiter.limit("60 per minute")
def admin_dispute_resolve(user):
    """outcome=buyer -> refund buyer + counts as a CONFIRMED dispute against the seller; outcome=seller -> release."""
    data = request.get_json(silent=True) or {}
    dispute_id = str(data.get("dispute_id") or "").strip()
    outcome = str(data.get("outcome") or "").strip().lower()
    if not dispute_id or outcome not in ("buyer", "seller"):
        return jsonify({"error": "dispute_id and outcome (buyer/seller) are required"}), 400
    try:
        rows = supabase.table("disputes").select("*").eq("id", dispute_id).limit(1).execute().data or []
    except Exception as exc:
        return _db_error(exc, "admin_dispute_resolve: lookup failed")
    if not rows:
        return jsonify({"error": "Dispute not found"}), 404
    dispute = rows[0]
    if dispute.get("status") != "open":
        return jsonify({"error": "Dispute already resolved", "code": "ALREADY_HANDLED"}), 409

    try:
        if outcome == "buyer":
            moved = _refund_escrow(dispute["order_id"], from_statuses=("disputed",))
        else:
            moved = _release_escrow(dispute["order_id"], from_statuses=("disputed",))
    except Exception:
        return jsonify({"error": "Could not move the funds. Please try again."}), 500
    if not moved:
        return jsonify({"error": "Order is no longer disputed", "code": "ORDER_NOT_DISPUTED"}), 409

    new_status = "confirmed" if outcome == "buyer" else "dismissed"
    try:
        supabase.table("disputes").update({"status": new_status}).eq("id", dispute_id).execute()
    except Exception:
        logging.exception("admin_dispute_resolve: funds moved but dispute status update failed for %s", dispute_id)
    banned = _check_auto_ban(dispute.get("seller_id")) if outcome == "buyer" else False
    return jsonify({"ok": True, "status": new_status, "banned": banned}), 200


ADMIN_USER_COLUMNS = "telegram_id,first_name,last_name,username,balance_kobo,banned,banned_permanent,deleted_at,created_at"


@app.route("/api/admin/users", methods=["GET"])
@require_auth
@admin_only
def admin_users(user):
    status = (request.args.get("status") or "all").strip().lower()
    # Strip everything that could alter the PostgREST filter string (commas, parentheses, wildcards...).
    term = re.sub(r"[^\w .-]", "", (request.args.get("search") or "").strip().lstrip("@"))[:50].strip()

    # (columns, order column): degrade gracefully if banned / created_at are not migrated.
    variants = [
        (ADMIN_USER_COLUMNS, "created_at"),
        (ADMIN_USER_COLUMNS.replace(",created_at", ""), "telegram_id"),
        ("telegram_id,first_name,last_name,username,balance_kobo,deleted_at", "telegram_id"),
    ]
    # "active" is filtered in Python (banned may be NULL on old rows), so fetch a wider window.
    window = 300 if status == "active" else 100

    rows, ok_query = [], False
    for cols, order_col in variants:
        try:
            q = supabase.table("users").select(cols)
            if status == "banned":
                q = q.eq("banned", True)
            if term:
                parts = [f"first_name.ilike.*{term}*", f"username.ilike.*{term}*"]
                if term.isdigit():
                    parts.append(f"telegram_id.eq.{term}")
                q = q.or_(",".join(parts))
            rows = q.order(order_col, desc=True).limit(window).execute().data or []
            ok_query = True
            break
        except Exception:
            logging.warning("admin_users: query variant failed (%s)", cols, exc_info=True)
    if not ok_query:
        return jsonify({"users": []}), 200

    if status == "active":
        rows = [r for r in rows if not r.get("banned") and not r.get("deleted_at")]
    return jsonify({"users": rows[:100]}), 200


@app.route("/api/admin/orders", methods=["GET"])
@require_auth
@admin_only
def admin_orders(user):
    status = (request.args.get("status") or "all").strip().lower()
    allowed = {"held", "released", "disputed", "refunded"}

    def build():  # fresh builder each time (builders are mutable)
        q = supabase.table("escrow_orders").select("*")
        return q.eq("status", status) if status in allowed else q

    try:
        try:
            orders = build().order("created_at", desc=True).limit(100).execute().data or []
        except Exception:
            orders = build().limit(100).execute().data or []
    except Exception:
        logging.exception("admin_orders: lookup failed")
        return jsonify({"orders": []}), 200

    ids = list({o.get("product_id") for o in orders if o.get("product_id") is not None})
    names = {}
    if ids:
        try:
            pr = supabase.table("products").select("id,name,title").in_("id", ids).execute()
            for p in pr.data or []:
                names[str(p["id"])] = p.get("name") or p.get("title")
        except Exception:
            logging.warning("admin_orders: product names unavailable", exc_info=True)
    for o in orders:
        o["product_name"] = names.get(str(o.get("product_id")))
    return jsonify({"orders": orders}), 200


@app.route("/api/admin/webhook-logs", methods=["GET"])
@require_auth
@admin_only
def admin_webhook_logs(user):
    try:
        rows = supabase.table("webhook_logs").select("*").order("created_at", desc=True).limit(50).execute().data or []
    except Exception as exc:
        if _is_schema_missing(exc):
            return jsonify({"logs": []}), 200
        logging.exception("admin_webhook_logs: lookup failed")
        return jsonify({"logs": []}), 200
    return jsonify({"logs": rows}), 200


# =============================================================================
# Frontend (serves ONLY index.html)
# =============================================================================

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path.startswith("api/") or path in ("webhook", "set-webhook"):
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


# =============================================================================
# Telegram bot (webhook mode) — thin handlers, no business logic
# =============================================================================

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio

async def _bot_start(update, context):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open PayLock", web_app=WebAppInfo(url=WEBAPP_URL))]]) if WEBAPP_URL else None
    await update.message.reply_text("Welcome to PayLock — Sell, buy and get paid safely with escrow.", reply_markup=kb)
async def _bot_support(update, context):
    await update.message.reply_text("Support: Describe your issue here. Our team will reply. For orders, include your Order ID / Reference.")
async def _bot_balance(update, context):
    await update.message.reply_text("Open PayLock to see your balance:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open PayLock", web_app=WebAppInfo(url=WEBAPP_URL))]]) if WEBAPP_URL else None)
async def _bot_help(update, context):
    await update.message.reply_text("/start - Open PayLock\n/support - Contact support\n/balance - Check balance\n/help - Show this")
async def _bot_admin(update, context):
    if update.effective_user is None or str(update.effective_user.id) != str(ADMIN_ID):
        await update.message.reply_text("Admin only")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Admin Panel", web_app=WebAppInfo(url=WEBAPP_URL))]]) if WEBAPP_URL else None
    await update.message.reply_text("Admin Panel - Daily activities", reply_markup=kb)
def _get_bot_app():
    app_bot = Application.builder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", _bot_start))
    app_bot.add_handler(CommandHandler("support", _bot_support))
    app_bot.add_handler(CommandHandler("balance", _bot_balance))
    app_bot.add_handler(CommandHandler("help", _bot_help))
    app_bot.add_handler(CommandHandler("admin", _bot_admin))
    return app_bot

# One coroutine per request: a single asyncio.run() keeps the bot's HTTP client
# on one event loop (separate asyncio.run() calls reuse a client bound to a closed loop).
async def _process_telegram_update(data):
    bot_app = _get_bot_app()
    await bot_app.initialize()
    try:
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
    finally:
        await bot_app.shutdown()

_PUBLIC_BOT_COMMANDS = [
    BotCommand("start", "Open PayLock"),
    BotCommand("support", "Contact support"),
    BotCommand("balance", "Check balance"),
    BotCommand("help", "Show help"),
]

async def _configure_telegram(url):
    bot_app = _get_bot_app()
    await bot_app.initialize()
    try:
        ok = await bot_app.bot.set_webhook(url)
        if ok:
            # Default scope (everyone): NO /admin.
            await bot_app.bot.set_my_commands(_PUBLIC_BOT_COMMANDS)
            # Admin chat only: same list plus /admin.
            if ADMIN_ID.isdigit():
                await bot_app.bot.set_my_commands(
                    _PUBLIC_BOT_COMMANDS + [BotCommand("admin", "Admin panel")],
                    scope=BotCommandScopeChat(chat_id=int(ADMIN_ID)),
                )
        return ok
    finally:
        await bot_app.shutdown()

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        asyncio.run(_process_telegram_update(data))
        return jsonify({"ok": True})
    except Exception:
        logging.exception("telegram_webhook: processing failed")
        return jsonify({"ok": False}), 200

@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    try:
        if not WEBAPP_URL:
            return jsonify({"ok": False, "error": "WEBAPP_URL not set"}), 500
        url = WEBAPP_URL + "/webhook"
        ok = asyncio.run(_configure_telegram(url))
        return jsonify({"ok": ok, "url": url})
    except Exception as e:
        logging.exception("set_webhook failed")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 5000)
    app.run(host="0.0.0.0", port=port)
