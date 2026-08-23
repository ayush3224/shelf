"""Device registration, snooze and reactivate (UC23, UC17, UC20, UC41).

Everything here stubs the database. What is being checked is the contract the
notification actions depend on: an action tapped in the shade must not need
the app to guess what happened, and it must not fail just because the item has
moved on since the push went out.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.db import Database, get_db
from backend.main import app

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"
TOKEN = "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]"


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
        snooze: Optional[dict[str, Any]] = None,
        reactivate: Optional[dict[str, Any]] = None,
        devices: int = 1,
    ) -> None:
        self.snooze = snooze
        self.reactivate = reactivate
        self.devices = devices
        self.registered: list[dict[str, Any]] = []
        self.snooze_calls: list[dict[str, Any]] = []
        self.reactivate_calls: list[dict[str, Any]] = []

    async def register_push_token(
        self,
        user_id: str,
        token: str,
        platform: str,
        device_name: Optional[str] = None,
    ) -> bool:
        self.registered.append(
            {
                "user_id": user_id,
                "token": token,
                "platform": platform,
                "device_name": device_name,
            }
        )
        return True

    async def push_token_count(self, user_id: str) -> int:
        return self.devices

    async def snooze_item(
        self, item_id: str, user_id: str, minutes: int
    ) -> Optional[dict[str, Any]]:
        self.snooze_calls.append(
            {"item_id": item_id, "user_id": user_id, "minutes": minutes}
        )
        return self.snooze

    async def reactivate_item(
        self, item_id: str, user_id: str, due_at: Optional[datetime] = None
    ) -> Optional[dict[str, Any]]:
        self.reactivate_calls.append(
            {"item_id": item_id, "user_id": user_id, "due_at": due_at}
        )
        return self.reactivate


@pytest.fixture
def client_and_db(request):
    """A TestClient with the database dependency stubbed."""
    stub = getattr(request, "param", None) or StubDb()
    app.dependency_overrides[get_db] = lambda: stub
    with TestClient(app) as client:
        yield client, stub
    app.dependency_overrides.clear()


ITEM = "3f8b1a10-0000-4000-8000-000000000001"


# -------------------------------------------------------------- /devices


def test_registering_a_device_stores_the_token(client_and_db):
    """The app calls this on every launch; the token is what a push needs (UC23)."""
    client, stub = client_and_db

    response = client.post(
        "/devices",
        json={"token": TOKEN, "platform": "android", "device_name": "Pixel"},
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json() == {"registered": True, "devices": 1}
    assert stub.registered == [
        {
            "user_id": USER,
            "token": TOKEN,
            "platform": "android",
            "device_name": "Pixel",
        }
    ]


def test_a_token_that_is_not_an_expo_token_is_refused(client_and_db):
    """Storing a malformed token means a silent non-delivery weeks later."""
    client, stub = client_and_db

    response = client.post("/devices", json={"token": "fcm-abc"}, headers=auth())

    assert response.status_code == 400
    assert stub.registered == []


def test_a_token_is_trimmed_before_it_is_stored(client_and_db):
    """A stray newline off a clipboard must not become a different token."""
    client, stub = client_and_db

    response = client.post("/devices", json={"token": f"  {TOKEN}\n"}, headers=auth())

    assert response.status_code == 200
    assert stub.registered[0]["token"] == TOKEN


def test_an_unknown_platform_is_refused(client_and_db):
    """The column has a check constraint; failing here says why."""
    client, _ = client_and_db

    response = client.post(
        "/devices", json={"token": TOKEN, "platform": "blackberry"}, headers=auth()
    )

    assert response.status_code == 400


def test_registering_a_device_needs_a_token(client_and_db):
    """Auth is fail-closed, and a push address is not a public thing to write (UC41)."""
    client, stub = client_and_db

    response = client.post("/devices", json={"token": TOKEN})

    assert response.status_code == 401
    assert stub.registered == []


# --------------------------------------------------------------- /snooze


SNOOZED = {
    "state": "active",
    "due_at": datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
    "snooze_count": 1,
    "changed": True,
}


@pytest.mark.parametrize("client_and_db", [StubDb(snooze=SNOOZED)], indirect=True)
def test_snooze_uses_the_configured_default_when_none_is_given(client_and_db):
    """The notification button sends no duration; the server owns that number."""
    client, stub = client_and_db

    response = client.post(f"/items/{ITEM}/snooze", headers=auth())

    assert response.status_code == 200
    assert stub.snooze_calls[0]["minutes"] == settings.snooze_minutes
    assert response.json()["changed"] is True
    assert response.json()["snooze_count"] == 1


@pytest.mark.parametrize("client_and_db", [StubDb(snooze=SNOOZED)], indirect=True)
def test_snooze_accepts_an_explicit_duration(client_and_db):
    """In-app snooze can ask for something other than the default (UC17)."""
    client, stub = client_and_db

    response = client.post(
        f"/items/{ITEM}/snooze", json={"minutes": 90}, headers=auth()
    )

    assert response.status_code == 200
    assert stub.snooze_calls[0]["minutes"] == 90


@pytest.mark.parametrize("client_and_db", [StubDb(snooze=SNOOZED)], indirect=True)
def test_an_absurd_snooze_is_refused_rather_than_clamped(client_and_db):
    """Silently shortening a snooze would be the system lying about what it did."""
    client, stub = client_and_db

    response = client.post(
        f"/items/{ITEM}/snooze",
        json={"minutes": settings.max_snooze_minutes + 1},
        headers=auth(),
    )

    assert response.status_code == 400
    assert stub.snooze_calls == []


@pytest.mark.parametrize("client_and_db", [StubDb(snooze=SNOOZED)], indirect=True)
def test_a_negative_snooze_is_refused(client_and_db):
    """Snoozing into the past would fire again immediately."""
    client, _ = client_and_db

    assert (
        client.post(
            f"/items/{ITEM}/snooze", json={"minutes": -5}, headers=auth()
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "client_and_db",
    [
        StubDb(
            snooze={
                "state": "shelved",
                "due_at": None,
                "snooze_count": 3,
                "changed": False,
            }
        )
    ],
    indirect=True,
)
def test_snoozing_a_decayed_item_reports_where_it_went(client_and_db):
    """A stale notification is not an error.

    The push was real when it was sent. By the time the button is pressed the
    item may have decayed to the shelf — the app needs to be able to say so,
    which a 404 or a 409 would not let it do.
    """
    client, _ = client_and_db

    response = client.post(f"/items/{ITEM}/snooze", headers=auth())

    assert response.status_code == 200
    assert response.json()["changed"] is False
    assert response.json()["state"] == "shelved"


def test_snoozing_something_that_is_not_there_is_a_404(client_and_db):
    """A deleted item's notification can still be in the shade."""
    client, _ = client_and_db

    assert client.post(f"/items/{ITEM}/snooze", headers=auth()).status_code == 404


