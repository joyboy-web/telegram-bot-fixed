# PayLock

Telegram Mini App escrow marketplace. Buyers fund a wallet, funds are held in escrow, the seller delivers, the buyer confirms, and funds are released to the seller.

## Features

- Telegram auth (HMAC SHA256 with `BOT_TOKEN`)
- Wallet funding via Paystack (inline or saved card)
- Escrow: Buy → Held → Confirm Delivery → Released (auto-release after 48 hours, checked every 5 minutes)
- Product listings with cover image and optional file (duplicate-file guard)
- Withdrawals to a verified bank account with a 4-digit PIN (5 failures lock for 15 minutes)
- Delete Account (soft delete, listings hidden, transaction history kept)
- Privacy and Terms pages in-app, dark mode synced to Telegram

## Repo Layout

| File | Purpose |
|---|---|
| `main.py` | Entrypoint (`gunicorn main:app`). Imports `app` from `app.py`, adds escrow routes and the auto-release worker |
| `app.py` | Flask app: auth, products, wallet, banks, account deletion |
| `index.html` | Single-file Mini App frontend (no build step) |
| `requirements.txt` | Python dependencies |
| `Procfile` | `web: gunicorn main:app` |
| `.env.example` | Environment variable template |
| `supabase_migration.sql` | Required schema changes |
| `PRIVACY.md`, `TERMS.md` | Legal text |

## Environment Variables

| Name | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram bot token, used to verify initData |
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Yes | Supabase service role key (server only) |
| `PAYSTACK_SECRET_KEY` | Yes | Paystack secret key |
| `PAYSTACK_PUBLIC_KEY` | Yes | Paystack public key |
| `PAYSTACK_BASE` | No | Override the Paystack API base URL |

## Supabase Setup

1. Make sure these tables exist: `users`, `products`, `wallet_transactions`, `saved_cards`, `withdrawals`, `escrow_orders`.
   `users` needs `pin_hash`, `pin_fail_count` and `pin_locked_until`.
2. Create storage buckets: `product-covers` and `product-files`.
3. Run `supabase_migration.sql` in the Supabase SQL editor **before deploying**. Without the `is_hidden` column the product list returns empty.

## Run Locally

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in real values
gunicorn main:app
```

Telegram initData only exists inside Telegram, so test the Mini App through your bot and not a plain browser. Use an HTTPS tunnel (for example ngrok) for local testing.

## Deploy on Render

1. Push this repo to GitHub.
2. Create a new **Web Service** from the repo.
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn main:app` (Render uses this setting, not the Procfile).
5. Add the environment variables above.
6. Optional, for 50MB uploads: `GUNICORN_CMD_ARGS=--timeout 120`
7. Paystack dashboard → set the webhook URL to `https://<your-service>.onrender.com/api/webhook/paystack`.

## Bot + Web App Flow

1. In BotFather, set the Mini App / menu button URL to your Render URL.
2. User opens the bot → Mini App loads `index.html`.
3. The app sends `Authorization: tma <initData>` and `X-Telegram-Init-Data` on every API call, read live from `Telegram.WebApp.initData`.
4. `/api/auth/validate` verifies the HMAC and creates or loads the user. A deleted account gets 403 `ACCOUNT_DELETED`.
5. Fund wallet → Buy product (funds move to held) → Seller delivers → Buyer confirms → Seller balance credited → Withdraw to bank.

## Fees

- Platform fee: ₦200 per transaction
- Fee split = round((platform fee + Paystack fee) / 2)
- Buyer pays = price + split
- Seller gets = price − split

## Delete Account

`DELETE /api/account` (3 per hour per user):

| Status | Code | Meaning |
|---|---|---|
| 200 | | Account soft-deleted, listings hidden, email/phone/account name anonymized |
| 400 | `PIN_REQUIRED` / `CONFIRM_REQUIRED` | Missing `pin` (PIN set) or `confirm: true` (no PIN) |
| 401 | `PIN_INVALID` | Wrong PIN, returns `attempts_left` |
| 403 | `ACCOUNT_DELETED` | Already deleted |
| 409 | `FUNDS_PENDING` | Balance above zero or an open escrow order (held or disputed) |
| 429 | `RATE_LIMITED` | PIN locked or too many requests, returns `retry_after` seconds |

Hidden products return 404 by id and 410 `PRODUCT_UNAVAILABLE` on buy.

## Acceptance Checklist

- [ ] Render deploys with `gunicorn main:app`, no ModuleNotFoundError
- [ ] `/api/auth/validate`: valid 200, tampered 401, deleted user 403
- [ ] List two products back to back (201, 201) with upload boxes reset
- [ ] Same file twice shows a toast and sends no POST
- [ ] Privacy and Terms open from the wallet footer, back works, dark mode syncs
- [ ] Delete flow: confirm → PIN → 200 → "Account deleted" gate → re-auth 403
- [ ] Balance eye toggle persists after reload
- [ ] Transaction filter chips work
- [ ] Fund → Buy → Escrow → Confirm → Withdraw works
- [ ] 4th `DELETE /api/account` in an hour returns 429
- [ ] No native browser dialogs in `index.html`
- [ ] `getAuthHeader` reads `tg.initData` live

## TODO

- Verify everything on Telegram iOS and Android before publishing.
- The rate limiter uses `memory://` and is per process. The default is 1 gunicorn worker. Before adding workers, use Redis via `RATELIMIT_STORAGE_URI`.
- `_credit_seller` has a read-then-write race, and the auto-release worker marks orders released before crediting. Both need atomic handling.
- Product files are served from a public URL. Move to a private bucket with signed URLs (PRIVACY §5 says private product files).
- The withdraw page "Available" shows `balance - held`, but the server allows withdrawing up to `balance_kobo`.
- A few light tint colors (badges, icon boxes) are not yet dark-mode aware.
- Server-side file-hash duplicate check.
- Dispute UI (the `/api/escrow/dispute` endpoint exists, the frontend does not expose it).

## Legal

See [PRIVACY.md](PRIVACY.md) and [TERMS.md](TERMS.md).
