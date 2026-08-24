"""People: resolution, linking and the two read paths (UC45, UC46, UC47).

Opt-in:

    pytest -m db

`resolve_entity` is a pure function and is tested as one in `test_people.py`.
This module is about what happens when it meets a database: the snapshot has
to stay correct *within* one capture, a promotion has to actually rename a row
without orphaning the mentions filed under the old name, and the unique
constraint on `(user_id, type, name)` has to survive a capture that says the
same name twice.

Against the suite's own schema, never `shelf`.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
            # `links` and `entities` cascade from the items and the user, but
            # the user outlives each test, so both are cleared by hand.
            await conn.execute(f"DELETE FROM {settings.db_schema}.links")
            await conn.execute(f"DELETE FROM {settings.db_schema}.items")
            await conn.execute(f"DELETE FROM {settings.db_schema}.entities")
        await database.disconnect()


async def make_item(
    db: Database, text: str = "Talked to somebody", age_days: float = 0.0
) -> str:
    """One item to hang links off."""
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.items
              (user_id, raw_text, parsed_text, created_at, updated_at,
               state_changed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (USER, text, text, created, created, created),
        )
        return (await result.fetchone())[0]


def person(name: str) -> dict[str, str]:
    """One entity as the parse hands it over."""
    return {"type": "person", "name": name}


async def entity_named(db: Database, name: str) -> Optional[dict[str, Any]]:
    """Look a row up by its canonical name."""
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT id::text, name, aliases
              FROM {settings.db_schema}.entities
             WHERE user_id = %s AND name = %s
            """,
            (USER, name),
        )
        row = await result.fetchone()
        return {"id": row[0], "name": row[1], "aliases": row[2]} if row else None


async def entity_count(db: Database) -> int:
    async with db.connection() as conn:
        result = await conn.execute(
            f"SELECT count(*) FROM {settings.db_schema}.entities WHERE user_id = %s",
            (USER,),
        )
        return (await result.fetchone())[0]


# ------------------------------------------------------------- UC45 linking


async def test_a_capture_creates_and_links_a_person(db):
    """The write that was always missing: the parse has returned these since
    the first version and every one of them was discarded (D7)."""
    item = await make_item(db, "Ring Priya about the deposit")

    linked = await db.link_entities(USER, item, [person("Priya")])

    assert [entry["name"] for entry in linked] == ["Priya"]
    assert await entity_count(db) == 1

    found, _ = await db.person_items(linked[0]["id"], USER)
    assert [row["id"] for row in found] == [item]


async def test_the_same_name_twice_is_one_person(db):
    """Two captures, one row — and the second must not violate the constraint."""
    first = await make_item(db, "Priya wants the invoice")
    second = await make_item(db, "Priya again about the invoice")

    a = await db.link_entities(USER, first, [person("Priya")])
    b = await db.link_entities(USER, second, [person("Priya")])

    assert a[0]["id"] == b[0]["id"]
    assert await entity_count(db) == 1


async def test_one_capture_naming_someone_twice_writes_one_row(db):
    """The snapshot has to update inside the loop, not just between calls.

    A split (UC4) is the real version of this: two items from one recording
    that both say "Priya". If each resolved against the state before either was
    written, the second would try to insert a duplicate.
    """
    item = await make_item(db)

    linked = await db.link_entities(USER, item, [person("Priya"), person("Priya")])

    assert await entity_count(db) == 1
    assert linked[0]["id"] == linked[1]["id"]


async def test_linking_the_same_item_twice_is_idempotent(db):
    """A retry must not double the mention count."""
    item = await make_item(db)

    await db.link_entities(USER, item, [person("Priya")])
    await db.link_entities(USER, item, [person("Priya")])

    people = await db.list_people(USER)
    assert people[0]["mentions"] == 1


async def test_a_fuller_name_renames_the_row_and_keeps_the_short_one(db):
    """ "Priya" then "Priya Sharma" is one person, filed under the fuller name."""
    first = await make_item(db, "Priya called")
    second = await make_item(db, "Priya Sharma sent the contract")

    await db.link_entities(USER, first, [person("Priya")])
    await db.link_entities(USER, second, [person("Priya Sharma")])

    assert await entity_count(db) == 1
    row = await entity_named(db, "Priya Sharma")
    assert row is not None
    assert row["aliases"] == ["Priya"]

    # The mention filed under the old name is still on the page — which is the
    # whole point of promoting rather than creating a second row.
    found, _ = await db.person_items(row["id"], USER)
    assert {r["id"] for r in found} == {first, second}


async def test_a_shorter_name_joins_the_fuller_one_as_an_alias(db):
    """And the row keeps the fuller name as its label."""
    first = await make_item(db)
    second = await make_item(db)

    await db.link_entities(USER, first, [person("Priya Sharma")])
    await db.link_entities(USER, second, [person("Priya")])

    assert await entity_count(db) == 1
    row = await entity_named(db, "Priya Sharma")
    assert row["aliases"] == ["Priya"]


async def test_two_priyas_stop_a_bare_priya_being_guessed(db):
    """The mistake that must never happen silently.

    Merging the wrong two people attributes what you said about one to the
    other and nothing on the screen ever looks wrong. Splitting them is two
    lists you can see. So an ambiguous name gets its own row.
    """
    await db.link_entities(USER, await make_item(db), [person("Priya Sharma")])
    await db.link_entities(USER, await make_item(db), [person("Priya Nair")])

    bare = await make_item(db, "Priya said yes")
    linked = await db.link_entities(USER, bare, [person("Priya")])

    assert linked[0]["ambiguous"] is True
    assert await entity_count(db) == 3

    # And it went nowhere near either of the existing two.
    sharma = await entity_named(db, "Priya Sharma")
    nair = await entity_named(db, "Priya Nair")
    assert linked[0]["id"] not in {sharma["id"], nair["id"]}


