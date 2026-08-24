"""Calendar sync against the real database (UC43).

    pytest -m db

The interesting half of this feature is not the HTTP call — that is
`test_gcal.py` — it is **when the database decides an item and its event have
drifted apart.** An item's state is written from the parse, an edit, done,
snooze, reactivate, a manual move and the tick's own two sweeps, and migration
007 puts a trigger under all of them rather than asking eight call sites to
remember. That is only worth doing if the trigger is actually right about what
counts as a change, so that is what most of this file is.

Same safety as the other db suites: the schema is built from the migrations,
dropped afterwards, and Google cannot be reached even by a test that forgets
to stub it.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from backend import gcal, scheduler
from backend.config import settings
from backend.db import Database
from tests.conftest import TEST_USER as USER

pytestmark = pytest.mark.db

CALENDAR = "owner-calendar-test@example.com"


# ------------------------------------------------------------------ setup


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """A calendar to write to, that is not the owner's."""
    monkeypatch.setattr(settings, "google_calendar_id", CALENDAR)


@pytest.fixture
async def db(scratch_schema):
    """A real database, pointed at the suite's own schema."""
    database = Database()
    await database.connect()
    try:
        yield database
    finally:
        async with database.connection() as conn:
            await conn.execute(f"DELETE FROM {settings.db_schema}.items")
            await conn.execute(f"DELETE FROM {settings.db_schema}.calendar_deletions")
        await database.disconnect()


class FakeCalendar:
    """Stands in for Google, and remembers what it was told.

    `fail` makes the next call raise; that is how the retry, the give-up and
    the recreate paths are reached without waiting for a real outage.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.created: list[str] = []
        self.patched: list[str] = []
        self.deleted: list[str] = []
        self.fail: Optional[gcal.CalendarError] = None
        self._next = 0

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def create_event(self, calendar_id: str, event: gcal.CalendarEvent) -> str:
        self._maybe_fail()
        self._next += 1
        event_id = f"evt-{self._next}"
        self.events[event_id] = {"calendar": calendar_id, "body": event.body()}
        self.created.append(event_id)
        return event_id

    async def patch_event(
        self, calendar_id: str, event_id: str, event: gcal.CalendarEvent
    ) -> None:
        self._maybe_fail()
        self.events[event_id] = {"calendar": calendar_id, "body": event.body()}
        self.patched.append(event_id)

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        self._maybe_fail()
        self.events.pop(event_id, None)
        self.deleted.append(event_id)

    def summary(self, event_id: str) -> str:
        return self.events[event_id]["body"]["summary"]

    def start(self, event_id: str) -> datetime:
        return datetime.fromisoformat(
            self.events[event_id]["body"]["start"]["dateTime"]
        )


@pytest.fixture
def calendar(monkeypatch):
    """Install the fake and let the tick authenticate."""
    fake = FakeCalendar()

    async def token() -> str:
        return "tok"

    monkeypatch.setattr(gcal, "access_token", token)
    monkeypatch.setattr(gcal, "create_event", fake.create_event)
    monkeypatch.setattr(gcal, "patch_event", fake.patch_event)
    monkeypatch.setattr(gcal, "delete_event", fake.delete_event)
    return fake


# ------------------------------------------------------------- utilities


async def link(db: Database, item_id: str) -> Optional[dict[str, Any]]:
    """The calendar link row for an item, or None."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT sync_state::text, google_event_id, calendar_id, attempts,
                   error_detail, last_synced_at
              FROM {settings.db_schema}.calendar_links
             WHERE item_id = %s
            """,
            (item_id,),
        )
        columns = [c.name for c in result.description or []]
        row = await result.fetchone()
        return dict(zip(columns, row)) if row else None


async def deletions(db: Database) -> list[dict[str, Any]]:
    """Everything sitting in the deletion outbox."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT google_event_id, calendar_id, attempts
              FROM {settings.db_schema}.calendar_deletions
            """
        )
        columns = [c.name for c in result.description or []]
        return [dict(zip(columns, row)) for row in await result.fetchall()]


async def timed_item(db: Database, text: str = "Call the insurance guy") -> str:
    """A captured item that was parsed with a due time, so it wants an event."""
    item_id = await db.create_item(user_id=USER, raw_text=text, source="voice")
    await db.apply_parse(
        item_id=item_id,
        user_id=USER,
        kind="task",
        parsed_text=text,
        due_at=datetime.now(timezone.utc) + timedelta(hours=3),
        critical=False,
        state="active",
    )
    return item_id


async def synced_item(db: Database, calendar: FakeCalendar) -> tuple[str, str]:
    """A timed item that has already been through one sync."""
    item_id = await timed_item(db)
    await scheduler._sync_calendar(db)
    row = await link(db, item_id)
    assert row is not None and row["google_event_id"]
    return item_id, row["google_event_id"]


# ------------------------------------------------- what counts as a change


async def test_an_item_with_no_time_never_reaches_the_calendar(db):
    """Most of the shelf, by definition (UC12). It should not be an event."""
    item_id = await db.create_item(
        user_id=USER, raw_text="read that paper", source="voice"
    )
    assert await link(db, item_id) is None


async def test_gaining_a_due_time_queues_an_event(db):
    """The parse supplying a time is what puts an item on the calendar."""
    item_id = await timed_item(db)
    row = await link(db, item_id)
    assert row is not None
    assert row["sync_state"] == "pending"
    assert row["google_event_id"] is None


async def test_editing_the_text_queues_an_update(db, calendar):
    """UC38: the event's summary is a copy of what the item says."""
    item_id, _ = await synced_item(db, calendar)
    assert (await link(db, item_id))["sync_state"] == "synced"

    await db.update_item(item_id=item_id, user_id=USER, text="Call the broker")
    assert (await link(db, item_id))["sync_state"] == "pending"


