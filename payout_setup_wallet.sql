-- payout_setup_wallet.sql - wallet HOLD setup. Run SECOND, AFTER payout_setup.sql. Safe to run twice.
-- Hold rule: PENDING withdrawal_requests row = hold. SENT / REJECTED = hold released.

alter table public.users add column if not exists balance_kobo bigint not null default 0;

alter table public.wallet_transactions add column if not exists user_id bigint;
alter table public.wallet_transactions add column if not exists type text;
alter table public.wallet_transactions add column if not exists amount_kobo bigint;
alter table public.wallet_transactions add column if not exists reason text;
alter table public.wallet_transactions add column if not exists reference text;

-- One ledger row per payout - prevents double debit
create unique index if not exists wallet_transactions_reference_unique
  on public.wallet_transactions (reference) where reference is not null;

-- Fast hold calculation: sum PENDING per seller
create index if not exists withdrawal_requests_seller_status_idx
  on public.withdrawal_requests (seller_id, status);

notify pgrst, 'reload schema';
