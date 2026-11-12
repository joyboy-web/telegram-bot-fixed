"""
PayLock production entrypoint.

Deploy:  gunicorn main:app --bind 0.0.0.0:$PORT   (Procfile: web: gunicorn main:app)
Local:   python main.py                           (reads PORT, defaults to 5000)

Wraps app.py and adds:
  - /api/products override (never touches is_public, explicit columns only)
  - /api/escrow/confirm, /api/escrow/dispute, /api/escrow/list, /api/escrow/resolve
  - background auto-release worker (every 5 minutes)
"""
import os
import hmac
import logging
import threading
import time
from datetime import datetime, timezone

from flask import request, jsonify

from app import app, supabase, require_auth, limiter, extract_init_data, verify_init_data

logging.basicConfig(level=logging.INFO)

AUTO_RELEASE_INTERVAL_SECONDS = 5 * 60

PRODUCT_COLUMNS = (
   "id,name,title,description,price,price_kobo,buyer_pays_kobo,"
    "seller_gets_kobo,image_url,file_url,seller_id,owner_id,"
    "created_at,category"
) 


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fetch_paged(build, page=1000, max_pages=10):
    """Fetches rows page by page. `build` returns a ready query builder."""
    rows = []
    for i in range(max_pages):
        res = build().range(i * page, i * page + page - 1).execute()
        data = res.data or []
        rows.extend(data)
        if len(data) < page:
            break
    return rows


def _claim_order(order_id, from_status, payloads):
    """
    Atomic status claim on escrow_orders (only matches rows still in
    `from_status`). Tries each payload in order; if one raises (for example a
    timestamp column that does not exist yet) the next, simpler payload is used.
    The last payload should be status-only.
    """
    last_exc = None
    for payload in payloads:
        try:
            return (
                supabase.table("escrow_orders")
                .update(payload)
                .eq("id", order_id)
                .eq("status", from_status)
                .execute()
            )
        except Exception as exc:
            last_exc = exc
            logging.exception(
                "escrow claim: update %s failed for order %s, trying fallback payload",
                list(payload.keys()), order_id,
            )
    raise last_exc


def _revert_order(order_id, from_status, to_status, ts_field):
    """Best-effort revert of a claimed order, clearing its timestamp if possible."""
    try:
        supabase.table("escrow_orders").update({"status": to_status, ts_field: None}).eq("id", order_id).eq("status", from_status).execute()
        return
    except Exception:
        logging.exception("revert: %s column missing or update failed for order %s, trying status-only", ts_field, order_id)
    try:
        supabase.table("escrow_orders").update({"status": to_status}).eq("id", order_id).eq("status", from_status).execute()
    except Exception:
        logging.exception("revert to %s failed for order %s", to_status, order_id)


def _guarded_balance_add(telegram_id, amount):
    """
    Adds `amount` kobo to a user's balance using an optimistic guard:
    read the balance, then update with .eq("balance_kobo", old_balance).
    If the guarded update matches no row (someone changed the balance in
    between), re-read and retry once. Raises if both attempts lose the race.
    Returns the new balance.
    """
    amount = int(amount)
    for _attempt in range(2):
        res = supabase.table("users").select("balance_kobo").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        if not rows:
            raise RuntimeError(f"user {telegram_id} not found for balance credit")
        old_balance = int(rows[0].get("balance_kobo") or 0)
        upd = (
            supabase.table("users")
            .update({"balance_kobo": old_balance + amount})
            .eq("telegram_id", telegram_id)
            .eq("balance_kobo", old_balance)
            .execute()
        )
        if upd.data:
            return old_balance + amount
    raise RuntimeError(f"balance credit contention for user {telegram_id}")


