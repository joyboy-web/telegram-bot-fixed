import logging
import uuid
from typing import Dict, Any
from supabase import Client

logger = logging.getLogger(__name__)

def get_balance(db: Client, telegram_id: str) -> int:
    res = db.table("users").select("balance").eq("telegram_id", telegram_id).single().execute()
    return res.data.get("balance", 0) if res.data else 0

def apply_transaction(db: Client, telegram_id: str, amount: int, tx_type: str, reference: str, meta: Dict[str, Any] = None) -> Dict:
    try:
        res = db.rpc("wallet_apply_transaction", {
            "p_user_id": telegram_id,
            "p_amount": amount,
            "p_type": tx_type,
            "p_reference": reference,
            "p_meta": meta or {}
        }).execute()
        return {"ok": True, "data": res.data}
    except Exception as e:
        msg = str(e).lower()
        if "insufficient" in msg or "balance" in msg:
            return {"ok": False, "error": "Insufficient balance"}
        logger.warning(f"wallet tx failed {reference}: {e}")
        return {"ok": False, "error": "Transaction failed"}

def fund_mock(db: Client, telegram_id: str, amount_kobo: int) -> Dict:
    ref = f"mock_{uuid.uuid4().hex[:8]}"
    return apply_transaction(db, telegram_id, amount_kobo, "fund_mock", ref, {"mock": True})
