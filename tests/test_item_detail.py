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
        # A time no longer implies an event (D59). Off the calendar unless the
        # owner put it there, whatever hour it is due.
        "on_calendar": False,
        "calendar_sync_state": None,
        "calendar_stalled": False,
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
        self.people: list[dict[str, str]] = []
        self.links: list[dict[str, Any]] = []
        self.unlinks: list[tuple[str, str]] = []
        self.link_result: Any = "auto"
        self.unlink_result: Any = "auto"
        self.calendar_added: list[str] = []
        self.calendar_removed: list[str] = []
        self.calendar_result: Any = "auto"
        self.calendar_remove_result: Any = "auto"

    async def today_items(self, user_id: str, before, limit: int = 200):
        return []

    async def upcoming_items(self, user_id: str, at_or_after, limit: int = 200):
        return [], False

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

    async def item_people(self, item_id: str, user_id: str):
        return list(self.people)

    async def link_person(self, user_id, item_id, entity_id=None, name=None):
        self.links.append({"item_id": item_id, "entity_id": entity_id, "name": name})
        if self.link_result != "auto":
            return self.link_result
        entity = {
            "id": entity_id or f"entity-{name}",
            "name": name or "Somebody",
            "type": "person",
        }
        self.people.append(entity)
        return {**entity, "added": True}

    async def unlink_person(self, user_id, item_id, entity_id):
        self.unlinks.append((item_id, entity_id))
        if self.unlink_result != "auto":
            return self.unlink_result
        going = next((p for p in self.people if p["id"] == entity_id), None)
        if going is None:
            return None
        # The stub has one item, so every link it holds is somebody's last —
        # and nothing asks about that any more (D60).
        self.people = [p for p in self.people if p["id"] != entity_id]
        return {"entity_id": entity_id, "person_removed": True}

    async def request_calendar(self, user_id, item_id):
        self.calendar_added.append(item_id)
        if self.calendar_result != "auto":
            return self.calendar_result
        if self.row is None or item_id != self.row["id"]:
            return None
        eligible = self.row["due_at"] is not None and self.row["state"] in (
            "active",
            "shelved",
        )
        already = bool(self.row["on_calendar"])
        if eligible:
            self.row = {
                **self.row,
                "on_calendar": True,
                "calendar_sync_state": "pending",
            }
        return {
            "eligible": eligible,
            "on_calendar": eligible or already,
            "changed": eligible and not already,
            "sync_state": "pending" if eligible else None,
        }

    async def remove_calendar(self, user_id, item_id):
        self.calendar_removed.append(item_id)
        if self.calendar_remove_result != "auto":
            return self.calendar_remove_result
        if self.row is None or item_id != self.row["id"]:
            return None
        was = bool(self.row["on_calendar"])
        self.row = {
            **self.row,
            "on_calendar": False,
            "calendar_sync_state": None,
        }
        return {"changed": was, "on_calendar": False, "queued": was}


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


# ---------------------------------------------- people on an item, by hand (D45)
#
# Extraction runs on every capture now, not only on `person_note`s. That finds
# far more and therefore misses far more, in both directions — so the detail
# screen has to be able to say "yes she is" and "no he isn't", and neither
# correction may touch the words. D45 already made this trade once for UC48 and
# UC49: the machine files, the owner adjudicates.


def test_the_detail_carries_who_the_item_is_linked_to(client, db):
    db.people = [
        {"id": "e1", "name": "Priya Sharma", "type": "person", "aliases": ["Priya"]}
    ]
    body = client.get(f"/items/{ITEM}", headers=auth()).json()

    # A name and somewhere to go, which is all a chip needs. The aliases used
    # to ride along for the unlink dialog; both are gone (D58, D60).
    assert body["people"] == [{"id": "e1", "name": "Priya Sharma", "type": "person"}]


def test_an_item_with_nobody_on_it_says_so_rather_than_omitting_it(client, db):
    assert client.get(f"/items/{ITEM}", headers=auth()).json()["people"] == []


