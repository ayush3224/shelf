"""The decay engine, against the real database and the real clock (UC18, UC19, UC23).

Opt-in, because it writes to Postgres and one test waits for a due time to
actually arrive:

    pytest -m db

Every other suite stubs the database. This one does not, and that is the
point: the tick is almost entirely SQL, and SQL that has only ever been read
is SQL that has not been tested. The clock is real everywhere it can be —
where a real wait would be absurd (an hour between pushes, ninety days on the
shelf) the timestamps are backdated on real rows instead, which is the same
proof without the wait.

The push service is stubbed. Delivery to a device is a separate check and
cannot be made repeatable; what these tests are about is what the database
does with the answer.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from backend import push, scheduler
from backend.config import settings
from backend.db import Database
from backend.push import PushTicket

pytestmark = pytest.mark.db

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"
TOKEN = "ExponentPushToken[db-test-token-do-not-use]"

# Every row this module writes carries it, so cleanup can find them and a
# human looking at the table knows what they are.
MARKER = "[scheduler-db-test]"


# ------------------------------------------------------------------ setup


@pytest.fixture
async def db():
    """A real database, with everything this module wrote removed afterwards."""
    database = Database()
    await database.connect()
    try:
        yield database
    finally:
        async with database.connection() as conn:
            await conn.execute(
                f"DELETE FROM {settings.db_schema}.items WHERE raw_text LIKE %s",
                (f"{MARKER}%",),
            )
            await conn.execute(
                f"DELETE FROM {settings.db_schema}.push_tokens WHERE token = %s",
                (TOKEN,),
            )
        await database.disconnect()


@pytest.fixture
def delivers(monkeypatch):
    """Make the push service accept everything, and record what it was given."""
    sent: list[push.PushMessage] = []

    async def fake_send(messages: list[push.PushMessage]) -> list[PushTicket]:
        sent.extend(messages)
        return [
            PushTicket(token=m.token, ok=True, ticket_id=f"ticket-{i}")
            for i, m in enumerate(messages)
        ]

    monkeypatch.setattr(push, "send", fake_send)
    return sent


# --------------------------------------------------------------- helpers


async def make_item(
    db: Database,
    *,
    state: str = "active",
    due_at: Optional[datetime] = None,
    text: str = "Call the insurance guy",
    push_count: int = 0,
    snooze_count: int = 0,
    age_days: float = 0.0,
) -> str:
    """Write one item straight into the table.

    Inserted rather than captured: this module is testing the scheduler, not
    the parse, and an insert also lets the row arrive already old. The update
    triggers are `BEFORE UPDATE`, so timestamps set here are not overwritten —
    which is how an item can be ninety days into its stay on the shelf without
    the test waiting ninety days for it.

    Args:
        db: Database.
        state: Starting state.
        due_at: When it falls due, or None.
        text: What it says.
        push_count: Ignores already recorded against it.
        snooze_count: Snoozes already recorded against it.
        age_days: How long ago it entered its current state.

    Returns:
        The new item's id.
    """
    entered = datetime.now(timezone.utc) - timedelta(days=age_days)
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.items
              (user_id, raw_text, parsed_text, source, parse_status, kind,
               state, due_at, push_count, snooze_count,
               state_changed_at, updated_at, created_at)
            VALUES (%s, %s, %s, 'text', 'ok', 'task',
                    %s, %s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (
                USER,
                f"{MARKER} {text}",
                text,
                state,
                due_at,
                push_count,
                snooze_count,
                entered,
                entered,
                entered,
            ),
        )
        row = await result.fetchone()
        assert row is not None
        return row[0]


async def register_device(db: Database) -> None:
    """Give the user somewhere for a push to go."""
    await db.register_push_token(USER, TOKEN, "android", "db test")


async def item_row(db: Database, item_id: str) -> dict[str, Any]:
    """The item as the database now has it."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT state::text, due_at, push_count, snooze_count
              FROM {settings.db_schema}.items WHERE id = %s
            """,
            (item_id,),
        )
        columns = [c.name for c in result.description or []]
        row = await result.fetchone()
        assert row is not None, "the item vanished"
        return dict(zip(columns, row))


async def notifications(db: Database, item_id: str) -> list[dict[str, Any]]:
    """Every notification row for an item, oldest first."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT id::text, sent_at, responded_at, response::text, attempts,
                   last_error
              FROM {settings.db_schema}.notifications
             WHERE item_id = %s
             ORDER BY scheduled_for
            """,
            (item_id,),
        )
        columns = [c.name for c in result.description or []]
        return [dict(zip(columns, row)) for row in await result.fetchall()]


async def transitions(db: Database, item_id: str) -> list[tuple[str, str, str]]:
    """Every state change recorded for an item, oldest first."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT from_state::text, to_state::text, reason::text
              FROM {settings.db_schema}.transitions
             WHERE item_id = %s
             ORDER BY created_at
            """,
            (item_id,),
        )
        return [tuple(row) for row in await result.fetchall()]


async def age_the_push(db: Database, item_id: str) -> None:
    """Backdate the outstanding push so the next one is due.

    This is the one thing a test cannot wait for honestly: the repeat interval
    is an hour by default. The row is real and so is the update — only the
    clock is being moved.
    """
    async with db.connection() as conn:
        await conn.execute(
            f"""
            UPDATE {settings.db_schema}.notifications
               SET sent_at = sent_at - make_interval(mins => %s)
             WHERE item_id = %s AND responded_at IS NULL AND sent_at IS NOT NULL
            """,
            (settings.push_repeat_minutes + 1, item_id),
        )


# ------------------------------------------------------- push at due time


async def test_a_due_item_is_pushed_once_and_only_once(db, delivers):
    """UC23, against the clock rather than a stubbed one.

    The item is created not yet due, and the wait is a real wait.
    """
    due = datetime.now(timezone.utc) + timedelta(seconds=20)
    item_id = await make_item(db, due_at=due)
    await register_device(db)

    before = await scheduler.tick(db)
    assert await notifications(db, item_id) == [], "pushed before it was due"
    assert before.queued == 0

    await asyncio.sleep(22)

    first = await scheduler.tick(db)
    assert first.queued >= 1 and first.sent >= 1
    rows = await notifications(db, item_id)
    assert len(rows) == 1
    assert rows[0]["sent_at"] is not None
    assert rows[0]["responded_at"] is None
    assert len(delivers) == 1
    assert delivers[0].token == TOKEN
    assert "insurance" in delivers[0].title

    # Still due, still unanswered — but the hour has not passed, so nothing new.
    second = await scheduler.tick(db)
    assert second.queued == 0 and second.sent == 0
    assert len(await notifications(db, item_id)) == 1
    assert (await item_row(db, item_id))["push_count"] == 0


async def test_an_item_with_no_device_keeps_its_attempts(db):
    """A push with nowhere to go must not burn the item's chances.

    No device registered is our problem, not the user's, and letting it
    exhaust the attempt budget would stall the item permanently for a reason
    that has nothing to do with them.
    """
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )

    for _ in range(3):
        await scheduler.tick(db)

    rows = await notifications(db, item_id)
    assert len(rows) == 1, "one queued push, not one per tick"
    assert rows[0]["sent_at"] is None
    assert rows[0]["attempts"] == 0


# ----------------------------------------------------- ignores and decay


async def test_an_unanswered_push_is_recorded_as_ignored(db, delivers):
    """The write that makes silence into data (UC18)."""
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await register_device(db)

    await scheduler.tick(db)
    await age_the_push(db, item_id)

    result = await scheduler.tick(db)

    assert result.ignored == 1
    rows = await notifications(db, item_id)
    assert rows[0]["response"] == "ignored"
    assert rows[0]["responded_at"] is not None
    assert (await item_row(db, item_id))["push_count"] == 1
    # And the next push went out on the same tick, because the item is due
    # and has nothing outstanding again.
    assert len(rows) == 2
    assert rows[1]["sent_at"] is not None


async def test_three_ignored_pushes_shelve_the_item_silently(db, delivers):
    """The whole decay path, end to end (UC18).

    And the part that is easy to get wrong now that UC22 is gone: **nothing is
    sent about it.** The `transitions` row is the entire record.
    """
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await register_device(db)

    await scheduler.tick(db)  # first push
    for _ in range(settings.shelve_after_ignores):
        await age_the_push(db, item_id)
        await scheduler.tick(db)

    row = await item_row(db, item_id)
    assert row["state"] == "shelved"
    assert row["push_count"] == settings.shelve_after_ignores

    assert await transitions(db, item_id) == [("active", "shelved", "decay")]

    # Silence, checked rather than assumed: exactly as many pushes as there
    # were ignores, none of them about the shelving.
    rows = await notifications(db, item_id)
    assert len(rows) == settings.shelve_after_ignores
    assert [r["response"] for r in rows] == ["ignored"] * settings.shelve_after_ignores
    assert len(delivers) == settings.shelve_after_ignores

    # And it stays quiet: a shelved item is not due, so nothing more goes out.
    after = await scheduler.tick(db)
    assert after.queued == 0 and after.sent == 0


async def test_a_snooze_counts_toward_the_same_threshold(db, delivers):
    """Ignoring and snoozing are both "not now" (UC17, UC18)."""
    item_id = await make_item(
        db,
        due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        push_count=settings.shelve_after_ignores - 1,
    )
    await register_device(db)

    await scheduler.tick(db)
    snoozed = await db.snooze_item(item_id, USER, 1)
    assert snoozed is not None and snoozed["changed"] is True

    rows = await notifications(db, item_id)
    assert rows[0]["response"] == "snooze", "the snooze answered the push"

    row = await item_row(db, item_id)
    assert row["snooze_count"] == 1
    assert row["state"] == "active", "snoozing does not shelve on its own"
    assert row["due_at"] > datetime.now(timezone.utc)

    # The snooze was the last decline the threshold had room for.
    result = await scheduler.tick(db)
    assert result.shelved == 1
    assert (await item_row(db, item_id))["state"] == "shelved"
    assert await transitions(db, item_id) == [("active", "shelved", "decay")]


async def test_a_snooze_moves_the_due_time_and_stops_the_pushes(db, delivers):
    """A snoozed item is not due, so nothing goes out until it is (UC17)."""
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await register_device(db)

    await scheduler.tick(db)
    await db.snooze_item(item_id, USER, 60)

    result = await scheduler.tick(db)

    assert result.queued == 0 and result.sent == 0
    assert len(await notifications(db, item_id)) == 1


async def test_finishing_an_item_answers_its_push(db, delivers):
    """UC15 and UC16 land in the same place: the push has been answered.

    Without this write the scheduler would read the same silence as an ignore
    an hour later, against an item that is already done.
    """
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await register_device(db)

    await scheduler.tick(db)
    assert await db.mark_done(item_id, USER) == "active"

    rows = await notifications(db, item_id)
    assert rows[0]["response"] == "done"
    assert rows[0]["responded_at"] is not None

    await age_the_push(db, item_id)
    result = await scheduler.tick(db)

    assert result.ignored == 0
    assert (await item_row(db, item_id))["push_count"] == 0
    assert (await item_row(db, item_id))["state"] == "done"


async def test_moving_to_a_non_active_state_is_still_a_manual_move(db):
    """Only the edge *into* `active` is special; the rest stay `manual` (UC21)."""
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )

    assert await db.set_state(item_id, USER, "dropped") == "active"

    assert await transitions(db, item_id) == [("active", "dropped", "manual")]


async def test_an_item_moved_by_hand_has_its_push_cancelled(db, delivers):
    """Moving an item is an answer too — just not one of the three (UC21).

    The row is closed with no response, which is this table's way of saying
    the item stopped waiting rather than that the user ignored it. Counting it
    as an ignore would decay something the user had just touched.
    """
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await register_device(db)

    await scheduler.tick(db)
    await db.set_state(item_id, USER, "shelved")

    result = await scheduler.tick(db)

    assert result.cancelled == 1
    rows = await notifications(db, item_id)
    assert rows[0]["responded_at"] is not None
    assert rows[0]["response"] is None
    assert (await item_row(db, item_id))["push_count"] == 0


# ------------------------------------------------------ delivery failures


async def test_a_push_that_never_left_cannot_decay_anything(db, monkeypatch):
    """The property the whole model rests on (UC18).

    If the push service is down, the user was never told. Reading that as
    "ignored" would shelve items for the server's failure.
    """

    async def refuse(messages):
        raise push.PushError("Expo is down")

    monkeypatch.setattr(push, "send", refuse)

    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await register_device(db)

    result = await scheduler.tick(db)

    assert result.sent == 0 and result.failed == 1
    rows = await notifications(db, item_id)
    assert rows[0]["sent_at"] is None, "never sent, so never ignorable"
    assert rows[0]["attempts"] == 1
    assert "Expo is down" in (rows[0]["last_error"] or "")

    # No amount of time makes an undelivered push into an ignore.
    for _ in range(settings.shelve_after_ignores + 1):
        await scheduler.tick(db)

    row = await item_row(db, item_id)
    assert row["push_count"] == 0
    assert row["state"] == "active"


async def test_a_dead_token_is_disabled_and_the_item_is_not_punished(db, monkeypatch):
    """`DeviceNotRegistered` means the app is gone from that phone (UC23)."""

    async def device_gone(messages):
        return [
            PushTicket(
                token=m.token,
                ok=False,
                error=push.DEVICE_NOT_REGISTERED,
                message="not a registered device",
            )
            for m in messages
        ]

    monkeypatch.setattr(push, "send", device_gone)

    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await register_device(db)

    result = await scheduler.tick(db)

    assert result.sent == 0 and result.failed == 1
    assert await db.push_token_count(USER) == 0, "the dead token was disabled"
    rows = await notifications(db, item_id)
    assert rows[0]["sent_at"] is None
    assert (await item_row(db, item_id))["push_count"] == 0


# ----------------------------------------------------------- expiry (UC19)


async def test_a_long_shelved_item_is_dropped_silently(db, delivers):
    """Ninety days without a word is an answer too."""
    item_id = await make_item(
        db, state="shelved", due_at=None, age_days=settings.drop_after_days + 1
    )

    result = await scheduler.tick(db)

    assert result.dropped >= 1
    assert (await item_row(db, item_id))["state"] == "dropped"
    assert await transitions(db, item_id) == [("shelved", "dropped", "expiry")]
    assert await notifications(db, item_id) == [], "nothing is sent about a drop"


async def test_a_shelved_item_inside_the_window_is_left_alone(db, delivers):
    """The boundary, from the safe side."""
    item_id = await make_item(
        db, state="shelved", due_at=None, age_days=settings.drop_after_days - 1
    )

    await scheduler.tick(db)

    assert (await item_row(db, item_id))["state"] == "shelved"
    assert await transitions(db, item_id) == []


async def test_touching_a_shelved_item_restarts_its_clock(db, delivers):
    """ "Untouched" means untouched (UC19).

    An item you edited last week is one you were demonstrably still thinking
    about, whatever the shelving date says.
    """
    item_id = await make_item(
        db,
        state="shelved",
        due_at=None,
        age_days=settings.drop_after_days + 1,
        text="Get the pollution certificate",
    )
    await db.update_item(item_id, USER, text="Get the pollution certificate today")

    await scheduler.tick(db)

    assert (await item_row(db, item_id))["state"] == "shelved"


# ------------------------------------------------------ reactivate (UC20)


async def test_reactivating_clears_the_decay_count(db, delivers):
    """Otherwise the escape hatch would not be one.

    An item brought back carrying the three ignores that shelved it would
    shelve again on its first push, and the user would have no way to keep it.
    """
    item_id = await make_item(
        db,
        state="shelved",
        due_at=None,
        push_count=settings.shelve_after_ignores,
        snooze_count=1,
    )

    result = await db.reactivate_item(item_id, USER)

    assert result is not None
    assert result["previous"] == "shelved"
    assert result["changed"] is True

    row = await item_row(db, item_id)
    assert row["state"] == "active"
    assert row["push_count"] == 0 and row["snooze_count"] == 0
    assert row["due_at"] is not None, "an active item with no due time is invisible"
    assert await transitions(db, item_id) == [("shelved", "active", "reactivation")]

    # And it survives the next tick rather than decaying straight back.
    await register_device(db)
    await scheduler.tick(db)
    assert (await item_row(db, item_id))["state"] == "active"


async def test_reactivating_keeps_a_future_due_time(db):
    """A time still ahead is the user's, not ours to overwrite."""
    later = datetime.now(timezone.utc) + timedelta(days=3)
    item_id = await make_item(db, state="shelved", due_at=later)

    result = await db.reactivate_item(item_id, USER)

    assert result is not None
    assert abs((result["due_at"] - later).total_seconds()) < 1


