-- 007_calendar.sql — writing timed items to Google Calendar (UC43)
--
-- `calendar_links` has existed since 001 and this is what finally uses it.
-- Two things are added: the bookkeeping a retry loop needs, and the triggers
-- that decide *when* an item and its event have drifted apart.
--
-- **The triggers are the design.** An item's state is changed from at least
-- eight places — the parse, an edit, done, snooze, reactivate, a manual move,
-- and the tick's own decay and expiry sweeps — and a calendar that is only
-- correct when every one of those remembered to say so is a calendar that
-- goes quietly wrong. So nothing remembers. The database notices that the
-- projection changed and marks the row `pending`; the tick reconciles it.
-- Same argument as the unique constraint on `digests` (D47): make the
-- database enforce it rather than hoping the code does.
--
-- One-way, always (D8). Nothing here reads Google.

-- --------------------------------------------------------------- retries

alter table shelf.calendar_links
  add column if not exists attempts int not null default 0;

comment on column shelf.calendar_links.attempts is
  'Syncs tried since the item last changed. Reset to 0 whenever the item is touched, because a fresh edit deserves a fresh chance at a calendar that was down last week.';

-- ------------------------------------------------------------ deletions
--
-- The one case a link row cannot survive: UC39 erases the item, and
-- `calendar_links.item_id` cascades with it, taking the `google_event_id`
-- with it. Deleting the Google event inline in the request would work until
-- the one time Google is unreachable, and then the event is orphaned with
-- nothing left that knows it exists. So the delete is written down first, in
-- the same transaction that erases the item, and drained by the tick.

create table if not exists shelf.calendar_deletions (
  id              uuid primary key default gen_random_uuid(),
  -- Deliberately not a foreign key. This row is written *while* the item is
  -- being deleted, and deleting a user cascades through items to here — a
  -- reference would either block that or cascade this away, and the event
  -- would outlive the only record of it either way. It is here to be read in
  -- a log, not joined.
  user_id         uuid,
  google_event_id text not null,
  calendar_id     text not null,
  requested_at    timestamptz not null default now(),
  attempts        int not null default 0,
  last_error      text,
  unique (calendar_id, google_event_id)
);

comment on table shelf.calendar_deletions is
  'Events whose item no longer exists (UC39). An outbox: the item delete records the intent, the tick performs it. Rows are removed once Google agrees the event is gone.';

create index if not exists calendar_deletions_pending_idx
  on shelf.calendar_deletions (requested_at);

-- -------------------------------------------------------------- triggers

-- Whether an item should have an event at all. One expression, called from
-- the trigger and mirrored by the tick's claim query — if the two ever
-- disagree, items sync forever without converging.
--
-- `shelved` keeps its event on purpose. Decay is silent (UC22 was dropped),
-- and an event that vanishes from the calendar the moment the system loses
-- interest is exactly the "things are disappearing" feeling `CLAUDE.md`
-- warns about. Only the terminal states — `done`, `dropped` — clear it.
create or replace function shelf.calendar_wanted(
  p_state shelf.item_state,
  p_due_at timestamptz
) returns boolean as $$
  select p_due_at is not null and p_state in ('active', 'shelved');
$$ language sql immutable;

-- The text the event's summary is a copy of. Same expression the rest of the
-- app uses to display an item, so an edit that changes what is on screen is
-- an edit that changes what is on the calendar.
create or replace function shelf.calendar_summary(
  p_parsed_text text,
  p_raw_text text
) returns text as $$
  select coalesce(nullif(p_parsed_text, ''), p_raw_text);
$$ language sql immutable;

create or replace function shelf.calendar_mark_dirty() returns trigger as $$
begin
  -- An update that changed nothing the event is a projection of is not a
  -- reason to talk to Google. `updated_at` moves on every write in this
  -- schema, so without this check a person link or a push count would queue
  -- a calendar sync.
  if tg_op = 'UPDATE'
     and new.due_at is not distinct from old.due_at
     and new.state  is not distinct from old.state
     and shelf.calendar_summary(new.parsed_text, new.raw_text)
         is not distinct from shelf.calendar_summary(old.parsed_text, old.raw_text)
  then
    return new;
  end if;

  if shelf.calendar_wanted(new.state, new.due_at) then
    insert into shelf.calendar_links (item_id, sync_state)
         values (new.id, 'pending')
    on conflict (item_id) do update
       set sync_state = 'pending', attempts = 0, error_detail = null;
  else
    -- No event wanted. A row only exists here if one was wanted before, and
    -- marking it pending is how the tick is told to take the event down.
    -- Nothing is inserted: an item that never had a time never had an event.
    update shelf.calendar_links
       set sync_state = 'pending', attempts = 0, error_detail = null
     where item_id = new.id;
  end if;

  return new;
end;
$$ language plpgsql;

create or replace function shelf.calendar_mark_deleted() returns trigger as $$
begin
  insert into shelf.calendar_deletions (user_id, google_event_id, calendar_id)
  select old.user_id, l.google_event_id, coalesce(l.calendar_id, '')
    from shelf.calendar_links l
   where l.item_id = old.id
     and l.google_event_id is not null
     and coalesce(l.calendar_id, '') <> ''
  on conflict (calendar_id, google_event_id) do nothing;
  return old;
end;
$$ language plpgsql;

drop trigger if exists items_calendar_dirty on shelf.items;
create trigger items_calendar_dirty
  after insert or update on shelf.items
  for each row execute function shelf.calendar_mark_dirty();

-- Before, not after: the link row is read here, and it cascades away with the
-- item the moment the delete lands.
drop trigger if exists items_calendar_deleted on shelf.items;
create trigger items_calendar_deleted
  before delete on shelf.items
  for each row execute function shelf.calendar_mark_deleted();

-- --------------------------------------------------------------- backfill
--
-- Items captured before this migration have never been offered to the
-- calendar. The triggers only fire on writes, so without this the calendar
-- would hold everything from today onwards and silently nothing from before.

insert into shelf.calendar_links (item_id, sync_state)
select id, 'pending'
  from shelf.items
 where due_at is not null
   and state in ('active', 'shelved')
on conflict (item_id) do nothing;

-- ------------------------------------------------------------------ RLS

alter table shelf.calendar_deletions enable row level security;

-- No user policy. Nothing user-facing reads this table; it is drained by the
-- tick, which connects as the owner of the schema rather than through RLS.
grant all on shelf.calendar_deletions to anon, authenticated, service_role;
