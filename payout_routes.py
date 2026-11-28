"""
payout_routes.py - NEW file. The routes for the Payout Patch, registered once from app.py with register(app, core).

  GET  /admin/payouts                      page shell (no data in it)
  GET  /api/admin/payouts                  admin only: list + filter + search + SENT audit
  POST /api/admin/payouts/<order_id>/done  admin only: Mark As Done (payout_logs only)
  GET  /api/payouts/mine                   signed-in user: read-only payout lines for seller / buyer history
  GET  /api/payouts/notifications          signed-in seller: read-only payout notifications

Nothing here writes to escrow_orders, users, wallets or any money field.
"""

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, jsonify, request

import payout_check
import payout_notify

_REF_RE = re.compile(r"^[A-Za-z0-9._/\- ]{3,60}$")
_STALE_CLAIM_SECONDS = 120     # kept for reference: the database function locks the request row, so a stale / retried claim can never debit twice
_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------------------------
# Pre-Send Guard + Receipt Upload (additive). Nothing below changes money logic: the guard is READ ONLY,
# and the only thing that ever debits is the existing process_manual_payout database function.
# ---------------------------------------------------------------------------------------------
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RECEIPT_MAX_BYTES = 10 * 1024 * 1024
_RECEIPT_EXTS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}
_RECEIPT_PREFIX = "payout-receipts"
_REASON_LABELS = {"manual_withdrawal": "Manual bank payout", "wallet_purchase": "Wallet Purchase", "purchase": "Wallet Purchase",
                  "withdrawal": "Withdrawal", "paystack_withdrawal": "Paystack Withdrawal"}


def _naira(kobo):
    k = int(kobo or 0)
    return "₦{:,.0f}".format(k / 100.0) if k % 100 == 0 else "₦{:,.2f}".format(k / 100.0)


def _ledger_ref(request_id):
    """The same reference the process_manual_payout function writes: wdr-<first 24 hex chars of the request id>."""
    return "wdr-" + str(request_id).replace("-", "").lower()[:24]


def _reason_label(reason):
    r = str(reason or "").strip()
    return _REASON_LABELS.get(r) or (r.replace("_", " ").title() if r else "-")


def _sniff_receipt(blob):
    """Real file type from the first bytes (never trust the file name): returns 'jpg' | 'png' | 'webp' | 'pdf' | None."""
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[:5] == b"%PDF-":
        return "pdf"
    return None


