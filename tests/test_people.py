"""Entity resolution and the People routes (UC45, UC46, UC47).

`resolve_entity` is a pure function, so the interesting half of UC45 is
testable without a database at all. That is deliberate: the rule it encodes —
**never guess when more than one thing matches** — is the part of this module
that is expensive to get wrong, because the failure is silent. Two people
merged into one row means what you said about one is filed under the other,
and nothing on any screen ever looks odd.

The database half is `test_people_db.py`, and the routes are stubbed here.
"""

import time
from datetime import datetime, timezone
from typing import Any, Optional

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.db import Database, get_db, resolve_entity
from backend.main import app

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
    return {"Authorization": f"Bearer {mint()}"}


def known(entity_id: str, name: str, aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    """One entity as `resolve_entity` expects to see it."""
    return {
        "id": entity_id,
        "type": "person",
        "name": name,
        "aliases": list(aliases),
    }


# --------------------------------------------------------- resolution rules


def test_an_unknown_name_is_somebody_new():
    result = resolve_entity("Priya", "person", [])

    assert result.entity_id is None
    assert result.created is True
    assert result.name == "Priya"


def test_the_same_name_is_the_same_person():
    result = resolve_entity("Priya", "person", [known("1", "Priya")])

    assert result.entity_id == "1"
    assert result.created is False


def test_case_and_spacing_are_not_a_different_person():
    result = resolve_entity("  priya   SHARMA ", "person", [known("1", "Priya Sharma")])

    assert result.entity_id == "1"
    # The stored label is left as the user first said it, not casefolded.
    assert result.name == "Priya Sharma"


def test_a_fuller_name_is_promoted_over_a_shorter_one():
    """ "Priya" on file, "Priya Sharma" arrives: one person, better label."""
    result = resolve_entity("Priya Sharma", "person", [known("1", "Priya")])

    assert result.entity_id == "1"
    assert result.name == "Priya Sharma"
    assert result.aliases == ["Priya"]
    assert result.promoted is True


def test_a_shorter_name_becomes_an_alias_of_the_fuller_one():
    result = resolve_entity("Priya", "person", [known("1", "Priya Sharma")])

    assert result.entity_id == "1"
    assert result.name == "Priya Sharma"
    assert result.aliases == ["Priya"]
    assert result.promoted is False


def test_a_recorded_alias_matches():
    result = resolve_entity("Priya", "person", [known("1", "Priya Sharma", ("Priya",))])

    assert result.entity_id == "1"
    # Already on file; nothing to add.
    assert result.aliases == ["Priya"]


def test_a_surname_alone_matches_a_unique_holder():
    result = resolve_entity("Sharma", "person", [known("1", "Priya Sharma")])

    assert result.entity_id == "1"
    assert result.aliases == ["Sharma"]


def test_two_candidates_are_never_guessed_between():
    """The one mistake that is invisible once made."""
    result = resolve_entity(
        "Priya", "person", [known("1", "Priya Sharma"), known("2", "Priya Nair")]
    )

    assert result.ambiguous is True
    assert result.entity_id is None
    assert result.created is True


def test_an_ambiguous_alias_is_also_declined():
    result = resolve_entity(
        "Pri",
        "person",
        [known("1", "Priya Sharma", ("Pri",)), known("2", "Priya Nair", ("Pri",))],
    )

    assert result.ambiguous is True
    assert result.entity_id is None


def test_two_full_names_sharing_a_token_are_two_people():
    """Neither token set contains the other, so neither is a match."""
    result = resolve_entity("Priya Nair", "person", [known("1", "Priya Sharma")])

    assert result.entity_id is None
    assert result.ambiguous is False


def test_resolution_never_crosses_type():
    place = {"id": "1", "type": "place", "name": "Preston", "aliases": []}
    result = resolve_entity("Preston", "person", [place])

    assert result.entity_id is None


def test_a_blank_name_resolves_to_nothing():
    assert resolve_entity("   ", "person", []) is None


def test_the_same_words_in_another_order_are_not_matched():
    """A known limit, written down rather than papered over.

    "Sharma Priya" has the same tokens as "Priya Sharma", so neither set is a
    *proper* subset of the other and no rule fires. It becomes its own row.
    Deliberate: the alternative is matching on token equality, which would also
    merge genuinely different orderings, and this is the direction that fails
    visibly.
    """
    result = resolve_entity("Sharma Priya", "person", [known("1", "Priya Sharma")])

    assert result.entity_id is None
    assert result.created is True


# ------------------------------------------------------------------ routes


class StubDb(Database):
    """A Database that answers the People routes from canned rows."""

    def __init__(
        self,
        people: Optional[list[dict[str, Any]]] = None,
        person: Optional[dict[str, Any]] = None,
        items: Optional[list[dict[str, Any]]] = None,
        has_more: bool = False,
    ) -> None:
        self.people = people if people is not None else []
        self.person = person
        self.items = items if items is not None else []
        self.has_more = has_more
        self.calls: list[dict[str, Any]] = []

    async def list_people(
        self, user_id: str, query: Optional[str] = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        self.calls.append({"user_id": user_id, "query": query, "limit": limit})
        return self.people

    async def get_person(
        self, entity_id: str, user_id: str
    ) -> Optional[dict[str, Any]]:
        return self.person

    async def person_items(
        self,
        entity_id: str,
        user_id: str,
        after: Optional[tuple[datetime, str]] = None,
        limit: int = 30,
    ) -> tuple[list[dict[str, Any]], bool]:
        self.calls.append({"entity_id": entity_id, "after": after, "limit": limit})
        return self.items, self.has_more


PERSON = {
    "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "name": "Priya Sharma",
    "type": "person",
    "aliases": ["Priya"],
    "mentions": 2,
    "last_mentioned": datetime(2026, 8, 23, tzinfo=timezone.utc),
}

NOTE = {
    "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "text": "Priya wants the deposit back",
    "raw_text": "Priya wants the deposit back",
    "kind": "person_note",
    "state": "shelved",
    "due_at": None,
    "critical": False,
    "parse_status": "ok",
    "has_audio": True,
    "created_at": datetime(2026, 8, 23, tzinfo=timezone.utc),
}


@pytest.fixture
def client():
    def use(db: StubDb) -> TestClient:
        app.dependency_overrides[get_db] = lambda: db
        return TestClient(app)

    yield use
    app.dependency_overrides.clear()


def test_people_requires_a_token(client):
    assert client(StubDb()).get("/people").status_code == 401


def test_a_person_page_requires_a_token(client):
    response = client(StubDb()).get(f"/people/{PERSON['id']}")
    assert response.status_code == 401


def test_people_are_listed(client):
    response = client(StubDb(people=[PERSON])).get("/people", headers=auth())

    assert response.status_code == 200
    body = response.json()["people"][0]
    assert body["name"] == "Priya Sharma"
    assert body["aliases"] == ["Priya"]
    assert body["mentions"] == 2


def test_a_search_term_reaches_the_query(client):
    db = StubDb()
    client(db).get("/people?q=priya", headers=auth())

    assert db.calls[0]["query"] == "priya"


def test_a_blank_search_is_not_a_search(client):
    db = StubDb()
    client(db).get("/people?q=%20%20", headers=auth())

    assert db.calls[0]["query"] == ""


def test_a_person_page_returns_the_person_and_their_notes(client):
    db = StubDb(person=PERSON, items=[NOTE])
    response = client(db).get(f"/people/{PERSON['id']}", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["person"]["name"] == "Priya Sharma"
    assert body["items"][0]["text"] == "Priya wants the deposit back"
    assert body["items"][0]["has_audio"] is True


def test_an_unknown_person_is_404(client):
    response = client(StubDb(person=None)).get(
        f"/people/{PERSON['id']}", headers=auth()
    )
    assert response.status_code == 404


def test_a_person_page_issues_a_cursor_only_when_there_is_more(client):
    ended = client(StubDb(person=PERSON, items=[NOTE], has_more=False)).get(
        f"/people/{PERSON['id']}", headers=auth()
    )
    assert ended.json()["next_cursor"] is None

    more = client(StubDb(person=PERSON, items=[NOTE], has_more=True)).get(
        f"/people/{PERSON['id']}", headers=auth()
    )
    assert more.json()["next_cursor"]


def test_a_person_page_cursor_round_trips(client):
    issued = (
        client(StubDb(person=PERSON, items=[NOTE], has_more=True))
        .get(f"/people/{PERSON['id']}", headers=auth())
        .json()["next_cursor"]
    )

    db = StubDb(person=PERSON)
    client(db).get(f"/people/{PERSON['id']}?cursor={issued}", headers=auth())

    assert db.calls[-1]["after"] == (NOTE["created_at"], NOTE["id"])


def test_a_bad_cursor_is_refused(client):
    response = client(StubDb(person=PERSON)).get(
        f"/people/{PERSON['id']}?cursor=nope", headers=auth()
    )
    assert response.status_code == 400


def test_an_established_alias_survives_a_new_namesake():
    """The asymmetry, pinned because it is a decision and not an accident.

    "Priya" is already an alias of Priya Sharma when Priya Nair turns up. The
    subset rule alone would now call a bare "Priya" ambiguous; the alias rule
    runs first and keeps it on Sharma.

    The reasoning is in `resolve_entity` and the residual risk is O6: an alias
    is a resolution that already happened out of real usage, a subset is an
    inference being made now, and one new namesake should not undo a binding
    built over a year of captures.
    """
    result = resolve_entity(
        "Priya",
        "person",
        [known("1", "Priya Sharma", ("Priya",)), known("2", "Priya Nair")],
    )

    assert result.entity_id == "1"
    assert result.ambiguous is False


def test_without_an_alias_the_same_pair_is_declined():
    """The contrast that makes the rule above a rule rather than a loophole."""
    result = resolve_entity(
        "Priya", "person", [known("1", "Priya Sharma"), known("2", "Priya Nair")]
    )

    assert result.ambiguous is True
    assert result.entity_id is None
