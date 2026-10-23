import os, hmac, hashlib, json, requests, uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify, send_from_directory
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

YOUR_PROFIT = 20000

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
        if not h or not BOT_TOKEN:
            return None
        check = "\n".join(f"{k}={v}" for k,v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h):
            return None
        return json.loads(parsed.get('user','{}'))
    except:
        return None

def get_user():
    auth = request.headers.get('Authorization','')
    if not auth.startswith('tma '):
        return None
    return verify_init_data(auth[4:])

# ===== FIXED ONLY THIS - SERVE index.html =====
@app.route('/')
@app.route('/app')
def serve_frontend():
    if os.path.exists('index.html'):
        return send_from_directory('.', 'index.html')
    if os.path.exists('static/index.html'):
        return send_from_directory('static', 'index.html')
    return jsonify(ok=False, error="index.html not found in root - push it")

@app.route('/health')
@app.route('/healthz')
def health():
    return jsonify(ok=True, service="PayLock V7.3 searchable banks", frontend=os.path.exists('index.html'))

@app.route('/api/auth/validate', methods=['POST'])
def auth_validate():
    user = verify_init_data((request.get_json() or {}).get('initData',''))
    if not user:
        return jsonify(ok=False), 401
    try:
        if supabase:
            ex = supabase.table('users').select('*').eq('telegram_id', user.get('id')).execute()
            if not ex.data:
                supabase.table('users').insert({'telegram_id': user.get('id'), 'first_name': user.get('first_name'), 'username': user.get('username',''), 'balance_kobo': 0}).execute()
    except:
        pass
    return jsonify(ok=True, user={"telegram_id": user.get('id'), "first_name": user.get('first_name'), "username": user.get('username','')})

@app.route('/api/products', methods=['GET'])
def list_products():
    if not supabase:
        return jsonify(ok=True, data=[])
    r = supabase.table('products').select('*').eq('is_public', True).order('created_at', desc=True).limit(100).execute()
    return jsonify(ok=True, data=r.data or [])

@app.route('/api/products', methods=['POST'])
def create_product():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    name = request.form.get('name','').strip()
    price_kobo = int(request.form.get('price_kobo',0))
    cover = request.files.get('cover')
    if not name or price_kobo < 50000 or not cover:
        return jsonify(ok=False, error="name min 500 cover required"), 400
    fees = calc_fees(price_kobo)
    cname = f"{uuid.uuid4().hex}_{secure_filename(cover.filename)}"
    supabase.storage.from_('product-covers').upload(cname, cover.read(), {"content-type": cover.mimetype})
    image_url = supabase.storage.from_('product-covers').get_public_url(cname)
    file_url = None
    pf = request.files.get('product_file')
    if pf:
        fname = f"{uuid.uuid4().hex}_{secure_filename(pf.filename)}"
        supabase.storage.from_('product-files').upload(fname, pf.read(), {"content-type": pf.mimetype})
        file_url = supabase.storage.from_('product-files').get_public_url(fname)
    ins = {"id": str(uuid.uuid4()), "seller_id": user.get('id'), "name": name, "description": request.form.get('description',''), "price_kobo": price_kobo, "buyer_pays_kobo": fees['buyer_pays_kobo'], "seller_gets_kobo": fees['seller_gets_kobo'], "platform_fee_kobo": fees['platform_fee_kobo'], "paystack_fee_kobo": fees['paystack_fee_kobo'], "image_url": image_url, "file_url": file_url, "category": request.form.get('category','Course'), "is_public": True, "created_at": datetime.utcnow().isoformat(), "escrow_release_at": (datetime.utcnow()+timedelta(hours=24)).isoformat()}
    supabase.table('products').insert(ins).execute()
    return jsonify(ok=True, data=ins)

@app.route('/api/wallet/balance', methods=['GET'])
def bal():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    r = supabase.table('users').select('balance_kobo').eq('telegram_id', user.get('id')).execute()
    return jsonify(ok=True, data={"balance": r.data[0]['balance_kobo'] if r.data else 0})

@app.route('/api/banks', methods=['GET'])
def banks():
    try:
        h = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
        r = requests.get("https://api.paystack.co/bank", headers=h, timeout=15)
        j = r.json()
        return jsonify(ok=True, data=j['data'] if j.get('status') else [])
    except:
        return jsonify(ok=True, data=[])

@app.route('/api/banks/resolve', methods=['POST'])
def resolve():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    d = request.get_json() or {}
    h = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
    r = requests.get(f"https://api.paystack.co/bank/resolve?account_number={d.get('account_number')}&bank_code={d.get('bank_code')}", headers=h, timeout=15)
    j = r.json()
    if j.get('status'):
        return jsonify(ok=True, data=j['data'])
    return jsonify(ok=False, error=j.get('message')), 400

@app.route('/api/wallet/fund/mock', methods=['POST'])
def fund_mock():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    amt = int((request.get_json() or {}).get('amount_kobo', 0))
    cur = supabase.table('users').select('balance_kobo').eq('telegram_id', user.get('id')).execute()
    bal = cur.data[0]['balance_kobo'] if cur.data else 0
    new_bal = bal + amt
    supabase.table('users').update({"balance_kobo": new_bal}).eq('telegram_id', user.get('id')).execute()
    supabase.table('wallet_transactions').insert({"id": str(uuid.uuid4()), "user_id": user.get('id'), "type": "credit", "amount_kobo": amt, "reason": "Wallet funding (mock)", "reference": f"mock_{uuid.uuid4().hex[:8]}", "created_at": datetime.utcnow().isoformat()}).execute()
    return jsonify(ok=True, data={"balance": new_bal})

