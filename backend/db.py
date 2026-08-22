"""Database connection and operations."""

from datetime import datetime
from typing import Optional
from psycopg_pool import AsyncConnectionPool

from backend.config import settings


class Database:
    """Database connection manager."""

    def __init__(self) -> None:
        """Initialize database connection pool."""
        self.pool: Optional[AsyncConnectionPool] = None

    async def connect(self) -> None:
        """Open the connection pool."""
        if self.pool is None:
            self.pool = AsyncConnectionPool(
                settings.database_url, min_size=1, max_size=10
            )
        await self.pool.open()

    async def disconnect(self) -> None:
        """Close the connection pool."""
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def _ensure_pool(self) -> AsyncConnectionPool:
        """Ensure pool exists and is open."""
        if self.pool is None:
            await self.connect()
        return self.pool  # type: ignore

    async def create_item(
        self,
        user_id: str,
        raw_text: str,
        source: str,
        parse_status: str = "failed",
        kind: str = "task",
        state: str = "shelved",
    ) -> str:
        """Create an item in the database.

        Args:
            user_id: User ID (UUID)
            raw_text: Raw captured text
            source: Source of capture ('voice', 'text', or 'widget')
            parse_status: Parse status (default 'failed', flipped to 'ok'
                by `apply_parse` — a crash mid-parse must not read as 'ok')
            kind: Item kind (default 'task')
            state: Initial state (default 'shelved')

        Returns:
            Created item ID
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO {settings.db_schema}.items
                  (user_id, raw_text, source, parse_status, kind, state)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id::text
                """,
                (user_id, raw_text, source, parse_status, kind, state),
            )
            row = await result.fetchone()
            if not row:
                raise ValueError("Failed to create item")
            return row[0]

    async def apply_parse(
        self,
        item_id: str,
        user_id: str,
        kind: str,
        parsed_text: str,
        due_at: Optional[datetime],
        critical: bool,
        state: str,
    ) -> bool:
        """Write a successful parse onto an existing item (UC9, UC10, UC12).

        Args:
            item_id: Item to update
            user_id: Owner of the item; the update is scoped to them
            kind: 'task', 'note' or 'person_note'
            parsed_text: Cleaned one-line description; raw_text is untouched
            due_at: Extracted due time, or None
            critical: Whether the capture carried an urgency cue
            state: 'active' if due_at is set, else 'shelved'

        Returns:
            True if the row was updated, False if no such row for this user.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                UPDATE {settings.db_schema}.items
                   SET kind = %s,
                       parsed_text = %s,
                       due_at = %s,
                       critical = %s,
                       state = %s,
                       parse_status = 'ok'
                 WHERE id = %s AND user_id = %s
                RETURNING id::text
                """,
                (kind, parsed_text, due_at, critical, state, item_id, user_id),
            )
            return await result.fetchone() is not None


_db_instance: Optional[Database] = None


def get_db() -> Database:
    """Get or create the database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance


async def init_db() -> None:
    """Initialize database connection pool."""
    db = get_db()
    await db.connect()


async def close_db() -> None:
    """Close database connection pool."""
    db = get_db()
    await db.disconnect()
