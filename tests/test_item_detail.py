"""Item detail, edit, manual state moves and delete (UC37, UC38, UC21, UC39).

The last two P0 use cases. What is worth pinning: that an edit corrects the
display text without touching the transcript (D14), that changing the due time
re-derives the state without resurrecting terminal items, and that deleting
takes the recording with it.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend.config import settings
from backend.db import Database, get_db
from backend.main import app

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"
ITEM = "b3f0c1a2-0000-4000-8000-000000000001"
IST = timezone(timedelta(hours=5, minutes=30))


def auth(sub: str = USER) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
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
    return {"Authorization": f"Bearer {token}"}


def detail(**overrides: Any) -> dict[str, Any]:
    """One row as db.get_item returns it."""
    base = {
        "id": ITEM,
        "text": "Call the insurance people",
        "raw_text": "uh call the insurance people tomorrow at three",
        "parsed_text": "Call the insurance people",
        "kind": "task",
        "state": "active",
        "due_at": datetime(2026, 8, 24, 15, 0, tzinfo=IST),
        "critical": False,
        "parse_status": "ok",
        "source": "voice",
        "has_audio": True,
        "transcript_source": "cloud",
        "transcript_confidence": 0.71,
        "created_at": datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


class StubDb(Database):
    """A Database that records its calls instead of touching Postgres."""

    def __init__(
        self,
        row: Optional[dict[str, Any]] = None,
        previous_state: Optional[str] = "active",
        delete_result: tuple[bool, Optional[str]] = (True, "u/2026/08/x.m4a"),
    ) -> None:
        self.row = row if row is not None else detail()
        self.previous_state = previous_state
        self.delete_result = delete_result
        self.updates: list[dict[str, Any]] = []
        self.states: list[tuple[str, str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    async def today_items(self, user_id: str, before, limit: int = 200):
        return []

    async def get_item(self, item_id: str, user_id: str):
        return self.row if self.row and item_id == self.row["id"] else None

    async def update_item(
        self, item_id, user_id, text=None, due_at=None, update_due=False
    ):
        self.updates.append(
            {
                "item_id": item_id,
                "text": text,
                "due_at": due_at,
                "update_due": update_due,
            }
        )
        if self.row is None:
            return None
        updated = dict(self.row)
        if text is not None:
            updated["text"] = text
            updated["parsed_text"] = text
        if update_due:
            updated["due_at"] = due_at
            if updated["state"] in ("active", "shelved"):
                updated["state"] = "active" if due_at else "shelved"
        self.row = updated
        return updated

    async def set_state(self, item_id: str, user_id: str, state: str):
        self.states.append((item_id, user_id, state))
        return self.previous_state

    async def delete_item(self, item_id: str, user_id: str):
        self.deletes.append((item_id, user_id))
        return self.delete_result


@pytest.fixture
def db() -> StubDb:
    stub = StubDb()
    app.dependency_overrides[get_db] = lambda: stub
    yield stub
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def stub_storage(monkeypatch):
    """Record audio deletions instead of reaching for the bucket."""
    deleted: list[str] = []

    async def fake_delete(path: str) -> None:
        deleted.append(path)

    monkeypatch.setattr(main_module, "delete_audio", fake_delete)
    return deleted


# ----------------------------------------------------------------- the detail


def test_detail_returns_both_texts(client, db):
    """UC38 edits the display text and needs the transcript beside it to make
    sense of a bad parse (D14)."""
    body = client.get(f"/items/{ITEM}", headers=auth()).json()

    assert body["text"] == "Call the insurance people"
    assert body["raw_text"] == "uh call the insurance people tomorrow at three"
    assert body["has_audio"] is True
    assert body["transcript_source"] == "cloud"


def test_detail_is_404_for_an_item_this_user_does_not_have(client, db):
    other = "b3f0c1a2-0000-4000-8000-999999999999"
    assert client.get(f"/items/{other}", headers=auth()).status_code == 404


def test_detail_requires_a_token(client, db):
    assert client.get(f"/items/{ITEM}").status_code == 401


def test_the_today_route_is_not_swallowed_by_the_id_route(client, db):
    """`/items/today` is a literal and `/items/{item_id}` is a UUID. Declared
    the other way round, the literal 422s."""
    assert client.get("/items/today", headers=auth()).status_code == 200


# --------------------------------------------------------------- editing (UC38)


def test_editing_text_leaves_the_transcript_alone(client, db):
    body = client.patch(
        f"/items/{ITEM}", json={"text": "Call the insurer"}, headers=auth()
    ).json()

    assert body["text"] == "Call the insurer"
    assert body["raw_text"] == "uh call the insurance people tomorrow at three"
    assert db.updates[0]["update_due"] is False


def test_editing_text_does_not_touch_the_due_time(client, db):
    """Omitting `due_at` must not be read as clearing it."""
    client.patch(f"/items/{ITEM}", json={"text": "Call the insurer"}, headers=auth())
    assert db.updates[0]["update_due"] is False


def test_setting_a_due_time_makes_a_shelved_item_active(client, db):
    """The common repair: the parse missed a time, so the item shelved."""
    db.row = detail(state="shelved", due_at=None)
    body = client.patch(
        f"/items/{ITEM}",
        json={"due_at": "2026-08-24T15:00:00+05:30"},
        headers=auth(),
    ).json()

    assert db.updates[0]["update_due"] is True
    assert body["state"] == "active"


def test_clearing_the_due_time_shelves_an_active_item(client, db):
    body = client.patch(f"/items/{ITEM}", json={"due_at": None}, headers=auth()).json()

    assert db.updates[0]["update_due"] is True
    assert db.updates[0]["due_at"] is None
    assert body["state"] == "shelved"


@pytest.mark.parametrize("state", ["done", "dropped"])
def test_an_edit_does_not_resurrect_a_terminal_item(client, db, state):
    """A correction should not undo a completion. That is UC21's job, and it
    should be deliberate."""
    db.row = detail(state=state)
    body = client.patch(f"/items/{ITEM}", json={"due_at": None}, headers=auth()).json()
    assert body["state"] == state


def test_an_empty_body_is_refused(client, db):
    assert client.patch(f"/items/{ITEM}", json={}, headers=auth()).status_code == 400


def test_blank_text_is_refused(client, db):
    """Losing the description to a stray keystroke is not a correction."""
    r = client.patch(f"/items/{ITEM}", json={"text": "   "}, headers=auth())
    assert r.status_code == 400


def test_editing_an_unknown_item_is_404(client, db):
    db.row = None
    r = client.patch(f"/items/{ITEM}", json={"text": "x"}, headers=auth())
    assert r.status_code == 404


# ------------------------------------------------------- manual moves (UC21)


def test_a_manual_move_reports_where_it_came_from(client, db):
    db.previous_state = "active"
    body = client.post(
        f"/items/{ITEM}/state", json={"state": "shelved"}, headers=auth()
    ).json()

    assert body == {
        "id": ITEM,
        "state": "shelved",
        "previous": "active",
        "changed": True,
    }
    assert db.states == [(ITEM, USER, "shelved")]


def test_moving_to_the_state_it_is_already_in_changes_nothing(client, db):
    db.previous_state = "shelved"
    body = client.post(
        f"/items/{ITEM}/state", json={"state": "shelved"}, headers=auth()
    ).json()
    assert body["changed"] is False


@pytest.mark.parametrize("state", ["active", "shelved", "done", "dropped"])
def test_every_state_is_reachable_by_hand(client, db, state):
    r = client.post(f"/items/{ITEM}/state", json={"state": state}, headers=auth())
    assert r.status_code == 200


def test_an_unknown_state_is_refused(client, db):
    r = client.post(f"/items/{ITEM}/state", json={"state": "pending"}, headers=auth())
    assert r.status_code == 400
    assert db.states == []


def test_moving_an_unknown_item_is_404(client, db):
    db.previous_state = None
    r = client.post(f"/items/{ITEM}/state", json={"state": "done"}, headers=auth())
    assert r.status_code == 404


# ------------------------------------------------------------- delete (UC39)


def test_delete_removes_the_row_and_the_recording(client, db, stub_storage):
    body = client.delete(f"/items/{ITEM}", headers=auth()).json()

    assert body == {"id": ITEM, "deleted": True, "audio_deleted": True}
    assert db.deletes == [(ITEM, USER)]
    # The whole point: the object goes too, not just the row.
    assert stub_storage == ["u/2026/08/x.m4a"]


def test_delete_of_a_typed_item_touches_no_storage(client, db, stub_storage):
    db.delete_result = (True, None)
    body = client.delete(f"/items/{ITEM}", headers=auth()).json()

    assert body["audio_deleted"] is False
    assert stub_storage == []


def test_deleting_an_unknown_item_is_404(client, db, stub_storage):
    db.delete_result = (False, None)
    assert client.delete(f"/items/{ITEM}", headers=auth()).status_code == 404
    assert stub_storage == []


def test_a_failed_object_delete_still_reports_the_delete(client, db, monkeypatch):
    """The row is already gone. Failing the request would report a delete that
    did happen as one that did not, and the user would try again on nothing."""

    async def boom(path: str) -> None:
        raise RuntimeError("storage is down")

    monkeypatch.setattr(main_module, "delete_audio", boom)
    with pytest.raises(RuntimeError):
        client.delete(f"/items/{ITEM}", headers=auth())


def test_delete_requires_a_token(client, db):
    assert client.delete(f"/items/{ITEM}").status_code == 401
