"""Shelf API service."""

import base64
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from psycopg.errors import ForeignKeyViolation
from pydantic import BaseModel, Field

from backend.auth import PUBLIC_PATHS, authenticate, current_user_id
from backend.config import capture_tz, settings
from backend.db import Database, close_db, get_db, init_db
from backend.parse import ParseError, ParseResult, parse_capture, parse_split
from backend.push import is_expo_token
from backend.storage import StorageError, delete_audio, signed_url, upload_audio
from backend.transcribe import Transcript, TranscriptionError, transcribe

logger = logging.getLogger(__name__)


class CaptureRequest(BaseModel):
    """Request body for /capture endpoint."""

    text: str = Field(..., min_length=1, description="Captured text")
    source: str = Field(
        ..., description="Source of capture: 'voice', 'text', or 'widget'"
    )


class HealthResponse(BaseModel):
    """Response for /health endpoint."""

    status: str
    db_connected: bool


class CaptureResponse(BaseModel):
    """Response for /capture endpoint.

    `text` is persisted as `items.parsed_text` (migration 002). `entities` are
    now stored as well — they are resolved to `entities` rows and linked to the
    item (UC45), and echoed here as the parse produced them. `project_hint`
    remains returned-but-unstored: UC11 was dropped.
    """

    id: str
    status: str
    parse_status: str
    state: str
    kind: str
    due_at: Optional[datetime] = None
    critical: bool = False
    text: Optional[str] = None
    project_hint: Optional[str] = None
    entities: list[dict[str, str]] = Field(default_factory=list)
    split: bool = False
    items: list["CapturedItem"] = Field(default_factory=list)


class CapturedItem(BaseModel):
    """One item a capture produced. A split (UC4) produces several."""

    id: str
    state: str
    kind: str
    text: Optional[str] = None
    due_at: Optional[datetime] = None
    critical: bool = False


class AudioCaptureResponse(BaseModel):
    """Response for POST /capture/audio.

    `items` is the whole truth — one entry normally, several after a split
    (UC4). The flat fields describe `items[0]` and exist so a client that
    predates splitting keeps working.
    """

    id: str
    status: str
    parse_status: str
    state: str
    kind: str
    due_at: Optional[datetime] = None
    critical: bool = False
    text: Optional[str] = None
    audio_path: Optional[str] = None
    transcript: Optional[str] = None
    transcript_source: str = "none"
    transcript_confidence: Optional[float] = None
    split: bool = False
    items: list[CapturedItem] = Field(default_factory=list)


class AudioUrlResponse(BaseModel):
    """Response for GET /items/{item_id}/audio (UC7)."""

    id: str
    url: str
    expires_in: int


class ItemDetail(BaseModel):
    """One item in full (UC37, UC38).

    Carries both texts: `text` is what is displayed and edited, `raw_text` is
    the transcript it was derived from. UC38 needs to show the second to make
    sense of a bad first (D14).
    """

    id: str
    text: str
    raw_text: str
    parsed_text: Optional[str] = None
    kind: str
    state: str
    due_at: Optional[datetime] = None
    critical: bool
    parse_status: str
    source: str
    has_audio: bool
    transcript_source: str
    transcript_confidence: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class ItemUpdate(BaseModel):
    """Body for PATCH /items/{item_id} (UC38).

    Both fields are optional and absence means "leave it". `due_at: null` is
    therefore different from omitting `due_at`: the first clears the time, the
    second does not touch it. `model_fields_set` is what tells them apart.
    """

    text: Optional[str] = Field(default=None, min_length=1)
    due_at: Optional[datetime] = None


class StateChange(BaseModel):
    """Body for POST /items/{item_id}/state (UC21)."""

    state: str


class StateResponse(BaseModel):
    """Response for a hand-made state move (UC21)."""

    id: str
    state: str
    previous: str
    changed: bool


class DeleteResponse(BaseModel):
    """Response for DELETE /items/{item_id} (UC39)."""

    id: str
    deleted: bool
    audio_deleted: bool


class TodayItem(BaseModel):
    """One row of the `Today` list.

    `text` is the parse's cleaned description, falling back to `raw_text`
    when the parse failed (D14). Both are sent: the app shows `text` and
    needs `raw_text` to show what was actually said on a flagged item (UC42).
    """

    id: str
    text: str
    raw_text: str
    kind: str
    state: str
    due_at: datetime
    critical: bool
    parse_status: str
    overdue: bool
    has_audio: bool = False


class TodayResponse(BaseModel):
    """Response for GET /items/today."""

    as_of: datetime
    items: list[TodayItem]


class ShelfItem(BaseModel):
    """One row of the Shelf list (UC33).

    Carries `project_id` and `project_name` because the client groups by
    project rather than the server sectioning the response: the list is
    keyset-paginated, so a group can span pages, and a response shaped as
    sections would have to either break a group at the page boundary or
    abandon the pagination.
    """

    id: str
    text: str
    raw_text: str
    kind: str
    state: str
    due_at: Optional[datetime] = None
    critical: bool
    parse_status: str
    has_audio: bool = False
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    created_at: datetime
    state_changed_at: datetime


