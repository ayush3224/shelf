"""`Today` list and mark-done route tests (UC32, UC16, UC41)."""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.config import capture_tz, settings
from backend.db import Database, get_db
from backend.main import app

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"
IST = timezone(timedelta(hours=5, minutes=30))


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


class StubDb(Database):
    """A Database that records its calls instead of touching Postgres."""

    def __init__(
        self,
        rows: Optional[list[dict[str, Any]]] = None,
        previous_state: Optional[str] = "active",
    ) -> None:
        self.rows = rows if rows is not None else []
        self.previous_state = previous_state
        self.today_calls: list[dict[str, Any]] = []
        self.done_calls: list[tuple[str, str]] = []

    async def today_items(
        self, user_id: str, before: datetime, limit: int = 200
    ) -> list[dict[str, Any]]:
        self.today_calls.append({"user_id": user_id, "before": before, "limit": limit})
        return self.rows

    async def mark_done(self, item_id: str, user_id: str) -> Optional[str]:
        self.done_calls.append((item_id, user_id))
        return self.previous_state


def row(**overrides: Any) -> dict[str, Any]:
    """One `Today` row as db.today_items returns it."""
    base = {
        "id": "b3f0c1a2-0000-4000-8000-000000000001",
        "text": "Call the insurance people",
        "raw_text": "uh call the insurance people tomorrow at three",
        "kind": "task",
        "state": "active",
        "due_at": datetime.now(IST) - timedelta(hours=2),
        "critical": False,
        "parse_status": "ok",
    }
    base.update(overrides)
    return base


@pytest.fixture
def client():
    """A TestClient with the database stubbed out.

    No `with` block: entering one would run the lifespan and open a real
    connection pool, which these tests have no use for.
    """
    stub = StubDb()
    app.dependency_overrides[get_db] = lambda: stub
    c = TestClient(app)
    c.stub = stub  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- auth (UC41)


@pytest.mark.parametrize("path", ["/items/today", "/items/abc/done"])
def test_new_routes_are_not_public(client, path):
    """Auth is fail-closed — a new route is protected unless PUBLIC_PATHS says so."""
    method = client.get if path.endswith("today") else client.post
    assert method(path).status_code == 401


def test_today_is_scoped_to_the_token_subject(client):
    """`user_id` comes from the token, never from the request body or query."""
    other = "00000000-0000-4000-8000-0000000000ff"
    client.get("/items/today", headers={"Authorization": f"Bearer {mint(sub=other)}"})
    assert client.stub.today_calls[0]["user_id"] == other


# --------------------------------------------------------------- Today (UC32)


def test_today_is_bounded_to_end_of_day_in_the_users_timezone(client):
    """The cut-off is wall-clock, so it resolves in TZ and not on the server (D15)."""
    client.get("/items/today", headers=auth())
    before = client.stub.today_calls[0]["before"]
    local = before.astimezone(capture_tz())
    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert local.date() == (datetime.now(capture_tz()) + timedelta(days=1)).date()


def test_past_due_items_are_flagged_overdue(client):
    client.stub.rows = [row(due_at=datetime.now(IST) - timedelta(hours=3))]
    body = client.get("/items/today", headers=auth()).json()
    assert body["items"][0]["overdue"] is True


def test_items_due_later_today_are_not_overdue(client):
    client.stub.rows = [row(due_at=datetime.now(IST) + timedelta(minutes=30))]
    body = client.get("/items/today", headers=auth()).json()
    assert body["items"][0]["overdue"] is False


def test_parse_status_and_raw_text_survive_the_response(client):
    """The flag and the original words reach the client, whatever it does with them.

    Nothing in Phase 1 can put a failed parse on `Today` — a failed parse has no
    `due_at`, so it shelves. The fields are carried anyway because UC38 will let
    a user give one a due date by hand, and dropping them here would be the bug.
    """
    client.stub.rows = [row(text="uh call the insurance people", parse_status="failed")]
    item = client.get("/items/today", headers=auth()).json()["items"][0]
    assert item["parse_status"] == "failed"
    assert item["raw_text"]


def test_empty_today_is_an_empty_list_not_an_error(client):
    body = client.get("/items/today", headers=auth()).json()
    assert body["items"] == []


# ----------------------------------------------------------- mark done (UC16)


def test_marking_done_reports_the_change(client):
    client.stub.previous_state = "active"
    body = client.post(f"/items/{row()['id']}/done", headers=auth()).json()
    assert body["state"] == "done"
    assert body["changed"] is True


def test_marking_an_already_done_item_is_a_no_op(client):
    """A double tap must not write a second transition."""
    client.stub.previous_state = "done"
    body = client.post(f"/items/{row()['id']}/done", headers=auth()).json()
    assert body["state"] == "done"
    assert body["changed"] is False


def test_done_on_someone_elses_item_is_a_404(client):
    """The update is scoped to the token subject; a miss is indistinguishable."""
    client.stub.previous_state = None
    assert client.post(f"/items/{row()['id']}/done", headers=auth()).status_code == 404
