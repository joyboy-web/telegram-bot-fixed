-- payout_setup.sql - BASE payout tables. Run FIRST. Safe to run twice.
-- Creates ONLY withdrawal_requests + payout_logs. Touches NO existing table.

create table if not exists public.withdrawal_requests (
  id uuid primary key default gen_random_uuid(),
  seller_id text not null,
  amount_kobo bigint not null check (amount_kobo > 0),
  status text not null default 'PENDING' check (status in ('PENDING','SENT','REJECTED')),
  sent_at timestamptz,
  receipt_path text,
  receipt_note text,
  created_at timestamptz not null default now()
);

create table if not exists public.payout_logs (
  id uuid primary key default gen_random_uuid(),
  order_id text not null,
  payout_status text not null default 'PENDING' check (payout_status in ('PENDING','QUEUED','SENT','DELAYED','REJECTED')),
  payout_ref text,
  payout_receipt_url text,
  marked_at timestamptz,
  admin_marked_by text,
  created_at timestamptz not null default now()
);

-- One PENDING request per seller - this is your HOLD protection
create unique index if not exists withdrawal_requests_one_pending_per_seller
  on public.withdrawal_requests (seller_id) where status = 'PENDING';

create index if not exists payout_logs_order_id_idx on public.payout_logs (order_id);

notify pgrst, 'reload schema';