class ShelfResponse(BaseModel):
    """Response for GET /items — one page of the Shelf.

    `next_cursor` is opaque and is the only thing a caller should use to ask
    for the next page. It is null exactly when `has_more` is false.
    """

    items: list[ShelfItem]
    next_cursor: Optional[str] = None
    has_more: bool = False
    states: list[str]


class ProjectSummary(BaseModel):
    """One project, for the filter chips (UC36)."""

    id: str
    name: str
    slug: str
    items: int


class ProjectsResponse(BaseModel):
    """Response for GET /projects.

    Normally empty: UC11 was dropped, so nothing sets `project_id` on its own
    and a project only exists if one was entered by hand.
    """

    projects: list[ProjectSummary]


class Person(BaseModel):
    """One person, for the People list and the top of their page (UC46, UC47)."""

    id: str
    name: str
    type: str
    #: Other names the same person goes by. This is what makes a bare "Priya"
    #: keep landing on the row that got renamed to "Priya Sharma".
    aliases: list[str] = Field(default_factory=list)
    mentions: int = 0
    last_mentioned: Optional[datetime] = None


class PeopleResponse(BaseModel):
    """Response for GET /people (UC47)."""

    people: list[Person]


class PersonItem(BaseModel):
    """One thing that was said about somebody (UC46)."""

    id: str
    text: str
    raw_text: str
    kind: str
    state: str
    due_at: Optional[datetime] = None
    critical: bool
    parse_status: str
    has_audio: bool = False
    created_at: datetime


class PersonResponse(BaseModel):
    """Response for GET /people/{id} — the person and one page of their notes."""

    person: Person
    items: list[PersonItem]
    next_cursor: Optional[str] = None
    has_more: bool = False


class DoneResponse(BaseModel):
    """Response for POST /items/{item_id}/done."""

    id: str
    state: str
    changed: bool


class DeviceRegistration(BaseModel):
    """Body for POST /devices — where this user's pushes should go (UC23)."""

    token: str = Field(..., description="Expo push token from the device")
    platform: str = Field(default="android", description="android, ios or web")
    device_name: Optional[str] = Field(
        default=None, description="Free text, for telling two devices apart"
    )


class DeviceResponse(BaseModel):
    """Response for POST /devices."""

    registered: bool
    devices: int


class SnoozeRequest(BaseModel):
    """Body for POST /items/{item_id}/snooze (UC17). Empty means the default."""

    minutes: Optional[int] = Field(
        default=None, gt=0, description="How far out to push the due time"
    )


class SnoozeResponse(BaseModel):
    """Response for a snooze.

    `changed` is False when the item was not `active` — a notification acted
    on after the item has already decayed. Not an error; the app says where
    the item actually went.
    """

    id: str
    state: str
    due_at: Optional[datetime] = None
    snooze_count: int
    changed: bool


class ReactivateRequest(BaseModel):
    """Body for POST /items/{item_id}/reactivate (UC20). Empty is the usual."""

    due_at: Optional[datetime] = Field(
        default=None, description="When it should come back; defaults to now"
    )


class ReactivateResponse(BaseModel):
    """Response for a reactivation."""

    id: str
    state: str
    previous: str
    due_at: Optional[datetime] = None
    changed: bool


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore
    """Manage app startup and shutdown."""
    await init_db()
    yield
    await close_db()


# Docs and the schema are off: this is a single-user private API, and an
# unauthenticated endpoint describing every route is not worth having.
app = FastAPI(
    title="Shelf API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def require_auth(request: Request, call_next):  # type: ignore
    """Reject anything without a valid Supabase token, except PUBLIC_PATHS (UC41)."""
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    try:
        request.state.user_id = authenticate(request)
    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": e.detail},
            headers=e.headers or {},
        )

    return await call_next(request)


