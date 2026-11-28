-- payout_setup_receipt.sql - receipt upload support. Run LAST. Safe to run twice.
alter table public.withdrawal_requests add column if not exists receipt_path text;
alter table public.withdrawal_requests add column if not exists receipt_note text;