async def test_distinct_full_names_never_merge(db):
    """ "Priya Sharma" and "Priya Nair" share a token and are not the same person."""
    await db.link_entities(USER, await make_item(db), [person("Priya Sharma")])
    await db.link_entities(USER, await make_item(db), [person("Priya Nair")])

    assert await entity_count(db) == 2


async def test_matching_never_crosses_type(db):
    """A place called Preston is not a person called Preston."""
    item = await make_item(db)

    await db.link_entities(
        USER,
        item,
        [{"type": "person", "name": "Preston"}, {"type": "place", "name": "Preston"}],
    )

    assert await entity_count(db) == 2
    # And the People list only ever shows the person.
    assert [p["name"] for p in await db.list_people(USER)] == ["Preston"]


async def test_case_and_spacing_do_not_make_a_second_person(db):
    await db.link_entities(USER, await make_item(db), [person("Priya Sharma")])
    await db.link_entities(USER, await make_item(db), [person("  priya   sharma ")])

    assert await entity_count(db) == 1


async def test_a_blank_name_is_skipped(db):
    """The model occasionally returns an empty string; it is not a person."""
    item = await make_item(db)

    linked = await db.link_entities(USER, item, [person("   "), person("Priya")])

    assert [entry["name"] for entry in linked] == ["Priya"]
    assert await entity_count(db) == 1


# ---------------------------------------------------------------- UC47 list


async def test_people_are_listed_most_recently_mentioned_first(db):
    """The list is for finding somebody, and recency is the better prior."""
    old = await make_item(db, age_days=30)
    recent = await make_item(db, age_days=1)

    await db.link_entities(USER, old, [person("Anil")])
    await db.link_entities(USER, recent, [person("Bela")])

    assert [p["name"] for p in await db.list_people(USER)] == ["Bela", "Anil"]


async def test_people_carry_their_mention_count(db):
    for _ in range(3):
        await db.link_entities(USER, await make_item(db), [person("Priya")])

    assert (await db.list_people(USER))[0]["mentions"] == 3


async def test_search_finds_a_person_by_name(db):
    await db.link_entities(USER, await make_item(db), [person("Priya Sharma")])
    await db.link_entities(USER, await make_item(db), [person("Anil Kumar")])

    assert [p["name"] for p in await db.list_people(USER, query="sharma")] == [
        "Priya Sharma"
    ]


async def test_search_finds_a_person_by_alias(db):
    """The alias is exactly the name you are likely to type.

    You look somebody up as "Priya"; the row calls itself "Priya Sharma"
    because a later capture promoted it. A search that only read the canonical
    name would miss the name you actually remember.
    """
    await db.link_entities(USER, await make_item(db), [person("Priya")])
    await db.link_entities(USER, await make_item(db), [person("Priya Sharma")])

    found = await db.list_people(USER, query="priya")
    assert [p["name"] for p in found] == ["Priya Sharma"]
    assert found[0]["aliases"] == ["Priya"]


async def test_a_percent_in_a_people_search_is_literal(db):
    """Same escape as the Shelf: a typed wildcard is a character."""
    await db.link_entities(USER, await make_item(db), [person("Priya")])

    assert await db.list_people(USER, query="%") == []


# ------------------------------------------------------------- UC46 page


async def test_a_person_page_is_newest_first(db):
    """The owner asked for this on 24 August 2026; use-cases.md said oldest."""
    old = await make_item(db, "first thing", age_days=10)
    new = await make_item(db, "latest thing", age_days=1)

    linked = await db.link_entities(USER, old, [person("Priya")])
    await db.link_entities(USER, new, [person("Priya")])

    rows, _ = await db.person_items(linked[0]["id"], USER)
    assert [r["id"] for r in rows] == [new, old]


async def test_a_person_page_shows_every_state(db):
    """Hiding what you have dealt with answers a narrower question than the
    one the page is for."""
    item = await make_item(db)
    async with db.connection() as conn:
        await conn.execute(
            f"UPDATE {settings.db_schema}.items SET state = 'done' WHERE id = %s",
            (item,),
        )
    linked = await db.link_entities(USER, item, [person("Priya")])

    rows, _ = await db.person_items(linked[0]["id"], USER)
    assert [r["state"] for r in rows] == ["done"]


async def test_a_person_page_pages_without_repeating(db):
    written = [await make_item(db, f"note {i}", age_days=i) for i in range(5)]
    linked = await db.link_entities(USER, written[0], [person("Priya")])
    entity_id = linked[0]["id"]
    for item in written[1:]:
        await db.link_entities(USER, item, [person("Priya")])

    seen: list[str] = []
    after = None
    while True:
        rows, has_more = await db.person_items(entity_id, USER, after=after, limit=2)
        seen += [r["id"] for r in rows]
        if not has_more:
            break
        after = (rows[-1]["created_at"], rows[-1]["id"])

    assert seen == written
    assert len(set(seen)) == 5


async def test_a_person_carries_a_mention_count_and_a_last_seen(db):
    item = await make_item(db, age_days=2)
    linked = await db.link_entities(USER, item, [person("Priya")])

    found = await db.get_person(linked[0]["id"], USER)
    assert found["name"] == "Priya"
    assert found["mentions"] == 1
    assert found["last_mentioned"] is not None


async def test_a_person_belonging_to_someone_else_is_not_found(db):
    """Scoping is on the row, not on the caller's good manners."""
    linked = await db.link_entities(USER, await make_item(db), [person("Priya")])

    other = "00000000-0000-4000-8000-0000000000fe"
    assert await db.get_person(linked[0]["id"], other) is None
    rows, _ = await db.person_items(linked[0]["id"], other)
    assert rows == []