# ---------------------------------------------------------------------------
# Products override (no is_public in any form; always HTTP 200)
# ---------------------------------------------------------------------------
def _enrich_products(products):
    """
    Adds seller_name, sales_count, views and reviews to each product in Python.
    Every block is isolated: a failure in one never breaks the product list.
    """
    for p in products:
        p["seller_name"] = "PayLock seller"
        p["sales_count"] = 0
        p["views"] = 0          # no views table yet
        p["reviews"] = []
    if not products:
        return products

    ids = [p["id"] for p in products if p.get("id") is not None]

    # seller_name: first_name + @username, fallback 'PayLock seller'
    try:
        seller_ids = set()
        for p in products:
            try:
                seller_ids.add(int(p.get("seller_id")))
            except (TypeError, ValueError):
                pass
        names = {}
        for chunk in _chunks(list(seller_ids), 100):
            ures = supabase.table("users").select("telegram_id,first_name,username").in_("telegram_id", chunk).execute()
            for u in (ures.data or []):
                first = (u.get("first_name") or "").strip()
                uname = (u.get("username") or "").strip().lstrip("@")
                parts = []
                if first:
                    parts.append(first)
                if uname:
                    parts.append("@" + uname)
                if parts:
                    names[int(u["telegram_id"])] = " ".join(parts)
        for p in products:
            try:
                nm = names.get(int(p.get("seller_id")))
            except (TypeError, ValueError):
                nm = None
            if nm:
                p["seller_name"] = nm
    except Exception:
        logging.exception("products enrichment: seller names failed")

    # sales_count: released escrow orders per product
    try:
        counts = {}
        for chunk in _chunks(ids, 100):
            rows = _fetch_paged(lambda c=chunk: (
                supabase.table("escrow_orders")
                .select("product_id")
                .eq("status", "released")
                .in_("product_id", c)
            ))
            for r in rows:
                k = str(r.get("product_id"))
                counts[k] = counts.get(k, 0) + 1
        for p in products:
            p["sales_count"] = counts.get(str(p.get("id")), 0)
    except Exception:
        logging.exception("products enrichment: sales_count failed")

    # reviews: newest first, max 20 per product
    try:
        by_product = {}
        for chunk in _chunks(ids, 100):
            rows = _fetch_paged(lambda c=chunk: (
                supabase.table("ratings")
                .select("product_id,stars,complaint_text,created_at")
                .in_("product_id", c)
                .order("created_at", desc=True)
            ), max_pages=3)
            for r in rows:
                k = str(r.get("product_id"))
                bucket = by_product.setdefault(k, [])
                if len(bucket) < 20:
                    bucket.append({
                        "stars": r.get("stars"),
                        "text": r.get("complaint_text"),
                        "created_at": r.get("created_at"),
                    })
        for p in products:
            p["reviews"] = by_product.get(str(p.get("id")), [])
    except Exception:
        logging.exception("products enrichment: reviews failed")

    return products


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
        products = res.data or []
        try:
            products = _enrich_products(products)
        except Exception:
            logging.exception("products_list_fixed: enrichment failed")
        return jsonify({"products": products}), 200
    except Exception:
        logging.exception("products_list_fixed failed")
        return jsonify({"products": []}), 200


app.view_functions["products_list"] = products_list_fixed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
    _guarded_balance_add(seller_id, amount)
    supabase.table("wallet_transactions").insert({"user_id": seller_id, "type": "credit", "amount_kobo": amount, "reason": "escrow_release", "reference": order.get("reference")}).execute()


def _refund_buyer(order):
    """Credits buyer_pays_kobo back to the buyer (guarded) and records escrow_refund."""
    order_reference = order.get("reference")
    if order_reference:
        existing = supabase.table("wallet_transactions").select("id").eq("reference", order_reference).eq("reason", "escrow_refund").limit(1).execute()
        if existing.data:
            return
    buyer_id = order["buyer_id"]
    amount = int(order["buyer_pays_kobo"])
    _guarded_balance_add(buyer_id, amount)
    try:
        supabase.table("wallet_transactions").insert({"user_id": buyer_id, "type": "credit", "amount_kobo": amount, "reason": "escrow_refund", "reference": order_reference}).execute()
    except Exception:
        # Balance is already credited; never revert the order from here or the
        # buyer could be refunded twice.
        logging.exception("CRITICAL escrow_refund: buyer credited but transaction row failed for order %s", order.get("id"))


