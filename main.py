# =============================================================================
# PayLock — main.py
# Real entrypoint for production. Wraps app.py, overrides /api/products with
# the is_public-free version (double safety net), and adds escrow confirm/
# dispute/list routes plus a background auto-release worker.
#
# Deploy: gunicorn main:app --bind 0.0.0.0:$PORT   (Procfile: web: gunicorn main:app)
# Local:  python main.py   (reads PORT env var, defaults 5000)
# =============================================================================

import os
import logging
import threading
import time as time_module
from datetime import datetime, timezone

from flask import request, jsonify

from app import app, supabase, require_auth, limiter

# -----------------------------------------------------------------------------
# /api/products override — belt-and-braces, never references is_public
# -----------------------------------------------------------------------------

def products_list_fixed():
    # FIXED is_public removed — explicit column select only, never touches
    # is_public in any form. Always returns HTTP 200, even on failure.
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
        logging.exception("products_list_fixed: failed to load products")
        return jsonify({"products": []}), 200


# Overrides the view function Flask calls for the existing "products_list"
# endpoint (registered by @app.route in app.py). This does not add a new
# route or duplicate the URL rule — it just swaps the function that runs.
app.view_functions["products_list"] = products_list_fixed


# -----------------------------------------------------------------------------
# Shared helper — credit a seller for a released escrow order
# -----------------------------------------------------------------------------

def _credit_seller(order):
    seller_id = order["seller_id"]
    amount = int(order["seller_gets_kobo"])

    user_res = supabase.table("users").select("balance_kobo").eq("telegram_id", seller_id).limit(1).execute()
    rows = user_res.data or []
    current_balance = int(rows[0].get("balance_kobo") or 0) if rows else 0
    new_balance = current_balance + amount

    supabase.table("users").update({"balance_kobo": new_balance}).eq("telegram_id", seller_id).execute()

    supabase.table("wallet_transactions").insert({
        "user_id": seller_id,
        "type": "credit",
        "amount_kobo": amount,
        "reason": "escrow_release",
        "reference": order.get("reference"),
    }).execute()


# -----------------------------------------------------------------------------
# Escrow routes
# -----------------------------------------------------------------------------

@app.route("/api/escrow/confirm", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def escrow_confirm(user):
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id") or data.get("id")
    reference = data.get("reference")

    if not order_id and not reference:
        return jsonify({"error": "order_id is required"}), 400

    query = supabase.table("escrow_orders").select("*")
    query = query.eq("id", order_id) if order_id else query.eq("reference", reference)
    res = query.limit(1).execute()
    rows = res.data or []
    if not rows:
        return jsonify({"error": "Escrow order not found"}), 404

    order = rows[0]

    if int(order["buyer_id"]) != int(user["telegram_id"]):
        return jsonify({"error": "Only the buyer can confirm delivery"}), 403

    if order["status"] != "held":
        return jsonify({"error": f"Order cannot be confirmed (status: {order['status']})"}), 400

    # Atomic conditional update — the .eq("status","held") guard is what
    # prevents a double-credit if this endpoint and the background
    # auto-release worker (or two gunicorn workers' threads) race on the
    # same order at the same moment. Whichever update actually matches a
    # row wins; the other returns no data and is treated as already-done.
    update_res = (
        supabase.table("escrow_orders")
        .update({"status": "released"})
        .eq("id", order["id"])
        .eq("status", "held")
        .execute()
    )
    if not update_res.data:
        return jsonify({"error": "Order already processed"}), 409

    try:
        _credit_seller(order)
    except Exception:
        logging.exception("escrow_confirm: failed to credit seller for order %s", order["id"])
        return jsonify({"error": "Order released but crediting seller failed. Contact support."}), 500

    return jsonify({"status": "success", "order_id": order["id"]}), 200


@app.route("/api/escrow/dispute", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def escrow_dispute(user):
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id") or data.get("id")
    reason = (data.get("reason") or data.get("dispute_reason") or "").strip()

    if not order_id:
        return jsonify({"error": "order_id is required"}), 400

    res = supabase.table("escrow_orders").select("*").eq("id", order_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        return jsonify({"error": "Escrow order not found"}), 404

    order = rows[0]
    tid = int(user["telegram_id"])
    if tid != int(order["buyer_id"]) and tid != int(order["seller_id"]):
        return jsonify({"error": "Forbidden"}), 403

    if order["status"] != "held":
        return jsonify({"error": f"Order cannot be disputed (status: {order['status']})"}), 400

    update_res = (
        supabase.table("escrow_orders")
        .update({"status": "disputed", "dispute_reason": reason or None})
        .eq("id", order["id"])
        .eq("status", "held")
        .execute()
    )
    if not update_res.data:
        return jsonify({"error": "Order already processed"}), 409

    return jsonify({"status": "success", "order_id": order["id"]}), 200


@app.route("/api/escrow/list", methods=["GET"])
@require_auth
def escrow_list(user):
    tid = user["telegram_id"]
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
        logging.exception("escrow_list: failed to load orders for telegram_id=%s", tid)
        return jsonify({"orders": []}), 200


# -----------------------------------------------------------------------------
# Background auto-release worker
# -----------------------------------------------------------------------------
# Runs in every gunicorn worker process as a daemon thread. Multiple workers
# polling the same table is expected and safe: the .eq("status","held")
# guard on the UPDATE means only one process's update can ever match a given
# row, so a seller can never be credited twice for the same order.

AUTO_RELEASE_INTERVAL_SECONDS = 5 * 60


def _auto_release_worker():
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            res = (
                supabase.table("escrow_orders")
                .select("*")
                .eq("status", "held")
                .lt("release_at", now_iso)
                .execute()
            )
            for order in (res.data or []):
                update_res = (
                    supabase.table("escrow_orders")
                    .update({"status": "released"})
                    .eq("id", order["id"])
                    .eq("status", "held")
                    .execute()
                )
                if update_res.data:
                    try:
                        _credit_seller(order)
                        logging.info("auto-release: released escrow order %s", order["id"])
                    except Exception:
                        logging.exception(
                            "auto-release: order %s marked released but crediting seller failed",
                            order["id"],
                        )
        except Exception:
            logging.exception("auto-release worker: loop iteration failed")

        time_module.sleep(AUTO_RELEASE_INTERVAL_SECONDS)


threading.Thread(target=_auto_release_worker, daemon=True).start()

print("main.py loaded - is_public patched + escrow active")

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