async def test_moving_the_due_time_queues_an_update(db, calendar):
    item_id, _ = await synced_item(db, calendar)
    await db.update_item(
        item_id=item_id,
        user_id=USER,
        due_at=datetime.now(timezone.utc) + timedelta(days=2),
        update_due=True,
    )
    assert (await link(db, item_id))["sync_state"] == "pending"


async def test_an_unrelated_write_does_not_queue_anything(db, calendar):
    """`updated_at` moves on every write; the calendar must not follow it.

    Without the trigger's field check, a person link or a push count would
    queue a write to Google — a request an hour, for nothing.
    """
    item_id, _ = await synced_item(db, calendar)
    async with db.connection() as conn:
        await conn.execute(
            f"UPDATE {settings.db_schema}.items SET push_count = push_count + 1 "
            f"WHERE id = %s",
            (item_id,),
        )
    assert (await link(db, item_id))["sync_state"] == "synced"


async def test_decay_keeps_the_event(db, calendar):
    """Decay is silent (UC22 dropped); an event vanishing would not be.

    A shelved item is one the system lost interest in, not one the owner
    finished. Taking it off the calendar is the "things are disappearing"
    feeling `CLAUDE.md` warns about, so `shelved` keeps its event.
    """
    item_id, event_id = await synced_item(db, calendar)
    await db.set_state(item_id, USER, "shelved")

    await scheduler._sync_calendar(db)

    assert event_id in calendar.events
    assert (await link(db, item_id))["sync_state"] == "synced"


async def test_completing_an_item_takes_the_event_down(db, calendar):
    """UC16, and the whole reason the sync watches state at all."""
    item_id, event_id = await synced_item(db, calendar)
    await db.mark_done(item_id, USER)

    written, removed, failed = await scheduler._sync_calendar(db)

    assert calendar.deleted == [event_id]
    assert (written, removed, failed) == (0, 1, 0)
    assert await link(db, item_id) is None


async def test_dropping_an_item_takes_the_event_down(db, calendar):
    """UC19's terminal state, reached by hand here."""
    item_id, event_id = await synced_item(db, calendar)
    await db.set_state(item_id, USER, "dropped")
    await scheduler._sync_calendar(db)
    assert calendar.deleted == [event_id]


async def test_clearing_the_due_time_takes_the_event_down(db, calendar):
    """An item with no time is not a calendar entry (UC12)."""
    item_id, event_id = await synced_item(db, calendar)
    await db.update_item(item_id=item_id, user_id=USER, due_at=None, update_due=True)

    await scheduler._sync_calendar(db)

    assert calendar.deleted == [event_id]
    assert await link(db, item_id) is None


async def test_a_reactivated_item_gets_a_fresh_event(db, calendar):
    """The link is dropped rather than kept as a tombstone, so this has to work."""
    item_id, first = await synced_item(db, calendar)
    await db.mark_done(item_id, USER)
    await scheduler._sync_calendar(db)

    await db.reactivate_item(item_id, USER)
    await scheduler._sync_calendar(db)

    row = await link(db, item_id)
    assert row is not None
    assert row["google_event_id"] not in (None, first)
    assert row["sync_state"] == "synced"


