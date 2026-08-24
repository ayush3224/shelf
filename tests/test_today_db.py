"""The `Today` split, against a real Postgres (UC32, D56).

Opt-in:

    pytest -m db

`test_items.py` proves the route asks the two queries with the same boundary
instant. It cannot prove the SQL those queries become agrees with it — and the
bug being fixed here was precisely a disagreement about a boundary: `Today`
stopped at the end of the day, the Shelf held everything not `active`, and an
active item due tomorrow was in neither. That gap survived eight days of
working software because nobody had captured a future date yet.

So this module asserts the property that makes the gap impossible rather than
the two queries separately: **every active timed item is on exactly one of the
two lists.** A test of `today_items` alone would have passed throughout the
period the bug existed.

Against the suite's own schema, never `shelf`. See `conftest.py`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.config import settings
from backend.db import Database
from tests.conftest import TEST_USER as USER

pytestmark = pytest.mark.db


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
        await database.disconnect()


async def make_item(
    db: Database,
    *,
    text: str,
    due_at: datetime | None,
    state: str = "active",
) -> str:
    """Write one item straight in, at whatever time the test needs."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.items
              (user_id, raw_text, parsed_text, state, due_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (USER, text, text, state, due_at),
        )
        row = await result.fetchone()
        assert row is not None
        return str(row[0])


async def both(db: Database, cut: datetime) -> tuple[list[str], list[str], bool]:
    """Both blocks as the route asks for them, as id lists."""
    due = await db.today_items(user_id=USER, before=cut)
    later, truncated = await db.upcoming_items(user_id=USER, at_or_after=cut)
    return [r["id"] for r in due], [r["id"] for r in later], truncated


async def test_an_item_due_tomorrow_is_on_exactly_one_list(db: Database):
    """The original bug, stated as the thing that must never be true again."""
    now = datetime.now(timezone.utc)
    cut = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    item = await make_item(
        db, text="Clip the dog's nails", due_at=now + timedelta(days=1)
    )

    due, later, _ = await both(db, cut)
    assert (item in due) != (item in later), "on both lists or on neither"
    assert item in later


async def test_nothing_active_and_timed_falls_between_the_two(db: Database):
    """Sweep the boundary: an hour either side of it, and on it exactly."""
    now = datetime.now(timezone.utc)
    cut = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    ids = {
        "overdue": await make_item(db, text="a", due_at=now - timedelta(days=3)),
        "earlier today": await make_item(db, text="b", due_at=now - timedelta(hours=1)),
        "later today": await make_item(db, text="c", due_at=cut - timedelta(hours=1)),
        "on the boundary": await make_item(db, text="d", due_at=cut),
        "tomorrow": await make_item(db, text="e", due_at=cut + timedelta(hours=9)),
        "next year": await make_item(db, text="f", due_at=cut + timedelta(days=400)),
    }

    due, later, _ = await both(db, cut)
    for when, item in ids.items():
        assert (item in due) != (item in later), f"{when} is on both lists or neither"

    # The boundary instant belongs to `later`: `today_items` is exclusive at
    # the top and `upcoming_items` inclusive at the bottom, which is the one
    # arrangement that neither duplicates nor drops midnight itself.
    assert ids["on the boundary"] in later


async def test_later_holds_only_active_items(db: Database):
    """A shelved item keeps its due date, and still belongs to the Shelf.

    Decay is silent (UC22 dropped). Something the system put away turning up
    under `Later` would be the system quietly handing work back.
    """
    now = datetime.now(timezone.utc)
    cut = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    shelved = await make_item(
        db, text="g", due_at=cut + timedelta(days=2), state="shelved"
    )
    done = await make_item(db, text="h", due_at=cut + timedelta(days=2), state="done")

    due, later, _ = await both(db, cut)
    assert shelved not in later and shelved not in due
    assert done not in later and done not in due


async def test_later_is_soonest_first(db: Database):
    """Ordered by when, not by when it was said — this block is an agenda."""
    now = datetime.now(timezone.utc)
    cut = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    far = await make_item(db, text="far", due_at=cut + timedelta(days=30))
    near = await make_item(db, text="near", due_at=cut + timedelta(days=2))

    _, later, _ = await both(db, cut)
    assert later == [near, far]


async def test_a_cut_off_later_list_reports_that_it_was_cut_off(db: Database):
    """The limit is allowed to bite; going quiet about it is not."""
    now = datetime.now(timezone.utc)
    cut = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for n in range(4):
        await make_item(db, text=f"item {n}", due_at=cut + timedelta(days=n + 1))

    rows, truncated = await db.upcoming_items(user_id=USER, at_or_after=cut, limit=2)
    assert len(rows) == 2
    assert truncated is True

    rows, truncated = await db.upcoming_items(user_id=USER, at_or_after=cut, limit=4)
    assert len(rows) == 4
    assert truncated is False
