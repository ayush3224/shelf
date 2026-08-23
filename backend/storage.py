"""Supabase Storage for the raw audio of a capture (UC7, UC42).

The recording is the one artefact that cannot be regenerated. A transcript can
be redone and a parse can be retried, but if the audio is gone the capture is
gone — so this module is deliberately dull: upload, sign, delete, and honest
errors when any of those fail.

Signed URLs rather than a public bucket: the bucket holds the user's voice.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# Uploading is on the capture path, so it cannot hang. Generous enough for a
# minute of audio on a bad connection, short enough to fail while the user is
# still looking at the screen.
_UPLOAD_TIMEOUT_SECONDS = 30.0
_SIGN_TIMEOUT_SECONDS = 10.0

# What the recorder produces, plus what a future widget might. Anything else is
# refused at the edge rather than stored as an unplayable blob.
#
# The extension is the source of truth and `EXTENSION_CONTENT_TYPES` decides
# what MIME type is actually sent, because clients and buckets disagree about
# `.m4a`: Android reports `audio/m4a`, the bucket's allow-list only knows the
# standard `audio/mp4`, and an upload declaring the former comes back 415
# `invalid_mime_type`. Deriving the stored type from the extension means the
# client can report whichever spelling its platform prefers, and means the two
# tables cannot drift apart.
ALLOWED_CONTENT_TYPES = {
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
}

# The spelling an object is stored under, keyed by extension. Standard names
# only — the bucket's allow-list is written in standard names. This is also
# the whole set of formats the API accepts: guessing beyond it (`mimetypes`
# happily resolves `audio/3gpp`) only moves the refusal from a clear 400 here
# to an opaque storage error the user cannot act on.
EXTENSION_CONTENT_TYPES = {
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}

# Matches the `shelf-audio` bucket's own `file_size_limit`, and is the binding
# limit of the three: Groq's free tier caps uploads at 25MB and MAX_RECORDING_MS
# caps a capture at two minutes, which is under 2MB of AAC. Deliberately not
# looser than the bucket — a server guard the store then overrules turns "too
# long" into an opaque storage error.
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def sniff_extension(data: bytes) -> Optional[str]:
    """Identify an audio format from its leading bytes.

    Preferred over both labels a client sends, because both have been observed
    lying. Android derives a file's MIME from `MimeTypeMap`, whose table maps
    `m4a` to `audio/mpeg` — so a perfectly good AAC recording arrives declaring
    itself an MP3, and trusting that stored it under `.mp3` with an
    `audio/mpeg` content type. The bucket accepts `audio/mpeg`, so nothing
    errored; the file simply lied about itself from then on.

    The container is not a matter of opinion, so it is read rather than asked
    about.

    Args:
        data: At least the first 16 bytes of the file.

    Returns:
        An extension including the leading dot, or None if unrecognised.
    """
    if len(data) < 12:
        return None

    # ISO base media: a `ftyp` box at offset 4. Covers .m4a, .mp4 and .3gp,
    # all of which the recorder can produce as an MPEG-4 container.
    if data[4:8] == b"ftyp":
        return ".m4a"
    if data[0:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[0:4] == b"OggS":
        return ".ogg"
    # EBML, i.e. WebM or Matroska.
    if data[0:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if data[0:3] == b"ID3":
        return ".mp3"
    # A bare MPEG audio frame: 11 sync bits.
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    # ADTS AAC shares the 0xFF sync, so it is checked after and only on the
    # narrower pattern.
    if data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return ".aac"
    return None


class StorageError(Exception):
    """The object store refused or could not be reached."""


@dataclass(frozen=True)
class StoredAudio:
    """Where a recording landed."""

    path: str
    content_type: str
    size_bytes: int


def _configured() -> tuple[str, str]:
    """Base URL and service key, or raise.

    Raises:
        StorageError: If Supabase Storage is not configured.
    """
    base = settings.supabase_url.rstrip("/")
    key = settings.supabase_service_key
    if not base or not key:
        raise StorageError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set to store audio"
        )
    return base, key


def extension_for(
    content_type: str,
    filename: Optional[str] = None,
    data: Optional[bytes] = None,
) -> str:
    """File extension to store a recording under.

    Resolved in order of how much each source can be trusted:

    1. **The bytes.** The container is not a matter of opinion.
    2. **The filename.** Written by whoever produced the file, so it knows what
       it wrote.
    3. **The declared content type.** Last, because on Android it is a
       `MimeTypeMap` lookup that returns `audio/mpeg` for `.m4a` — the label
       that had real AAC captures stored as `.mp3`.

    Args:
        content_type: MIME type the client declared.
        filename: Original filename.
        data: Leading bytes of the file, if available.

    Returns:
        An extension including the leading dot.

    Raises:
        StorageError: If the format is not one that can be stored.
    """
    if data:
        sniffed = sniff_extension(data)
        if sniffed:
            return sniffed

    if filename and "." in filename:
        suffix = "." + filename.rsplit(".", 1)[1].lower()
        if suffix in EXTENSION_CONTENT_TYPES:
            return suffix

    normalised = (content_type or "").split(";")[0].strip().lower()
    if normalised in ALLOWED_CONTENT_TYPES:
        return ALLOWED_CONTENT_TYPES[normalised]

    raise StorageError(
        f"Unsupported audio type: {content_type!r}"
        + (f" (filename {filename!r})" if filename else "")
    )


def canonical_content_type(
    content_type: str,
    filename: Optional[str] = None,
    data: Optional[bytes] = None,
) -> str:
    """The MIME type an upload should declare.

    Derived from the resolved extension rather than echoed back, so that a
    client reporting `audio/m4a` — which is what Android does, and which is not
    a registered type — still stores an object the bucket accepts.

    Args:
        content_type: MIME type the client declared.
        filename: Original filename.
        data: Leading bytes of the file, if available.

    Returns:
        The standard MIME type for the resolved format.

    Raises:
        StorageError: If the format is not one that can be stored.
    """
    return EXTENSION_CONTENT_TYPES[extension_for(content_type, filename, data)]


def audio_key(
    user_id: str,
    content_type: str,
    filename: Optional[str] = None,
    data: Optional[bytes] = None,
) -> str:
    """Build the storage key for one recording.

    Partitioned by user and month so the bucket stays listable by hand, and
    suffixed with a fresh uuid so two captures in the same second cannot
    collide. The key is opaque to everything except this module.

    Args:
        user_id: Owner of the recording.
        content_type: MIME type the client declared.
        filename: Original filename, used only to recover an extension.

    Returns:
        A bucket-relative object key.
    """
    stamp = datetime.now(timezone.utc)
    return (
        f"{user_id}/{stamp:%Y/%m}/{stamp:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
        f"{extension_for(content_type, filename, data)}"
    )


async def upload_audio(
    user_id: str,
    data: bytes,
    content_type: str,
    filename: Optional[str] = None,
) -> StoredAudio:
    """Store one recording and return its key.

    Args:
        user_id: Owner of the recording.
        data: Raw bytes of the audio file.
        content_type: MIME type the client declared.
        filename: Original filename, used only to recover an extension.

    Returns:
        The stored key, the content type it was stored under (canonicalised,
        so not necessarily the one passed in), and its size.

    Raises:
        StorageError: If the audio is empty, too large, of an unsupported type,
            or the store rejected the write.
    """
    if not data:
        raise StorageError("Refusing to store an empty recording")
    if len(data) > MAX_AUDIO_BYTES:
        raise StorageError(
            f"Recording is {len(data)} bytes, over the {MAX_AUDIO_BYTES} limit"
        )

    base, key = _configured()
    path = audio_key(user_id, content_type, filename, data)
    stored_type = canonical_content_type(content_type, filename, data)
    bucket = settings.supabase_storage_bucket

    try:
        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base}/storage/v1/object/{bucket}/{path}",
                content=data,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": stored_type,
                    # Keys carry a uuid, so a collision is a bug, not a retry.
                    "x-upsert": "false",
                },
            )
    except httpx.HTTPError as e:
        raise StorageError(f"Could not reach storage: {e}") from e

    if response.status_code >= 400:
        raise StorageError(
            f"Storage rejected the upload ({response.status_code}): {response.text}"
        )

    return StoredAudio(path=path, content_type=stored_type, size_bytes=len(data))


async def signed_url(path: str, expires_in: Optional[int] = None) -> str:
    """Mint a time-limited URL for playback (UC7).

    Args:
        path: Bucket-relative object key, as stored on `items.audio_path`.
        expires_in: Lifetime in seconds; defaults to the configured TTL.

    Returns:
        An absolute, time-limited URL.

    Raises:
        StorageError: If the store could not be reached or refused to sign.
    """
    base, key = _configured()
    bucket = settings.supabase_storage_bucket
    ttl = expires_in if expires_in is not None else settings.audio_url_ttl_seconds

    try:
        async with httpx.AsyncClient(timeout=_SIGN_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base}/storage/v1/object/sign/{bucket}/{path}",
                json={"expiresIn": ttl},
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as e:
        raise StorageError(f"Could not reach storage: {e}") from e

    if response.status_code >= 400:
        raise StorageError(
            f"Storage would not sign ({response.status_code}): {response.text}"
        )

    body = response.json()
    relative = body.get("signedURL") or body.get("signedUrl")
    if not relative:
        raise StorageError("Storage signed the object but returned no URL")

    return f"{base}/storage/v1{relative}" if relative.startswith("/") else relative


async def delete_audio(path: str) -> None:
    """Remove a recording. Best effort — used when a capture is abandoned.

    A leaked object costs storage; a deleted one that should have been kept
    costs the capture. So a failure here is logged, never raised.

    Args:
        path: Bucket-relative object key.
    """
    try:
        base, key = _configured()
        bucket = settings.supabase_storage_bucket
        async with httpx.AsyncClient(timeout=_SIGN_TIMEOUT_SECONDS) as client:
            await client.delete(
                f"{base}/storage/v1/object/{bucket}/{path}",
                headers={"Authorization": f"Bearer {key}"},
            )
    except (StorageError, httpx.HTTPError) as e:
        logger.warning("Could not delete orphaned audio %s: %s", path, e)
