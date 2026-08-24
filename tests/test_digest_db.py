"""The weekly digest, against the real database (UC31).

    pytest -m db

This is the feature standing between "silence is signal" and "things vanish".
UC22 was dropped, so nothing announces a decay as it happens; if these queries
are wrong, the system quietly throws work away and the owner's only evidence
is a feeling that the app is lossy. So the content is tested against real
rows rather than read and believed.

Same safety as the other db suites: the schema is built from the migrations,
thrown away afterwards, and belongs to a user who exists only while the tests
run. The push service is stubbed by default and cannot be reached by
forgetting a fixture.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend import digest, push, scheduler
from backend.config import capture_tz, settings
from backend.db import Database
from backend.push import PushTicket
from tests.conftest import TEST_USER as USER

pytestmark = pytest.mark.db

TOKEN = "ExponentPushToken[digest-db-test-do-not-use]"
MARKER = "[digest-db-test]"


# ------------------------------------------------------------------ setup


@pytest.fixture(autouse=True)
def never_reaches_the_push_service(monkeypatch):
    """No test here may talk to Expo, whether or not it remembered to stub it."""

    async def refuse(messages):
        raise AssertionError(
            "a db test tried to reach the real push service; "
            "use the `delivers` fixture or stub `push.send` yourself"
        )

    monkeypatch.setattr(push, "send", refuse)


@pytest.fixture
async def db(scratch_schema):
    """A real database, pointed at the suite's own schema.

    `digests` is cleared alongside `items` because it hangs off the *user*,
    not off an item, so it is the one table here that would otherwise survive
    into the next test and make a digest look already-sent.
    """
    database = Database()
    await database.connect()
    try:
        yield database
    finally:
        async with database.connection() as conn:
            await conn.execute(f"DELETE FROM {settings.db_schema}.items")
            await conn.execute(f"DELETE FROM {settings.db_schema}.push_tokens")
            await conn.execute(f"DELETE FROM {settings.db_schema}.digests")
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
    text: str,
    state: str = "shelved",
    untouched_days: float = 0.0,
) -> str:
    """Write one item straight in, already as old as the test needs it.

    The update triggers are `BEFORE UPDATE`, so timestamps set on insert
    survive — which is how a row can be eighty days into its stay on the shelf
    without the test waiting eighty days.
    """
    entered = datetime.now(timezone.utc) - timedelta(days=untouched_days)
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.items
              (user_id, raw_text, parsed_text, source, parse_status, kind,
               state, state_changed_at, updated_at, created_at)
            VALUES (%s, %s, %s, 'text', 'ok', 'task', %s, %s, %s, %s)
            RETURNING id::text
            """,
            (USER, f"{MARKER} {text}", text, state, entered, entered, entered),
        )
        row = await result.fetchone()
        assert row is not None
        return row[0]


async def log_transition(
    db: Database, item_id: str, reason: str, at: datetime, to_state: str = "shelved"
) -> None:
    """Put a decay or expiry into the audit log, at a chosen moment."""
    from_state = "active" if to_state == "shelved" else "shelved"
    async with db.connection() as conn:
        await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.transitions
              (item_id, from_state, to_state, reason, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (item_id, from_state, to_state, reason, at),
        )


async def digest_rows(db: Database) -> list[dict[str, Any]]:
    """Every digest row, oldest week first."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT period_start, period_end, shelved, dropped, expiring,
                   empty, sent_at, attempts, ticket_id, last_error
              FROM {settings.db_schema}.digests
             ORDER BY period_start
            """
        )
        columns = [c.name for c in result.description or []]
        return [dict(zip(columns, row)) for row in await result.fetchall()]


def a_week_ago_now() -> tuple[datetime, datetime, datetime]:
    """The current digest week, and an instant safely inside it."""
    now = datetime.now(timezone.utc)
    start, end = digest.period_for(now)
    return start, end, start + (end - start) / 2


# ------------------------------------------------------- what decayed (SQL)


