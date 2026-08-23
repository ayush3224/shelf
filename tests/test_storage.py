"""Audio storage tests (UC7, UC42).

The recording is the one part of a capture that cannot be reproduced, so the
interesting cases here are the refusals: what this module declines to store,
and what it does when the store says no.
"""

import re

import httpx
import pytest

from backend.config import settings
from backend.storage import (
    MAX_AUDIO_BYTES,
    StorageError,
    audio_key,
    canonical_content_type,
    delete_audio,
    extension_for,
    signed_url,
    sniff_extension,
    upload_audio,
)

# The first bytes of a real capture off the phone: an `ftyp` box declaring
# `mp42`, i.e. AAC in an MPEG-4 container. The device declared this
# `audio/mpeg`, which is what stored it as `.mp3`.
REAL_M4A = bytes.fromhex("00000018667479706d70343200000000") + b"\x00" * 48

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://sb.test")
    monkeypatch.setattr(settings, "supabase_service_key", "service-key")
    monkeypatch.setattr(settings, "supabase_storage_bucket", "shelf-audio")


@pytest.fixture
def stub_http(monkeypatch):
    """Stub one httpx verb; return a dict recording the request."""

    def _install(verb="post", *, status=200, json_body=None, raises=False):
        seen: dict = {}

        class FakeResponse:
            status_code = status
            text = "denied"

            def json(self):
                return json_body

        async def fake(self, url, **kwargs):
            seen.update({"url": url, **kwargs})
            if raises:
                raise httpx.ConnectError("unreachable")
            return FakeResponse()

        monkeypatch.setattr(httpx.AsyncClient, verb, fake)
        return seen

    return _install


# ------------------------------------------------------------------ extensions


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("audio/m4a", ".m4a"),
        ("audio/mp4", ".m4a"),
        ("audio/aac", ".aac"),
        ("audio/wav", ".wav"),
        ("audio/webm", ".webm"),
        ("audio/m4a; codecs=mp4a.40.2", ".m4a"),
        ("AUDIO/M4A", ".m4a"),
    ],
)
def test_known_audio_types_map_to_an_extension(content_type, expected):
    assert extension_for(content_type) == expected


# ---------------------------------------------------------------- sniffing


def test_a_real_capture_is_recognised_as_m4a():
    """The bytes the phone actually sent. Android reported them as
    `audio/mpeg`; the container says otherwise and the container is right."""
    assert sniff_extension(REAL_M4A) == ".m4a"


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16, ".m4a"),
        (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 16, ".m4a"),
        (b"RIFF\x24\x08\x00\x00WAVEfmt ", ".wav"),
        (b"OggS\x00\x02\x00\x00\x00\x00\x00\x00", ".ogg"),
        (b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f", ".webm"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00", ".mp3"),
        (b"\xff\xfb\x90\x00" + b"\x00" * 12, ".mp3"),
    ],
)
def test_known_containers_are_identified(data, expected):
    assert sniff_extension(data) == expected


@pytest.mark.parametrize("data", [b"", b"short", b"not audio at all!!!!"])
def test_unrecognised_bytes_return_none(data):
    """None means "no opinion", so the caller falls back to the labels."""
    assert sniff_extension(data) is None


# --------------------------------------------------- resolution precedence


def test_the_bytes_beat_a_wrong_content_type():
    """The bug this fixes: Android's MimeTypeMap maps `m4a` to `audio/mpeg`,
    so a real AAC capture arrived claiming to be an MP3 and was stored as
    `.mp3` under `audio/mpeg` — which the bucket accepts, so nothing errored."""
    assert extension_for("audio/mpeg", "recording-abc.m4a", REAL_M4A) == ".m4a"
    assert canonical_content_type("audio/mpeg", "recording-abc.m4a", REAL_M4A) == (
        "audio/mp4"
    )


def test_the_filename_beats_a_wrong_content_type_when_there_are_no_bytes():
    """Belt and braces: the filename is written by whoever made the file."""
    assert extension_for("audio/mpeg", "recording-abc.m4a") == ".m4a"


def test_a_genuine_mp3_is_still_stored_as_one():
    """Precedence must not turn into "always m4a"."""
    mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 8
    assert extension_for("audio/mpeg", "voice.mp3", mp3) == ".mp3"
    assert canonical_content_type("audio/mpeg", "voice.mp3", mp3) == "audio/mpeg"


def test_the_content_type_is_still_the_last_resort():
    assert extension_for("audio/wav", "blob", b"unrecognised bytes!!") == ".wav"


async def test_upload_stores_a_real_capture_as_m4a(stub_http):
    """End to end through upload_audio, with the phone's own labels."""
    seen = stub_http("post")
    stored = await upload_audio(USER, REAL_M4A, "audio/mpeg", "recording-abc.m4a")

    assert stored.path.endswith(".m4a")
    assert stored.content_type == "audio/mp4"
    assert seen["headers"]["Content-Type"] == "audio/mp4"


# ------------------------------------------------------- canonical MIME types


@pytest.mark.parametrize(
    "reported,stored",
    [
        ("audio/m4a", "audio/mp4"),
        ("audio/x-m4a", "audio/mp4"),
        ("audio/mp4", "audio/mp4"),
        ("audio/x-wav", "audio/wav"),
        ("audio/aac", "audio/aac"),
        ("AUDIO/M4A; codecs=x", "audio/mp4"),
    ],
)
def test_the_stored_type_is_the_standard_spelling(reported, stored):
    """The bucket's allow-list is written in standard names, and an upload
    declaring `audio/m4a` — which is what Android reports — comes back 415."""
    assert canonical_content_type(reported) == stored


