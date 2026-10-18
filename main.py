"""PayLock API: Telegram initData validation and authenticated routes."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qsl

from flask import Flask, Response, g, jsonify, request
from flask_cors import CORS
from supabase import Client, create_client

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    stream=sys.stdout,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("paylock")

# --------------------------------------------------------------------------- #
# Configuration (fail fast: a misconfigured deploy must not boot)
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

_missing = [
    name
    for name, value in (
        ("BOT_TOKEN", BOT_TOKEN),
        ("SUPABASE_URL", SUPABASE_URL),
        ("SUPABASE_SERVICE_KEY", SUPABASE_SERVICE_KEY),
        ("ALLOWED_ORIGINS", ALLOWED_ORIGINS),
    )
    if not value
]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60  # replay window
CLOCK_SKEW_SECONDS = 60                   # tolerate small clock drift
MAX_INIT_DATA_LENGTH = 8192
MAX_FIELD_LENGTH = 256

# Secret key = HMAC_SHA256(key="WebAppData", msg=bot_token), derived once.
_SECRET_KEY: bytes = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.json.sort_keys = False

CORS(
    app,
    resources={r"/api/*": {"origins": ALLOWED_ORIGINS}},
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "OPTIONS"],
    supports_credentials=False,
    max_age=600,
)


@app.after_request
def set_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


# --------------------------------------------------------------------------- #
# initData validation
# --------------------------------------------------------------------------- #
def _reject(reason: str) -> None:
    """Log a rejection reason (never the payload) and return None."""
    logger.warning("initData rejected: %s", reason)
    return None


def validate_init_data(init_data: str) -> dict[str, Any] | None:
    """Validate Telegram Mini App initData.

    Implements https://core.telegram.org/bots/web_apps#validating-data-received-via-the-mini-app

    Returns a dict with keys ``user`` (dict), ``auth_date`` (int), ``query_id``
    and ``start_param`` (optional) on success, otherwise ``None``.
    """
    if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        return _reject("missing_or_oversized")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return _reject("malformed_query_string")

    fields = dict(pairs)
    if len(fields) != len(pairs):
        return _reject("duplicate_keys")

    received_hash = fields.pop("hash", None)
    if not received_hash:
        return _reject("missing_hash")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    expected_hash = hmac.new(_SECRET_KEY, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Timing-safe comparison on bytes (str compare raises on non-ASCII input).
    if not hmac.compare_digest(expected_hash.encode(), received_hash.lower().encode()):
        return _reject("hash_mismatch")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError):
        return _reject("invalid_auth_date")

    age = int(time.time()) - auth_date
    if age > INIT_DATA_MAX_AGE_SECONDS:
        return _reject("expired")
    if age < -CLOCK_SKEW_SECONDS:
        return _reject("auth_date_in_future")

    try:
        user = json.loads(fields.get("user", ""))
    except json.JSONDecodeError:
        return _reject("invalid_user_json")

    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:
        return _reject("invalid_user_object")
    if not isinstance(user.get("first_name"), str) or not user["first_name"].strip():
        return _reject("invalid_user_name")

    return {
        "user": user,
        "auth_date": auth_date,
        "query_id": fields.get("query_id"),
        "start_param": fields.get("start_param"),
    }


# --------------------------------------------------------------------------- #
# Auth decorator
# --------------------------------------------------------------------------- #
F = TypeVar("F", bound=Callable[..., Any])


def _error(code: str, status: int) -> tuple[Response, int]:
    return jsonify({"ok": False, "error": code}), status


def require_telegram_auth(view: F) -> F:
    """Protect a route. Client sends ``Authorization: tma <initData>``.

    On success the verified Telegram user is available as ``g.tg_user``.
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "tma" or not token:
            return _error("unauthorized", 401)

        parsed = validate_init_data(token)
        if parsed is None:
            return _error("invalid_init_data", 401)

        g.tg_user = parsed["user"]
        g.auth_date = parsed["auth_date"]
        return view(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()[:MAX_FIELD_LENGTH]
    return value or None


def _clean_photo_url(value: Any) -> str | None:
    url = _clean_text(value)
    return url if url and url.startswith("https://") else None


def upsert_user(user: dict[str, Any], auth_date: int) -> dict[str, Any]:
    """Insert or update the user keyed by telegram_id; returns the stored row."""
    row = {
        "telegram_id": user["id"],
        "first_name": _clean_text(user.get("first_name")) or "User",
        "last_name": _clean_text(user.get("last_name")),
        "username": _clean_text(user.get("username")),
        "photo_url": _clean_photo_url(user.get("photo_url")),
        "auth_date": datetime.fromtimestamp(auth_date, tz=timezone.utc).isoformat(),
    }
    result = supabase.table("users").upsert(row, on_conflict="telegram_id").execute()
    if not result.data:
        raise RuntimeError("Upsert returned no rows")
    return result.data[0]


def _public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "telegram_id": row["telegram_id"],
        "first_name": row["first_name"],
        "last_name": row.get("last_name"),
        "username": row.get("username"),
        "photo_url": row.get("photo_url"),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.post("/api/auth/validate")
def auth_validate():
    payload = request.get_json(silent=True)
    init_data = payload.get("initData") if isinstance(payload, dict) else None
    if not isinstance(init_data, str):
        return _error("bad_request", 400)

    parsed = validate_init_data(init_data)
    if parsed is None:
        return _error("invalid_init_data", 401)

    try:
        row = upsert_user(parsed["user"], parsed["auth_date"])
    except Exception as exc:  # noqa: BLE001 - never leak DB internals to clients
        logger.error("user upsert failed: %s", type(exc).__name__)
        return _error("service_unavailable", 503)

    logger.info("auth ok telegram_id=%s", row["telegram_id"])
    return jsonify({"ok": True, "user": _public_user(row)})


@app.get("/api/me")
@require_telegram_auth
def me():
    """Example protected route; every future /api route uses the same decorator."""
    result = (
        supabase.table("users")
        .select("telegram_id, first_name, last_name, username, photo_url")
        .eq("telegram_id", g.tg_user["id"])
        .limit(1)
        .execute()
    )
    if not result.data:
        return _error("user_not_found", 404)
    return jsonify({"ok": True, "user": result.data[0]})


# --------------------------------------------------------------------------- #
# Error handlers (JSON only, no stack traces)
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def not_found(_: Exception):
    return _error("not_found", 404)


@app.errorhandler(405)
def method_not_allowed(_: Exception):
    return _error("method_not_allowed", 405)


@app.errorhandler(413)
def payload_too_large(_: Exception):
    return _error("payload_too_large", 413)


@app.errorhandler(Exception)
def unhandled(exc: Exception):
    logger.error("unhandled error: %s", type(exc).__name__, exc_info=True)
    return _error("internal_error", 500)
