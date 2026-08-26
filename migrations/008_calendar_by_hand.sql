-- 008_calendar_by_hand.sql — the calendar is asked for, not assumed (UC43)
--
-- 007 put every timed item on the calendar. That is the wrong default for
-- this app: most captures carry a time because a reminder needs one, not
-- because they are appointments, and syncing all of them buries the four
-- things that genuinely belong in a day under thirty that belong in a push.
--
-- **The machinery does not change. What starts it does.** The trigger still
-- decides when an item and its event have drifted apart, the tick still does
-- the writing, the outbox still drains a deletion whose item is gone (D53).
-- The single change is that nothing here invents work any more: a row in
-- `calendar_links` means *the owner asked for this item to be on the
-- calendar*, and only the route behind the "Add to calendar" button writes
-- one. No row, no event, however timed the item is.
--
-- One-way, still and always (D8). Nothing here reads Google.

comment on table shelf.calendar_links is
  'One row per item the owner has put on the calendar (UC43). The row *is* the request: it is written by POST /items/{id}/calendar and by nothing else, and its absence means no event is wanted no matter what time the item carries. Everything else here — sync_state, attempts, google_event_id — is how the tick keeps the event in step once it exists.';

-- --------------------------------------------------------------- trigger
--
-- Same body as 007 with the insert taken out. Keeping an item and its event
-- in step is still the database's job; deciding there should *be* an event is
-- now the owner's.

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

  -- Update, never insert — in both directions. If the owner has asked for
  -- this item, the edit is queued (and a take-down is queued the same way,
  -- because `calendar_wanted` reads the item's new state and the tick reads
  -- it again). If they have not, there is no row, and this changes nothing.
  update shelf.calendar_links
     set sync_state = 'pending', attempts = 0, error_detail = null
   where item_id = new.id;

  return new;
end;
$$ language plpgsql;

-- ------------------------------------------------------- the 007 backfill
--
-- 007 queued every timed item, and everything it has synced since is on the
-- calendar because the app decided so rather than because the owner did.
-- Rows that reached Google are left alone: those events exist, the owner can
-- see them, and taking them down silently is the failure mode `CLAUDE.md`
-- warns about. Rows that never reached Google are work nobody asked for, and
-- deleting them is what stops the next tick doing it anyway.

delete from shelf.calendar_links
 where google_event_id is null;