def test_snooze_needs_a_token(client_and_db):
    """Every route but /health is behind the session (UC41)."""
    client, stub = client_and_db

    assert client.post(f"/items/{ITEM}/snooze").status_code == 401
    assert stub.snooze_calls == []


# ----------------------------------------------------------- /reactivate


REACTIVATED = {
    "previous": "shelved",
    "due_at": datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
    "changed": True,
}


@pytest.mark.parametrize(
    "client_and_db", [StubDb(reactivate=REACTIVATED)], indirect=True
)
def test_reactivating_takes_an_item_off_the_shelf(client_and_db):
    """The counterweight to silent decay (UC20)."""
    client, stub = client_and_db

    response = client.post(f"/items/{ITEM}/reactivate", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "active"
    assert body["previous"] == "shelved"
    assert body["changed"] is True
    assert stub.reactivate_calls[0]["due_at"] is None


@pytest.mark.parametrize(
    "client_and_db", [StubDb(reactivate=REACTIVATED)], indirect=True
)
def test_reactivating_can_name_a_time(client_and_db):
    """ "Bring it back on Thursday" is the same action with a time attached."""
    client, stub = client_and_db
    when = datetime.now(timezone.utc) + timedelta(days=2)

    response = client.post(
        f"/items/{ITEM}/reactivate", json={"due_at": when.isoformat()}, headers=auth()
    )

    assert response.status_code == 200
    assert stub.reactivate_calls[0]["due_at"] == when


def test_reactivating_something_that_is_not_there_is_a_404(client_and_db):
    """Scoped to the owner: a guessed id must not reach someone else's item."""
    client, _ = client_and_db

    assert client.post(f"/items/{ITEM}/reactivate", headers=auth()).status_code == 404


def test_reactivate_needs_a_token(client_and_db):
    """Fail-closed, like everything else (UC41)."""
    client, stub = client_and_db

    assert client.post(f"/items/{ITEM}/reactivate").status_code == 401
    assert stub.reactivate_calls == []