@app.get("/health", response_model=HealthResponse)
async def health(db: Database = Depends(get_db)) -> HealthResponse:
    """Check API and database connectivity. Unauthenticated by design."""
    try:
        pool = await db._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        return HealthResponse(status="ok", db_connected=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {str(e)}")


SOURCES = ("voice", "text", "widget")
STATES = ("active", "shelved", "done", "dropped")
PLATFORMS = ("android", "ios", "web")


def _require_source(source: str) -> str:
    """Validate the capture source.

    Args:
        source: Value the client sent.

    Returns:
        The source, unchanged.

    Raises:
        HTTPException: 400 if it is not one of SOURCES.
    """
    if source not in SOURCES:
        raise HTTPException(
            status_code=400, detail="source must be 'voice', 'text', or 'widget'"
        )
    return source


def _captured(item_id: str, parsed: ParseResult) -> CapturedItem:
    """One item of a capture response."""
    return CapturedItem(
        id=item_id,
        state=parsed.state,
        kind=parsed.kind,
        text=parsed.text,
        due_at=parsed.due_at,
        critical=parsed.critical,
    )


async def _write_split(
    db: Database,
    user_id: str,
    item_id: str,
    raw_text: str,
    source: str,
    audio_path: Optional[str] = None,
    transcript_source: str = "none",
    transcript_confidence: Optional[float] = None,
) -> list[CapturedItem]:
    """Re-prompt for the separate items and write a row for each (UC4).

    The first item lands on the row `create_item` already wrote before the
    model call (D6); the rest are inserted alongside it, every one carrying the
    same `audio_path` so a recording is playable from any of its items (UC7).

    Args:
        db: Database connection.
        user_id: Authenticated user.
        item_id: The row already written for this capture.
        raw_text: The transcript the first parse saw.
        source: 'voice', 'text' or 'widget'.
        audio_path: Storage key shared by every sibling, if this was a voice
            capture.
        transcript_source: Shared by every sibling.
        transcript_confidence: Shared by every sibling.

    Returns:
        One entry per written item, first entry first. Empty if the split
        failed — the caller then keeps the single parse it already has, which
        is a worse answer but never a lost capture (UC42).
    """
    try:
        parts = await parse_split(raw_text)
    except ParseError as e:
        logger.warning("Split failed for item %s, keeping one item: %s", item_id, e)
        return []

    if len(parts) == 1:
        # The model set `split` and then thought better of it. Nothing to do
        # beyond the ordinary single-item path.
        logger.info("Split for item %s returned one item", item_id)

    try:
        await db.apply_parse(
            item_id=item_id,
            user_id=user_id,
            kind=parts[0].kind,
            parsed_text=parts[0].text,
            due_at=parts[0].due_at,
            critical=parts[0].critical,
            state=parts[0].state,
        )
    except Exception as e:
        logger.warning("Could not store split head for item %s: %s", item_id, e)
        return []

    written = [_captured(item_id, parts[0])]
    await _link_people(db, user_id, item_id, parts[0].entities)

    for part in parts[1:]:
        try:
            sibling_id = await db.create_split_item(
                user_id=user_id,
                raw_text=raw_text,
                source=source,
                kind=part.kind,
                parsed_text=part.text,
                due_at=part.due_at,
                critical=part.critical,
                state=part.state,
                audio_path=audio_path,
                transcript_source=transcript_source,
                transcript_confidence=transcript_confidence,
            )
        except Exception as e:
            # A sibling that will not write is one lost item, not a lost
            # capture: the head row already holds the whole transcript.
            logger.warning("Could not write a split sibling of %s: %s", item_id, e)
            continue
        written.append(_captured(sibling_id, part))
        await _link_people(db, user_id, sibling_id, part.entities)

    return written


async def _link_people(
    db: Database, user_id: str, item_id: str, entities: list[dict[str, str]]
) -> None:
    """Attach a written item to the people it named (UC45).

    Enrichment, never a gate — the same rule the parse itself follows (D6,
    UC42). A capture whose people cannot be resolved is a capture with the
    words still in it; failing the request here would trade the thing that
    cannot be reproduced for the thing that can be recomputed later.

    Args:
        db: Database connection.
        user_id: Owner.
        item_id: The item just written.
        entities: `{type, name}` from the parse.
    """
    if not entities:
        return
    try:
        linked = await db.link_entities(user_id, item_id, entities)
    except Exception as e:
        logger.warning("Could not link people for item %s: %s", item_id, e)
        return

    # An ambiguous name got its own row rather than being guessed onto an
    # existing one. Worth a line in the log: it is the case a human may want
    # to reconcile, and nothing on the screen announces it (O6).
    for entry in linked:
        if entry.get("ambiguous"):
            logger.info(
                "Ambiguous entity %r on item %s — filed under its own row %s",
                entry.get("name"),
                item_id,
                entry.get("id"),
            )


@app.post("/capture", response_model=CaptureResponse)
async def capture(
    request: CaptureRequest,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> CaptureResponse:
    """Capture text, store it, then enrich it with one Haiku parse.

    The row is written before the model call (D6): parsing is enrichment,
    never a gate. A failed parse keeps the row with `parse_status = 'failed'`
    so the capture is never lost (UC42).

    Args:
        request: Contains text and source
        db: Database connection
        user_id: Authenticated user, from the Supabase token

    Returns:
        Created item ID, its resulting state, and the parse.
    """
    _require_source(request.source)

    try:
        item_id = await db.create_item(
            user_id=user_id,
            raw_text=request.text,
            source=request.source,
            parse_status="failed",
        )
    except ForeignKeyViolation:
        # Signature was good but the subject is not a Supabase user.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown user",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        logger.error("Failed to create item for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to create item")

    try:
        parsed = await parse_capture(request.text)
    except ParseError as e:
        logger.warning("Parse failed for item %s: %s", item_id, e)
        return CaptureResponse(
            id=item_id,
            status="captured",
            parse_status="failed",
            state="shelved",
            kind="task",
        )

    written: list[CapturedItem] = []
    if parsed.split:
        written = await _write_split(
            db=db,
            user_id=user_id,
            item_id=item_id,
            raw_text=request.text,
            source=request.source,
        )

    if not written:
        try:
            await db.apply_parse(
                item_id=item_id,
                user_id=user_id,
                kind=parsed.kind,
                parsed_text=parsed.text,
                due_at=parsed.due_at,
                critical=parsed.critical,
                state=parsed.state,
            )
        except Exception as e:
            logger.warning("Could not store parse for item %s: %s", item_id, e)
            return CaptureResponse(
                id=item_id,
                status="captured",
                parse_status="failed",
                state="shelved",
                kind="task",
            )
        written = [_captured(item_id, parsed)]
        await _link_people(db, user_id, item_id, parsed.entities)

    head = written[0]
    return CaptureResponse(
        id=head.id,
        status="captured",
        parse_status="ok",
        state=head.state,
        kind=head.kind,
        due_at=head.due_at,
        critical=head.critical,
        text=head.text,
        project_hint=parsed.project_hint,
        entities=parsed.entities,
        split=len(written) > 1,
        items=written,
    )


@app.post("/capture/audio", response_model=AudioCaptureResponse)
async def capture_audio(
    audio: UploadFile = File(..., description="The recording"),
    source: str = Form("voice"),
    transcript: Optional[str] = Form(
        None, description="On-device transcript, if the client produced one"
    ),
    transcript_confidence: Optional[float] = Form(None),
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> AudioCaptureResponse:
    """Capture a recording (UC1, UC7, UC8, UC4, UC42).

    The order here is the whole design. The recording is stored *first*,
    because it is the only part of a capture that cannot be reproduced: a
    transcript can be redone and a parse retried, but lost audio is a lost
    thought. Then the row, then the transcript, then the parse — each step an
    enrichment of the one before, and each able to fail without costing the
    steps already done (D6, UC42).

    If the upload itself fails the request fails, deliberately. The recording
    is still on the device at that point, and answering "saved" when the audio
    was dropped is the one lie this endpoint must not tell.

    Args:
        audio: The recording, as multipart.
        source: 'voice' or 'widget'.
        transcript: A transcript the client already has, from on-device
            recognition. When present no cloud call is made.
        transcript_confidence: The client's confidence in that transcript.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        Every item the capture produced — several after a split (UC4) — plus
        which transcription path ran and how confident it was.

    Raises:
        HTTPException: 400 on a bad source or unusable audio, 503 if the
            recording could not be stored.
    """
    _require_source(source)

    data = await audio.read()
    content_type = audio.content_type or "audio/m4a"

    try:
        stored = await upload_audio(
            user_id=user_id,
            data=data,
            content_type=content_type,
            filename=audio.filename,
        )
    except StorageError as e:
        logger.error("Could not store audio for user %s: %s", user_id, e)
        # 503, not 500: the client should keep the file and try again.
        raise HTTPException(
            status_code=503,
            detail="Could not save the recording. It is still on your device.",
        )

    # Transcribe. An on-device transcript is trusted as-is; otherwise the cloud
    # path runs, and if it fails the row is still written with the audio
    # attached and no words (UC42).
    result: Optional[Transcript] = None
    if transcript and transcript.strip():
        result = Transcript(
            text=transcript.strip(),
            confidence=transcript_confidence,
            source="on_device",
        )
    else:
        try:
            # The stored key and type, not what the client reported: the
            # extension is what tells the transcriber how to decode the audio,
            # and `stored` is where it was resolved and canonicalised. Slicing
            # an extension off the client's filename gets `.webm` wrong.
            result = await transcribe(
                data,
                filename=stored.path.rsplit("/", 1)[-1],
                content_type=stored.content_type,
            )
        except TranscriptionError as e:
            logger.warning("Transcription failed for user %s: %s", user_id, e)

    raw_text = result.text if result else ""
    # 'none' is the honest value when nothing transcribed it, and distinct from
    # a cloud attempt that returned words.
    transcript_path = result.source if result else "none"

    try:
        item_id = await db.create_item(
            user_id=user_id,
            raw_text=raw_text,
            source=source,
            parse_status="failed",
            audio_path=stored.path,
            transcript_source=transcript_path,
            transcript_confidence=result.confidence if result else None,
        )
    except ForeignKeyViolation:
        await delete_audio(stored.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown user",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        logger.error("Failed to create item for user %s: %s", user_id, e)
        # The row is what makes the object reachable; an unreferenced one is
        # just a bill.
        await delete_audio(stored.path)
        raise HTTPException(status_code=500, detail="Failed to create item")

    def _failed(**extra: Any) -> AudioCaptureResponse:
        """A capture that was kept but not understood (UC42)."""
        return AudioCaptureResponse(
            id=item_id,
            status="captured",
            parse_status="failed",
            state="shelved",
            kind="task",
            audio_path=stored.path,
            transcript=raw_text or None,
            transcript_source=transcript_path,
            transcript_confidence=result.confidence if result else None,
            items=[CapturedItem(id=item_id, state="shelved", kind="task", text=None)],
            **extra,
        )

    if not raw_text:
        return _failed()

    try:
        parsed = await parse_capture(raw_text)
    except ParseError as e:
        logger.warning("Parse failed for item %s: %s", item_id, e)
        return _failed()

    written: list[CapturedItem] = []
    if parsed.split:
        written = await _write_split(
            db=db,
            user_id=user_id,
            item_id=item_id,
            raw_text=raw_text,
            source=source,
            audio_path=stored.path,
            transcript_source=transcript_path,
            transcript_confidence=result.confidence if result else None,
        )

    if not written:
        try:
            await db.apply_parse(
                item_id=item_id,
                user_id=user_id,
                kind=parsed.kind,
                parsed_text=parsed.text,
                due_at=parsed.due_at,
                critical=parsed.critical,
                state=parsed.state,
            )
        except Exception as e:
            logger.warning("Could not store parse for item %s: %s", item_id, e)
            return _failed()
        written = [_captured(item_id, parsed)]

    # A transcript we do not trust is parsed anyway, then flagged. This is the
    # use D13 reserved for `needs_review` and never had one for.
    parse_status = "ok"
    if result and result.low_confidence:
        parse_status = "needs_review"
        for item in written:
            try:
                await db.set_parse_status(item.id, user_id, "needs_review")
            except Exception as e:
                logger.warning("Could not flag %s for review: %s", item.id, e)

    head = written[0]
    return AudioCaptureResponse(
        id=head.id,
        status="captured",
        parse_status=parse_status,
        state=head.state,
        kind=head.kind,
        due_at=head.due_at,
        critical=head.critical,
        text=head.text,
        audio_path=stored.path,
        transcript=raw_text,
        transcript_source=transcript_path,
        transcript_confidence=result.confidence if result else None,
        split=len(written) > 1,
        items=written,
    )


@app.get("/items/{item_id}/audio", response_model=AudioUrlResponse)
async def item_audio(
    item_id: UUID,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> AudioUrlResponse:
    """A time-limited URL for an item's recording (UC7).

    Signed per request rather than stored on the row: the bucket holds the
    user's voice, and a URL that never expires is a public one.

    Args:
        item_id: Item whose recording to play.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        An absolute URL and how long it is good for.

    Raises:
        HTTPException: 404 if this user has no such item or it has no audio,
            503 if the store could not sign.
    """
    try:
        path = await db.item_audio_path(str(item_id), user_id)
    except Exception as e:
        logger.error("Failed to look up audio for %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to load item")

    if not path:
        raise HTTPException(status_code=404, detail="No recording for this item")

    try:
        url = await signed_url(path)
    except StorageError as e:
        logger.error("Could not sign audio for %s: %s", item_id, e)
        raise HTTPException(status_code=503, detail="Could not reach storage")

    return AudioUrlResponse(
        id=str(item_id), url=url, expires_in=settings.audio_url_ttl_seconds
    )


@app.get("/items/today", response_model=TodayResponse)
async def items_today(
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> TodayResponse:
    """The `Today` list: active items due or overdue (UC32).

    Bounded to the end of the user's day in their timezone, not the server's
    (D15) — the cut-off is a wall-clock notion and the server runs in UTC.
    `Today` has to stay finishable; anything due later is not on it.

    Args:
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        Items ordered oldest-due first, each flagged `overdue` or not.
    """
    tz = capture_tz()
    now = datetime.now(tz)
    end_of_day = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    try:
        rows = await db.today_items(user_id=user_id, before=end_of_day)
    except Exception as e:
        logger.error("Failed to load Today for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to load items")

    return TodayResponse(
        as_of=now,
        items=[TodayItem(**row, overdue=row["due_at"] <= now) for row in rows],
    )


# Everything not `active`. The Shelf is what `Today` deliberately is not: an
# archive you go to, rather than a list that comes to you.
_SHELF_STATES: tuple[str, ...] = ("shelved", "done", "dropped")
_ALL_STATES: tuple[str, ...] = ("active",) + _SHELF_STATES

#: Shortest search that is worth running. Below this every row matches and the
#: trigram indexes cannot be used anyway, so it is refused rather than served
#: slowly and uselessly.
_MIN_SEARCH_CHARS = 2

#: Page size. Small enough that the first screen arrives immediately, large
#: enough that a scroll does not fetch on every flick.
_DEFAULT_PAGE = 30
_MAX_PAGE = 100


def _encode_cursor(created_at: datetime, item_id: str) -> str:
    """Pack a page boundary into one opaque string.

    Opaque on purpose: it is a keyset, and a client that took it apart and
    rebuilt it would be depending on the sort order, which is the one thing
    here that is allowed to change.

    Args:
        created_at: Capture time of the last row on the page.
        item_id: Its id, which is what makes the order total.

    Returns:
        A URL-safe token to hand back as `cursor`.
    """
    raw = f"{created_at.isoformat()}|{item_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Unpack a cursor produced by `_encode_cursor`.

    Args:
        cursor: The token from a previous response.

    Returns:
        The `(created_at, id)` boundary to page after.

    Raises:
        HTTPException: 400 if the token is not one we issued.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, _, item_id = base64.urlsafe_b64decode(padded).decode().partition("|")
        return datetime.fromisoformat(at), str(UUID(item_id))
    except Exception:
        raise HTTPException(status_code=400, detail="Bad cursor")


@app.get("/items", response_model=ShelfResponse)
async def browse_items(
    q: Optional[str] = Query(default=None, description="Search text (UC34)"),
    state: Optional[list[str]] = Query(
        default=None, description="States to include; repeatable (UC36)"
    ),
    project: Optional[str] = Query(
        default=None, description="Project id, or 'none' for unsorted (UC36)"
    ),
    created_from: Optional[datetime] = Query(
        default=None, alias="from", description="Earliest capture time, inclusive"
    ),
    created_to: Optional[datetime] = Query(
        default=None, alias="to", description="Latest capture time, exclusive"
    ),
    cursor: Optional[str] = Query(default=None, description="From a previous page"),
    limit: int = Query(default=_DEFAULT_PAGE, ge=1, le=_MAX_PAGE),
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> ShelfResponse:
    """Browse, search and filter every item (UC33, UC34, UC36).

    The default with no parameters is the Shelf proper: everything that is not
    `active`. **A search, though, spans all four states** — you look for a
    thing you said, and whether it happens to be due today is not something
    you should have to have guessed before typing. An explicit `state` filter
    always wins over both defaults, which is what makes the chips able to
    narrow a search back down.

    Args:
        q: Substring to look for in the transcript and the parsed description.
        state: States to include. Repeat the parameter for several.
        project: A project id, or the literal `none` for items with no project.
        created_from: Earliest capture time, inclusive.
        created_to: Latest capture time, exclusive.
        cursor: Opaque page boundary from the previous response.
        limit: Page size.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        One page, newest capture first, with a cursor for the next.

    Raises:
        HTTPException: 400 on an unknown state, an unusable search term, a
            bad project id or a cursor we did not issue.
    """
    # Whitespace is not a short search, it is no search: a box holding a
    # trailing space after the word was deleted has to fall back to the Shelf
    # rather than answer 400 at somebody still typing.
    term = (q.strip() or None) if q else None
    if term is not None and len(term) < _MIN_SEARCH_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Search for at least {_MIN_SEARCH_CHARS} characters",
        )

    if state:
        requested = [s.strip().lower() for s in state if s and s.strip()]
        unknown = sorted(set(requested) - set(_ALL_STATES))
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"Unknown state: {', '.join(unknown)}"
            )
        # Ordered by the canonical list rather than by how they arrived, so
        # the echo in the response is stable and comparable between requests.
        states = tuple(s for s in _ALL_STATES if s in set(requested))
    else:
        states = _ALL_STATES if term else _SHELF_STATES

    unsorted_only = project is not None and project.lower() == "none"
    project_id: Optional[str] = None
    if project and not unsorted_only:
        try:
            project_id = str(UUID(project))
        except ValueError:
            raise HTTPException(status_code=400, detail="Bad project id")

    after = _decode_cursor(cursor) if cursor else None

    try:
        rows, has_more = await db.browse_items(
            user_id=user_id,
            states=states,
            query=term,
            project_id=project_id,
            unsorted_only=unsorted_only,
            created_from=created_from,
            created_to=created_to,
            after=after,
            limit=limit,
        )
    except Exception as e:
        logger.error("Failed to browse items for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to load items")

    return ShelfResponse(
        items=[ShelfItem(**row) for row in rows],
        next_cursor=(
            _encode_cursor(rows[-1]["created_at"], rows[-1]["id"])
            if has_more and rows
            else None
        ),
        has_more=has_more and bool(rows),
        states=list(states),
    )


@app.get("/people", response_model=PeopleResponse)
async def list_people(
    q: Optional[str] = Query(default=None, description="Search names and aliases"),
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> PeopleResponse:
    """Browse and search the people who have been mentioned (UC47).

    Not paginated, unlike every other list here. This one is bounded by how
    many people are in a life rather than by how much gets captured, so the
    keyset machinery the Shelf needs (D39) would be weight without a load.

    Args:
        q: Substring to match against names and aliases.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        People, most recently mentioned first.
    """
    try:
        rows = await db.list_people(user_id, query=q.strip() if q else None)
    except Exception as e:
        logger.error("Failed to list people for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to load people")

    return PeopleResponse(people=[Person(**row) for row in rows])


@app.get("/people/{entity_id}", response_model=PersonResponse)
async def person_page(
    entity_id: UUID,
    cursor: Optional[str] = Query(default=None, description="From a previous page"),
    limit: int = Query(default=_DEFAULT_PAGE, ge=1, le=_MAX_PAGE),
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> PersonResponse:
    """Everything ever said about one person (UC46).

    Newest first. `docs/use-cases.md` wrote UC46 as "oldest to newest"; the
    owner asked for the reverse on 24 August 2026 and the doc has been amended
    to match. The reason holds up: the page is opened to remember where things
    stand, and oldest-first buries that under the history every time.

    Every state is here, including `done` and `dropped`. A person page that
    hid what you had already dealt with would be answering a narrower question
    than the one it is for.

    Args:
        entity_id: Who.
        cursor: Opaque page boundary from the previous response.
        limit: Page size.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        The person and one page of what was said about them.

    Raises:
        HTTPException: 404 if this user has no such person, 400 on a bad cursor.
    """
    after = _decode_cursor(cursor) if cursor else None

    try:
        person = await db.get_person(str(entity_id), user_id)
    except Exception as e:
        logger.error("Failed to load person %s: %s", entity_id, e)
        raise HTTPException(status_code=500, detail="Failed to load person")

    if person is None:
        raise HTTPException(status_code=404, detail="No such person")

    try:
        rows, has_more = await db.person_items(
            str(entity_id), user_id, after=after, limit=limit
        )
    except Exception as e:
        logger.error("Failed to load notes for person %s: %s", entity_id, e)
        raise HTTPException(status_code=500, detail="Failed to load person")

    return PersonResponse(
        person=Person(**person),
        items=[PersonItem(**row) for row in rows],
        next_cursor=(
            _encode_cursor(rows[-1]["created_at"], rows[-1]["id"])
            if has_more and rows
            else None
        ),
        has_more=has_more and bool(rows),
    )


@app.get("/projects", response_model=ProjectsResponse)
async def list_projects(
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> ProjectsResponse:
    """The projects the filter chips are drawn from (UC36).

    Args:
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        Every project, busiest first. Empty until one is created by hand —
        UC11 was dropped, so nothing infers them.
    """
    try:
        rows = await db.list_projects(user_id)
    except Exception as e:
        logger.error("Failed to list projects for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to load projects")

    return ProjectsResponse(projects=[ProjectSummary(**row) for row in rows])


# Declared after `/items/today` on purpose: FastAPI matches in order, and a
# `{item_id}` route placed first would swallow the literal and 422 on "today".


@app.get("/items/{item_id}", response_model=ItemDetail)
async def item_detail(
    item_id: UUID,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> ItemDetail:
    """One item in full (UC37), which is what UC38 edits against.

    Args:
        item_id: Item to load.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        The item, including the raw transcript and its provenance.

    Raises:
        HTTPException: 404 if this user has no such item.
    """
    try:
        row = await db.get_item(str(item_id), user_id)
    except Exception as e:
        logger.error("Failed to load item %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to load item")

    if row is None:
        raise HTTPException(status_code=404, detail="No such item")
    return ItemDetail(**row)


@app.patch("/items/{item_id}", response_model=ItemDetail)
async def edit_item(
    item_id: UUID,
    request: ItemUpdate,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> ItemDetail:
    """Correct a mis-parsed item (UC38).

    The edit lands on `parsed_text`; `raw_text` is never rewritten (D14).
    Changing the due time re-derives the state, because `due_at` is what
    decides it (UC12) — the common repair is a parse that missed a time, and
    leaving such an item shelved after the time is supplied would be the wrong
    answer. Terminal states are left alone.

    Args:
        item_id: Item to correct.
        request: The fields to change. Omitting `due_at` leaves the time as it
            is; sending `null` clears it.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        The item as it now stands, so the caller can announce any state move.

    Raises:
        HTTPException: 400 if nothing was sent, 404 if no such item.
    """
    fields = request.model_fields_set
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change")

    text = request.text.strip() if request.text is not None else None
    if text is not None and not text:
        raise HTTPException(status_code=400, detail="text cannot be blank")

    try:
        row = await db.update_item(
            item_id=str(item_id),
            user_id=user_id,
            text=text,
            due_at=request.due_at,
            update_due="due_at" in fields,
        )
    except Exception as e:
        logger.error("Failed to update item %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to update item")

    if row is None:
        raise HTTPException(status_code=404, detail="No such item")
    return ItemDetail(**row)


@app.post("/items/{item_id}/state", response_model=StateResponse)
async def move_item(
    item_id: UUID,
    request: StateChange,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> StateResponse:
    """Move an item between states by hand (UC21).

    The escape hatch for when behaviour got it wrong. Logged with reason
    `manual` so decay-driven moves stay distinguishable from user-driven ones.

    Args:
        item_id: Item to move.
        request: The target state.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        Where it went and where it came from; `changed` is False if it was
        already there.

    Raises:
        HTTPException: 400 on an unknown state, 404 if no such item.
    """
    if request.state not in STATES:
        raise HTTPException(
            status_code=400,
            detail=f"state must be one of {', '.join(STATES)}",
        )

    try:
        previous = await db.set_state(str(item_id), user_id, request.state)
    except Exception as e:
        logger.error("Failed to move item %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to update item")

    if previous is None:
        raise HTTPException(status_code=404, detail="No such item")

    return StateResponse(
        id=str(item_id),
        state=request.state,
        previous=previous,
        changed=previous != request.state,
    )


@app.delete("/items/{item_id}", response_model=DeleteResponse)
async def remove_item(
    item_id: UUID,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> DeleteResponse:
    """Delete an item and its recording permanently (UC39).

    The row goes first, then the object. That order is deliberate: a failed
    object delete leaves storage to pay for, while a failed row delete would
    leave an item whose recording is already gone — and "keep the audio" is
    the promise the rest of this system is built on (UC42), so the state that
    breaks it is the worse one to risk.

    Args:
        item_id: Item to delete.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        Whether the row went, and whether an object went with it.

    Raises:
        HTTPException: 404 if this user has no such item.
    """
    try:
        deleted, audio_path = await db.delete_item(str(item_id), user_id)
    except Exception as e:
        logger.error("Failed to delete item %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to delete item")

    if not deleted:
        raise HTTPException(status_code=404, detail="No such item")

    audio_deleted = False
    if audio_path:
        # Best effort, and it never raises: the item is already gone, so
        # failing the request now would report a delete that did happen as one
        # that did not.
        await delete_audio(audio_path)
        audio_deleted = True

    return DeleteResponse(id=str(item_id), deleted=True, audio_deleted=audio_deleted)


@app.post("/items/{item_id}/done", response_model=DoneResponse)
async def mark_item_done(
    item_id: UUID,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> DoneResponse:
    """Mark an item done (UC16).

    The only state the user ever sets by hand. Idempotent, so a double tap
    or a retried request does not write a second transition.

    Args:
        item_id: Item to complete. Typed as a UUID so a malformed id is a 422
            from the framework, not a database error dressed up as a 500.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        The item id and its now-terminal state; `changed` is False if it was
        already done.

    Raises:
        HTTPException: 404 if this user has no such item.
    """
    try:
        previous = await db.mark_done(item_id=str(item_id), user_id=user_id)
    except Exception as e:
        logger.error("Failed to mark item %s done: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to update item")

    if previous is None:
        raise HTTPException(status_code=404, detail="No such item")

    return DoneResponse(id=str(item_id), state="done", changed=previous != "done")


@app.post("/devices", response_model=DeviceResponse)
async def register_device(
    request: DeviceRegistration,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> DeviceResponse:
    """Tell the server where to send this user's pushes (UC23).

    The app calls this on every launch, not just the first: Expo reissues a
    token when the app is reinstalled or its data is cleared, and a stale
    token is a reminder that goes nowhere with nothing to show for it. The
    write is an upsert keyed on the token, so calling it repeatedly is free.

    Args:
        request: The token, and what kind of device it came from.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        Whether it registered, and how many live devices this user now has.

    Raises:
        HTTPException: 400 if the token is not an Expo push token — a
            malformed one stored now is a silent non-delivery later, and by
            then the device is not around to ask again.
    """
    token = request.token.strip()
    if not is_expo_token(token):
        raise HTTPException(status_code=400, detail="Not an Expo push token")
    if request.platform not in PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"platform must be one of {', '.join(PLATFORMS)}",
        )

    try:
        registered = await db.register_push_token(
            user_id=user_id,
            token=token,
            platform=request.platform,
            device_name=request.device_name,
        )
        devices = await db.push_token_count(user_id)
    except Exception as e:
        logger.error("Failed to register a device for %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to register device")

    return DeviceResponse(registered=registered, devices=devices)


@app.post("/items/{item_id}/snooze", response_model=SnoozeResponse)
async def snooze_item(
    item_id: UUID,
    request: Optional[SnoozeRequest] = None,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> SnoozeResponse:
    """Not now (UC17).

    Pushes the due time out and counts the decline. A snooze is not a free
    pass: it feeds the same threshold an ignore does (UC18), because both are
    the user saying "not now" and the system's whole job is to read that.

    Answering a push for an item that has already decayed is not an error —
    the push was real when it was sent — so this returns `changed = False` and
    the item's actual state rather than a 404 or a 409.

    Args:
        item_id: Item to put off.
        request: How long for. Omitted means `SNOOZE_MINUTES`.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        The item's state, new due time and snooze count.

    Raises:
        HTTPException: 400 if the duration is beyond the ceiling, 404 if this
            user has no such item.
    """
    minutes = (request.minutes if request else None) or settings.snooze_minutes
    if minutes > settings.max_snooze_minutes:
        raise HTTPException(
            status_code=400,
            detail=f"snooze cannot exceed {settings.max_snooze_minutes} minutes",
        )

    try:
        row = await db.snooze_item(str(item_id), user_id, minutes)
    except Exception as e:
        logger.error("Failed to snooze item %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to snooze item")

    if row is None:
        raise HTTPException(status_code=404, detail="No such item")

    return SnoozeResponse(
        id=str(item_id),
        state="active" if row["changed"] else row["state"],
        due_at=row["due_at"],
        snooze_count=row["snooze_count"],
        changed=row["changed"],
    )


@app.post("/items/{item_id}/reactivate", response_model=ReactivateResponse)
async def reactivate_item(
    item_id: UUID,
    request: Optional[ReactivateRequest] = None,
    db: Database = Depends(get_db),
    user_id: str = Depends(current_user_id),
) -> ReactivateResponse:
    """Take an item back off the shelf (UC20).

    Decay is silent (UC22 was dropped), so this is the counterweight: the one
    obvious action that undoes what the system decided on its own. It also
    gives the item a due time, because an `active` item without one is
    invisible — `Today` is bounded on `due_at` (D17) and the scheduler only
    pushes what is due.

    Args:
        item_id: Item to bring back.
        request: When it should come back. Omitted means now.
        db: Database connection.
        user_id: Authenticated user, from the Supabase token.

    Returns:
        Where it came from, where it is, and when it is next due.

    Raises:
        HTTPException: 404 if this user has no such item.
    """
    try:
        row = await db.reactivate_item(
            str(item_id), user_id, request.due_at if request else None
        )
    except Exception as e:
        logger.error("Failed to reactivate item %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail="Failed to reactivate item")

    if row is None:
        raise HTTPException(status_code=404, detail="No such item")

    return ReactivateResponse(
        id=str(item_id),
        state="active",
        previous=row["previous"],
        due_at=row["due_at"],
        changed=row["changed"],
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.debug,
    )
