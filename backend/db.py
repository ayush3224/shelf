"""Database connection and operations."""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from backend.config import settings

# Coming back to `active` from one of these is a reactivation (UC20) rather
# than a plain manual move (UC21) — the system put the item away and the user
# took it back out, which is exactly the event O1 and O2 get tuned against.
# Undoing a `done` is not that, so `done` is not in here.
_REACTIVATED_FROM = ("shelved", "dropped")


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

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """A pooled connection, for SQL that lives outside this class.

        The scheduler's tick (UC18, UC19, UC23) is one ordered script rather
        than a set of API operations, so it keeps its own SQL in
        `backend/scheduler.py` — but it has no business reaching into a
        private attribute to get a connection.

        Yields:
            An open connection from the pool.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            yield conn

    async def create_item(
        self,
        user_id: str,
        raw_text: str,
        source: str,
        parse_status: str = "failed",
        kind: str = "task",
        state: str = "shelved",
        audio_path: Optional[str] = None,
        transcript_source: str = "none",
        transcript_confidence: Optional[float] = None,
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
            audio_path: Storage key of the recording, kept until the item is
                deleted (UC7). None for typed captures.
            transcript_source: 'on_device', 'cloud' or 'none' — which path
                produced `raw_text`.
            transcript_confidence: Transcriber confidence in [0,1], or None.

        Returns:
            Created item ID
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO {settings.db_schema}.items
                  (user_id, raw_text, source, parse_status, kind, state,
                   audio_path, transcript_source, transcript_confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id::text
                """,
                (
                    user_id,
                    raw_text,
                    source,
                    parse_status,
                    kind,
                    state,
                    audio_path,
                    transcript_source,
                    transcript_confidence,
                ),
            )
            row = await result.fetchone()
            if not row:
                raise ValueError("Failed to create item")
            return row[0]

    async def create_split_item(
        self,
        user_id: str,
        raw_text: str,
        source: str,
        kind: str,
        parsed_text: str,
        due_at: Optional[datetime],
        critical: bool,
        state: str,
        audio_path: Optional[str],
        transcript_source: str,
        transcript_confidence: Optional[float],
    ) -> str:
        """Write one sibling of a split capture, already parsed (UC4).

        The first item of a split reuses the row that `create_item` wrote
        before the model call (D6); this writes the rest. They are inserted
        parsed rather than written-then-updated because their parse is already
        in hand — there is no window where a crash could leave them claiming a
        parse that never happened.

        Every sibling carries the same `audio_path`. That shared key is the
        only thing grouping them, so it is what UC7 plays back for any of them.

        Args:
            user_id: Owner of the item.
            raw_text: The whole transcript, not this item's slice of it. UC38
                edits against what was actually said and UC34 searches it, and
                neither is served by storing a fragment the user never spoke.
            source: 'voice', 'text' or 'widget'.
            kind: 'task', 'note' or 'person_note'.
            parsed_text: This item's cleaned one-line description.
            due_at: This item's own due time, or None.
            critical: Whether this item carried an urgency cue.
            state: 'active' if due_at is set, else 'shelved' (UC12).
            audio_path: Shared with every sibling.
            transcript_source: Shared with every sibling.
            transcript_confidence: Shared with every sibling.

        Returns:
            Created item ID.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO {settings.db_schema}.items
                  (user_id, raw_text, source, parse_status, kind, state,
                   parsed_text, due_at, critical,
                   audio_path, transcript_source, transcript_confidence)
                VALUES (%s, %s, %s, 'ok', %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id::text
                """,
                (
                    user_id,
                    raw_text,
                    source,
                    kind,
                    state,
                    parsed_text,
                    due_at,
                    critical,
                    audio_path,
                    transcript_source,
                    transcript_confidence,
                ),
            )
            row = await result.fetchone()
            if not row:
                raise ValueError("Failed to create split item")
            return row[0]

    async def set_parse_status(self, item_id: str, user_id: str, status: str) -> bool:
        """Set `parse_status` on its own (UC42, D13).

        Used when the transcript is usable but not trusted — a low-confidence
        recognition becomes `needs_review` rather than `ok`, which is the use
        D13 reserved for that status.

        Args:
            item_id: Item to flag.
            user_id: Owner; the update is scoped to them.
            status: 'ok', 'failed' or 'needs_review'.

        Returns:
            True if the row was updated.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                UPDATE {settings.db_schema}.items
                   SET parse_status = %s
                 WHERE id = %s AND user_id = %s
                RETURNING id::text
                """,
                (status, item_id, user_id),
            )
            return await result.fetchone() is not None

    async def item_audio_path(self, item_id: str, user_id: str) -> Optional[str]:
        """The storage key of an item's recording, if it has one (UC7).

        Scoped to the owner: this is what stands between a guessed item id and
        someone else's voice.

        Args:
            item_id: Item to look up.
            user_id: Owner.

        Returns:
            The storage key, or None if this user has no such item or it has
            no audio.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT audio_path
                  FROM {settings.db_schema}.items
                 WHERE id = %s AND user_id = %s
                """,
                (item_id, user_id),
            )
            row = await result.fetchone()
            return row[0] if row else None

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
                       parse_status::text,
                       audio_path IS NOT NULL AS has_audio
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

    async def get_item(self, item_id: str, user_id: str) -> Optional[dict[str, Any]]:
        """One item in full, for the detail screen (UC37, UC38).

        Returns more than `Today` does: the raw transcript, which is what UC38
        edits against, and the transcription provenance, which is what explains
        a flagged row to the person looking at it.

        Args:
            item_id: Item to load.
            user_id: Owner; scoping this is what stops a guessed id reading
                someone else's capture.

        Returns:
            The row, or None if this user has no such item.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT id::text,
                       coalesce(nullif(parsed_text, ''), raw_text) AS text,
                       raw_text,
                       parsed_text,
                       kind::text,
                       state::text,
                       due_at,
                       critical,
                       parse_status::text,
                       source::text,
                       audio_path IS NOT NULL AS has_audio,
                       transcript_source::text,
                       transcript_confidence,
                       created_at,
                       updated_at
                  FROM {settings.db_schema}.items
                 WHERE id = %s AND user_id = %s
                """,
                (item_id, user_id),
            )
            columns = [c.name for c in result.description or []]
            row = await result.fetchone()
            return dict(zip(columns, row)) if row else None

    async def update_item(
        self,
        item_id: str,
        user_id: str,
        text: Optional[str] = None,
        due_at: Optional[datetime] = None,
        update_due: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Correct a mis-parsed item (UC38).

        Edits land on `parsed_text`, never `raw_text` (D14): the transcript is
        what the user actually said, and rewriting it would leave them unable
        to see what the model misheard.

        Changing the due time re-derives the state, because `due_at` is what
        decides it (UC12) — adding the time a parse missed should put the item
        on `Today`, and clearing it should shelve it. That only applies to
        `active` and `shelved`; an edit must not resurrect something `done` or
        `dropped`, which would be a surprise rather than a correction. Any
        resulting move is logged with reason `manual`, because a state change
        nobody recorded is a hole in the data O1 and O2 get tuned from.

        Args:
            item_id: Item to correct.
            user_id: Owner; the update is scoped to them.
            text: New display text, or None to leave it.
            due_at: New due time. Only read when `update_due` is set.
            update_due: Whether `due_at` is being changed at all — the flag is
                what separates "clear the time" from "leave the time alone",
                which None cannot express on its own.

        Returns:
            The updated row, or None if this user has no such item.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH prev AS (
                    SELECT id, state
                      FROM {settings.db_schema}.items
                     WHERE id = %(item_id)s AND user_id = %(user_id)s
                ),
                target AS (
                    SELECT prev.id,
                           prev.state AS from_state,
                           CASE
                             WHEN NOT %(update_due)s THEN prev.state
                             WHEN prev.state NOT IN ('active', 'shelved')
                               THEN prev.state
                             WHEN %(due_at)s::timestamptz IS NOT NULL
                               THEN 'active'::{settings.db_schema}.item_state
                             ELSE 'shelved'::{settings.db_schema}.item_state
                           END AS to_state
                      FROM prev
                ),
                upd AS (
                    UPDATE {settings.db_schema}.items i
                       SET parsed_text = coalesce(%(text)s, i.parsed_text),
                           due_at = CASE
                             WHEN %(update_due)s THEN %(due_at)s::timestamptz
                             ELSE i.due_at
                           END,
                           state = target.to_state
                      FROM target
                     WHERE i.id = target.id
                    RETURNING i.id
                ),
                logged AS (
                    INSERT INTO {settings.db_schema}.transitions
                      (item_id, from_state, to_state, reason)
                    SELECT target.id, target.from_state, target.to_state, 'manual'
                      FROM target JOIN upd ON upd.id = target.id
                     WHERE target.from_state IS DISTINCT FROM target.to_state
                )
                SELECT target.from_state::text FROM target
                """,
                {
                    "item_id": item_id,
                    "user_id": user_id,
                    "text": text,
                    "due_at": due_at,
                    "update_due": update_due,
                },
            )
            if await result.fetchone() is None:
                return None

        return await self.get_item(item_id, user_id)

    async def set_state(self, item_id: str, user_id: str, state: str) -> Optional[str]:
        """Move an item between states by hand (UC21).

        The escape hatch for when behaviour got it wrong. Logged with reason
        `manual` so that decay-driven moves stay distinguishable from
        user-driven ones — the whole point of O1 and O2 is telling them apart.

        Moving *to* `active` is handed to `reactivate_item` rather than done
        here, whichever control asked for it. Two reasons, and both are things
        this method got wrong on its own: the move has to settle a due time,
        because `active` with none is a state nothing in this app can show
        (D17, D35) — and coming back from `shelved` or `dropped` has to be
        logged as `reactivation`, because that edge *is* UC20 and counting it
        is how O1 gets answered. One implementation, so the two cannot drift.

        Idempotent: setting the state an item is already in writes no
        transition.

        Args:
            item_id: Item to move.
            user_id: Owner; the update is scoped to them.
            state: Target state.

        Returns:
            The state the item was in before, or None if no such item.
        """
        if state == "active":
            row = await self.reactivate_item(item_id, user_id)
            return row["previous"] if row else None

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
                       SET state = %s
                     WHERE id = (SELECT id FROM prev)
                       AND state <> %s
                    RETURNING id
                ),
                logged AS (
                    INSERT INTO {settings.db_schema}.transitions
                      (item_id, from_state, to_state, reason)
                    SELECT prev.id, prev.state, %s, 'manual'
                      FROM prev JOIN upd ON upd.id = prev.id
                )
                SELECT prev.state::text FROM prev
                """,
                (item_id, user_id, state, state, state),
            )
            row = await result.fetchone()
            return row[0] if row else None

    async def delete_item(
        self, item_id: str, user_id: str
    ) -> tuple[bool, Optional[str]]:
        """Delete an item permanently (UC39).

        Returns the audio key so the caller can remove the object too. The row
        goes first on purpose: an orphaned object costs storage, whereas a row
        pointing at an object that is already gone is a detail screen that
        cannot play its own recording.

        `transitions` rows cascade with the item. That loses a little of the
        history O1 and O2 are tuned from, but keeping audit rows for an item
        the user asked to erase is not what "delete permanently" means.

        Args:
            item_id: Item to delete.
            user_id: Owner; the delete is scoped to them.

        Returns:
            Whether a row was deleted, and its audio key if it had one.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                DELETE FROM {settings.db_schema}.items
                 WHERE id = %s AND user_id = %s
                RETURNING audio_path
                """,
                (item_id, user_id),
            )
            row = await result.fetchone()
            return (True, row[0]) if row else (False, None)

    async def mark_done(self, item_id: str, user_id: str) -> Optional[str]:
        """Mark an item done and log the transition (UC16).

        `done` is the one state the user sets by hand, so it is also the one
        transition that is not the scheduler's. The `transitions` row is
        written in the same statement as the update — an unlogged state change
        is a hole in the data the decay constants get tuned from.

        Any push still waiting on an answer is closed here with
        `response = 'done'`, whether the answer came from the notification
        (UC15) or from the app (UC16). That write is what stops the scheduler
        reading the same silence as an ignore a minute later, and it is also
        the only record of whether pushes are being acted on at all.

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
                ),
                answered AS (
                    UPDATE {settings.db_schema}.notifications n
                       SET responded_at = now(), response = 'done'
                      FROM upd
                     WHERE n.item_id = upd.id AND n.responded_at IS NULL
                )
                SELECT prev.state::text FROM prev
                """,
                (item_id, user_id),
            )
            row = await result.fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------- devices

    async def register_push_token(
        self,
        user_id: str,
        token: str,
        platform: str,
        device_name: Optional[str] = None,
    ) -> bool:
        """Store the device's push token, or move it to this user (UC23).

        Keyed on the token, not on the user. An Expo push token identifies an
        install, and the same install can be signed in as someone else
        tomorrow — so a re-registration reassigns the row rather than adding a
        second one, which would push the same phone twice.

        Registering also clears any `disabled_at`: a token we had written off
        as dead has just proved otherwise by turning up again.

        Args:
            user_id: Owner of the device, from the token's `sub`.
            token: `ExponentPushToken[...]`, already validated by the caller.
            platform: 'android', 'ios' or 'web'.
            device_name: Free text for telling two devices apart in the table.

        Returns:
            True if the row was written.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO {settings.db_schema}.push_tokens
                  (user_id, token, platform, device_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (token) DO UPDATE
                   SET user_id = EXCLUDED.user_id,
                       platform = EXCLUDED.platform,
                       device_name = coalesce(
                           EXCLUDED.device_name,
                           {settings.db_schema}.push_tokens.device_name
                       ),
                       disabled_at = NULL,
                       disabled_reason = NULL
                RETURNING id::text
                """,
                (user_id, token, platform, device_name),
            )
            return await result.fetchone() is not None

    async def disable_push_token(self, token: str, reason: str) -> bool:
        """Stop using a token the push service has rejected.

        Called when Expo answers `DeviceNotRegistered` — the app was
        uninstalled or replaced. The row is kept rather than deleted so that a
        device going quiet is a recorded fact instead of an absence.

        Args:
            token: The dead token.
            reason: What the push service said.

        Returns:
            True if a row was disabled.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                UPDATE {settings.db_schema}.push_tokens
                   SET disabled_at = now(), disabled_reason = %s
                 WHERE token = %s AND disabled_at IS NULL
                RETURNING id::text
                """,
                (reason[:500], token),
            )
            return await result.fetchone() is not None

    async def note_push_success(self, tokens: list[str]) -> None:
        """Record that these tokens accepted a push.

        Not load-bearing, and deliberately so: it is what tells you, months
        later, whether a device stopped receiving without anyone noticing.

        Args:
            tokens: Tokens Expo accepted a message for.
        """
        if not tokens:
            return
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                UPDATE {settings.db_schema}.push_tokens
                   SET last_success_at = now()
                 WHERE token = ANY(%s)
                """,
                (tokens,),
            )

    async def push_token_count(self, user_id: str) -> int:
        """How many live devices this user has registered.

        Args:
            user_id: Owner.

        Returns:
            Count of tokens not marked dead.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT count(*)
                  FROM {settings.db_schema}.push_tokens
                 WHERE user_id = %s AND disabled_at IS NULL
                """,
                (user_id,),
            )
            row = await result.fetchone()
            return int(row[0]) if row else 0

    # ------------------------------------------------------- not now (UC17)

    async def snooze_item(
        self, item_id: str, user_id: str, minutes: int
    ) -> Optional[dict[str, Any]]:
        """Push an item's due time out and count it as a decline (UC17).

        A snooze answers the outstanding push, so the scheduler will not read
        the same silence as an ignore — but it still counts toward the decay
        threshold. Ignoring and snoozing are both "not now" (UC18), and a
        system that let you snooze forever would be a system you have to
        administer, which is the thing this design refuses to be.

        Only an `active` item can be snoozed. A notification acted on after
        the item has already decayed to the shelf is not an error — the push
        was real when it was sent — so this reports `changed = False` rather
        than failing, and the caller says where the item actually went.

        The new due time is measured from now, not from the old due time: a
        snooze answered ten minutes late means ten minutes late.

        Args:
            item_id: Item to put off.
            user_id: Owner; the update is scoped to them.
            minutes: How far out to push the due time.

        Returns:
            The item's state, due time and snooze count after the attempt,
            with `changed` saying whether anything moved. None if this user
            has no such item.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH prev AS (
                    SELECT id, state, due_at, snooze_count
                      FROM {settings.db_schema}.items
                     WHERE id = %(item_id)s AND user_id = %(user_id)s
                ),
                upd AS (
                    UPDATE {settings.db_schema}.items i
                       SET due_at = now() + make_interval(mins => %(minutes)s),
                           snooze_count = i.snooze_count + 1
                      FROM prev
                     WHERE i.id = prev.id AND prev.state = 'active'
                    RETURNING i.id, i.due_at, i.snooze_count
                ),
                answered AS (
                    UPDATE {settings.db_schema}.notifications n
                       SET responded_at = now(), response = 'snooze'
                      FROM upd
                     WHERE n.item_id = upd.id AND n.responded_at IS NULL
                    RETURNING n.id
                )
                SELECT prev.state::text AS state,
                       coalesce((SELECT due_at FROM upd), prev.due_at) AS due_at,
                       coalesce(
                           (SELECT snooze_count FROM upd), prev.snooze_count
                       ) AS snooze_count,
                       (SELECT count(*) FROM upd) > 0 AS changed
                  FROM prev
                """,
                {"item_id": item_id, "user_id": user_id, "minutes": minutes},
            )
            columns = [c.name for c in result.description or []]
            row = await result.fetchone()
            return dict(zip(columns, row)) if row else None

    # ---------------------------------------------------- reactivate (UC20)

    async def reactivate_item(
        self, item_id: str, user_id: str, due_at: Optional[datetime] = None
    ) -> Optional[dict[str, Any]]:
        """Take an item back off the shelf (UC20).

        The deliberate counterweight to silent decay: the system puts things
        away on its own, so there has to be one obvious action that undoes it.

        It gives the item a due time, because `active` without one is a state
        this app cannot show you — `Today` is bounded on `due_at` (D17) and
        the scheduler only pushes what is due, so an active item with no time
        is one nothing would ever surface again. A time in the future is
        respected; a time in the past is replaced with now, because the point
        of reactivating is that you want it *now*, not that you want to be
        told it was overdue in March.

        `push_count` and `snooze_count` reset themselves on the way in, by
        trigger (migration 004) — otherwise the item would arrive carrying the
        ignores that shelved it and re-shelve on its first push.

        Args:
            item_id: Item to bring back.
            user_id: Owner; the update is scoped to them.
            due_at: An explicit due time, or None to let the rule above decide.

        Returns:
            Where the item came from, where it is now and when it is due, with
            `changed` False if it was already active. None if no such item.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH prev AS (
                    SELECT id, state, due_at
                      FROM {settings.db_schema}.items
                     WHERE id = %(item_id)s AND user_id = %(user_id)s
                ),
                upd AS (
                    UPDATE {settings.db_schema}.items i
                       SET state = 'active',
                           due_at = coalesce(
                               %(due_at)s::timestamptz,
                               CASE
                                 WHEN i.due_at > now() THEN i.due_at
                                 ELSE now()
                               END
                           )
                      FROM prev
                     WHERE i.id = prev.id AND prev.state <> 'active'
                    RETURNING i.id, i.due_at
                ),
                logged AS (
                    INSERT INTO {settings.db_schema}.transitions
                      (item_id, from_state, to_state, reason)
                    SELECT prev.id, prev.state, 'active',
                           CASE
                             WHEN prev.state::text = ANY(%(reactivated)s)
                               THEN 'reactivation'
                             ELSE 'manual'
                           END::{settings.db_schema}.transition_reason
                      FROM prev JOIN upd ON upd.id = prev.id
                )
                SELECT prev.state::text AS previous,
                       coalesce((SELECT due_at FROM upd), prev.due_at) AS due_at,
                       (SELECT count(*) FROM upd) > 0 AS changed
                  FROM prev
                """,
                {
                    "item_id": item_id,
                    "user_id": user_id,
                    "due_at": due_at,
                    "reactivated": list(_REACTIVATED_FROM),
                },
            )
            columns = [c.name for c in result.description or []]
            row = await result.fetchone()
            return dict(zip(columns, row)) if row else None


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