# ---------------------------------------------------------- reconciliation


async def test_first_sync_creates_and_records_the_event(db, calendar):
    item_id = await timed_item(db)

    written, removed, failed = await scheduler._sync_calendar(db)

    assert (written, removed, failed) == (1, 0, 0)
    row = await link(db, item_id)
    assert row["sync_state"] == "synced"
    assert row["calendar_id"] == CALENDAR
    assert row["attempts"] == 0
    assert row["last_synced_at"] is not None
    assert calendar.summary(row["google_event_id"]) == "Call the insurance guy"


async def test_a_second_sync_patches_rather_than_creating_a_duplicate(db, calendar):
    """The failure this guards is two events for one item, forever."""
    item_id, event_id = await synced_item(db, calendar)
    await db.update_item(item_id=item_id, user_id=USER, text="Call the broker")

    await scheduler._sync_calendar(db)

    assert calendar.created == [event_id]
    assert calendar.patched == [event_id]
    assert calendar.summary(event_id) == "Call the broker"


async def test_a_synced_item_is_not_touched_again(db, calendar):
    """Nothing dirty, nothing sent. The tick runs every minute forever."""
    await synced_item(db, calendar)
    assert await scheduler._sync_calendar(db) == (0, 0, 0)


async def test_an_idle_tick_does_not_even_authenticate(db, monkeypatch):
    """The tick is short-lived, so its token cache never sees a second tick.

    Without the check that comes first, every minute of every day would buy an
    access token in order to discover there was nothing to do with it.
    """
    reached = False

    async def token() -> str:
        nonlocal reached
        reached = True
        return "tok"

    monkeypatch.setattr(gcal, "access_token", token)

    assert await scheduler._sync_calendar(db) == (0, 0, 0)
    assert reached is False


async def test_an_event_deleted_in_google_comes_back(db, calendar):
    """The app is the source of truth (D8), so the item wins.

    Removing something from the calendar is done by completing or dropping the
    item, not by deleting the event — the alternative is a projection that
    quietly stops matching what it is a projection of.
    """
    item_id, first = await synced_item(db, calendar)
    await db.update_item(item_id=item_id, user_id=USER, text="Call the broker")
    calendar.fail = gcal.CalendarError("gone", gone=True)

    await scheduler._sync_calendar(db)

    row = await link(db, item_id)
    assert row["google_event_id"] is None
    assert row["sync_state"] == "pending"

    calendar.fail = None
    await scheduler._sync_calendar(db)
    row = await link(db, item_id)
    assert row["google_event_id"] not in (None, first)


# ------------------------------------------------------------- when it fails


async def test_a_failed_sync_is_recorded_and_retried(db, calendar):
    item_id = await timed_item(db)
    calendar.fail = gcal.CalendarError("Google returned 503")

    written, removed, failed = await scheduler._sync_calendar(db)

    assert (written, removed, failed) == (0, 0, 1)
    row = await link(db, item_id)
    assert row["sync_state"] == "error"
    assert row["attempts"] == 1
    assert "503" in row["error_detail"]

    calendar.fail = None
    assert await scheduler._sync_calendar(db) == (1, 0, 0)
    assert (await link(db, item_id))["sync_state"] == "synced"


async def test_a_row_stops_being_retried_once_its_attempts_run_out(
    db, calendar, monkeypatch
):
    """A permanently broken row must not write to Google every minute forever."""
    monkeypatch.setattr(settings, "google_calendar_max_attempts", 2)
    item_id = await timed_item(db)
    calendar.fail = gcal.CalendarError("Google returned 503")

    for _ in range(4):
        await scheduler._sync_calendar(db)

    assert (await link(db, item_id))["attempts"] == 2


async def test_editing_a_stalled_item_gives_it_another_chance(
    db, calendar, monkeypatch
):
    """Giving up is never permanent — a Google outage costs a stall, not an event."""
    monkeypatch.setattr(settings, "google_calendar_max_attempts", 1)
    item_id = await timed_item(db)
    calendar.fail = gcal.CalendarError("Google returned 503")
    await scheduler._sync_calendar(db)
    assert (await link(db, item_id))["attempts"] == 1

    calendar.fail = None
    await db.update_item(item_id=item_id, user_id=USER, text="Call the broker")

    assert (await link(db, item_id))["attempts"] == 0
    assert await scheduler._sync_calendar(db) == (1, 0, 0)