def _pre_send_check(sb, request_id):
    """READ ONLY. Never debits, never writes, never changes a hold. Returns None when the request does not exist.
    available = wallet balance minus the holds of the seller's OTHER pending requests (this request's own hold is
    shown separately, because the payout is what spends it)."""
    rid = str(request_id).strip().lower()
    if not _UUID_RE.match(rid):
        return None
    wrs = sb.table("withdrawal_requests").select("*").eq("id", rid).limit(1).execute().data or []
    if not wrs:
        return None
    wr = wrs[0]
    seller_id = str(wr.get("seller_id") or "")
    uid = int(seller_id) if seller_id.isdigit() else seller_id
    requested = int(wr.get("amount_kobo") or 0)
    status = str(wr.get("status") or "")
    urows = sb.table("users").select("balance_kobo").eq("telegram_id", uid).limit(1).execute().data or []
    balance = int((urows[0].get("balance_kobo") if urows else 0) or 0)
    pend = sb.table("withdrawal_requests").select("id,amount_kobo").eq("seller_id", seller_id).eq("status", "PENDING").execute().data or []
    total_hold = sum(int(r.get("amount_kobo") or 0) for r in pend)
    other_hold = sum(int(r.get("amount_kobo") or 0) for r in pend if str(r.get("id")).lower() != rid)
    available = balance - other_hold
    own_ref = _ledger_ref(rid)
    rows = sb.table("wallet_transactions").select("*").eq("user_id", uid).order("created_at", desc=True).limit(20).execute().data or []
    log = []
    for t in rows:
        reason = str(t.get("reason") or "")
        kind = str(t.get("type") or "")
        pending_row = reason.endswith("_pending")
        log.append({
            "id": t.get("id"), "date": t.get("created_at"), "type": kind,
            "amount_kobo": int(t.get("amount_kobo") or 0),
            "description": _reason_label(reason),
            "status": "PENDING" if pending_row else ("SENT" if reason == "manual_withdrawal" else "COMPLETED"),
            "reference": t.get("reference"),
            "balance_after": None,          # the existing ledger has no balance_after column; the schema is not changed
        })
    spent = []
    req_at = wr.get("requested_at")
    if req_at:
        after = sb.table("wallet_transactions").select("*").eq("user_id", uid).eq("type", "debit").gte("created_at", req_at) \
            .order("created_at", desc=True).limit(50).execute().data or []
        spent = [t for t in after if t.get("reference") != own_ref and not str(t.get("reason") or "").endswith("_pending")]
    spent_total = sum(int(t.get("amount_kobo") or 0) for t in spent)
    spent_flag = len(spent) > 0
    status_ok = status == "PENDING"
    can_send = bool(status_ok and requested > 0 and available >= requested)
    reason_bits = []
    if not status_ok:
        reason_bits.append("Request is already " + (status or "closed"))
    else:
        if spent_flag and not can_send:
            first = spent[0]
            reason_bits.append("Seller spent {} after request on {} - {}".format(
                _naira(spent_total), str(first.get("created_at") or "")[:16].replace("T", " "), _reason_label(first.get("reason"))))
        if available < requested:
            reason_bits.append("Available {} < Requested {}".format(_naira(available), _naira(requested)))
    block_reason = " - ".join(reason_bits) if not can_send else ""
    if not can_send:
        level = "RED"
    elif available == requested or spent_flag:
        level = "YELLOW"
    else:
        level = "GREEN"
    return {
        "request_id": rid, "status": status, "seller_id": seller_id, "seller_name": wr.get("seller_name"),
        "requested_amount_kobo": requested, "hold_amount_kobo": total_hold, "wallet_balance_kobo": balance,
        "available_balance_kobo": available, "transactions": log,
        "spent_after_request_flag": spent_flag, "spent_after_request_kobo": spent_total,
        "can_send": can_send, "block_reason": block_reason, "warning_level": level,
        "has_receipt": bool(wr.get("receipt_path")),
    }


