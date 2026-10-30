"""
services/wallet_service.py

Rewritten against the LOCKED users schema: telegram_id, balance_kobo.

No Postgres RPC (wallet_apply_transaction) is used anywhere. Every
balance change is a direct SELECT -> compute new balance -> UPDATE,
plus a best-effort ledger insert into wallet_transactions (a ledger
write failure is logged but does not roll back the balance change,
matching the direct-update style already used in app.py).
"""

import logging
import uuid
from typing import Dict, Any, Optional
from supabase import Client

logger = logging.getLogger(__name__)


def get_balance(db: Client, telegram_id: str) -> int:
    """Return the user's balance_kobo as an int. 0 if user not found or on error."""
    try:
        res = (
            db.table("users")
            .select("balance_kobo")
            .eq("telegram_id", telegram_id)
            .single()
            .execute()
        )
        return int(res.data.get("balance_kobo", 0)) if res.data else 0
    except Exception as e:
        logger.error(f"get_balance failed for telegram_id={telegram_id}: {e}")
        return 0


def apply_transaction(
    db: Client,
    telegram_id: str,
    amount: int,
    tx_type: str,
    reference: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict:
    """
    Apply a signed `amount` (kobo) delta to the user's balance_kobo via a
    direct read-modify-write. Positive amount credits, negative debits.
    No rpc call. Returns {"ok": False, "error": ...} on insufficient
    balance / missing user / failure, matching the previous return shape.
    """
    try:
        res = (
            db.table("users")
            .select("telegram_id,balance_kobo")
            .eq("telegram_id", telegram_id)
            .single()
            .execute()
        )
        if not res.data:
            return {"ok": False, "error": "User not found"}

        previous_balance_kobo = int(res.data.get("balance_kobo") or 0)
        amount = int(amount)
        new_balance_kobo = previous_balance_kobo + amount

        if new_balance_kobo < 0:
            return {"ok": False, "error": "Insufficient balance"}

        db.table("users").update({"balance_kobo": new_balance_kobo}).eq(
            "telegram_id", telegram_id
        ).execute()

        try:
            db.table("wallet_transactions").insert(
                {
                    "user_id": telegram_id,
                    "type": tx_type,
                    "amount_kobo": amount,
                    "reason": tx_type,
                    "reference": reference,
                }
            ).execute()
        except Exception as e:
            logger.warning(f"wallet_transactions ledger insert failed {reference}: {e}")

        return {
            "ok": True,
            "data": {
                "telegram_id": telegram_id,
                "previous_balance_kobo": previous_balance_kobo,
                "new_balance_kobo": new_balance_kobo,
                "amount_kobo": amount,
                "reference": reference,
            },
        }
    except Exception as e:
        logger.warning(f"wallet tx failed {reference}: {e}")
        return {"ok": False, "error": "Transaction failed"}


def fund_mock(db: Client, telegram_id: str, amount_kobo: int) -> Dict:
    """Mock funding entrypoint, built entirely on apply_transaction (no rpc, no separate path)."""
    ref = f"mock_{uuid.uuid4().hex[:8]}"
    return apply_transaction(db, telegram_id, amount_kobo, "fund_mock", ref, {"mock": True})
