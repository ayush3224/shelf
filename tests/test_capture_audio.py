"""Voice capture route tests (UC1, UC7, UC8, UC4, UC42).

The ordering is the design, so it is what gets asserted: the recording is
stored before anything else, the row is written before the model call (D6),
and every downstream failure leaves the audio and the row intact.
"""

import time
from datetime import datetime
from typing import Any, Optional

import jwt
import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend.config import settings
from backend.db import Database, get_db
from backend.main import app
from backend.parse import ParseError, ParseResult
from backend.storage import StorageError, StoredAudio
from backend.transcribe import Transcript, TranscriptionError

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"


def auth() -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": "authenticated",
            "role": "authenticated",
            "iss": "supabase",
            "iat": now,
            "exp": now + 3600,
            "sub": USER,
        },
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


class StubDb(Database):
    """A Database that records its calls instead of touching Postgres."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.splits: list[dict[str, Any]] = []
        self.parses: list[dict[str, Any]] = []
        self.statuses: list[tuple[str, str]] = []
        self.audio_paths: dict[str, Optional[str]] = {}
        self.next_id = 0

    def _id(self) -> str:
        self.next_id += 1
        return f"00000000-0000-4000-8000-{self.next_id:012d}"

    async def create_item(self, **kwargs: Any) -> str:
        self.created.append(kwargs)
        return self._id()

    async def create_split_item(self, **kwargs: Any) -> str:
        self.splits.append(kwargs)
        return self._id()

    async def apply_parse(self, **kwargs: Any) -> bool:
        self.parses.append(kwargs)
        return True

    async def set_parse_status(self, item_id: str, user_id: str, status: str) -> bool:
        self.statuses.append((item_id, status))
        return True

    async def item_audio_path(self, item_id: str, user_id: str) -> Optional[str]:
        return self.audio_paths.get(item_id)


def parsed(**overrides: Any) -> ParseResult:
    base = {
        "kind": "task",
        "text": "Call the bank",
        "due_at": datetime(2026, 8, 24, 9, 30),
        "critical": False,
        "project_hint": None,
        "entities": [],
        "split": False,
    }
    base.update(overrides)
    return ParseResult(**base)  # type: ignore[arg-type]


@pytest.fixture
def db() -> StubDb:
    stub = StubDb()
    app.dependency_overrides[get_db] = lambda: stub
    yield stub
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub storage, transcription and the parse. Returns a call log."""

    def _install(
        *,
        upload: Any = StoredAudio("u/2026/08/x.m4a", "audio/m4a", 12),
        transcript: Any = Transcript("call the bank", 0.92, "cloud"),
        parse_result: Any = None,
        split_result: Any = None,
    ) -> dict:
        log: dict = {"uploaded": [], "transcribed": [], "deleted": [], "split": 0}

        async def fake_upload(**kwargs):
            log["uploaded"].append(kwargs)
            if isinstance(upload, Exception):
                raise upload
            return upload

        async def fake_transcribe(data, filename="c.m4a", content_type="audio/m4a"):
            log["transcribed"].append(
                {"bytes": data, "filename": filename, "content_type": content_type}
            )
            if isinstance(transcript, Exception):
                raise transcript
            return transcript

        async def fake_parse(raw_text, now=None):
            result = parse_result if parse_result is not None else parsed()
            if isinstance(result, Exception):
                raise result
            return result

        async def fake_split(raw_text, now=None):
            log["split"] += 1
            if isinstance(split_result, Exception):
                raise split_result
            return split_result

        async def fake_delete(path):
            log["deleted"].append(path)

        monkeypatch.setattr(main_module, "upload_audio", fake_upload)
        monkeypatch.setattr(main_module, "transcribe", fake_transcribe)
        monkeypatch.setattr(main_module, "parse_capture", fake_parse)
        monkeypatch.setattr(main_module, "parse_split", fake_split)
        monkeypatch.setattr(main_module, "delete_audio", fake_delete)
        return log

    return _install


def post(client: TestClient, **form: Any):
    return client.post(
        "/capture/audio",
        files={"audio": ("capture.m4a", b"fake-audio", "audio/m4a")},
        data={"source": "voice", **form},
        headers=auth(),
    )


