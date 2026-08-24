"""The Shelf query, against a real Postgres (UC33, UC34, UC36).

Opt-in:

    pytest -m db

`test_shelf.py` proves the route hands the right arguments down. Nothing there
proves the SQL those arguments become does what it says — keyset paging is the
kind of thing that looks obviously correct and is off by one row at the page
boundary, and an `ILIKE` escape is only ever wrong on the input nobody tried.
So this module runs the real statements.

Against the suite's own schema, never `shelf`: these tests write items, and the
live schema is the owner's actual archive. See `conftest.py` — the same lesson
the scheduler suite learned by sending a real notification to a real phone.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

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
            await conn.execute(f"DELETE FROM {settings.db_schema}.projects")
        await database.disconnect()


async def make_item(
    db: Database,
    *,
    raw_text: str = "Get the pollution certificate",
    parsed_text: Optional[str] = None,
    state: str = "shelved",
    project_id: Optional[str] = None,
    age_days: float = 0.0,
) -> str:
    """Write one item straight in, optionally already old.

    Inserted rather than captured: this is a test of the browse query, not of
    the parse, and `created_at` has to be settable for ordering and the date
    filter to be testable at all. The update triggers are `BEFORE UPDATE`, so
    a timestamp set on insert survives.
    """
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.items
              (user_id, raw_text, parsed_text, state, project_id,
               created_at, updated_at, state_changed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (USER, raw_text, parsed_text, state, project_id, created, created, created),
        )
        return (await result.fetchone())[0]


async def make_project(db: Database, name: str = "House") -> str:
    """A project, which nothing creates on its own now UC11 is dropped."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.projects (user_id, name, slug)
            VALUES (%s, %s, %s) RETURNING id::text
            """,
            (USER, name, name.lower()),
        )
        return (await result.fetchone())[0]


async def ids(db: Database, **kwargs) -> list[str]:
    """Just the ids a browse returns, in order."""
    rows, _ = await db.browse_items(user_id=USER, **kwargs)
    return [r["id"] for r in rows]


# ----------------------------------------------------------------- browse


async def test_newest_capture_first(db):
    """Ordered by when it was said (D38), not by when the system moved it."""
    old = await make_item(db, raw_text="oldest", age_days=30)
    middle = await make_item(db, raw_text="middle", age_days=10)
    new = await make_item(db, raw_text="newest", age_days=1)

    assert await ids(db, states=("shelved",)) == [new, middle, old]


async def test_states_narrow_the_list(db):
    """The chips are a filter on the same query, not a different one (UC36)."""
    shelved = await make_item(db, state="shelved")
    done = await make_item(db, state="done")
    await make_item(db, state="active")

    assert set(await ids(db, states=("shelved", "done", "dropped"))) == {shelved, done}
    assert await ids(db, states=("done",)) == [done]


async def test_no_states_means_every_state(db):
    """Which is what a search asks for."""
    await make_item(db, state="active")
    await make_item(db, state="dropped")

    assert len(await ids(db, states=())) == 2


async def test_one_user_cannot_see_another(db):
    """Scoping is on the row, not on the caller's good manners."""
    mine = await make_item(db)
    async with db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING
            """,
            ("00000000-0000-4000-8000-0000000000ff",),
        )
        await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.items (user_id, raw_text)
            VALUES (%s, %s)
            """,
            ("00000000-0000-4000-8000-0000000000ff", "someone else's note"),
        )

    try:
        assert await ids(db, states=()) == [mine]
    finally:
        async with db.connection() as conn:
            await conn.execute(
                "DELETE FROM auth.users WHERE id = %s",
                ("00000000-0000-4000-8000-0000000000ff",),
            )


# ----------------------------------------------------------------- search


async def test_search_reads_both_texts(db):
    """`raw_text` is what was said, `parsed_text` is what is shown (UC34).

    Migration 001 indexed only the first and 002 added the second without one,
    so this is the half of every search that used to be a sequential scan.
    """
    spoken = await make_item(db, raw_text="ring the dentist about the crown")
    cleaned = await make_item(
        db, raw_text="uh, so, that thing", parsed_text="Book a dentist"
    )

    assert set(await ids(db, states=(), query="dentist")) == {spoken, cleaned}


async def test_search_ignores_case(db):
    """A search box is not case-sensitive, whatever the user's keyboard did."""
    item = await make_item(db, raw_text="Get the Pollution Certificate")
    assert await ids(db, states=(), query="pollution") == [item]


async def test_search_matches_inside_a_word(db):
    """Substring, not prefix — half-remembered is the normal case here."""
    item = await make_item(db, raw_text="reschedule the dentist")
    assert await ids(db, states=(), query="schedul") == [item]


async def test_a_percent_is_a_percent_and_not_a_wildcard(db):
    """The one input nobody tries: a typed `%` must not match the whole table.

    Unescaped this returns every row, which reads as a broken filter rather
    than as an injection — which is exactly why it would survive review.
    """
    literal = await make_item(db, raw_text="pay the 20% deposit")
    await make_item(db, raw_text="nothing to do with money")

    assert await ids(db, states=(), query="20%") == [literal]


async def test_an_underscore_is_a_literal_too(db):
    """`_` is ILIKE's single-character wildcard; it is also a character."""
    literal = await make_item(db, raw_text="rename to shelf_tick")
    await make_item(db, raw_text="shelfXtick would match a wildcard")

    assert await ids(db, states=(), query="shelf_tick") == [literal]


