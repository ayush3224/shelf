-- 006_digest.sql — the weekly digest (UC31)
--
-- UC22 was dropped, so decay is silent: items shelve and drop without saying
-- so. This is the one place it becomes visible again, which makes the digest
-- load-bearing rather than a nicety.
--
-- **The digest's content is not stored.** What decayed in a week is already
-- in `transitions`, and what is about to drop is a property of `items` right
-- now — recomputing both is one query each and cannot go stale. This table is
-- delivery bookkeeping only: it records that a given week *was announced*, so
-- that a tick running every minute announces it exactly once.

create table if not exists shelf.digests (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  -- The week covered, [period_start, period_end). `period_start` is the key:
  -- one digest per user per week, enforced rather than remembered.
  period_start timestamptz not null,
  period_end   timestamptz not null,

  -- The counts as they were when the digest was built. They exist for the
  -- notification body, which has to say something before the screen is open.
  -- The screen recomputes; see the note on `empty` for why they can differ.
  shelved      int not null default 0,
  dropped      int not null default 0,
  expiring     int not null default 0,

  -- Nothing decayed and nothing is near dropping. The row is written anyway,
  -- so the week is known to have been considered, but no push goes out: a
  -- weekly "nothing happened" is how you teach someone to swipe the digest
  -- away unread.
  empty        boolean not null default false,

  created_at   timestamptz not null default now(),
  sent_at      timestamptz,
  attempts     int not null default 0,
  last_error   text,
  ticket_id    text,

  unique (user_id, period_start)
);

comment on table shelf.digests is
  'Delivery record for the weekly digest (UC31). Content lives in transitions and items and is recomputed on read; this table only says which weeks were announced.';
comment on column shelf.digests.sent_at is
  'When the push actually left. Null with empty=false means still outstanding; a digest that goes stale is abandoned rather than sent late.';
comment on column shelf.digests.expiring is
  'How many shelved items were within the warning window when the digest was built. Unlike shelved/dropped this is a forecast, not history, so the screen may show a different number later.';

-- The claim: outstanding digests, oldest first. Small table, but the tick
-- reads it every minute for the rest of the project's life.
create index if not exists digests_outstanding_idx
  on shelf.digests (period_end)
  where sent_at is null and empty = false;

-- ------------------------------------------------------------------- RLS

alter table shelf.digests enable row level security;

create policy own_digests on shelf.digests
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

grant all on shelf.digests to anon, authenticated, service_role;