# --------------------------------------------------------------- the happy path


def test_a_recording_is_stored_transcribed_parsed(client, db, stub_pipeline):
    log = stub_pipeline()
    response = post(client)

    assert response.status_code == 200
    body = response.json()
    assert body["parse_status"] == "ok"
    assert body["state"] == "active"
    assert body["audio_path"] == "u/2026/08/x.m4a"
    assert body["transcript"] == "call the bank"
    assert len(log["uploaded"]) == 1
    assert len(log["transcribed"]) == 1


def test_the_row_keeps_the_audio_path_and_the_transcription_path(
    client, db, stub_pipeline
):
    """Which path produced the words is part of the row, not just the reply."""
    stub_pipeline()
    post(client)

    created = db.created[0]
    assert created["audio_path"] == "u/2026/08/x.m4a"
    assert created["transcript_source"] == "cloud"
    assert created["transcript_confidence"] == pytest.approx(0.92)
    # D13: the row is written pessimistic and promoted by the parse.
    assert created["parse_status"] == "failed"


def test_the_transcriber_gets_the_resolved_name_and_canonical_type(
    client, db, stub_pipeline
):
    """Not the client's spelling: the extension is what tells the transcriber
    how to decode the audio, and `audio/m4a` is not a registered type."""
    log = stub_pipeline(
        upload=StoredAudio("u/2026/08/20260823T080848-abc.m4a", "audio/mp4", 12)
    )
    post(client)

    sent = log["transcribed"][0]
    assert sent["filename"] == "20260823T080848-abc.m4a"
    assert sent["content_type"] == "audio/mp4"


def test_an_on_device_transcript_skips_the_cloud(client, db, stub_pipeline):
    log = stub_pipeline()
    response = post(client, transcript="call the bank", transcript_confidence="0.8")

    assert log["transcribed"] == []
    assert response.json()["transcript_source"] == "on_device"
    assert db.created[0]["transcript_source"] == "on_device"


def test_the_capture_is_marked_as_voice(client, db, stub_pipeline):
    stub_pipeline()
    post(client)
    assert db.created[0]["source"] == "voice"


def test_a_bad_source_is_refused(client, db, stub_pipeline):
    stub_pipeline()
    assert post(client, source="carrier-pigeon").status_code == 400


# ---------------------------------------------------------------- UC42 failures


def test_a_failed_upload_is_a_503_and_writes_nothing(client, db, stub_pipeline):
    """The file is still on the device. Answering "saved" would be a lie."""
    stub_pipeline(upload=StorageError("bucket is gone"))
    response = post(client)

    assert response.status_code == 503
    assert "still on your device" in response.json()["detail"]
    assert db.created == []


def test_a_failed_transcription_keeps_the_audio_and_the_row(client, db, stub_pipeline):
    stub_pipeline(transcript=TranscriptionError("groq is down"))
    response = post(client)
    body = response.json()

    assert response.status_code == 200
    assert body["parse_status"] == "failed"
    assert body["audio_path"] == "u/2026/08/x.m4a"
    assert body["transcript"] is None
    # 'none' is distinct from a cloud attempt that produced words.
    assert body["transcript_source"] == "none"
    assert db.created[0]["audio_path"] == "u/2026/08/x.m4a"
    assert db.created[0]["raw_text"] == ""


def test_a_failed_parse_keeps_the_audio_and_the_words(client, db, stub_pipeline):
    stub_pipeline(parse_result=ParseError("model returned nonsense"))
    response = post(client)
    body = response.json()

    assert response.status_code == 200
    assert body["parse_status"] == "failed"
    assert body["transcript"] == "call the bank"
    assert db.created[0]["raw_text"] == "call the bank"


def test_orphaned_audio_is_cleaned_up_when_the_row_will_not_write(
    client, db, stub_pipeline, monkeypatch
):
    """An object no row points at is just a bill."""
    log = stub_pipeline()

    async def boom(**kwargs):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(db, "create_item", boom)
    response = post(client)

    assert response.status_code == 500
    assert log["deleted"] == ["u/2026/08/x.m4a"]


# ------------------------------------------------------- low-confidence review


