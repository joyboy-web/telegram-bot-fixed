-- payout_setup_atomic.sql - wallet HOLD + atomic payout. Run AFTER payout_setup.sql and payout_setup_wallet.sql. Safe to run twice.
-- Touches only OUR tables (withdrawal_requests, payout_logs) and creates two functions.
-- The functions read/write users.balance_kobo and wallet_transactions exactly the way the app does (one debit + one ledger row).
-- FIXED: p_receipt now also saves to withdrawal_requests.receipt_path, so download will work.

-- REJECTED is a new final state (hold released, nothing debited).
alter table public.withdrawal_requests drop constraint if exists withdrawal_requests_status_check;
alter table public.withdrawal_requests add constraint withdrawal_requests_status_check
  check (status in ('PENDING','SENT','REJECTED'));
alter table public.payout_logs drop constraint if exists payout_logs_payout_status_check;
alter table public.payout_logs add constraint payout_logs_payout_status_check
  check (payout_status in ('PENDING','QUEUED','SENT','DELAYED','REJECTED'));

-- The hold is simply: every withdrawal_requests row with status = 'PENDING' holds its amount_kobo.
-- SENT (debit done) and REJECTED release it automatically; the unique index already allows one PENDING row per seller.

create or replace function public.process_manual_payout(
  p_request_id uuid, p_admin_id text, p_ref text, p_receipt text default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  wr      public.withdrawal_requests%rowtype;
  v_bal   bigint;
  v_ref   text;
begin
  -- 1) CLAIM: lock the request row. A second caller waits here, then sees SENT and stops.
  select * into wr from public.withdrawal_requests where id = p_request_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'code', 'NO_REQUEST', 'error', 'Withdrawal request not found');
  end if;
  if wr.status = 'SENT' then
    return jsonb_build_object('ok', false, 'code', 'ALREADY_SENT', 'error', 'Already paid out');
  end if;
  if wr.status <> 'PENDING' then
    return jsonb_build_object('ok', false, 'code', 'NOT_PENDING', 'error', 'This request is ' || lower(wr.status));
  end if;

  v_ref := 'wdr-' || substr(replace(wr.id::text, '-', ''), 1, 24);

  -- 2) Never debit twice: if this request's ledger row already exists, the money was taken earlier. Only finish.
  if not exists (select 1 from public.wallet_transactions where reference = v_ref) then
    -- 3) BALANCE CHECK + DEBIT (row lock on the user; the app's own compare-and-set updates wait for it)
    select balance_kobo into v_bal from public.users where telegram_id = wr.seller_id::bigint for update;
    if not found or coalesce(v_bal, 0) < wr.amount_kobo then
      return jsonb_build_object('ok', false, 'code', 'INSUFFICIENT_BALANCE', 'error', 'Insufficient wallet balance');
    end if;
    update public.users set balance_kobo = balance_kobo - wr.amount_kobo where telegram_id = wr.seller_id::bigint;
    -- 4) LEDGER
    insert into public.wallet_transactions (user_id, type, amount_kobo, reason, reference)
    values (wr.seller_id::bigint, 'debit', wr.amount_kobo, 'manual_withdrawal', v_ref);
  end if;

  -- 5) MARK SENT (this also releases the hold, because the request is no longer PENDING)
  -- FIXED: Also save receipt to withdrawal_requests.receipt_path
  update public.payout_logs
     set payout_status = 'SENT', payout_ref = p_ref, payout_receipt_url = nullif(p_receipt, ''),
         marked_at = now(), admin_marked_by = p_admin_id
   where order_id = p_request_id::text;
  update public.withdrawal_requests set status = 'SENT', sent_at = now(), receipt_path = coalesce(nullif(p_receipt, ''), receipt_path) where id = p_request_id;

  return jsonb_build_object('ok', true, 'request_id', p_request_id, 'seller_id', wr.seller_id,
                            'amount_kobo', wr.amount_kobo, 'ledger_ref', v_ref);
  -- Any error above (including a failed insert) aborts the whole function call: every step rolls back together.
end;
$$;

create or replace function public.reject_manual_payout(p_request_id uuid, p_admin_id text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare wr public.withdrawal_requests%rowtype;
begin
  select * into wr from public.withdrawal_requests where id = p_request_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'code', 'NO_REQUEST', 'error', 'Withdrawal request not found');
  end if;
  if wr.status <> 'PENDING' then
    return jsonb_build_object('ok', false, 'code', 'NOT_PENDING', 'error', 'This request is already ' || lower(wr.status));
  end if;
  update public.withdrawal_requests set status = 'REJECTED' where id = p_request_id;
  update public.payout_logs set payout_status = 'REJECTED', admin_marked_by = p_admin_id, marked_at = now()
   where order_id = p_request_id::text;
  return jsonb_build_object('ok', true, 'request_id', p_request_id);
end;
$$;

-- Only the server (service role) may call these. Without this, anyone with the public anon key could.
revoke all on function public.process_manual_payout(uuid, text, text, text) from public, anon, authenticated;
revoke all on function public.reject_manual_payout(uuid, text) from public, anon, authenticated;
grant execute on function public.process_manual_payout(uuid, text, text, text) to service_role;
grant execute on function public.reject_manual_payout(uuid, text) to service_role;

notify pgrst, 'reload schema';
