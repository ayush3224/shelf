"""Shelf API service."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg.errors import ForeignKeyViolation
from pydantic import BaseModel, Field

from backend.auth import PUBLIC_PATHS, authenticate, current_user_id
from backend.config import settings
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.debug,
    )