def test_a_low_confidence_transcript_is_flagged_not_dropped(client, db, stub_pipeline):
    """D13 reserved `needs_review` for exactly this and never had a use."""
    stub_pipeline(transcript=Transcript("mumbled and distant", 0.2, "cloud"))
    response = post(client)

    assert response.json()["parse_status"] == "needs_review"
    assert db.statuses and db.statuses[0][1] == "needs_review"


def test_a_confident_transcript_is_not_flagged(client, db, stub_pipeline):
    stub_pipeline()
    assert post(client).json()["parse_status"] == "ok"
    assert db.statuses == []


# ----------------------------------------------------------------- UC4 splits


def test_no_split_flag_means_no_second_call(client, db, stub_pipeline):
    """The common path stays one model call — that is the cost rule."""
    log = stub_pipeline()
    post(client)
    assert log["split"] == 0


def test_a_split_writes_one_row_per_item_sharing_the_audio(client, db, stub_pipeline):
    stub_pipeline(
        parse_result=parsed(split=True),
        split_result=[
            parsed(text="Call the bank"),
            parsed(text="Ravi liked the office", kind="note", due_at=None),
        ],
    )
    body = post(client).json()

    assert body["split"] is True
    assert [i["text"] for i in body["items"]] == [
        "Call the bank",
        "Ravi liked the office",
    ]
    # The head reuses the row already written; only the tail is inserted.
    assert len(db.splits) == 1
    assert db.splits[0]["audio_path"] == "u/2026/08/x.m4a"
    assert db.splits[0]["transcript_source"] == "cloud"


def test_split_siblings_each_get_their_own_state(client, db, stub_pipeline):
    stub_pipeline(
        parse_result=parsed(split=True),
        split_result=[parsed(), parsed(kind="note", due_at=None)],
    )
    body = post(client).json()
    assert [i["state"] for i in body["items"]] == ["active", "shelved"]


def test_every_sibling_keeps_the_whole_transcript(client, db, stub_pipeline):
    """UC38 edits against what was said and UC34 searches it; a fragment the
    user never spoke serves neither."""
    stub_pipeline(
        parse_result=parsed(split=True),
        split_result=[parsed(), parsed(text="second")],
    )
    post(client)
    assert db.splits[0]["raw_text"] == "call the bank"


def test_a_failed_split_degrades_to_one_item(client, db, stub_pipeline):
    stub_pipeline(
        parse_result=parsed(split=True),
        split_result=ParseError("split reply was truncated"),
    )
    body = post(client).json()

    assert body["parse_status"] == "ok"
    assert body["split"] is False
    assert len(body["items"]) == 1
    assert db.splits == []


def test_text_capture_splits_too(client, db, stub_pipeline):
    """Nothing about UC4 is specific to voice."""
    stub_pipeline(
        parse_result=parsed(split=True),
        split_result=[parsed(), parsed(text="second", due_at=None)],
    )
    response = client.post(
        "/capture",
        json={"text": "two things at once", "source": "text"},
        headers=auth(),
    )
    body = response.json()

    assert body["split"] is True
    assert len(body["items"]) == 2
    # A typed capture has no recording to share.
    assert db.splits[0]["audio_path"] is None


# ------------------------------------------------------------ playback (UC7)


def test_audio_url_is_signed_for_an_item_that_has_one(client, db, monkeypatch):
    item_id = "00000000-0000-4000-8000-000000000001"
    db.audio_paths[item_id] = "u/2026/08/x.m4a"

    async def fake_sign(path, expires_in=None):
        return f"https://sb.test/signed/{path}"

    monkeypatch.setattr(main_module, "signed_url", fake_sign)
    response = client.get(f"/items/{item_id}/audio", headers=auth())

    assert response.status_code == 200
    assert response.json()["url"].endswith("u/2026/08/x.m4a")


def test_audio_url_is_404_for_an_item_without_a_recording(client, db):
    response = client.get(
        "/items/00000000-0000-4000-8000-000000000009/audio", headers=auth()
    )
    assert response.status_code == 404


def test_audio_url_requires_a_token(client, db):
    """The bucket holds the user's voice; the item id is not the credential."""
    response = client.get("/items/00000000-0000-4000-8000-000000000001/audio")
    assert response.status_code == 401
