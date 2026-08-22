"""Shelf API service."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg.errors import ForeignKeyViolation
from pydantic import BaseModel, Field

from backend.auth import PUBLIC_PATHS, authenticate, current_user_id
from backend.config import capture_tz, settings
from backend.db import Database, close_db, get_db, init_db
from backend.parse import ParseError, parse_capture

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

    `text` is persisted as `items.parsed_text` (migration 002). `project_hint`
    and `entities` are returned but not stored — project inference is UC11 and
    entity linking is UC44, both phase 6.
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


class TodayResponse(BaseModel):
    """Response for GET /items/today."""

    as_of: datetime
    items: list[TodayItem]


class DoneResponse(BaseModel):
    """Response for POST /items/{item_id}/done."""

    id: str
    state: str
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
    if request.source not in ("voice", "text", "widget"):
        raise HTTPException(
            status_code=400, detail="source must be 'voice', 'text', or 'widget'"
        )

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

    return CaptureResponse(
        id=item_id,
        status="captured",
        parse_status="ok",
        state=parsed.state,
        kind=parsed.kind,
        due_at=parsed.due_at,
        critical=parsed.critical,
        text=parsed.text,
        project_hint=parsed.project_hint,
        entities=parsed.entities,
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.debug,
    )