async def test_the_week_reports_what_the_system_put_away(db: Database):
    start, end, midweek = a_week_ago_now()

    shelved = await make_item(db, text="Renew the passport")
    dropped = await make_item(db, text="That podcast idea", state="dropped")
    await log_transition(db, shelved, "decay", midweek)
    await log_transition(db, dropped, "expiry", midweek, to_state="dropped")

    rows = await db.digest_decayed(USER, start, end)

    by_reason = {row["reason"]: row for row in rows}
    assert by_reason["decay"]["text"] == "Renew the passport"
    assert by_reason["expiry"]["text"] == "That podcast idea"
    assert by_reason["decay"]["total"] == 1
    assert by_reason["expiry"]["total"] == 1


async def test_last_week_stays_in_last_week(db: Database):
    """The window is half-open, and both ends of it are load-bearing.

    A digest that leaked either way would double-report — the same shelving
    announced two weeks running is how a weekly summary stops being read.
    """
    start, end, _ = a_week_ago_now()
    item = await make_item(db, text="Book the dentist")

    await log_transition(db, item, "decay", start - timedelta(minutes=1))
    assert await db.digest_decayed(USER, start, end) == []

    await log_transition(db, item, "decay", start)
    assert len(await db.digest_decayed(USER, start, end)) == 1


async def test_the_end_of_the_week_belongs_to_the_next_one(db: Database):
    start, end, _ = a_week_ago_now()
    item = await make_item(db, text="Book the dentist")
    await log_transition(db, item, "decay", end)
    assert await db.digest_decayed(USER, start, end) == []


async def test_reactivating_something_does_not_erase_it_from_the_week(db: Database):
    """What the system did is history; where the item is now is a second fact.

    Both are reported, separately. Dropping the row once the item comes back
    would hide exactly the cases the decay constants need tuning against (O1):
    a decay the owner immediately undid is the strongest evidence there is
    that the threshold is wrong.
    """
    start, end, midweek = a_week_ago_now()
    item = await make_item(db, text="Chase the invoice")
    await log_transition(db, item, "decay", midweek)
    await db.reactivate_item(item, USER)

    (row,) = await db.digest_decayed(USER, start, end)
    assert row["reason"] == "decay"
    assert row["state_now"] == "active"


async def test_a_truncated_section_still_knows_how_many_there_were(db: Database):
    start, end, midweek = a_week_ago_now()
    for n in range(5):
        item = await make_item(db, text=f"Thing {n}")
        await log_transition(db, item, "decay", midweek + timedelta(minutes=n))

    rows = await db.digest_decayed(USER, start, end, limit=2)

    assert len(rows) == 2
    assert all(row["total"] == 5 for row in rows)
    # Newest first, so a truncated list shows the most recent decisions.
    assert [row["text"] for row in rows] == ["Thing 4", "Thing 3"]


async def test_another_user_s_week_is_not_in_this_one(db: Database):
    start, end, midweek = a_week_ago_now()
    item = await make_item(db, text="Private")
    await log_transition(db, item, "decay", midweek)

    other = "00000000-0000-4000-8000-0000000000ff"
    assert await db.digest_decayed(other, start, end) == []


# ---------------------------------------------------- what is about to drop


async def test_the_warning_covers_the_next_fortnight(db: Database):
    """Everything within the warning window, nothing outside it."""
    near = await make_item(
        db, text="Nearly gone", untouched_days=settings.drop_after_days - 3
    )
    await make_item(db, text="Plenty of time", untouched_days=10)

    rows = await db.digest_expiring(USER, warn_days=14)

    assert [row["text"] for row in rows] == ["Nearly gone"]
    assert rows[0]["id"] == near
    assert rows[0]["total"] == 1


async def test_the_warning_names_the_day_the_sweep_would_take_it(db: Database):
    """`drops_at` is derived from the same expression the expiry sweep uses.

    Stored separately it would drift, and a warning that names a date the
    sweep disagrees with is worse than no warning at all.
    """
    untouched = settings.drop_after_days - 2
    await make_item(db, text="Nearly gone", untouched_days=untouched)

    (row,) = await db.digest_expiring(USER, warn_days=14)

    expected = row["untouched_since"] + timedelta(days=settings.drop_after_days)
    assert row["drops_at"] == expected
    assert row["drops_at"] > datetime.now(timezone.utc)


