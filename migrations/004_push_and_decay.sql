-- 004_push_and_decay.sql — push delivery and the decay engine
--
-- UC23 (push at due time), UC15 (done from the notification), UC17 (snooze),
-- UC18 (auto-shelve after N ignored or snoozed pushes), UC19 (auto-drop after
-- M days shelved), UC20 (reactivate).
--
-- `notifications` and `transitions` already exist from 001. What was missing
-- is somewhere to keep the device tokens, a record of what a send attempt did,
-- and the guarantee that an item coming back to `active` starts its decay
-- count from zero.

-- ------------------------------------------------------------ push tokens
-- One row per install, not per user: an Expo push token identifies a device,
-- and the same device can be signed in as someone else tomorrow. `token` is
-- therefore the unique key and `user_id` is what gets reassigned — otherwise
-- a re-registration would leave two rows and push twice.

create table if not exists shelf.push_tokens (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references auth.users(id) on delete cascade,
  token           text not null unique,
  platform        text not null default 'android'
                    check (platform in ('android', 'ios', 'web')),
  device_name     text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  last_success_at timestamptz,
  -- Set when Expo tells us the token is dead (DeviceNotRegistered). Kept
  -- rather than deleted so a device that goes quiet is visible as a fact
  -- rather than as an absence.
  disabled_at     timestamptz,
  disabled_reason text
);

create index if not exists push_tokens_user_idx
  on shelf.push_tokens (user_id) where disabled_at is null;

create trigger push_tokens_touch
  before update on shelf.push_tokens
  for each row execute function shelf.touch_updated_at();

-- ------------------------------------------------- notification send state
-- A send can fail for reasons that have nothing to do with the user: Expo
-- down, no device registered yet, a dead token. None of those may be read as
-- "ignored", so `sent_at` stays null until a push actually left, and the
-- failure is recorded beside it instead.

alter table shelf.notifications
  add column if not exists attempts   int not null default 0,
  add column if not exists last_error text,
  add column if not exists ticket_id  text;

comment on column shelf.notifications.attempts is
  'Send attempts made. A row past the cap stalls rather than being marked sent — an undelivered push must never decay an item.';
comment on column shelf.notifications.ticket_id is
  'Expo push ticket id, for tracing a delivery that the phone never showed.';
comment on column shelf.notifications.responded_at is
  'When the item stopped waiting on an answer. A non-null responded_at with a null response means the item left `active` some other way and the push was cancelled, not answered.';

-- ---------------------------------------------------- decay counter reset
-- push_count and snooze_count are the decay counter (UC18). They count how
-- many times *this* stretch of being active was declined, so coming back to
-- `active` has to clear them — otherwise a reactivated item (UC20) would
-- carry its old ignores and re-shelve on the first push, and the escape hatch
-- would not be one.
--
-- A trigger rather than four call sites: the scheduler, UC20, UC21 and the
-- edit path (D29) can all move an item into `active`, and a rule enforced in
-- one place cannot be forgotten in a fifth.

create or replace function shelf.reset_nudges_on_activate() returns trigger as $$
begin
  if new.state = 'active' and old.state is distinct from 'active' then
    new.push_count := 0;
    new.snooze_count := 0;
  end if;
  return new;
end;
$$ language plpgsql;

-- Fires before items_state_touch and items_touch (triggers run in name
-- order); all three are independent.
create trigger items_reset_nudges
  before update on shelf.items
  for each row execute function shelf.reset_nudges_on_activate();

-- ------------------------------------------------------ scheduler indexes
-- The tick scans every user, so the (user_id, ...) indexes from 001 do not
-- serve it — its leading column is the timestamp.

create index if not exists items_due_scan_idx
  on shelf.items (due_at) where state = 'active';

create index if not exists items_shelved_scan_idx
  on shelf.items (state_changed_at) where state = 'shelved';

-- The ignore sweep: sent, still unanswered, old enough to count as declined.
create index if not exists notifications_open_idx
  on shelf.notifications (sent_at)
  where sent_at is not null and responded_at is null;

-- ------------------------------------------------------------------- RLS

alter table shelf.push_tokens enable row level security;

create policy own_push_tokens on shelf.push_tokens
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

grant all on shelf.push_tokens to anon, authenticated, service_role;
