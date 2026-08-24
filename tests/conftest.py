"""Fixtures for the `db`-marked suite.

Those tests exercise the scheduler, and the scheduler is deliberately not
scoped to a user — it sweeps every row in the schema, because that is what a
cron tick is for. Run against the live `shelf` schema that makes it a test
that reaches into production data: it will enqueue the owner's real items,
send real notifications to the owner's real phone, and — as happened once —
disable a live push token because a test decided to pretend it was dead.

So the suite gets its own schema, built from the same migration files and
dropped afterwards. The migrations name `shelf.` explicitly, so the schema
name is substituted on the way in; nothing is edited on disk (`CLAUDE.md`:
never edit an applied migration).

The one thing not duplicated is `auth.users`, which the foreign keys point at
and which belongs to Supabase. A throwaway user is created there instead and
removed after; everything else cascades from it.
"""

import re
from pathlib import Path

import psycopg
import pytest

from backend import gcal
from backend.config import settings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

#: The schema the db suite builds and drops. Never `shelf`.
SCRATCH_SCHEMA = "shelf_test"

#: A user that exists only for the duration of the suite. Items and push
#: tokens hang off it, so removing it removes them.
TEST_USER = "00000000-0000-4000-8000-000000000001"


def _rewritten(sql: str, schema: str) -> str:
    """Point a migration at another schema.

    Args:
        sql: The migration as written, naming `shelf`.
        schema: Where it should go instead.

    Returns:
        The same SQL against `schema`.
    """
    # Word-boundary matched so `shelf-audio` and prose about "the shelf" are
    # left alone; only the qualified identifier moves.
    return re.sub(r"\bshelf\.", f"{schema}.", sql).replace(
        "create schema if not exists shelf;", f"create schema if not exists {schema};"
    )


@pytest.fixture(autouse=True)
def never_reaches_google_calendar(monkeypatch):
    """No test writes to a real Google Calendar (UC43).

    This is autouse and lives here rather than in the calendar suite, because
    the tests that need protecting are the ones that have never heard of the
    calendar: `scheduler.tick()` gained a step that talks to Google, and every
    db test that runs a tick would otherwise put its fixture items on the
    owner's actual calendar. That is not hypothetical — it happened once, and
    the event had to be deleted by hand.

    Two locks, because either alone can be undone by a fixture that means
    well: the calendar id is cleared so the step skips itself, and the calls
    themselves are replaced with something that refuses loudly. A test that
    genuinely wants the client back has to say so.
    """
    monkeypatch.setattr(settings, "google_calendar_id", "")

    async def refuse(*args, **kwargs):
        raise AssertionError(
            "a test tried to reach the real Google Calendar; stub `gcal` "
            "(see the `calendar` fixture in tests/test_calendar_db.py)"
        )

    for name in ("create_event", "patch_event", "delete_event", "access_token"):
        monkeypatch.setattr(gcal, name, refuse)


@pytest.fixture(scope="session")
def scratch_schema():
    """Build the suite's own schema from the migrations, and drop it after.

    Yields:
        The schema name, with `settings.db_schema` pointed at it for the
        duration so every query in `backend` follows.
    """
    if SCRATCH_SCHEMA == settings.db_schema:
        raise RuntimeError("refusing to run db tests against the live schema")

    files = sorted(MIGRATIONS.glob("*.sql"))
    assert files, f"no migrations found in {MIGRATIONS}"

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCRATCH_SCHEMA}")
        for path in files:
            conn.execute(_rewritten(path.read_text(), SCRATCH_SCHEMA))
        conn.execute(
            "INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING",
            (TEST_USER,),
        )

    live = settings.db_schema
    settings.db_schema = SCRATCH_SCHEMA
    try:
        yield SCRATCH_SCHEMA
    finally:
        settings.db_schema = live
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE")
            # Cascades to nothing now, but the user is ours and should not
            # outlive the suite that made it.
            conn.execute("DELETE FROM auth.users WHERE id = %s", (TEST_USER,))
