"""Database connection and operations."""

from datetime import datetime
from typing import Any, Optional
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

    async def today_items(
        self, user_id: str, before: datetime, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Active items due at or before `before` — the `Today` list (UC32).

        Bounded on purpose. `Today` shows due and overdue only; anything
        further out is not today's problem and putting it here is what turns
        the screen into a wall the user can never finish.

        Args:
            user_id: Owner of the items.
            before: Exclusive upper bound on `due_at`, normally the end of
                the user's day in their timezone.
            limit: Hard cap on rows returned.

        Returns:
            Rows ordered oldest-due first, so overdue sits at the top.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT id::text,
                       coalesce(nullif(parsed_text, ''), raw_text) AS text,
                       raw_text,
                       kind::text,
                       state::text,
                       due_at,
                       critical,
                       parse_status::text
                  FROM {settings.db_schema}.items
                 WHERE user_id = %s
                   AND state = 'active'
                   AND due_at IS NOT NULL
                   AND due_at < %s
                 ORDER BY due_at ASC
                 LIMIT %s
                """,
                (user_id, before, limit),
            )
            columns = [c.name for c in result.description or []]
            return [dict(zip(columns, row)) for row in await result.fetchall()]

    async def mark_done(self, item_id: str, user_id: str) -> Optional[str]:
        """Mark an item done and log the transition (UC16).

        `done` is the one state the user sets by hand, so it is also the one
        transition that is not the scheduler's. The `transitions` row is
        written in the same statement as the update — an unlogged state change
        is a hole in the data the decay constants get tuned from.

        Idempotent: marking an already-done item again is a no-op and writes
        no second transition.

        Args:
            item_id: Item to complete.
            user_id: Owner; the update is scoped to them.

        Returns:
            The state the item was in before, or None if the user has no such
            item.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH prev AS (
                    SELECT id, state
                      FROM {settings.db_schema}.items
                     WHERE id = %s AND user_id = %s
                ),
                upd AS (
                    UPDATE {settings.db_schema}.items
                       SET state = 'done'
                     WHERE id = (SELECT id FROM prev)
                       AND state <> 'done'
                    RETURNING id
                ),
                logged AS (
                    INSERT INTO {settings.db_schema}.transitions
                      (item_id, from_state, to_state, reason)
                    SELECT prev.id, prev.state, 'done', 'completion'
                      FROM prev JOIN upd ON upd.id = prev.id
                )
                SELECT prev.state::text FROM prev
                """,
                (item_id, user_id),
            )
            row = await result.fetchone()
            return row[0] if row else None


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