async def test_an_auth_failure_claims_nothing(db, monkeypatch):
    """One broken credential must not spend the whole backlog's attempts."""
    item_id = await timed_item(db)

    async def refuse() -> str:
        raise gcal.CalendarError("bad key", retryable=False)

    monkeypatch.setattr(gcal, "access_token", refuse)

    assert await scheduler._sync_calendar(db) == (0, 0, 0)
    assert (await link(db, item_id))["attempts"] == 0


async def test_the_sync_is_skipped_when_no_calendar_is_configured(db, monkeypatch):
    """A deployment without a calendar ticks quietly (P1, single user)."""
    monkeypatch.setattr(settings, "google_calendar_id", "")
    await timed_item(db)
    assert await scheduler._sync_calendar(db) == (0, 0, 0)


# ----------------------------------------------------- a deleted item (UC39)


async def test_deleting_an_item_queues_its_event_for_removal(db, calendar):
    """The link cascades away with the item, so the intent is written first."""
    item_id, event_id = await synced_item(db, calendar)

    await db.delete_item(item_id, USER)

    queued = await deletions(db)
    assert len(queued) == 1
    assert queued[0]["google_event_id"] == event_id
    assert queued[0]["calendar_id"] == CALENDAR


async def test_the_tick_drains_the_deletion_outbox(db, calendar):
    item_id, event_id = await synced_item(db, calendar)
    await db.delete_item(item_id, USER)

    written, removed, failed = await scheduler._sync_calendar(db)

    assert (written, removed, failed) == (0, 1, 0)
    assert calendar.deleted == [event_id]
    assert await deletions(db) == []


async def test_a_deletion_that_fails_stays_queued(db, calendar):
    """An orphaned event with nothing left that knows about it is the failure."""
    item_id, event_id = await synced_item(db, calendar)
    await db.delete_item(item_id, USER)
    calendar.fail = gcal.CalendarError("Google returned 503")

    assert await scheduler._sync_calendar(db) == (0, 0, 1)
    assert (await deletions(db))[0]["attempts"] == 1

    calendar.fail = None
    assert await scheduler._sync_calendar(db) == (0, 1, 0)
    assert await deletions(db) == []


async def test_deleting_an_item_that_was_never_synced_queues_nothing(db):
    """No event id, nothing to remove."""
    item_id = await timed_item(db)
    await db.delete_item(item_id, USER)
    assert await deletions(db) == []


# --------------------------------------------------------------- the tick


async def test_the_tick_syncs_the_calendar(db, calendar):
    """Step 8 is wired in, and its counts reach the log line."""
    await timed_item(db)

    result = await scheduler.tick(db)

    assert result.calendar_written == 1
    assert result.calendar_failed == 0
    assert "cal=1written" in result.summary()


async def test_a_tick_in_another_suite_is_a_no_op(db, monkeypatch):
    """The regression this pair of tests exists downstream of.

    Every db suite runs ticks, and the tick now talks to Google. Before
    `tests/conftest.py` grew its guard, a scheduler test put one of its
    fixture items on the owner's real calendar, where it had to be found and
    deleted by hand.

    This is what those suites see: no calendar id, so the step skips itself.
    """
    monkeypatch.setattr(settings, "google_calendar_id", "")
    await timed_item(db)

    result = await scheduler.tick(db)

    assert (result.calendar_written, result.calendar_removed) == (0, 0)


async def test_an_unstubbed_calendar_call_fails_loudly(db):
    """The second lock, and the one that actually caught this.

    A fixture that sets a calendar id — this file's own does — undoes the
    first lock without meaning to. So the client is stubbed out as well, and
    a test that would have written to Google fails instead of writing.
    """
    await timed_item(db)

    with pytest.raises(AssertionError, match="real Google Calendar"):
        await scheduler.tick(db)


async def test_an_item_dropped_by_the_tick_loses_its_event_on_the_same_tick(
    db, calendar, monkeypatch
):
    """Step 8 runs after the sweeps for exactly this reason (UC19)."""
    monkeypatch.setattr(settings, "drop_after_days", 0)
    item_id, event_id = await synced_item(db, calendar)
    await db.set_state(item_id, USER, "shelved")

    result = await scheduler.tick(db)

    assert result.dropped == 1
    assert calendar.deleted == [event_id]
