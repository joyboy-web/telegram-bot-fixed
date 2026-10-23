import os, hmac, hashlib, json, requests, uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

BOT_TOKEN = os.getenv("BOT_TOKEN") or ""
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

YOUR_PROFIT = 20000  # ₦200

def calc_fees(price_kobo):
    price_naira = price_kobo / 100
    if price_naira <= 2500:
        fee = price_naira * 0.015
    else:
        fee = price_naira * 0.015 + 100
    fee = min(fee, 2000)
    paystack_fee = int(round(fee * 100))
    total_extra = YOUR_PROFIT + paystack_fee
    split = total_extra // 2
    return {"paystack_fee_kobo": paystack_fee, "platform_fee_kobo": YOUR_PROFIT, "total_extra_kobo": total_extra, "split_kobo": split, "buyer_pays_kobo": price_kobo + split, "seller_gets_kobo": price_kobo - split}

def verify_init_data(init_data):
    try:
        parsed = dict(parse_qsl(init_data))
        h = parsed.pop('hash', None)
        if not h or not BOT_TOKEN: return None
        check = "\n".join(f"{k}={v}" for k,v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h
