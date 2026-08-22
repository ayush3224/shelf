"""Shelf API service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field

from backend.config import settings
from backend.db import get_db, Database, init_db, close_db


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
    """Response for /capture endpoint."""

    id: str
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore
    """Manage app startup and shutdown."""
    await init_db()
    yield
    await close_db()


app = FastAPI(title="Shelf API", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health(db: Database = Depends(get_db)) -> HealthResponse:
    """Check API and database connectivity."""
    try:
        pool = await db._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        return HealthResponse(status="ok", db_connected=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {str(e)}")


@app.post("/capture", response_model=CaptureResponse)
async def capture(
    request: CaptureRequest, db: Database = Depends(get_db)
) -> CaptureResponse:
    """Capture text and store as item with needs_review status.

    Args:
        request: Contains text and source
        db: Database connection

    Returns:
        Created item ID and status
    """
    if request.source not in ("voice", "text", "widget"):
        raise HTTPException(
            status_code=400, detail="source must be 'voice', 'text', or 'widget'"
        )

    try:
        item_id = await db.create_item(
            user_id=settings.default_user_id,
            raw_text=request.text,
            source=request.source,
            parse_status="needs_review",
        )
        return CaptureResponse(id=item_id, status="captured")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create item: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.debug,
    )