async def test_reactivating_can_be_given_a_time(db):
    """ "Bring it back on Thursday" (UC20)."""
    thursday = datetime.now(timezone.utc) + timedelta(days=4)
    item_id = await make_item(db, state="shelved", due_at=None)

    result = await db.reactivate_item(item_id, USER, thursday)

    assert result is not None
    assert abs((result["due_at"] - thursday).total_seconds()) < 1


async def test_reactivating_something_already_active_changes_nothing(db):
    """Idempotent, so a double tap writes no second transition."""
    item_id = await make_item(
        db, due_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )

    result = await db.reactivate_item(item_id, USER)

    assert result is not None and result["changed"] is False
    assert await transitions(db, item_id) == []


async def test_a_dropped_item_can_be_brought_back(db):
    """Terminal is not permanent; `dropped` is a decayed state too."""
    item_id = await make_item(db, state="dropped", due_at=None)

    result = await db.reactivate_item(item_id, USER)

    assert result is not None and result["changed"] is True
    assert await transitions(db, item_id) == [("dropped", "active", "reactivation")]


async def test_undoing_a_done_item_is_a_manual_move_not_a_reactivation(db):
    """The reason column is what O1 gets tuned from, so it has to mean something.

    Fetching something back off the shelf is evidence the threshold is wrong.
    Un-finishing something you finished is not.
    """
    item_id = await make_item(db, state="done", due_at=None)

    await db.reactivate_item(item_id, USER)

    assert await transitions(db, item_id) == [("done", "active", "manual")]


async def test_the_chip_route_into_active_is_the_same_move(db):
    """UC21's manual move and UC20 are one event when they cross the same edge.

    The chip goes through the same implementation, so it cannot skip either of
    the two things reactivation has to do: log the edge as `reactivation`, and
    give the item a due time. An `active` item with no time is one nothing in
    the app can show (D35).
    """
    item_id = await make_item(db, state="shelved", due_at=None, push_count=3)

    assert await db.set_state(item_id, USER, "active") == "shelved"

    assert await transitions(db, item_id) == [("shelved", "active", "reactivation")]
    row = await item_row(db, item_id)
    assert row["due_at"] is not None
    assert row["push_count"] == 0


async def test_snoozing_a_shelved_item_reports_rather_than_moves_it(db):
    """A notification acted on after the item decayed (UC17)."""
    item_id = await make_item(db, state="shelved", due_at=None)

    result = await db.snooze_item(item_id, USER, 30)

    assert result is not None
    assert result["changed"] is False
    assert result["state"] == "shelved"
    assert (await item_row(db, item_id))["state"] == "shelved"
