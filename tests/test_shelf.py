"""The Shelf route: browse, search and filter (UC33, UC34, UC36).

Route-level, against a stubbed database — what is under test here is the
translation from query string to query arguments, which is where the defaults
live and where the two of them that differ (searching widens to every state,
browsing does not) either hold or quietly stop holding.

The SQL those arguments turn into is tested against a real Postgres in
`test_shelf_db.py`, because a query that has only ever been read by a stub is
a query that has not been run.
"""

import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.db import Database, get_db
from backend.main import _encode_cursor, app

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"


def mint(sub: str = USER) -> str:
    """Mint a Supabase-shaped access token."""
    now = int(time.time())
    return jwt.encode(
        {
            "aud": "authenticated",
            "role": "authenticated",
            "iss": "supabase",
            "iat": now,
            "exp": now + 3600,
            "sub": sub,
        },
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )


def auth() -> dict[str, str]:
    """Authorization header for the test user."""
    return {"Authorization": f"Bearer {mint()}"}


def row(
    item_id: str = "11111111-1111-4111-8111-111111111111",
    *,
    state: str = "shelved",
    text: str = "Get the pollution certificate",
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """One row shaped as `browse_items` returns it."""
    at = created_at or datetime(2026, 8, 23, 11, 33, tzinfo=timezone.utc)
    return {
        "id": item_id,
        "text": text,
        "raw_text": text,
        "kind": "task",
        "state": state,
        "due_at": None,
        "critical": False,
        "parse_status": "ok",
        "has_audio": False,
        "project_id": project_id,
        "project_name": project_name,
        "created_at": at,
        "state_changed_at": at,
    }


class StubDb(Database):
    """A Database that records what the route asked it for."""

    def __init__(
        self,
        rows: Optional[list[dict[str, Any]]] = None,
        has_more: bool = False,
        projects: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.rows = rows if rows is not None else []
        self.has_more = has_more
        self.projects = projects if projects is not None else []
        self.calls: list[dict[str, Any]] = []

    async def browse_items(
        self,
        user_id: str,
        states: Sequence[str],
        query: Optional[str] = None,
        project_id: Optional[str] = None,
        unsorted_only: bool = False,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        after: Optional[tuple[datetime, str]] = None,
        limit: int = 30,
    ) -> tuple[list[dict[str, Any]], bool]:
        self.calls.append(
            {
                "user_id": user_id,
                "states": tuple(states),
                "query": query,
                "project_id": project_id,
                "unsorted_only": unsorted_only,
                "created_from": created_from,
                "created_to": created_to,
                "after": after,
                "limit": limit,
            }
        )
        return self.rows, self.has_more

    async def list_projects(self, user_id: str) -> list[dict[str, Any]]:
        return self.projects


@pytest.fixture
def client():
    """A TestClient whose database is a stub, cleaned up after."""
    stubs: list[StubDb] = []

    def use(db: StubDb) -> TestClient:
        app.dependency_overrides[get_db] = lambda: db
        stubs.append(db)
        return TestClient(app)

    yield use
    app.dependency_overrides.clear()


# ------------------------------------------------------------------- auth


def test_browse_requires_a_token(client):
    """The Shelf is the whole archive; it is not readable without a token (UC41)."""
    response = client(StubDb()).get("/items")
    assert response.status_code == 401


def test_projects_requires_a_token(client):
    """Same for the list the filter chips are drawn from."""
    response = client(StubDb()).get("/projects")
    assert response.status_code == 401


# --------------------------------------------------------------- defaults


def test_browsing_defaults_to_everything_not_active(client):
    """The Shelf is defined by what it excludes: the list `Today` already owns."""
    db = StubDb()
    response = client(db).get("/items", headers=auth())

    assert response.status_code == 200
    assert db.calls[0]["states"] == ("shelved", "done", "dropped")
    assert response.json()["states"] == ["shelved", "done", "dropped"]


def test_a_search_spans_every_state(client):
    """You look for a thing you said, not for a state it happens to be in (UC34).

    The failure this pins down is the quiet one: a search that silently could
    not see today's items would return nothing and look like an empty archive
    rather than a filter you did not know was on.
    """
    db = StubDb()
    client(db).get("/items?q=pollution", headers=auth())

    assert db.calls[0]["states"] == ("active", "shelved", "done", "dropped")
    assert db.calls[0]["query"] == "pollution"


def test_an_explicit_state_beats_both_defaults(client):
    """Which is what lets a chip narrow a search back down again (UC36)."""
    db = StubDb()
    client(db).get("/items?q=pollution&state=done", headers=auth())

    assert db.calls[0]["states"] == ("done",)


def test_states_come_back_in_canonical_order(client):
    """Not the order they arrived in, so the echo is comparable between calls."""
    db = StubDb()
    response = client(db).get("/items?state=dropped&state=active", headers=auth())

    assert response.json()["states"] == ["active", "dropped"]


def test_an_unknown_state_is_refused_by_name(client):
    """A typo'd chip must not silently widen the list to everything."""
    response = client(StubDb()).get("/items?state=parked", headers=auth())

    assert response.status_code == 400
    assert "parked" in response.json()["detail"]


# ----------------------------------------------------------------- search


def test_a_one_character_search_is_refused(client):
    """Below the trigram floor every row matches and no index can help."""
    db = StubDb()
    response = client(db).get("/items?q=p", headers=auth())

    assert response.status_code == 400
    assert not db.calls, "the query should not have reached the database"


def test_a_blank_search_is_not_a_search(client):
    """Clearing the box falls back to the Shelf rather than erroring."""
    db = StubDb()
    response = client(db).get("/items?q=%20%20", headers=auth())

    assert response.status_code == 200
    assert db.calls[0]["query"] is None
    assert db.calls[0]["states"] == ("shelved", "done", "dropped")


def test_a_search_term_is_trimmed(client):
    """Trailing space from a keyboard should not change what matches."""
    db = StubDb()
    client(db).get("/items?q=%20pollution%20", headers=auth())

    assert db.calls[0]["query"] == "pollution"


# ---------------------------------------------------------------- filters


def test_project_none_means_unsorted(client):
    """With UC11 dropped this is where nearly everything lives."""
    db = StubDb()
    client(db).get("/items?project=none", headers=auth())

    assert db.calls[0]["unsorted_only"] is True
    assert db.calls[0]["project_id"] is None


def test_a_project_id_is_passed_through(client):
    db = StubDb()
    pid = "22222222-2222-4222-8222-222222222222"
    client(db).get(f"/items?project={pid}", headers=auth())

    assert db.calls[0]["project_id"] == pid
    assert db.calls[0]["unsorted_only"] is False


def test_a_malformed_project_id_is_refused(client):
    """400 rather than a 500 out of the driver."""
    response = client(StubDb()).get("/items?project=not-a-uuid", headers=auth())
    assert response.status_code == 400


def test_a_date_range_is_passed_through(client):
    """Both ends, and both against capture time (D38)."""
    db = StubDb()
    client(db).get(
        "/items?from=2026-08-01T00:00:00Z&to=2026-08-24T00:00:00Z", headers=auth()
    )

    assert db.calls[0]["created_from"] == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert db.calls[0]["created_to"] == datetime(2026, 8, 24, tzinfo=timezone.utc)


# ------------------------------------------------------------- pagination


def test_a_cursor_is_issued_only_when_there_is_more(client):
    """`next_cursor` and `has_more` must never disagree — the client trusts one."""
    last = row("33333333-3333-4333-8333-333333333333")

    ended = client(StubDb([last], has_more=False)).get("/items", headers=auth()).json()
    assert ended["has_more"] is False
    assert ended["next_cursor"] is None

    more = client(StubDb([last], has_more=True)).get("/items", headers=auth()).json()
    assert more["has_more"] is True
    assert more["next_cursor"]


def test_a_cursor_round_trips_to_the_row_it_names(client):
    """The keyset is the last row of the page, which is where the next one starts."""
    at = datetime(2026, 8, 23, 11, 33, tzinfo=timezone.utc)
    last = row("44444444-4444-4444-8444-444444444444", created_at=at)

    issued = (
        client(StubDb([last], has_more=True)).get("/items", headers=auth()).json()
    )["next_cursor"]

    db = StubDb()
    client(db).get(f"/items?cursor={issued}", headers=auth())
    assert db.calls[0]["after"] == (at, "44444444-4444-4444-8444-444444444444")


def test_an_empty_page_claims_no_more(client):
    """`has_more` with nothing to anchor a cursor to would be an infinite scroll."""
    response = client(StubDb([], has_more=True)).get("/items", headers=auth()).json()

    assert response["has_more"] is False
    assert response["next_cursor"] is None


def test_a_cursor_we_did_not_issue_is_refused(client):
    """400, not a 500 from base64 or `fromisoformat`."""
    for bad in ("notacursor", _encode_cursor(datetime.now(timezone.utc), "nope")):
        response = client(StubDb()).get(f"/items?cursor={bad}", headers=auth())
        assert response.status_code == 400, bad


def test_the_page_size_is_capped(client):
    """A client asking for the whole table is refused rather than served it."""
    assert client(StubDb()).get("/items?limit=5000", headers=auth()).status_code == 422
    assert client(StubDb()).get("/items?limit=0", headers=auth()).status_code == 422


# ---------------------------------------------------------------- shaping


def test_a_row_carries_its_project_so_the_client_can_group(client):
    """Sections are the client's job — a group can straddle a page boundary."""
    db = StubDb(
        [
            row(
                "55555555-5555-4555-8555-555555555555",
                project_id="22222222-2222-4222-8222-222222222222",
                project_name="House",
            )
        ]
    )
    item = client(db).get("/items", headers=auth()).json()["items"][0]

    assert item["project_name"] == "House"
    assert item["project_id"] == "22222222-2222-4222-8222-222222222222"


def test_the_list_is_scoped_to_the_caller(client):
    """The token decides whose archive this is, never a parameter."""
    db = StubDb()
    client(db).get("/items", headers=auth())
    assert db.calls[0]["user_id"] == USER


def test_projects_are_listed_for_the_chips(client):
    db = StubDb(projects=[{"id": "p", "name": "House", "slug": "house", "items": 3}])
    response = client(db).get("/projects", headers=auth())

    assert response.status_code == 200
    assert response.json()["projects"][0]["items"] == 3


def test_projects_are_normally_empty(client):
    """UC11 was dropped; nothing infers a project, so the chip row stays hidden."""
    response = client(StubDb()).get("/projects", headers=auth())
    assert response.json() == {"projects": []}