async def test_the_soonest_to_go_is_first(db: Database):
    await make_item(db, text="Later", untouched_days=settings.drop_after_days - 10)
    await make_item(db, text="Sooner", untouched_days=settings.drop_after_days - 2)

    rows = await db.digest_expiring(USER, warn_days=14)

    assert [row["text"] for row in rows] == ["Sooner", "Later"]


async def test_only_the_shelf_can_expire(db: Database):
    """`done` and `dropped` are not on their way anywhere."""
    await make_item(
        db, text="Finished", state="done", untouched_days=settings.drop_after_days - 1
    )
    await make_item(
        db, text="Gone", state="dropped", untouched_days=settings.drop_after_days - 1
    )

    assert await db.digest_expiring(USER, warn_days=14) == []


async def test_editing_a_shelved_item_pushes_its_drop_date_back(db: Database):
    """Touching something is evidence you are still thinking about it (D37).

    The warning list has to agree with that or it would keep naming items the
    sweep is never going to take.
    """
    item = await make_item(
        db, text="Still thinking", untouched_days=settings.drop_after_days - 1
    )
    assert len(await db.digest_expiring(USER, warn_days=14)) == 1

    await db.update_item(item, USER, text="Still thinking, actually")

    assert await db.digest_expiring(USER, warn_days=14) == []


# ------------------------------------------------------------- the tick


@pytest.fixture
def digest_day_is_today(monkeypatch):
    """Make now a moment just after this week's digest hour.

    The tick reads the calendar, so the alternative is a suite that only
    proves anything on Sundays.
    """
    local = datetime.now(capture_tz())
    monkeypatch.setattr(settings, "digest_day", local.strftime("%A").lower())
    monkeypatch.setattr(settings, "digest_hour", local.hour)
    return local


async def test_the_tick_announces_the_week_once(
    db: Database, delivers, digest_day_is_today
):
    """Idempotence is the whole of the delivery design.

    The tick runs every minute for the twenty-four hours the digest is fresh.
    Sending it once is enforced by the unique constraint, not remembered by
    the code — a `SELECT` first would be a race with the next minute's tick.
    """
    await db.register_push_token(USER, TOKEN, "android", "digest test")
    _, _, midweek = a_week_ago_now()
    item = await make_item(db, text="Renew the passport")
    await log_transition(db, item, "decay", midweek)

    now = datetime.now(timezone.utc)
    built, sent = await scheduler._send_digests(db, now)
    assert (built, sent) == (1, 1)

    again = await scheduler._send_digests(db, now)
    assert again == (0, 0)

    assert len(delivers) == 1
    (row,) = await digest_rows(db)
    assert row["shelved"] == 1
    assert row["sent_at"] is not None
    assert row["attempts"] == 1


async def test_the_notification_is_a_summary_not_a_reminder(
    db: Database, delivers, digest_day_is_today
):
    """Its own channel, normal priority, no Done and no Snooze.

    Those buttons act on one item and a digest is about several; the channel
    is separate because a weekly summary that interrupts like a due item is
    how the channel that matters ends up muted.
    """
    await db.register_push_token(USER, TOKEN, "android", "digest test")
    _, _, midweek = a_week_ago_now()
    item = await make_item(db, text="Renew the passport")
    await log_transition(db, item, "decay", midweek)
    for days in (2, 5):
        await make_item(
            db,
            text=f"Nearly gone ({days})",
            untouched_days=settings.drop_after_days - days,
        )

    await scheduler._send_digests(db, datetime.now(timezone.utc))

    (message,) = delivers
    payload = message.payload()
    assert payload["title"] == "Your week on the shelf"
    assert payload["body"] == "1 shelved · 2 about to drop"
    assert payload["channelId"] == settings.digest_channel_id
    assert "categoryId" not in payload
    assert payload["priority"] == "normal"
    start, _ = digest.period_for(datetime.now(timezone.utc))
    assert payload["data"] == {"digest": start.date().isoformat()}


