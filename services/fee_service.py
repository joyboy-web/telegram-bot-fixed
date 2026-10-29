"""
services/fee_service.py

Dynamic fee model for PayLock — replaces the old fixed NGN 336 model,
which caused schema drift ("failed to list product") and didn't track
real Paystack costs.

  PLATFORM_FEE_KOBO      = 20000   (flat NGN 200 platform fee)
  PAYSTACK_FEE_CAP_KOBO  = 200000  (Paystack fee hard cap, NGN 2000)
  MIN_PRICE_KOBO         = 50000   (minimum listable product price, NGN 500)

  _calc_paystack_fee(price_kobo):
    price_kobo <= 250000  -> round_half_up(price_kobo * 1.5%)
    price_kobo  > 250000  -> round_half_up(price_kobo * 1.5% + 10000)
    capped at PAYSTACK_FEE_CAP_KOBO

  calculate_fees(price_kobo):
    total          = paystack_fee_kobo + PLATFORM_FEE_KOBO
    split_kobo     = (total + 1) // 2
    buyer_pays_kobo = price_kobo + split_kobo
    seller_gets_kobo = price_kobo - split_kobo

Worked example (price_kobo=200000, NGN 2000):
    paystack_fee_kobo = 3000
    total             = 23000
    split_kobo        = 11500
    buyer_pays_kobo   = 211500 (NGN 2115.00)
    seller_gets_kobo  = 188500 (NGN 1885.00)
    platform keeps    = 20000  (NGN 200.00)

Return dict carries BOTH the canonical *_kobo keys (matching the
locked products table columns) AND the old short-form keys
(price, buyer_pays, seller_gets, total_fee, buyer_fee, seller_fee)
so any existing caller reading the old keys keeps working unchanged.
"""

from typing import Dict

PLATFORM_FEE_KOBO: int = 20000
PAYSTACK_FEE_CAP_KOBO: int = 200000
MIN_PRICE_KOBO: int = 50000  # NGN 500

_PAYSTACK_PERCENT = 0.015
_PAYSTACK_FLAT_ADDON_KOBO = 10000
_PAYSTACK_FLAT_ADDON_THRESHOLD_KOBO = 250000


def _round_half_up(value: float) -> int:
    return int(value + 0.5)


def _calc_paystack_fee(price_kobo: int) -> int:
    price_kobo = int(price_kobo)
    if price_kobo <= _PAYSTACK_FLAT_ADDON_THRESHOLD_KOBO:
        fee = _round_half_up(price_kobo * _PAYSTACK_PERCENT)
    else:
        fee = _round_half_up(price_kobo * _PAYSTACK_PERCENT + _PAYSTACK_FLAT_ADDON_KOBO)
    return min(fee, PAYSTACK_FEE_CAP_KOBO)


def calculate_fees(price_kobo: int) -> Dict[str, int]:
    """Dynamic fee model. All values in kobo. Raises ValueError below MIN_PRICE_KOBO."""
    price_kobo = int(price_kobo)
    if price_kobo < MIN_PRICE_KOBO:
        raise ValueError("Minimum price is \u20a6500")

    paystack_fee_kobo = _calc_paystack_fee(price_kobo)
    total_fee_kobo = paystack_fee_kobo + PLATFORM_FEE_KOBO
    split_kobo = (total_fee_kobo + 1) // 2

    buyer_pays_kobo = price_kobo + split_kobo
    seller_gets_kobo = price_kobo - split_kobo

    return {
        # canonical kobo keys — match products table columns
        "price_kobo": price_kobo,
        "buyer_pays_kobo": buyer_pays_kobo,
        "seller_gets_kobo": seller_gets_kobo,
        "platform_fee_kobo": PLATFORM_FEE_KOBO,
        "paystack_fee_kobo": paystack_fee_kobo,
        "split_kobo": split_kobo,

        # backward-compatible short-form keys (old callers)
        "price": price_kobo,
        "total_fee": total_fee_kobo,
        "buyer_pays": buyer_pays_kobo,
        "seller_gets": seller_gets_kobo,
        "buyer_fee": split_kobo,
        "seller_fee": split_kobo,
    }


def format_naira(kobo: int) -> str:
    try:
        kobo = int(kobo)
    except (TypeError, ValueError):
        kobo = 0
    return f"\u20a6{kobo/100:,.2f}"
