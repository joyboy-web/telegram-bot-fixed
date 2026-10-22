from typing import Dict

TOTAL_FEE_KOBO: int = 33600  # ₦336 fixed - covers Paystack ₦136 + platform ₦200
SPLIT_KOBO: int = TOTAL_FEE_KOBO // 2  # 16800 = ₦168 each
MIN_PRICE_KOBO: int = 50000  # ₦500

def calculate_fees(price_kobo: int) -> Dict[str, int]:
    """Fixed ₦336 model - balanced 50/50. All values in kobo."""
    if price_kobo < MIN_PRICE_KOBO:
        raise ValueError("Minimum price is ₦500")
    
    return {
        "price": price_kobo,
        "total_fee": TOTAL_FEE_KOBO,
        "buyer_pays": price_kobo + SPLIT_KOBO,
        "seller_gets": price_kobo - SPLIT_KOBO,
        "buyer_fee": SPLIT_KOBO,
        "seller_fee": SPLIT_KOBO,
    }

def format_naira(kobo: int) -> str:
    return f"₦{kobo/100:,.2f}"