async def test_a_quiet_week_is_written_down_but_not_sent(
    db: Database, delivers, digest_day_is_today
):
    """Nothing moved, so nothing is said — but the week is closed anyway.

    Both halves matter. A weekly "nothing happened" is how you teach someone
    to swipe the digest away unread; and closing the week is what stops a
    decay at noon producing a digest six hours late, announcing a week that
    was already considered.
    """
    await db.register_push_token(USER, TOKEN, "android", "digest test")

    built, sent = await scheduler._send_digests(db, datetime.now(timezone.utc))

    assert (built, sent) == (1, 0)
    assert delivers == []
    (row,) = await digest_rows(db)
    assert row["empty"] is True
    assert row["sent_at"] is None
    assert row["attempts"] == 0


async def test_a_week_nobody_was_running_for_is_abandoned(
    db: Database, delivers, monkeypatch
):
    """A stale digest is not late news, it is wrong news.

    The opposite of the rule for item pushes (D32), which are kept queued
    indefinitely because a due item is still due whenever the reminder lands.
    A summary of a week you are halfway through is not.
    """
    await db.register_push_token(USER, TOKEN, "android", "digest test")
    monkeypatch.setattr(settings, "digest_max_age_hours", 0)

    built, sent = await scheduler._send_digests(db, datetime.now(timezone.utc))

    assert (built, sent) == (0, 0)
    assert await digest_rows(db) == []
    assert delivers == []


async def test_no_device_means_no_digest(db: Database, delivers, digest_day_is_today):
    """Not queued for later, the way an item push is — simply not built.

    A digest that could not be delivered on the day is not worth keeping, and
    keeping it would mean sending last week's summary to a phone that has just
    been set up.
    """
    _, _, midweek = a_week_ago_now()
    item = await make_item(db, text="Renew the passport")
    await log_transition(db, item, "decay", midweek)

    built, sent = await scheduler._send_digests(db, datetime.now(timezone.utc))

    assert (built, sent) == (0, 0)
    assert await digest_rows(db) == []


async def test_a_refused_send_is_retried_and_never_marked_sent(
    db: Database, monkeypatch, digest_day_is_today
):
    """Same discipline as an item push: `sent_at` means it actually left."""
    await db.register_push_token(USER, TOKEN, "android", "digest test")
    _, _, midweek = a_week_ago_now()
    item = await make_item(db, text="Renew the passport")
    await log_transition(db, item, "decay", midweek)

    async def refuses(messages):
        raise push.PushError("Expo is down")

    monkeypatch.setattr(push, "send", refuses)

    now = datetime.now(timezone.utc)
    assert await scheduler._send_digests(db, now) == (1, 0)
    (row,) = await digest_rows(db)
    assert row["sent_at"] is None
    assert row["attempts"] == 1
    assert "Expo is down" in row["last_error"]

    sent: list[push.PushMessage] = []

    async def accepts(messages):
        sent.extend(messages)
        return [PushTicket(token=m.token, ok=True, ticket_id="t") for m in messages]

    monkeypatch.setattr(push, "send", accepts)
    assert await scheduler._send_digests(db, now) == (0, 1)

    (row,) = await digest_rows(db)
    assert row["sent_at"] is not None
    assert row["attempts"] == 2
    assert row["last_error"] is None


async def test_the_full_tick_runs_the_digest(
    db: Database, delivers, digest_day_is_today
):
    """Wiring, not logic: the step is only useful if `tick` actually calls it."""
    await db.register_push_token(USER, TOKEN, "android", "digest test")
    _, _, midweek = a_week_ago_now()
    item = await make_item(db, text="Renew the passport")
    await log_transition(db, item, "decay", midweek)

    result = await scheduler.tick(db)

    assert (result.digests_built, result.digests_sent) == (1, 1)
    assert "digests=1built/1sent" in result.summary()
    assert not result.quiet