async def test_a_backslash_does_not_break_the_escape(db):
    """Escaping the wildcards must not be undone by escaping the escape."""
    item = await make_item(db, raw_text=r"the path is C:\temp")
    assert await ids(db, states=(), query=r"C:\temp") == [item]


async def test_search_and_filters_compose(db):
    """A search you cannot narrow is a second, worse list (UC34 with UC36)."""
    await make_item(db, raw_text="dentist", state="shelved")
    done = await make_item(db, raw_text="dentist again", state="done")

    assert await ids(db, states=("done",), query="dentist") == [done]


# ---------------------------------------------------------------- filters


async def test_a_date_range_bounds_on_capture_time(db):
    """`from` is inclusive and `to` is exclusive, so ranges can abut cleanly."""
    recent = await make_item(db, age_days=2)
    await make_item(db, age_days=40)

    now = datetime.now(timezone.utc)
    assert await ids(db, states=("shelved",), created_from=now - timedelta(days=7)) == [
        recent
    ]
    assert await ids(db, states=("shelved",), created_to=now - timedelta(days=7)) != [
        recent
    ]


async def test_a_project_filter_selects_one_project(db):
    house = await make_project(db)
    inside = await make_item(db, project_id=house)
    await make_item(db)

    assert await ids(db, states=("shelved",), project_id=house) == [inside]


async def test_unsorted_selects_the_items_with_no_project(db):
    """Which, with UC11 dropped, is nearly all of them."""
    house = await make_project(db)
    await make_item(db, project_id=house)
    loose = await make_item(db)

    assert await ids(db, states=("shelved",), unsorted_only=True) == [loose]


async def test_a_row_carries_its_project_name(db):
    """The client groups on this; a join that returned null would flatten it."""
    house = await make_project(db, "House")
    await make_item(db, project_id=house)

    rows, _ = await db.browse_items(user_id=USER, states=("shelved",))
    assert rows[0]["project_name"] == "House"
    assert rows[0]["project_id"] == house


async def test_projects_are_listed_with_their_counts(db):
    house = await make_project(db, "House")
    await make_item(db, project_id=house)
    await make_item(db, project_id=house)

    listed = await db.list_projects(USER)
    assert listed == [{"id": house, "name": "House", "slug": "house", "items": 2}]


# ------------------------------------------------------------- pagination


async def test_a_full_walk_repeats_nothing_and_skips_nothing(db):
    """The property that matters: paging is a partition of the result set."""
    written = [await make_item(db, raw_text=f"item {i}", age_days=i) for i in range(7)]

    seen: list[str] = []
    after = None
    while True:
        rows, has_more = await db.browse_items(
            user_id=USER, states=("shelved",), after=after, limit=2
        )
        seen += [r["id"] for r in rows]
        if not has_more:
            break
        after = (rows[-1]["created_at"], rows[-1]["id"])

    assert seen == written  # written oldest-last, and the list is newest-first
    assert len(set(seen)) == len(written)


async def test_has_more_is_false_on_the_last_page(db):
    """Asking for one row past the limit is how this is known without a count."""
    for i in range(3):
        await make_item(db, age_days=i)

    _, more = await db.browse_items(user_id=USER, states=("shelved",), limit=3)
    assert more is False

    _, more = await db.browse_items(user_id=USER, states=("shelved",), limit=2)
    assert more is True


async def test_the_page_does_not_leak_the_probe_row(db):
    """`limit + 1` is fetched; `limit` is returned."""
    for i in range(5):
        await make_item(db, age_days=i)

    rows, _ = await db.browse_items(user_id=USER, states=("shelved",), limit=2)
    assert len(rows) == 2


async def test_the_id_tiebreak_makes_the_order_total(db):
    """Two captures in the same microsecond must still page deterministically.

    Without `id` in the sort and in the cursor this is where a row repeats or
    vanishes — and captures that share a timestamp are exactly what a split
    (UC4) produces.
    """
    same = datetime.now(timezone.utc) - timedelta(days=1)
    async with db.connection() as conn:
        for n in range(4):
            await conn.execute(
                f"""
                INSERT INTO {settings.db_schema}.items
                  (user_id, raw_text, created_at, updated_at, state_changed_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (USER, f"split part {n}", same, same, same),
            )

    seen: list[str] = []
    after = None
    while True:
        rows, has_more = await db.browse_items(
            user_id=USER, states=("shelved",), after=after, limit=1
        )
        seen += [r["id"] for r in rows]
        if not has_more:
            break
        after = (rows[-1]["created_at"], rows[-1]["id"])

    assert len(seen) == 4
    assert len(set(seen)) == 4


async def test_an_item_captured_mid_scroll_cannot_shift_a_page(db):
    """The reason this is a keyset and not an OFFSET.

    A capture landing between two pages moves every row down by one under
    `OFFSET`, so the row at the boundary is served twice. The cursor names a
    row rather than a position, so a newer item simply sorts above the whole
    walk and is not seen by it.
    """
    written = [
        await make_item(db, raw_text=f"item {i}", age_days=i + 1) for i in range(4)
    ]

    rows, _ = await db.browse_items(user_id=USER, states=("shelved",), limit=2)
    first_page = [r["id"] for r in rows]
    after = (rows[-1]["created_at"], rows[-1]["id"])

    await make_item(db, raw_text="captured while scrolling", age_days=0)

    rows, _ = await db.browse_items(
        user_id=USER, states=("shelved",), after=after, limit=2
    )
    second_page = [r["id"] for r in rows]

    assert first_page + second_page == written
    assert not set(first_page) & set(second_page)