def register(app, core):
    sb = core.supabase
    bp = Blueprint("payouts", __name__)
    limiter = core.limiter

    def _product_names(order_ids):
        out = {}
        try:
            orders = sb.table("escrow_orders").select("id,product_id,buyer_id,created_at,release_at") \
                .in_("id", order_ids).execute().data or []
            pids = list({o["product_id"] for o in orders if o.get("product_id") is not None})
            names = {}
            if pids:
                for p in (sb.table("products").select("id,name,title").in_("id", pids).execute().data or []):
                    names[str(p["id"])] = p.get("name") or p.get("title")
            for o in orders:
                out[str(o["id"])] = {"product": names.get(str(o.get("product_id"))) or "Product",
                                     "buyer_id": o.get("buyer_id"),
                                     "confirmed_at": o.get("release_at") or o.get("created_at")}
        except Exception:
            logging.warning("payouts: order details unavailable", exc_info=True)
        return out

    @bp.route("/admin/payouts", methods=["GET"])
    def admin_payouts_page():
        # Page shell only. Every piece of data comes from /api/admin/payouts, which checks Telegram auth + admin.
        try:
            with open(os.path.join(_HERE, "admin_payouts.html"), "rb") as fh:
                return Response(fh.read(), mimetype="text/html")
        except OSError:
            return jsonify({"error": "Not found"}), 404

    @bp.route("/api/admin/payouts", methods=["GET"])
    @core.require_auth
    @core.admin_only
    @limiter.limit("60 per minute")
    def admin_payouts_list(user):
        payout_check.refresh(sb)
        status = str(request.args.get("status") or "").upper()
        q = str(request.args.get("q") or "").strip().lower()[:60]
        try:
            rows = sb.table("payout_logs").select("*").order("payout_due_at").limit(500).execute().data or []
        except Exception as exc:
            return core._db_error(exc, "admin_payouts_list: lookup failed")
        wr = {}
        try:
            for w in (sb.table("withdrawal_requests").select("*").in_("id", [r["order_id"] for r in rows]).execute().data or []) if rows else []:
                wr[str(w["id"])] = w
        except Exception as exc:
            return core._db_error(exc, "admin_payouts_list: withdrawal requests unavailable")
        rows = [r for r in rows if str(r["order_id"]) in wr]      # only real withdrawal requests can be paid
        out, sent = [], []
        for r in rows:
            w = wr[str(r["order_id"])]
            item = {
                "order_id": r["order_id"], "product": "Wallet withdrawal",
                "bank_name": w.get("bank_name"), "account_number": w.get("account_number"), "account_name": w.get("account_name"),
                "seller_name": r.get("seller_name") or r.get("seller_id"), "amount": r.get("amount"),
                "buyer_confirmed_at": w.get("requested_at"), "payout_due_at": r.get("payout_due_at"),
                "status": r.get("payout_status"), "payout_ref": r.get("payout_ref"),
                "marked_at": r.get("marked_at"), "admin_marked_by": r.get("admin_marked_by"),
            }
            if item["status"] == "SENT":
                sent.append(item)
            if status in payout_check.STATUSES and item["status"] != status:
                continue
            if q and q not in str(item["order_id"]).lower() and q not in str(item["seller_name"]).lower():
                continue
            out.append(item)
        sent.sort(key=lambda x: str(x.get("marked_at") or ""), reverse=True)
        return jsonify({"payouts": out, "sent_log": sent[:100]}), 200

    @bp.route("/api/admin/payouts/<order_id>/done", methods=["POST"])
    @core.require_auth
    @core.admin_only
    @limiter.limit("30 per minute")
    def admin_payout_done(user, order_id):
        data = request.get_json(silent=True) or {}
        ref = str(data.get("payout_ref") or "").strip()
        receipt = str(data.get("receipt_url") or "").strip()
        if not _REF_RE.match(ref):
            return jsonify({"error": "Enter the payout reference (3-60 letters, numbers, - . / _)"}), 400
        if receipt and (not receipt.startswith("https://") or len(receipt) > 500 or " " in receipt):
            return jsonify({"error": "Receipt link must be a https:// link"}), 400
        order_id = str(order_id)
        try:                                                   # Pre-Send Guard: read-only, runs BEFORE anything that could debit
            _guard = _pre_send_check(sb, order_id)
        except Exception:
            logging.exception("admin_payout_done: pre-send check unavailable for %s (nothing was changed)", order_id)
            return jsonify({"error": "Pre-send check unavailable. Nothing was sent. Try again.", "code": "PRESEND_UNAVAILABLE"}), 503
        if _guard and _guard.get("status") == "PENDING" and not _guard.get("can_send"):
            return jsonify({"error": _guard.get("block_reason") or "Blocked by pre-send check", "code": "PRESEND_BLOCKED",
                            "block_reason": _guard.get("block_reason"), "warning_level": _guard.get("warning_level")}), 409
        row = payout_check.check_payout_status(sb, order_id)
        if not row:
            return jsonify({"error": "Payout record not found"}), 404
        if row.get("payout_status") == "SENT":
            return jsonify({"error": "Already paid out", "code": "ALREADY_SENT"}), 409
        try:
            wrs = sb.table("withdrawal_requests").select("*").eq("id", order_id).limit(1).execute().data or []
        except Exception as exc:
            return core._db_error(exc, "admin_payout_done: withdrawal request lookup failed")
        if not wrs:
            return jsonify({"error": "Only wallet withdrawal requests can be paid out", "code": "NO_REQUEST"}), 409
        wr = wrs[0]
        if wr.get("status") == "SENT":
            return jsonify({"error": "Already paid out", "code": "ALREADY_SENT"}), 409
        admin_id = str(user.get("telegram_id"))
        admin_name = user.get("first_name") or user.get("username") or admin_id
        # ONE database transaction does: claim + balance check + debit + ledger row wdr-<id> + mark SENT.
        # Any failure inside rolls everything back, so there is never a half-debit. No fallback path exists on purpose.
        try:
            res = sb.rpc("process_manual_payout", {
                "p_request_id": order_id, "p_admin_id": admin_id, "p_ref": ref, "p_receipt": receipt or None}).execute()
            out = res.data if isinstance(res.data, dict) else {}
        except Exception:
            logging.exception("admin_payout_done: process_manual_payout failed for %s (nothing was changed)", order_id)
            return jsonify({"error": "Payout could not be processed. Nothing was debited. (Has payout_setup_atomic.sql been run?)",
                            "code": "RPC_FAILED"}), 503
        if not out.get("ok"):
            code = out.get("code") or "FAILED"
            http = {"ALREADY_SENT": 409, "NOT_PENDING": 409, "INSUFFICIENT_BALANCE": 400, "NO_REQUEST": 404}.get(code, 400)
            return jsonify({"error": out.get("error") or "Payout failed", "code": code}), http
        try:
            done = (sb.table("payout_logs").select("*").eq("order_id", order_id).limit(1).execute().data or [{}])[0]
        except Exception:
            done = {}
        done.setdefault("order_id", order_id); done.setdefault("seller_id", row.get("seller_id"))
        done.setdefault("seller_name", row.get("seller_name")); done.setdefault("amount", row.get("amount"))
        done["payout_ref"] = ref; done["payout_receipt_url"] = receipt or None
        core._async(payout_notify.notify_payout_sent, sb, core._tg_send, done, "Wallet withdrawal", admin_name)
        logging.info("payout marked SENT for request %s", order_id)
        return jsonify({"ok": True, "payout": {
            "order_id": order_id, "status": "SENT", "payout_ref": ref, "marked_at": done.get("marked_at")}}), 200

    @bp.route("/api/admin/payouts/<order_id>/reject", methods=["POST"])
    @core.require_auth
    @core.admin_only
    @limiter.limit("30 per minute")
    def admin_payout_reject(user, order_id):
        """Rejecting releases the hold. Nothing was debited, so nothing is refunded."""
        try:
            res = sb.rpc("reject_manual_payout", {"p_request_id": str(order_id), "p_admin_id": str(user.get("telegram_id"))}).execute()
            out = res.data if isinstance(res.data, dict) else {}
        except Exception:
            logging.exception("admin_payout_reject: failed for %s", order_id)
            return jsonify({"error": "Could not reject. Nothing was changed.", "code": "RPC_FAILED"}), 503
        if not out.get("ok"):
            return jsonify({"error": out.get("error") or "Could not reject", "code": out.get("code") or "FAILED"}), 409
        return jsonify({"ok": True}), 200

    @bp.route("/api/payouts/request", methods=["POST"])
    @core.require_auth
    @limiter.limit("5 per minute")
    def payouts_request(user):
        """Seller asks for a bank payout from the wallet. Nothing is debited here: the debit happens on Mark As Done."""
        tid = user["telegram_id"]
        data = request.get_json(silent=True) or {}
        amount_kobo = core._amount_kobo_from(data)
        if amount_kobo is None or amount_kobo < core.WITHDRAWAL_MIN_KOBO:
            return jsonify({"error": "Minimum withdrawal is ₦1,000", "code": "AMOUNT_TOO_LOW"}), 400
        bank_name = str(data.get("bank_name") or "").strip()[:60]
        account_number = str(data.get("account_number") or "").strip()
        account_name = str(data.get("account_name") or "").strip()[:80]
        if not bank_name or not account_name or not re.fullmatch(r"\d{10}", account_number):
            return jsonify({"error": "Bank name, 10-digit account number and account name are required"}), 400
        pin = str(data.get("pin") or "")
        if not re.fullmatch(r"\d{4}", pin):
            return jsonify({"error": "A 4-digit PIN is required", "code": "PIN_INVALID"}), 400
        try:
            rows = sb.table("users").select("balance_kobo,pin_hash,pin_fail_count,pin_locked_until").eq("telegram_id", tid).limit(1).execute().data or []
        except Exception as exc:
            return core._db_error(exc, "payouts_request: user lookup failed")
        if not rows:
            return jsonify({"error": "User not found"}), 404
        u = rows[0]
        if not u.get("pin_hash"):
            return jsonify({"error": "Set a PIN before withdrawing", "code": "PIN_NOT_SET"}), 403
        remaining = core._lock_seconds_remaining(u.get("pin_locked_until"))
        if remaining > 0:
            return jsonify({"error": "Too many wrong PIN attempts. Try again later.", "code": "RATE_LIMITED", "retry_after": remaining}), 429
        pin_fail = core._check_pin_and_track(tid, u, pin)       # the same ONE-PIN check as every other withdrawal
        if pin_fail is not None:
            return pin_fail
        if amount_kobo > int(u.get("balance_kobo") or 0) - core._manual_payout_hold_kobo(tid):
            return jsonify({"error": "Insufficient balance", "code": "INSUFFICIENT_BALANCE"}), 400
        name = user.get("first_name") or user.get("username") or str(tid)
        req_id = str(uuid.uuid4())
        try:
            sb.table("withdrawal_requests").insert({
                "id": req_id, "seller_id": str(tid), "seller_name": name, "amount_kobo": amount_kobo,
                "amount": round(amount_kobo / 100.0, 2), "bank_name": bank_name,
                "account_number": account_number, "account_name": account_name}).execute()
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                return jsonify({"error": "You already have a payout request waiting", "code": "REQUEST_OPEN"}), 409
            return core._db_error(exc, "payouts_request: save failed")
        try:
            sb.table("payout_logs").insert({
                "order_id": req_id, "seller_id": str(tid), "seller_name": name, "amount": round(amount_kobo / 100.0, 2),
                "payout_due_at": (datetime.now(timezone.utc) + payout_check._window()).isoformat(), "payout_status": "PENDING"}).execute()
        except Exception as exc:
            try:
                sb.table("withdrawal_requests").delete().eq("id", req_id).execute()
            except Exception:
                logging.warning("payouts_request: could not remove orphan request", exc_info=True)
            return core._db_error(exc, "payouts_request: payout log failed")
        return jsonify({"ok": True, "request_id": req_id}), 201

    @bp.route("/api/payouts/mine", methods=["GET"])
    @core.require_auth
    @limiter.limit("60 per minute")
    def payouts_mine(user):
        tid = str(user["telegram_id"])
        payout_check.refresh(sb)
        out = []
        try:
            mine = sb.table("payout_logs").select("order_id,payout_status,payout_ref,marked_at,seller_id") \
                .eq("seller_id", tid).order("created_at", desc=True).limit(50).execute().data or []
            for r in mine:
                out.append({"order_id": r["order_id"], "role": "seller", "status": r["payout_status"],
                            "payout_ref": r.get("payout_ref") if r["payout_status"] == "SENT" else None,
                            "marked_at": r.get("marked_at")})
            bo = sb.table("escrow_orders").select("id").eq("buyer_id", int(tid)).eq("status", "released") \
                .order("created_at", desc=True).limit(50).execute().data or []
            ids = [str(o["id"]) for o in bo]
            if ids:
                for r in (sb.table("payout_logs").select("order_id,payout_status,marked_at")
                          .in_("order_id", ids).eq("payout_status", "SENT").execute().data or []):
                    out.append({"order_id": r["order_id"], "role": "buyer", "status": "SENT",
                                "payout_ref": None, "marked_at": r.get("marked_at")})   # buyers never see the ref or amount
        except Exception:
            logging.warning("payouts_mine: lookup failed", exc_info=True)
        try:                                                   # additive: lets the seller's payout line show a Download receipt link
            ids = [o["order_id"] for o in out if o.get("role") == "seller"]
            if ids:
                got = {str(r["id"]): bool(r.get("receipt_path")) for r in
                       (sb.table("withdrawal_requests").select("id,receipt_path").in_("id", ids).eq("seller_id", tid).execute().data or [])}
                for o in out:
                    if o.get("role") == "seller":
                        o["has_receipt"] = bool(got.get(str(o["order_id"])))
        except Exception:
            logging.info("payouts_mine: receipt flags unavailable (payout_setup_receipt.sql not run yet?)")
        return jsonify({"payouts": out}), 200

    @bp.route("/api/payouts/notifications", methods=["GET"])
    @core.require_auth
    def payouts_notifications(user):
        try:
            rows = sb.table("payout_notifications").select("message,payout_ref,created_at") \
                .eq("seller_id", str(user["telegram_id"])).order("created_at", desc=True).limit(30).execute().data or []
        except Exception:
            rows = []
        return jsonify({"notifications": rows}), 200

    # ------------------------------ Pre-Send Guard route (read only) ------------------------------
    @bp.route("/admin/payout/<request_id>/pre-send-check", methods=["GET"])
    @core.require_auth
    @core.admin_only
    @limiter.limit("60 per minute")
    def admin_pre_send_check(user, request_id):
        if not _UUID_RE.match(str(request_id)):
            return jsonify({"error": "Not found"}), 404
        try:
            out = _pre_send_check(sb, request_id)
        except Exception as exc:
            return core._db_error(exc, "admin_pre_send_check failed")
        if not out:
            return jsonify({"error": "Request not found"}), 404
        return jsonify(out), 200

    # ------------------------------ Receipt upload ------------------------------
    def _receipt_path_ok(path):
        return bool(path) and str(path).startswith(_RECEIPT_PREFIX + "/") and ".." not in str(path)

    def _tg_receipt(seller_id, admin_tid, seller_name, amount_kobo, ref, status, fname, blob):
        """Best effort. Uses the EXISTING bot sender; a Telegram failure never blocks the payout or the saved receipt."""
        from concurrent.futures import ThreadPoolExecutor
        caption = ("PayLock Payout Receipt\nSeller: {}\nAmount: {}\nRef: {}\nStatus: {}\nDate: {}"
                   .format(str(seller_name or seller_id)[:60], _naira(amount_kobo), ref, status,
                           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))
        targets = []
        for cid in (seller_id, admin_tid):
            c = str(cid or "").strip()
            if c.isdigit() and c not in targets:
                targets.append(c)
        if not targets:
            return False, "no Telegram chat to send to"
        ex = ThreadPoolExecutor(max_workers=len(targets))
        futs = [ex.submit(core._tg_send_document, c, fname, blob, caption) for c in targets]
        ok_all, err = True, None
        try:
            for f in futs:
                try:
                    if not f.result(timeout=25):
                        ok_all, err = False, "Telegram did not accept the receipt"
                except Exception:
                    ok_all, err = False, "Telegram send timed out or failed"
        finally:
            ex.shutdown(wait=False)
        return ok_all, err

    @bp.route("/admin/payout/<request_id>/upload-receipt", methods=["POST"])
    @core.require_auth
    @core.admin_only
    @limiter.limit("10 per minute")
    def admin_upload_receipt(user, request_id):
        rid = str(request_id).strip().lower()
        if not _UUID_RE.match(rid):
            return jsonify({"error": "Request not found"}), 404
        mode = str(request.form.get("mode") or "only").strip().lower()
        if mode not in ("send", "only"):
            return jsonify({"error": "Invalid mode"}), 400
        bank_ref = str(request.form.get("bank_ref") or "").strip()
        note = re.sub(r"[\x00-\x1f\x7f]", " ", str(request.form.get("note") or "")).strip()[:200]
        if bank_ref and not _REF_RE.match(bank_ref):
            return jsonify({"error": "Bank reference: 3-60 letters, numbers, - . / _"}), 400
        f = request.files.get("file")
        if not f or not getattr(f, "filename", ""):
            return jsonify({"error": "Choose a receipt file (JPG, PNG, WEBP or PDF)"}), 400
        ext = os.path.splitext(str(f.filename))[1].lower().lstrip(".")
        if ext not in _RECEIPT_EXTS:
            return jsonify({"error": "Only JPG, PNG, WEBP or PDF files are allowed"}), 400
        blob = f.read(_RECEIPT_MAX_BYTES + 1)
        if not blob:
            return jsonify({"error": "The file is empty"}), 400
        if len(blob) > _RECEIPT_MAX_BYTES:
            return jsonify({"error": "File is too large (max 10MB)"}), 400
        kind = _sniff_receipt(blob)
        if kind is None or _RECEIPT_EXTS[kind] != _RECEIPT_EXTS[ext]:
            return jsonify({"error": "The file content does not match a JPG, PNG, WEBP or PDF"}), 400
        # ---- nothing has been written yet; every check below can still refuse ----
        try:
            wrs = sb.table("withdrawal_requests").select("*").eq("id", rid).limit(1).execute().data or []
        except Exception as exc:
            return core._db_error(exc, "admin_upload_receipt: request lookup failed")
        if not wrs:
            return jsonify({"error": "Request not found"}), 404
        wr = wrs[0]
        admin_tid = str(user.get("telegram_id"))
        if str(wr.get("seller_id")) == admin_tid:
            return jsonify({"error": "You cannot upload a receipt for your own request", "code": "SELF_REQUEST"}), 403
        status = str(wr.get("status") or "")
        if status not in ("PENDING", "SENT"):
            return jsonify({"error": "Receipts can only be added to PENDING or SENT requests", "code": "NOT_ALLOWED"}), 409
        if mode == "send":
            if status == "SENT":
                return jsonify({"error": "Already paid out", "code": "ALREADY_SENT"}), 409
            try:
                guard = _pre_send_check(sb, rid)
            except Exception:
                logging.exception("admin_upload_receipt: pre-send check unavailable for %s", rid)
                return jsonify({"error": "Pre-send check unavailable. Nothing was sent. Try again.", "code": "PRESEND_UNAVAILABLE"}), 503
            if not guard or not guard.get("can_send"):
                return jsonify({"error": (guard or {}).get("block_reason") or "Blocked by pre-send check", "code": "PRESEND_BLOCKED",
                                "block_reason": (guard or {}).get("block_reason"), "warning_level": (guard or {}).get("warning_level")}), 409
        try:                                                   # the one new column must exist BEFORE we debit anything
            sb.table("withdrawal_requests").select("receipt_path").eq("id", rid).limit(1).execute()
        except Exception:
            logging.exception("admin_upload_receipt: receipt_path column missing")
            return jsonify({"error": "Receipt storage is not set up. Run payout_setup_receipt.sql first. Nothing was changed.",
                            "code": "SETUP_NEEDED"}), 503
        seller_id = re.sub(r"[^0-9A-Za-z_\-]", "", str(wr.get("seller_id") or "x"))[:40] or "x"
        fname = "receipt_{}_{}_{}.{}".format(rid, int(datetime.now(timezone.utc).timestamp()), uuid.uuid4().hex[:8], kind)
        path = "{}/{}/{}".format(_RECEIPT_PREFIX, seller_id, fname)
        try:
            sb.storage.from_(core.PRODUCT_FILE_BUCKET).upload(path=path, file=blob, file_options={"content-type": _RECEIPT_EXTS[kind]})
        except Exception:
            logging.exception("admin_upload_receipt: storage upload failed for %s", rid)
            return jsonify({"error": "Could not store the receipt. Nothing was changed.", "code": "STORAGE_FAILED"}), 503

        def _drop(p):
            try:
                if _receipt_path_ok(p):
                    sb.storage.from_(core.PRODUCT_FILE_BUCKET).remove([p])
            except Exception:
                logging.info("admin_upload_receipt: could not remove a stray receipt file")

        def _save_path():
            sb.table("withdrawal_requests").update({"receipt_path": path}).eq("id", rid).execute()

        download_url = "/api/payouts/{}/receipt".format(rid)
        amount_kobo = int(wr.get("amount_kobo") or 0)
        wref = _ledger_ref(rid)
        if mode == "only":
            old = wr.get("receipt_path")
            try:
                _save_path()
            except Exception as exc:
                _drop(path)
                return core._db_error(exc, "admin_upload_receipt: could not save receipt path")
            if old and old != path:
                _drop(old)
            tg_ok, tg_err = _tg_receipt(wr.get("seller_id"), admin_tid, wr.get("seller_name"), amount_kobo,
                                        bank_ref or wref, status, fname, blob)
            logging.info("receipt uploaded (only) for request %s", rid)
            return jsonify({"success": True, "receipt_url": download_url, "transaction_log_id": None, "status": status,
                            "download_url": download_url, "tg_sent": tg_ok, "tg_error": tg_err}), 200
        # ---- send mode: the EXISTING atomic function does the debit, ledger row and SENT mark ----
        ref = bank_ref or "AUTO-{}-{}".format(rid.replace("-", "")[:8].upper(), int(datetime.now(timezone.utc).timestamp()))
        try:
            res = sb.rpc("process_manual_payout", {"p_request_id": rid, "p_admin_id": admin_tid, "p_ref": ref, "p_receipt": None}).execute()
            out = res.data if isinstance(res.data, dict) else {}
        except Exception:
            logging.exception("admin_upload_receipt: process_manual_payout failed for %s (nothing was changed)", rid)
            try:
                _save_path()                                   # keep the file as a PENDING upload
            except Exception:
                pass
            return jsonify({"success": False, "error": "Payout could not be processed. Nothing was debited. Receipt kept as PENDING.",
                            "code": "RPC_FAILED", "status": "PENDING", "receipt_url": download_url, "download_url": download_url,
                            "transaction_log_id": None, "tg_sent": False, "tg_error": None}), 503
        if not out.get("ok"):
            code = out.get("code") or "FAILED"
            http = {"ALREADY_SENT": 409, "NOT_PENDING": 409, "INSUFFICIENT_BALANCE": 400, "NO_REQUEST": 404}.get(code, 400)
            if code in ("ALREADY_SENT", "NOT_PENDING", "NO_REQUEST"):
                _drop(path)                                    # someone else finished it first: do not keep a duplicate file
            else:
                try:
                    _save_path()
                except Exception:
                    pass
            return jsonify({"success": False, "error": out.get("error") or "Payout failed", "code": code,
                            "status": "PENDING"}), http
        try:
            _save_path()
        except Exception:
            logging.exception("admin_upload_receipt: SENT but receipt path not saved for %s", rid)
        tx_id = None
        try:
            tx = sb.table("wallet_transactions").select("id").eq("reference", wref).limit(1).execute().data or []
            tx_id = tx[0]["id"] if tx else None
        except Exception:
            pass
        admin_name = user.get("first_name") or user.get("username") or admin_tid
        done = {"order_id": rid, "seller_id": wr.get("seller_id"), "seller_name": wr.get("seller_name"),
                "amount": round(amount_kobo / 100.0, 2), "payout_ref": ref, "payout_receipt_url": None}
        core._async(payout_notify.notify_payout_sent, sb, core._tg_send, done, "Wallet withdrawal", admin_name)
        tg_ok, tg_err = _tg_receipt(wr.get("seller_id"), admin_tid, wr.get("seller_name"), amount_kobo, ref, "SENT", fname, blob)
        logging.info("payout marked SENT with receipt for request %s", rid)
        return jsonify({"success": True, "receipt_url": download_url, "transaction_log_id": tx_id, "status": "SENT",
                        "download_url": download_url, "tg_sent": tg_ok, "tg_error": tg_err}), 200

    @bp.route("/api/payouts/<request_id>/receipt", methods=["GET"])
    @core.require_auth
    @limiter.limit("30 per minute")
    def payout_receipt_download(user, request_id):
        """The seller who owns the request, or the admin. Anyone else gets 404 (no hint that it exists)."""
        rid = str(request_id).strip().lower()
        if not _UUID_RE.match(rid):
            return jsonify({"error": "Not found"}), 404
        try:
            wrs = sb.table("withdrawal_requests").select("seller_id,receipt_path").eq("id", rid).limit(1).execute().data or []
        except Exception as exc:
            return core._db_error(exc, "payout_receipt_download: lookup failed")
        if not wrs:
            return jsonify({"error": "Not found"}), 404
        wr = wrs[0]
        if not (core._is_admin(user) or str(wr.get("seller_id")) == str(user.get("telegram_id"))):
            return jsonify({"error": "Not found"}), 404
        path = wr.get("receipt_path")
        if not _receipt_path_ok(path):
            return jsonify({"error": "No receipt uploaded yet"}), 404
        try:
            blob = sb.storage.from_(core.PRODUCT_FILE_BUCKET).download(path)
        except Exception:
            logging.warning("payout_receipt_download: file unavailable for %s", rid, exc_info=True)
            return jsonify({"error": "Receipt file unavailable"}), 404
        ext = os.path.splitext(str(path))[1].lower().lstrip(".")
        resp = Response(blob, mimetype=_RECEIPT_EXTS.get(ext, "application/octet-stream"))
        resp.headers["Content-Disposition"] = 'attachment; filename="payout_receipt_{}.{}"'.format(rid[:8], ext if ext in _RECEIPT_EXTS else "bin")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    app.register_blueprint(bp)