# ---------------------------------------------------------------------------
# Escrow: confirm
# ---------------------------------------------------------------------------
@app.route("/api/escrow/confirm", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def escrow_confirm(user):
    data = request.get_json(silent=True) or {}
    order_ref = data.get("order_id") or data.get("id") or data.get("reference")
    if not order_ref:
        return jsonify({"error": "order_id is required"}), 400

    order = _find_order(order_ref)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    tid = user["telegram_id"]
    try:
        is_buyer = int(order.get("buyer_id")) == int(tid)
    except (TypeError, ValueError):
        is_buyer = False
    if not is_buyer:
        return jsonify({"error": "Only the buyer can confirm delivery"}), 403

    if order.get("status") != "held":
        return jsonify({"error": "Order is not in held state"}), 400

    # Atomic claim: only one caller can move held -> released.
    released = _claim_order(
        order["id"], "held",
        [{"status": "released", "released_at": _now_iso()}, {"status": "released"}],
    )
    if not released.data:
        return jsonify({"error": "Order already processed"}), 409

    try:
        _credit_seller(order)
    except Exception:
        logging.exception("escrow_confirm: crediting seller failed for order %s", order.get("id"))
        _revert_order(order["id"], "released", "held", "released_at")
        return jsonify({"error": "Order released but crediting seller failed. Contact support."}), 500

    return jsonify({"status": "success", "order_id": order["id"]}), 200


# ---------------------------------------------------------------------------
# Escrow: dispute
# ---------------------------------------------------------------------------
@app.route("/api/escrow/dispute", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def escrow_dispute(user):
    data = request.get_json(silent=True) or {}
    order_ref = data.get("order_id") or data.get("id")
    reason = str(data.get("reason") or data.get("dispute_reason") or "").strip()
    if not order_ref:
        return jsonify({"error": "order_id is required"}), 400

    order = _find_order(order_ref)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    tid = user["telegram_id"]
    try:
        is_buyer = int(order.get("buyer_id")) == int(tid)
    except (TypeError, ValueError):
        is_buyer = False
    try:
        is_seller = int(order.get("seller_id")) == int(tid)
    except (TypeError, ValueError):
        is_seller = False
    if not (is_buyer or is_seller):
        return jsonify({"error": "Not authorized for this order"}), 403

    if order.get("status") != "held":
        return jsonify({"error": "Only held orders can be disputed"}), 400

    now_iso = _now_iso()
    update_res = _claim_order(
        order["id"], "held",
        [
            {"status": "disputed", "dispute_reason": reason or None, "disputed_at": now_iso},
            {"status": "disputed", "dispute_reason": reason or None},
            {"status": "disputed", "disputed_at": now_iso},
            {"status": "disputed"},
        ],
    )

    if not update_res.data:
        return jsonify({"error": "Order already processed"}), 409

    return jsonify({"status": "success", "order_id": order["id"]}), 200


# ---------------------------------------------------------------------------
# Escrow: list
# ---------------------------------------------------------------------------
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
        orders = res.data or []

        # Enrichment only adds product_name + image_url; order fields are untouched.
        try:
            product_ids = list({o.get("product_id") for o in orders if o.get("product_id") is not None})
            pmap = {}
            if product_ids:
                pres = supabase.table("products").select("id,name,image_url").in_("id", product_ids).execute()
                pmap = {str(p.get("id")): p for p in (pres.data or [])}
            for o in orders:
                p = pmap.get(str(o.get("product_id")))
                o["product_name"] = p.get("name") if p else None
                o["image_url"] = p.get("image_url") if p else None
        except Exception:
            logging.exception("escrow_list: product enrichment failed")

        return jsonify({"orders": orders}), 200
    except Exception:
        logging.exception("escrow_list failed")
        return jsonify({"orders": []}), 200


# ---------------------------------------------------------------------------
# Escrow: resolve (admin only)
# ---------------------------------------------------------------------------
def _is_admin_request():
    admin_id = (os.environ.get("ADMIN_ID") or "").strip()
    if not admin_id:
        return False

    secret = request.headers.get("X-Admin-Secret", "")
    if secret and hmac.compare_digest(secret.encode("utf-8"), admin_id.encode("utf-8")):
        return True

    init_data = extract_init_data()
    if init_data:
        tg_user = verify_init_data(init_data)
        if tg_user:
            try:
                return int(tg_user.get("id")) == int(admin_id)
            except (TypeError, ValueError):
                return False
    return False


@app.route("/api/escrow/resolve", methods=["POST"])
@limiter.limit("20 per minute")
def escrow_resolve():
    if not _is_admin_request():
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json(silent=True) or {}
    order_ref = data.get("order_id") or data.get("id")
    action = str(data.get("action") or "").strip().lower()
    if not order_ref:
        return jsonify({"error": "order_id is required"}), 400
    if action not in ("refund", "release"):
        return jsonify({"error": "action must be 'refund' or 'release'"}), 400

    order = _find_order(order_ref)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    if order.get("status") != "disputed":
        return jsonify({"error": "Only disputed orders can be resolved"}), 400

    now_iso = _now_iso()

    if action == "refund":
        try:
            if int(order.get("buyer_pays_kobo") or 0) <= 0:
                return jsonify({"error": "Order has no refundable amount"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "Order has no refundable amount"}), 400

        # Atomic claim: only one resolve can move disputed -> refunded.
        claimed = _claim_order(
            order["id"], "disputed",
            [{"status": "refunded", "refunded_at": now_iso}, {"status": "refunded"}],
        )
        if not claimed.data:
            return jsonify({"error": "Order already processed"}), 409

        try:
            _refund_buyer(order)
        except Exception:
            logging.exception("escrow_resolve: refund failed for order %s", order.get("id"))
            _revert_order(order["id"], "refunded", "disputed", "refunded_at")
            return jsonify({"error": "Refund failed. Order is still disputed."}), 500
    else:
        # Atomic claim: only one resolve can move disputed -> released.
        claimed = _claim_order(
            order["id"], "disputed",
            [{"status": "released", "released_at": now_iso}, {"status": "released"}],
        )
        if not claimed.data:
            return jsonify({"error": "Order already processed"}), 409

        try:
            _credit_seller(order)
        except Exception:
            logging.exception("escrow_resolve: crediting seller failed for order %s", order.get("id"))
            _revert_order(order["id"], "released", "disputed", "released_at")
            return jsonify({"error": "Release failed. Order is still disputed."}), 500

    return jsonify({"status": "success"}), 200


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
                    released = _claim_order(
                        order["id"], "held",
                        [{"status": "released", "released_at": _now_iso()}, {"status": "released"}],
                    )
                    if not released.data:
                        continue
                    try:
                        _credit_seller(order)
                        logging.info("auto-release: released order %s", order.get("id"))
                    except Exception:
                        logging.exception("auto-release: crediting seller failed for order %s", order.get("id"))
                        _revert_order(order["id"], "released", "held", "released_at")
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
