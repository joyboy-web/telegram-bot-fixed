"""
PayLock production entrypoint.

Deploy:  gunicorn main:app --bind 0.0.0.0:$PORT   (Procfile: web: gunicorn main:app)
Local:   python main.py                           (reads PORT, defaults to 5000)

Wraps app.py and adds:
  - /api/products override (never touches is_public, explicit columns only)
  - /api/escrow/confirm, /api/escrow/dispute, /api/escrow/list
  - background auto-release worker (every 5 minutes)
"""
import os
import logging
import threading
import time
from datetime import datetime, timezone

from flask import request, jsonify

from app import app, supabase, require_auth, limiter

logging.basicConfig(level=logging.INFO)

AUTO_RELEASE_INTERVAL_SECONDS = 5 * 60

PRODUCT_COLUMNS = (
    "id,name,description,price_kobo,buyer_pays_kobo,seller_gets_kobo,"
    "image_url,file_url,seller_id,created_at,category"
)


# ---------------------------------------------------------------------------
# Products override (no is_public in any form; always HTTP 200)
# ---------------------------------------------------------------------------
def products_list_fixed():
    try:
        res = (
            supabase.table("products")
            .select(PRODUCT_COLUMNS)
            .eq("is_hidden", False)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        return jsonify({"products": res.data or []}), 200
    except Exception:
        logging.exception("products_list_fixed failed")
        return jsonify({"products": []}), 200


app.view_functions["products_list"] = products_list_fixed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _current_tid():
    """Telegram ID of the authenticated user, set by require_auth (as int or None)."""
    for name in ("user", "tg_user", "telegram_user", "current_user"):
        u = getattr(request, name, None)
        if isinstance(u, dict) and u.get("telegram_id") is not None:
            try:
                return int(u["telegram_id"])
            except (TypeError, ValueError):
                pass
    for name in ("telegram_id", "user_id", "tg_id"):
        v = getattr(request, name, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _find_order(order_ref):
    """Look an escrow order up by id, falling back to reference."""
    try:
        res = supabase.table("escrow_orders").select("*").eq("id", order_ref).limit(1).execute()
        if res.data:
            return res.data[0]
    except Exception:
        logging.info("escrow lookup by id failed for %s, trying reference", order_ref)
    try:
        res = supabase.table("escrow_orders").select("*").eq("reference", str(order_ref)).limit(1).execute()
        if res.data:
            return res.data[0]
    except Exception:
        logging.exception("escrow lookup by reference failed for %s", order_ref)
    return None


def _credit_seller(order):
    order_reference = order.get("reference")
    if order_reference:
        existing = supabase.table("wallet_transactions").select("id").eq("reference", order_reference).eq("reason", "escrow_release").limit(1).execute()
        if existing.data:
            return
    seller_id = order["seller_id"]
    amount = int(order["seller_gets_kobo"])
    # wallet_buy puts the FULL buyer_pays amount into the seller's held balance,
    # so release must subtract buyer_pays_kobo from held (not seller_gets_kobo).
    held_amount = int(order.get("buyer_pays_kobo") or amount)
    user_res = supabase.table("users").select("balance_kobo,held_kobo").eq("telegram_id", seller_id).limit(1).execute()
    rows = user_res.data or []
    current_balance = int(rows[0].get("balance_kobo") or 0) if rows else 0
    current_held = int(rows[0].get("held_kobo") or 0) if rows else 0
    supabase.table("users").update({"balance_kobo": current_balance + amount, "held_kobo": max(0, current_held - held_amount)}).eq("telegram_id", seller_id).execute()
    supabase.table("wallet_transactions").insert({"user_id": seller_id, "type": "credit", "amount_kobo": amount, "reason": "escrow_release", "reference": order.get("reference")}).execute()


# ---------------------------------------------------------------------------
# Escrow: confirm
# ---------------------------------------------------------------------------
@app.route("/api/escrow/confirm", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def escrow_confirm():
    data = request.get_json(silent=True) or {}
    order_ref = data.get("order_id") or data.get("id") or data.get("reference")
    if not order_ref:
        return jsonify({"error": "order_id is required"}), 400

    order = _find_order(order_ref)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    tid = _current_tid()
    try:
        is_buyer = tid is not None and int(order.get("buyer_id")) == int(tid)
    except (TypeError, ValueError):
        is_buyer = False
    if not is_buyer:
        return jsonify({"error": "Only the buyer can confirm delivery"}), 403

    if order.get("status") != "held":
        return jsonify({"error": "Order is not in held state"}), 400

    # Atomic claim: only one caller can move held -> released.
    released = (
        supabase.table("escrow_orders")
        .update({"status": "released"})
        .eq("id", order["id"])
        .eq("status", "held")
        .execute()
    )
    if not released.data:
        return jsonify({"error": "Order already processed"}), 409

    try:
        _credit_seller(order)
    except Exception:
        logging.exception("escrow_confirm: crediting seller failed for order %s", order.get("id"))
        try:
            supabase.table("escrow_orders").update({"status": "held"}).eq("id", order["id"]).eq("status", "released").execute()
        except Exception:
            logging.exception("escrow_confirm: revert to held failed for order %s", order.get("id"))
        return jsonify({"error": "Order released but crediting seller failed. Contact support."}), 500

    return jsonify({"status": "success", "order_id": order["id"]}), 200


# ---------------------------------------------------------------------------
# Escrow: dispute
# ---------------------------------------------------------------------------
@app.route("/api/escrow/dispute", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def escrow_dispute():
    data = request.get_json(silent=True) or {}
    order_ref = data.get("order_id") or data.get("id")
    reason = str(data.get("reason") or data.get("dispute_reason") or "").strip()
    if not order_ref:
        return jsonify({"error": "order_id is required"}), 400

    order = _find_order(order_ref)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    tid = _current_tid()
    try:
        is_buyer = tid is not None and int(order.get("buyer_id")) == int(tid)
    except (TypeError, ValueError):
        is_buyer = False
    try:
        is_seller = tid is not None and int(order.get("seller_id")) == int(tid)
    except (TypeError, ValueError):
        is_seller = False
    if not (is_buyer or is_seller):
        return jsonify({"error": "Not authorized for this order"}), 403

    if order.get("status") != "held":
        return jsonify({"error": "Only held orders can be disputed"}), 400

    try:
        update_res = supabase.table("escrow_orders").update({"status": "disputed", "dispute_reason": reason or None}).eq("id", order["id"]).eq("status", "held").execute()
    except Exception:
        logging.exception("escrow_dispute: dispute_reason column missing, falling back to status-only update for order %s", order["id"])
        update_res = supabase.table("escrow_orders").update({"status": "disputed"}).eq("id", order["id"]).eq("status", "held").execute()

    if not update_res.data:
        return jsonify({"error": "Order already processed"}), 409

    return jsonify({"status": "success", "order_id": order["id"]}), 200


# ---------------------------------------------------------------------------
# Escrow: list
# ---------------------------------------------------------------------------
@app.route("/api/escrow/list", methods=["GET"])
@require_auth
def escrow_list():
    tid = _current_tid()
    if tid is None:
        return jsonify({"orders": []}), 200
    try:
        res = (
            supabase.table("escrow_orders")
            .select("*")
            .or_(f"buyer_id.eq.{tid},seller_id.eq.{tid}")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        return jsonify({"orders": res.data or []}), 200
    except Exception:
        logging.exception("escrow_list failed")
        return jsonify({"orders": []}), 200


# ---------------------------------------------------------------------------
# Auto-release worker (daemon thread; safe with multiple gunicorn workers
# because every release goes through the atomic held -> released guard)
# ---------------------------------------------------------------------------
def _auto_release_worker():
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                res = supabase.table("escrow_orders").select("*").eq("status", "held").lt("release_at", now_iso).execute()
                orders = res.data or []
            except Exception:
                logging.exception("auto-release worker: release_at query failed - check escrow_orders.release_at column exists")
                orders = []

            for order in orders:
                try:
                    released = (
                        supabase.table("escrow_orders")
                        .update({"status": "released"})
                        .eq("id", order["id"])
                        .eq("status", "held")
                        .execute()
                    )
                    if not released.data:
                        continue
                    try:
                        _credit_seller(order)
                        logging.info("auto-release: released order %s", order.get("id"))
                    except Exception:
                        logging.exception("auto-release: crediting seller failed for order %s", order.get("id"))
                        try:
                            supabase.table("escrow_orders").update({"status": "held"}).eq("id", order["id"]).eq("status", "released").execute()
                        except Exception:
                            logging.exception("auto-release: revert to held failed for order %s", order.get("id"))
                except Exception:
                    logging.exception("auto-release: failed processing order %s", order.get("id"))
        except Exception:
            logging.exception("auto-release worker loop error")
        time.sleep(AUTO_RELEASE_INTERVAL_SECONDS)


_worker_thread = threading.Thread(target=_auto_release_worker, daemon=True)
_worker_thread.start()

print("main.py loaded - is_public patched + escrow active")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
