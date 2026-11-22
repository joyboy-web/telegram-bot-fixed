import os
import logging

logging.basicConfig(level=logging.INFO)

try:
    from app import app
    logging.info("main.py loaded - using locked app.py only")
except Exception:
    logging.exception("main.py: importing app.py failed; serving degraded /api/health only")
    from flask import Flask, jsonify

    app = Flask(__name__)

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "degraded", "error": "app import failed"}), 200


# =============================================================================
# Batch 7 additions (everything above this line is the original startup logic).
# Everything below is ADDITIVE and guarded: a failure in here is logged and never
# stops the app from booting or changes how an existing route behaves.
# main.py imports ONLY `app` from app.py. The polling backup file is never imported.
# =============================================================================

import sys
from urllib.parse import urlparse

from flask import jsonify as _jsonify
from werkzeug.exceptions import HTTPException as _HTTPException

# --- .env ---------------------------------------------------------------------
# app.py already loads .env when it is imported; this makes the same values
# available to main.py even when the import above fell back to the degraded app.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    logging.warning("main.py: python-dotenv not available; using the process environment only")

# TODO FINAL: Switch to Live Keys at end
# (Test keys stay in place for now. Live keys are a change to the PAYSTACK_SECRET_KEY /
#  PAYSTACK_PUBLIC_KEY environment variables only; nothing in this file reads or swaps them.)