async def test_upload_sends_the_canonical_type_not_the_reported_one(stub_http):
    seen = stub_http("post")
    stored = await upload_audio(USER, b"bytes", "audio/m4a", "capture.m4a")

    assert seen["headers"]["Content-Type"] == "audio/mp4"
    assert stored.content_type == "audio/mp4"
    # The extension is unaffected: the file really is a .m4a.
    assert stored.path.endswith(".m4a")


def test_the_size_cap_matches_the_bucket():
    """A server guard the store then overrules turns "too long" into an opaque
    storage error instead of a clear one."""
    assert MAX_AUDIO_BYTES == 10 * 1024 * 1024


def test_an_unknown_type_falls_back_to_the_filename():
    assert extension_for("application/octet-stream", "capture.m4a") == ".m4a"


def test_a_format_the_bucket_cannot_hold_is_refused_at_the_edge():
    """`.3gp` used to be waved through on the filename. The bucket rejects
    audio/3gpp, so accepting it here only moved the failure somewhere the user
    could not act on."""
    with pytest.raises(StorageError, match="Unsupported audio type"):
        extension_for("audio/3gpp", "capture.3gp")


def test_an_unknown_type_with_no_filename_is_refused():
    """Storing an unplayable blob is worse than refusing it at the edge."""
    with pytest.raises(StorageError, match="Unsupported audio type"):
        extension_for("application/octet-stream")


# ------------------------------------------------------------------------ keys


def test_the_key_is_partitioned_by_user_and_month():
    key = audio_key(USER, "audio/m4a")
    assert key.startswith(f"{USER}/")
    assert re.match(
        rf"^{USER}/\d{{4}}/\d{{2}}/\d{{8}}T\d{{6}}-[0-9a-f]{{8}}\.m4a$", key
    )


def test_two_keys_in_the_same_second_do_not_collide():
    """Two captures a moment apart must not overwrite one another."""
    assert audio_key(USER, "audio/m4a") != audio_key(USER, "audio/m4a")


# ---------------------------------------------------------------------- upload


async def test_upload_posts_the_bytes_and_returns_the_key(stub_http):
    seen = stub_http("post")
    stored = await upload_audio(USER, b"bytes", "audio/m4a", "capture.m4a")

    assert seen["url"] == f"https://sb.test/storage/v1/object/shelf-audio/{stored.path}"
    assert seen["content"] == b"bytes"
    assert seen["headers"]["Authorization"] == "Bearer service-key"
    # Keys carry a uuid, so an upsert would only ever hide a bug.
    assert seen["headers"]["x-upsert"] == "false"
    assert stored.size_bytes == 5


async def test_upload_refuses_an_empty_recording():
    with pytest.raises(StorageError, match="empty"):
        await upload_audio(USER, b"", "audio/m4a")


async def test_upload_refuses_an_oversized_recording():
    with pytest.raises(StorageError, match="over the"):
        await upload_audio(USER, b"x" * (MAX_AUDIO_BYTES + 1), "audio/m4a")


async def test_upload_without_configuration_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_key", "")
    with pytest.raises(StorageError, match="must be set"):
        await upload_audio(USER, b"x", "audio/m4a")


async def test_upload_surfaces_a_rejection(stub_http):
    stub_http("post", status=403)
    with pytest.raises(StorageError, match="rejected"):
        await upload_audio(USER, b"x", "audio/m4a")


async def test_upload_surfaces_a_transport_failure(stub_http):
    stub_http("post", raises=True)
    with pytest.raises(StorageError, match="Could not reach"):
        await upload_audio(USER, b"x", "audio/m4a")


# ------------------------------------------------------------------ signed URL


async def test_signed_url_is_absolute(stub_http):
    seen = stub_http("post", json_body={"signedURL": "/object/sign/shelf-audio/k?t=1"})
    url = await signed_url("k")

    assert url == "https://sb.test/storage/v1/object/sign/shelf-audio/k?t=1"
    assert seen["json"]["expiresIn"] == settings.audio_url_ttl_seconds


async def test_signed_url_honours_an_explicit_ttl(stub_http):
    seen = stub_http("post", json_body={"signedURL": "/x"})
    await signed_url("k", expires_in=60)
    assert seen["json"]["expiresIn"] == 60


async def test_signed_url_accepts_the_camelcase_spelling(stub_http):
    stub_http("post", json_body={"signedUrl": "/object/sign/x"})
    assert await signed_url("k") == "https://sb.test/storage/v1/object/sign/x"


async def test_signed_url_fails_when_the_store_returns_no_url(stub_http):
    stub_http("post", json_body={})
    with pytest.raises(StorageError, match="no URL"):
        await signed_url("k")


# ---------------------------------------------------------------------- delete


async def test_delete_never_raises(stub_http):
    """A leaked object costs storage; a raised error here would cost a request
    that had already succeeded."""
    stub_http("delete", raises=True)
    await delete_audio("k")


async def test_delete_survives_missing_configuration(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "")
    await delete_audio("k")