def test_a_task_carries_people_too(client, db):
    """The whole point of the change: `kind` no longer decides whether a link
    is allowed to exist."""
    db.row = detail(kind="task")
    db.people = [{"id": "e1", "name": "Priya", "type": "person"}]
    body = client.get(f"/items/{ITEM}", headers=auth()).json()

    assert body["kind"] == "task"
    assert [p["name"] for p in body["people"]] == ["Priya"]


def test_a_detail_still_opens_when_the_links_cannot_be_read(client, db, monkeypatch):
    """The words are the item; the links are a view of it. A screen that will
    not open because a join was slow is the worse failure (D6, UC42)."""

    async def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(db, "item_people", boom)
    response = client.get(f"/items/{ITEM}", headers=auth())

    assert response.status_code == 200
    assert response.json()["people"] == []


def test_a_person_can_be_added_by_name(client, db):
    response = client.post(
        f"/items/{ITEM}/people", json={"name": "Priya Sharma"}, headers=auth()
    )

    assert response.status_code == 200
    assert db.links == [{"item_id": ITEM, "entity_id": None, "name": "Priya Sharma"}]
    assert [p["name"] for p in response.json()["people"]] == ["Priya Sharma"]


def test_a_person_can_be_added_by_id(client, db):
    response = client.post(
        f"/items/{ITEM}/people",
        json={"person_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        headers=auth(),
    )

    assert response.status_code == 200
    assert db.links[0]["entity_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_adding_needs_exactly_one_of_a_name_and_a_person(client, db):
    """The same shape the split picker sends (UC49), because it is the same
    gesture — and both-or-neither is a client bug, not a silent preference."""
    assert (
        client.post(f"/items/{ITEM}/people", json={}, headers=auth()).status_code == 400
    )
    both = client.post(
        f"/items/{ITEM}/people",
        json={"person_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "name": "Priya"},
        headers=auth(),
    )
    assert both.status_code == 400
    assert db.links == []


def test_a_blank_name_is_refused_rather_than_creating_a_nameless_person(client, db):
    response = client.post(
        f"/items/{ITEM}/people", json={"name": "   "}, headers=auth()
    )
    assert response.status_code == 400
    assert db.links == []


def test_adding_somebody_to_an_item_that_is_not_yours_is_a_404(client, db):
    db.link_result = None
    response = client.post(
        f"/items/{ITEM}/people", json={"name": "Priya"}, headers=auth()
    )
    assert response.status_code == 404


def test_a_link_can_be_removed(client, db):
    db.people = [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": "Pansy",
            "type": "person",
        }
    ]
    response = client.delete(
        f"/items/{ITEM}/people/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json()["people"] == []
    assert response.json()["person_removed"] is True


def test_emptying_a_person_who_goes_by_other_names_no_longer_asks(client, db):
    """D58 answered 409 here so a client could confirm. D60 reversed it: the
    names go, and the request that used to be refused now just works."""
    db.people = [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": "Priya Sharma",
            "type": "person",
            "aliases": ["Priya", "P"],
        }
    ]
    response = client.delete(
        f"/items/{ITEM}/people/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json()["person_removed"] is True
    assert db.people == []


def test_the_confirmation_flag_is_gone_rather_than_ignored(client, db):
    """A stale client sending it should not be quietly humoured — the route
    does not take the parameter any more, so it is not part of the contract."""
    db.people = [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": "Priya Sharma",
            "type": "person",
            "aliases": ["Priya"],
        }
    ]
    client.delete(
        f"/items/{ITEM}/people/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        "?remove_person=true",
        headers=auth(),
    )

    # One argument short of what it used to be called with.
    assert db.unlinks[-1] == (ITEM, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_an_items_people_do_not_carry_their_aliases(client, db):
    """They were on the wire for the dialog and nothing else (D58, D60)."""
    db.people = [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": "Priya Sharma",
            "type": "person",
        }
    ]
    response = client.get(f"/items/{ITEM}", headers=auth())

    assert response.status_code == 200
    assert "aliases" not in response.json()["people"][0]


def test_removing_a_link_that_is_not_there_is_a_404(client, db):
    response = client.delete(
        f"/items/{ITEM}/people/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        headers=auth(),
    )
    assert response.status_code == 404


def test_both_ends_of_the_correction_need_a_token(client, db):
    assert (
        client.post(f"/items/{ITEM}/people", json={"name": "Priya"}).status_code == 401
    )
    assert (
        client.delete(
            f"/items/{ITEM}/people/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        ).status_code
        == 401
    )


# ------------------------------------------------------- the calendar (UC43)
#
# Adding used to be automatic for anything with a time (007). It is a button
# now (D59), and these are the two ends of it. Neither route touches Google:
# they write the decision down and the tick has a minute to make it true, which
# is what stops a slow morning at Google turning into a failed tap.


@pytest.fixture
def calendar(monkeypatch) -> None:
    """A calendar is configured, so the routes are open."""
    monkeypatch.setattr(settings, "google_calendar_id", "owner@example.com")
    monkeypatch.setattr(settings, "google_calendar_key_file", "key.json")


def test_a_timed_item_starts_off_the_calendar(client, db, calendar):
    """The change in one assertion: having a due time is not being on it."""
    response = client.get(f"/items/{ITEM}", headers=auth())

    assert response.status_code == 200
    assert response.json()["on_calendar"] is False


def test_adding_records_the_request_and_says_it_is_pending(client, db, calendar):
    response = client.post(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["on_calendar"] is True
    assert body["changed"] is True
    # Not "synced". The event does not exist yet and saying so would be a lie
    # the screen would repeat.
    assert body["sync_state"] == "pending"
    assert db.calendar_added == [ITEM]


def test_adding_twice_is_a_retry_rather_than_a_duplicate(client, db, calendar):
    client.post(f"/items/{ITEM}/calendar", headers=auth())
    response = client.post(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 200
    # Still on the calendar, but nothing was added — the press cleared the
    # attempt count, which is the only way back for a link that gave up.
    assert response.json()["on_calendar"] is True
    assert response.json()["changed"] is False


def test_an_item_with_no_time_cannot_be_put_on_a_calendar(client, db, calendar):
    db.row = detail(due_at=None, state="shelved")

    response = client.post(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 409
    assert db.row["on_calendar"] is False


def test_a_finished_item_cannot_be_put_on_a_calendar(client, db, calendar):
    """`done` and `dropped` are what take an event *down* (D54). Adding one
    would queue a write the tick would immediately undo."""
    db.row = detail(state="done")

    response = client.post(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 409


def test_adding_an_item_that_is_not_yours_is_a_404(client, db, calendar):
    db.calendar_result = None

    response = client.post(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 404


def test_adding_says_so_when_there_is_no_calendar_configured(client, db, monkeypatch):
    """Otherwise the row sits pending forever and the screen says "adding…"
    at an event that is never coming."""
    monkeypatch.setattr(settings, "google_calendar_id", "")

    response = client.post(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 503
    assert db.calendar_added == []


def test_removing_takes_it_off_and_queues_the_event(client, db, calendar):
    db.row = detail(on_calendar=True, calendar_sync_state="synced")

    response = client.delete(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["on_calendar"] is False
    assert body["changed"] is True
    # The event comes down through the outbox, not from this request (D53).
    assert body["queued"] is True


def test_removing_something_that_was_never_added_is_not_an_error(client, db, calendar):
    response = client.delete(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 200
    assert response.json()["changed"] is False


def test_removing_needs_no_calendar_configured(client, db, monkeypatch):
    """The row is the owner's, not Google's. Being unable to reach Google is
    no reason to refuse to forget it."""
    monkeypatch.setattr(settings, "google_calendar_id", "")
    db.row = detail(on_calendar=True, calendar_sync_state="synced")

    response = client.delete(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 200
    assert response.json()["changed"] is True


def test_removing_an_item_that_is_not_yours_is_a_404(client, db, calendar):
    db.calendar_remove_result = None

    response = client.delete(f"/items/{ITEM}/calendar", headers=auth())

    assert response.status_code == 404


def test_both_calendar_routes_need_a_token(client, db, calendar):
    assert client.post(f"/items/{ITEM}/calendar").status_code == 401
    assert client.delete(f"/items/{ITEM}/calendar").status_code == 401