def _env_flag(name):
    return str(os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# --- DEBUG --------------------------------------------------------------------
# Production default is False. Local debugging is opt-in: set PAYLOCK_DEBUG=1.
# It is ignored on Render (RENDER is set there), so a stray variable can never
# turn on the interactive debugger in production.
DEBUG = _env_flag("PAYLOCK_DEBUG") and not os.environ.get("RENDER")
try:
    app.debug = DEBUG
except Exception:
    logging.warning("main.py: could not set app.debug", exc_info=True)


# --- JSON errors (never an HTML page, never a stack trace) -----------------------
def _has_error_handler(code):
    try:
        return bool(app.error_handler_spec.get(None, {}).get(code))
    except Exception:
        return False


def _json_http_error(exc):
    """Any HTTP error that has no handler of its own (400, 401, 403, 405, 413, ...)."""
    code = getattr(exc, "code", None) or 500
    return _jsonify({"error": getattr(exc, "name", None) or "Error"}), code


def _install_json_errors():
    # Codes that app.py already handles (404 / 429 / 500) keep app.py's handlers untouched.
    # The degraded fallback app has none, so it gets the same JSON answers here.
    if not _has_error_handler(404):
        app.register_error_handler(404, lambda e: (_jsonify({"error": "Not found"}), 404))
    if not _has_error_handler(429):
        app.register_error_handler(
            429, lambda e: (_jsonify({"error": "Too many requests", "code": "RATE_LIMITED"}), 429))
    if not _has_error_handler(500):
        def _json_500(e):
            logging.error("Internal server error: %r", getattr(e, "original_exception", None) or e)
            return _jsonify({"error": "Internal server error"}), 500
        app.register_error_handler(500, _json_500)
    # Catch-all for every OTHER HTTP error. Flask resolves code-specific handlers first,
    # so this never overrides the three above (or any handler app.py defines).
    app.register_error_handler(_HTTPException, _json_http_error)


try:
    _install_json_errors()
except Exception:
    logging.warning("main.py: could not install JSON error handlers", exc_info=True)


# --- CORS: Telegram WebApp only ---------------------------------------------------
# The Mini App is served from WEBAPP_URL (same origin as this API), so it needs no CORS
# at all. Browser-based Telegram clients are the only other legitimate callers.
# This wraps the app OUTSIDE app.py's own CORS(app): after it runs, a CORS header is only
# ever present for an allowed origin. Requests without an Origin header are not affected.
_TELEGRAM_ORIGINS = (
    "https://web.telegram.org",
    "https://webk.telegram.org",
    "https://webz.telegram.org",
)


def _allowed_origins():
    allowed = set(_TELEGRAM_ORIGINS)
    web = urlparse(os.environ.get("WEBAPP_URL", "").strip())
    if web.scheme and web.netloc:
        allowed.add(f"{web.scheme}://{web.netloc}")
    for extra in str(os.environ.get("CORS_EXTRA_ORIGINS") or "").split(","):
        extra = extra.strip().rstrip("/")
        if extra:
            allowed.add(extra)
    return allowed


class _TelegramOnlyCORS:
    def __init__(self, wsgi_app, allowed, allow_local):
        self.wsgi_app = wsgi_app
        self.allowed = allowed
        self.allow_local = allow_local

    def _origin_ok(self, origin):
        if origin in self.allowed:
            return True
        if self.allow_local:  # local debugging only
            host = urlparse(origin).hostname
            return host in ("localhost", "127.0.0.1")
        return False

    def __call__(self, environ, start_response):
        origin = environ.get("HTTP_ORIGIN")

        def _start(status, headers, exc_info=None):
            headers = [(k, v) for (k, v) in headers if not k.lower().startswith("access-control-")]
            if origin and self._origin_ok(origin):
                headers += [
                    ("Access-Control-Allow-Origin", origin),
                    ("Vary", "Origin"),
                    ("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"),
                    ("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Telegram-Init-Data"),
                    ("Access-Control-Max-Age", "600"),
                ]
            return start_response(status, headers, exc_info)

        return self.wsgi_app(environ, _start)


try:
    if not getattr(app, "_paylock_cors_wrapped", False):
        app.wsgi_app = _TelegramOnlyCORS(app.wsgi_app, _allowed_origins(), allow_local=DEBUG)
        app._paylock_cors_wrapped = True
except Exception:
    logging.warning("main.py: could not install the Telegram-only CORS wrapper", exc_info=True)


# --- Startup checks (log only; they never block boot) ------------------------------
# Tables the app cannot work without (a miss is logged as ERROR).
_CRITICAL_TABLES = ("users", "products", "escrow_orders", "wallet_transactions", "withdrawals", "saved_cards")
# Tables whose features answer 501 NOT_READY when missing (a miss is logged as WARNING).
_OPTIONAL_TABLES = (
    "ratings", "disputes", "product_reports", "appeals", "referrals", "referral_commissions",
    "webhook_logs", "extra_images", "support_tickets", "chat_messages", "chat_reads",
    "product_events", "user_settings",
)

# (table, column, fallback table, fallback column) for the Batch 7 columns you asked for.
# app.py keeps the consent / terms / passcode / theme values in user_settings and counts
# views / clicks from product_events, so a column missing on users / products is reported
# as informational when the real storage place exists.
_REQUESTED_COLUMNS = (
    ("users", "has_consented", "user_settings", "has_consented"),
    ("users", "has_accepted_listing_terms", "user_settings", "has_accepted_listing_terms"),
    ("users", "has_accepted_boosting_terms", "user_settings", "has_accepted_boosting_terms"),
    ("users", "passcode_hash", "user_settings", "passcode_hash"),
    ("users", "theme_mode", "user_settings", "theme_mode"),
    ("products", "views", "product_events", "event_type"),
    ("products", "clicks", "product_events", "event_type"),
    ("products", "visible", None, None),
    ("products", "is_boosted", None, None),
)

# Columns app.py itself reads or writes for Batch 7 (these are the ones that matter at runtime).
_APP_COLUMNS = (
    ("user_settings", "telegram_id"), ("user_settings", "display_name"), ("user_settings", "photo_url"),
    ("user_settings", "passcode_on"), ("user_settings", "passcode_fail_count"),
    ("user_settings", "passcode_block_level"), ("user_settings", "passcode_block_until"),
    ("user_settings", "sessions_revoked_at"), ("user_settings", "deactivated_at"),
    ("user_settings", "deactivated_product_ids"),
    ("products", "is_hidden"), ("products", "boosted_until"),
    ("product_events", "product_id"), ("product_events", "user_id"), ("product_events", "created_at"),
    ("chat_reads", "order_id"), ("chat_reads", "user_id"), ("chat_reads", "last_read_at"),
)

# Routes that must exist once app.py is imported (checked by path, no request is made).
_EXPECTED_ROUTES = (
    "/api/health", "/api/auth/validate", "/api/products", "/api/wallet/buy/<product_id>",
    "/api/settings", "/api/account/deactivate", "/api/leaderboard", "/api/network",
    "/api/analytics/series", "/api/products/<product_id>/event", "/webhook", "/set-webhook",
)


def _probe(core, table, column="*"):
    """'ok', 'missing' (table/column does not exist) or 'error' (could not be verified)."""
    try:
        core.supabase.table(table).select(column).limit(1).execute()
        return "ok"
    except Exception as exc:
        try:
            if core._is_schema_missing(exc):
                return "missing"
        except Exception:
            pass
        logging.warning("startup check: could not verify %s.%s (%s)", table, column, exc)
        return "error"


def run_startup_checks():
    """
    Verifies configuration, tables and the Batch 7 columns. Returns a report dict and
    logs it; never raises and never stops the app. Set SKIP_STARTUP_CHECKS=1 to skip it.
    """
    report = {"ok": True, "problems": [], "notes": []}

    def problem(level, msg):
        report["problems"].append(msg)
        if level == "error":
            report["ok"] = False
            logging.error("STARTUP CHECK: %s", msg)
        else:
            logging.warning("STARTUP CHECK: %s", msg)

    try:
        core = sys.modules.get("app")
        if core is None or not hasattr(core, "supabase"):
            problem("error", "app.py is not loaded (degraded mode): checks skipped")
            return report

        # Configuration ---------------------------------------------------------
        admin_id = str(getattr(core, "ADMIN_ID", "") or "").strip()
        if not admin_id:
            problem("error", "ADMIN_ID is not set: every /api/admin/* route will answer 403 and bot /admin is disabled")
        elif not admin_id.isdigit():
            problem("error", "ADMIN_ID is not a numeric Telegram id")

        secret = str(getattr(core, "PAYSTACK_SECRET_KEY", "") or "")
        public = str(getattr(core, "PAYSTACK_PUBLIC_KEY", "") or "")
        if not secret:
            problem("error", "PAYSTACK_SECRET_KEY is missing (payments, transfers and the webhook will not work)")
        if not public:
            problem("error", "PAYSTACK_PUBLIC_KEY is missing (wallet funding will not work)")
        if secret and public:
            if secret.startswith("sk_test_") and public.startswith("pk_test_"):
                report["notes"].append("Paystack TEST keys in use (TODO FINAL: switch to Live Keys at the end)")
                logging.info("STARTUP CHECK: Paystack TEST keys in use. TODO FINAL: switch to Live Keys at the end.")
            elif secret.startswith("sk_live_") and public.startswith("pk_live_"):
                report["notes"].append("Paystack LIVE keys in use")
                logging.info("STARTUP CHECK: Paystack LIVE keys in use.")
            else:
                problem("warn", "Paystack secret and public keys do not look like the same mode (test / live)")
        if not str(getattr(core, "BOT_TOKEN", "") or ""):
            problem("error", "BOT_TOKEN is missing (Telegram sign-in will fail)")
        if not str(getattr(core, "WEBAPP_URL", "") or ""):
            problem("warn", "WEBAPP_URL is not set (/set-webhook and the Open PayLock button need it)")

        # Database ----------------------------------------------------------------
        if core.supabase is None:
            problem("error", "Supabase client was not created: database checks skipped")
            return report

        missing_tables = set()
        for table in _CRITICAL_TABLES:
            if _probe(core, table) == "missing":
                missing_tables.add(table)
                problem("error", f"table '{table}' does not exist")
        for table in _OPTIONAL_TABLES:
            if _probe(core, table) == "missing":
                missing_tables.add(table)
                problem("warn", f"table '{table}' does not exist (its feature will answer 501 NOT_READY)")

        for table, column, alt_table, alt_column in _REQUESTED_COLUMNS:
            if table in missing_tables:
                continue
            state = _probe(core, table, column)
            if state != "missing":
                continue
            if alt_table and alt_table not in missing_tables and _probe(core, alt_table, alt_column) == "ok":
                note = (f"{table}.{column} is not a column, but app.py stores it in "
                        f"{alt_table}.{alt_column} (OK)")
                report["notes"].append(note)
                logging.info("STARTUP CHECK: %s", note)
            else:
                problem("warn", f"column {table}.{column} does not exist")

        for table, column in _APP_COLUMNS:
            if table in missing_tables:
                continue
            if _probe(core, table, column) == "missing":
                problem("warn", f"column {table}.{column} does not exist (app.py uses it)")

        # Routes ------------------------------------------------------------------
        try:
            registered = {rule.rule for rule in app.url_map.iter_rules()}
            for path in _EXPECTED_ROUTES:
                if path not in registered:
                    problem("warn", f"route {path} is not registered")
        except Exception:
            logging.warning("startup check: route check skipped", exc_info=True)

        if not report["problems"]:
            logging.info("STARTUP CHECK: all Batch 7 checks passed")
        else:
            logging.warning("STARTUP CHECK: finished with %d issue(s); the app is still running",
                            len(report["problems"]))
    except Exception:
        logging.warning("STARTUP CHECK: crashed and was skipped (the app is not affected)", exc_info=True)
    return report


try:
    if _env_flag("SKIP_STARTUP_CHECKS"):
        logging.info("main.py: startup checks skipped (SKIP_STARTUP_CHECKS is set)")
    else:
        app.config["PAYLOCK_STARTUP_CHECK"] = run_startup_checks()
except Exception:
    logging.warning("main.py: startup checks could not run", exc_info=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or 5000), debug=DEBUG)