@app.route('/api/wallet/fund/initialize', methods=['POST'])
def fund_init():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    data = request.get_json() or {}
    amt = int(data.get('amount_kobo',0))
    email = data.get('email', f"{user.get('id')}@paylock.local")
    h = {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}
    r = requests.post("https://api.paystack.co/transaction/initialize", json={"email": email, "amount": amt, "metadata": {"telegram_id": user.get('id')}}, headers=h, timeout=15)
    j = r.json()
    if not j.get('status'):
        return jsonify(ok=False, error=j.get('message')), 400
    return jsonify(ok=True, data=j['data'])

@app.route('/api/wallet/verify-transaction', methods=['POST'])
def verify_tx():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    ref = (request.get_json() or {}).get('reference')
    h = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
    r = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=h, timeout=15)
    j = r.json()
    if j.get('status') and j['data']['status'] == 'success':
        amt = j['data']['amount']
        ex = supabase.table('wallet_transactions').select('*').eq('reference', ref).execute()
        if ex.data:
            return jsonify(ok=True, data={"already": True})
        cur = supabase.table('users').select('balance_kobo').eq('telegram_id', user.get('id')).execute()
        bal = cur.data[0]['balance_kobo'] if cur.data else 0
        supabase.table('users').update({"balance_kobo": bal + amt}).eq('telegram_id', user.get('id')).execute()
        supabase.table('wallet_transactions').insert({"id": str(uuid.uuid4()), "user_id": user.get('id'), "type": "credit", "amount_kobo": amt, "reason": "Paystack funding", "reference": ref, "created_at": datetime.utcnow().isoformat()}).execute()
        return jsonify(ok=True, data={"credited": amt})
    return jsonify(ok=False), 400

@app.route('/api/wallet/withdraw', methods=['POST'])
def withdraw():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    data = request.get_json() or {}
    amt = int(data.get('amount_kobo',0))
    if amt < 100000:
        return jsonify(ok=False, error="min 1000"), 400
    cur = supabase.table('users').select('balance_kobo').eq('telegram_id', user.get('id')).execute()
    bal = cur.data[0]['balance_kobo'] if cur.data else 0
    if bal < amt:
        return jsonify(ok=False, error="insufficient"), 400
    supabase.table('users').update({"balance_kobo": bal - amt}).eq('telegram_id', user.get('id')).execute()
    wd_id = str(uuid.uuid4())
    supabase.table('withdrawals').insert({"id": wd_id, "user_id": user.get('id'), "amount_kobo": amt, "bank_name": data.get('bank_name'), "bank_code": data.get('bank_code',''), "account_number": data.get('account_number'), "account_name": data.get('account_name'), "status": "pending", "reference": f"WD_{wd_id[:8]}", "created_at": datetime.utcnow().isoformat()}).execute()
    return jsonify(ok=True, data={"id": wd_id})

@app.route('/api/wallet/withdrawals', methods=['GET'])
def withdrawals():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    try:
        r = supabase.table('withdrawals').select('*').eq('user_id', user.get('id')).order('created_at', desc=True).execute()
        return jsonify(ok=True, data=r.data or [])
    except:
        return jsonify(ok=True, data=[])

@app.route('/api/wallet/transactions', methods=['GET'])
def tx():
    user = get_user()
    if not user:
        return jsonify(ok=False), 401
    try:
        r = supabase.table('wallet_transactions').select('*').eq('user_id', user.get('id')).order('created_at', desc=True).limit(100).execute()
        return jsonify(ok=True, data=r.data or [])
    except:
        return jsonify(ok=True, data=[])

@app.route('/api/wallet/buy/<pid>', methods=['POST'])
def buy(pid):
    user = get_user()
    if not user:
        return jsonify(ok=False, error="unauthorized"), 401
    prod = supabase.table('products').select('*').eq('id', pid).execute()
    if not prod.data:
        return jsonify(ok=False, error="not found"), 404
    p = prod.data[0]
    buyer_pays = p['buyer_pays_kobo']
    cur = supabase.table('users').select('balance_kobo').eq('telegram_id', user.get('id')).execute()
    bal = cur.data[0]['balance_kobo'] if cur.data else 0
    if bal < buyer_pays:
        return jsonify(ok=False, error="fund wallet"), 400
    supabase.table('users').update({"balance_kobo": bal - buyer_pays}).eq('telegram_id', user.get('id')).execute()
    seller_cur = supabase.table('users').select('balance_kobo').eq('telegram_id', p['seller_id']).execute()
    seller_bal = seller_cur.data[0]['balance_kobo'] if seller_cur.data else 0
    supabase.table('users').update({"balance_kobo": seller_bal + p['seller_gets_kobo']}).eq('telegram_id', p['seller_id']).execute()
    supabase.table('wallet_transactions').insert([
        {"id": str(uuid.uuid4()), "user_id": user.get('id'), "type": "debit", "amount_kobo": buyer_pays, "reason": f"Bought {p['name']}", "reference": pid, "created_at": datetime.utcnow().isoformat()},
        {"id": str(uuid.uuid4()), "user_id": p['seller_id'], "type": "credit", "amount_kobo": p['seller_gets_kobo'], "reason": f"Sold {p['name']} (escrow 24h)", "reference": pid, "created_at": datetime.utcnow().isoformat()}
    ]).execute()
    return jsonify(ok=True, data={"fees": {"buyer_pays": buyer_pays}})

@app.route('/api/webhook/paystack', methods=['POST'])
def webhook():
    sig = request.headers.get('x-paystack-signature')
    if PAYSTACK_SECRET and sig:
        exp = hmac.new(PAYSTACK_SECRET.encode(), request.data, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(exp, sig):
            return jsonify(ok=False), 401
    return jsonify(ok=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000)))
