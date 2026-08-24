"""Database connection and operations."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Optional, Sequence

from psycopg import AsyncConnection
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from backend.config import settings

# Coming back to `active` from one of these is a reactivation (UC20) rather
# than a plain manual move (UC21) — the system put the item away and the user
# took it back out, which is exactly the event O1 and O2 get tuned against.
# Undoing a `done` is not that, so `done` is not in here.
_REACTIVATED_FROM = ("shelved", "dropped")


def _escape_like(term: str) -> str:
    """Make a search term literal inside an ILIKE pattern.

    The user types into a search box, not a query language: a `%` they typed
    is a percent sign they are looking for, and left unescaped it would match
    the entire table instead. The backslash is escaped first, or escaping the
    wildcards would then be undone by it.

    Args:
        term: Raw text from the search field.

    Returns:
        The same text, safe to wrap in `%...%`.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class EntityResolution:
    """What to do with one name the parse pulled out of a capture (UC45)."""

    #: The entity this name belongs to, or None when a new row is needed.
    entity_id: Optional[str]
    #: The canonical name to store. May differ from the one that arrived.
    name: str
    #: The full alias list to store alongside it.
    aliases: list[str]
    #: True when nothing matched and a row has to be written.
    created: bool
    #: True when a fuller name displaced the one already on the row.
    promoted: bool
    #: True when a match was declined because more than one thing matched.
    ambiguous: bool


def _normalized(name: str) -> str:
    """Casefold and collapse whitespace, for comparison only.

    Never stored. What the user said is what gets displayed; this is purely
    the key the matching runs on.
    """
    return " ".join(name.split()).casefold()


def _tokens(name: str) -> frozenset[str]:
    """The set of words in a name, normalised."""
    return frozenset(_normalized(name).split())


def resolve_entity(
    name: str, entity_type: str, existing: Sequence[dict[str, Any]]
) -> Optional[EntityResolution]:
    """Decide which person a name refers to (UC45).

    The whole problem is that "Priya" and "Priya Sharma" are usually the same
    person and occasionally are not, and getting it wrong in the merging
    direction is much worse than getting it wrong in the splitting direction:
    a wrongly split person is two lists you can see and reconcile, a wrongly
    merged one silently attributes what you said about somebody to somebody
    else, and nothing on the screen ever looks odd.

    So the rule is **never guess when more than one thing matches**, in this
    order:

    1. The same name, normalised — the common case, and unique by constraint.
    2. A recorded alias. If two entities claim the same alias, that is a tie
       and it is declined.

       **An alias beats the subset rule below, even when the subset rule would
       find the match ambiguous.** Once "Priya" is an alias of Priya Sharma, a
       later Priya Nair does not make bare "Priya" ambiguous again — it keeps
       going to Sharma. That is a deliberate asymmetry: an alias is a
       resolution that already happened out of real usage, whereas a subset is
       an inference this function is making right now. Letting one new person
       invalidate a binding built over a year would make the system worse the
       longer it is used. The residual risk is real and is stated in O6: a bare
       "Priya" that meant Nair lands on Sharma's page, and there is no way to
       move it by hand yet.
    3. A token subset in either direction: "Priya" against "Priya Sharma", or
       "Priya Sharma" against a bare "Priya" already on file. **Only when
       exactly one entity matches.** Two Priyas on file means a bare "Priya"
       resolves to neither.
    4. Otherwise it is somebody new.

    Case 3 going the fuller way *promotes*: a row called "Priya" that meets
    "Priya Sharma" is renamed, keeping "Priya" as an alias, because the fuller
    name is the better label for a person page and the alias is what keeps the
    shorter mentions attached.

    A declined match creates its own row rather than linking to nothing. The
    note has to be findable under some name, and a visible second "Priya" is a
    thing the owner can see and act on; a note attached to nobody is not.

    Args:
        name: The name as the parse produced it.
        entity_type: `person`, `org` or `place`. Matching never crosses types.
        existing: Rows already on file, each with `id`, `type`, `name` and
            `aliases`.

    Returns:
        What to write, or None if the name was blank.
    """
    wanted = _normalized(name)
    if not wanted:
        return None

    display = " ".join(name.split())
    same_type = [e for e in existing if e["type"] == entity_type]

    # 1. The same name.
    for entity in same_type:
        if _normalized(entity["name"]) == wanted:
            return EntityResolution(
                entity_id=entity["id"],
                name=entity["name"],
                aliases=list(entity["aliases"]),
                created=False,
                promoted=False,
                ambiguous=False,
            )

    # 2. A name we have already recorded as an alias of somebody.
    alias_hits = [
        e for e in same_type if any(_normalized(a) == wanted for a in e["aliases"])
    ]
    if len(alias_hits) == 1:
        entity = alias_hits[0]
        return EntityResolution(
            entity_id=entity["id"],
            name=entity["name"],
            aliases=list(entity["aliases"]),
            created=False,
            promoted=False,
            ambiguous=False,
        )
    if len(alias_hits) > 1:
        return EntityResolution(
            entity_id=None,
            name=display,
            aliases=[],
            created=True,
            promoted=False,
            ambiguous=True,
        )

    # 3. One name contains the other, and only one candidate does.
    wanted_tokens = _tokens(name)
    subset_hits = [
        e
        for e in same_type
        if wanted_tokens < _tokens(e["name"]) or _tokens(e["name"]) < wanted_tokens
    ]

    if len(subset_hits) > 1:
        # Two Priyas. Guessing here is the one mistake that is invisible.
        return EntityResolution(
            entity_id=None,
            name=display,
            aliases=[],
            created=True,
            promoted=False,
            ambiguous=True,
        )

    if len(subset_hits) == 1:
        entity = subset_hits[0]
        aliases = list(entity["aliases"])
        if _tokens(entity["name"]) < wanted_tokens:
            # The fuller name wins the label; the shorter one becomes the alias
            # that keeps every past mention attached.
            if not any(_normalized(a) == _normalized(entity["name"]) for a in aliases):
                aliases.append(entity["name"])
            return EntityResolution(
                entity_id=entity["id"],
                name=display,
                aliases=aliases,
                created=False,
                promoted=True,
                ambiguous=False,
            )
        if not any(_normalized(a) == wanted for a in aliases):
            aliases.append(display)
        return EntityResolution(
            entity_id=entity["id"],
            name=entity["name"],
            aliases=aliases,
            created=False,
            promoted=False,
            ambiguous=False,
        )

    # 4. Somebody new.
    return EntityResolution(
        entity_id=None,
        name=display,
        aliases=[],
        created=True,
        promoted=False,
        ambiguous=False,
    )


def merged_aliases(
    survivor_name: str,
    survivor_aliases: Sequence[str],
    absorbed_name: str,
    absorbed_aliases: Sequence[str],
) -> list[str]:
    """Every name the survivor of a merge should answer to (UC48).

    The absorbed person's canonical name is the important one: it is what the
    parse will keep producing tomorrow, and without it as an alias the next
    mention would resolve to nobody and quietly recreate the row that was just
    folded away.

    Order is survivor's own aliases, then the absorbed name, then theirs —
    first-seen wins, so the display keeps whatever spelling was already in use.

    Args:
        survivor_name: Canonical name of the row that stays.
        survivor_aliases: Its aliases.
        absorbed_name: Canonical name of the row being folded in.
        absorbed_aliases: Its aliases.

    Returns:
        A deduplicated alias list that never contains the survivor's own name.
    """
    out: list[str] = []
    seen = {_normalized(survivor_name)}
    for alias in [*survivor_aliases, absorbed_name, *absorbed_aliases]:
        key = _normalized(alias)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(alias)
    return out


def aliases_after_split(
    source_aliases: Sequence[str], target_name: str, target_aliases: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Which of the source's aliases follow the notes out (UC49, D45).

    The case worth handling is the one that caused the split. A row called
    "Priya Sharma" picked up "Priya" as an alias because a bare mention landed
    on it; if those notes turn out to be about a different Priya and get moved,
    leaving "Priya" on Sharma means the *next* bare "Priya" resolves straight
    back onto the row you just corrected, and the correction undoes itself.

    So the rule is narrow and stated rather than inferred: **an alias that
    names the target stops being an alias of the source.** Matching the
    target's canonical name or any alias it already has.

    What this deliberately does not do is read the notes. Deciding from an
    item's text which alias it was responsible for is exactly the kind of
    guess the manual path exists to replace — and it would be a guess about
    identity, which is now the owner's call (D45).

    Args:
        source_aliases: Aliases on the row the notes are leaving.
        target_name: Canonical name of the row they are going to.
        target_aliases: Aliases it already has.

    Returns:
        `(kept, moved)` — what stays on the source, and what the target had
        claim to.
    """
    claimed = {_normalized(target_name)}
    claimed.update(_normalized(alias) for alias in target_aliases)

    kept: list[str] = []
    moved: list[str] = []
    for alias in source_aliases:
        (moved if _normalized(alias) in claimed else kept).append(alias)
    return kept, moved


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

    async def browse_items(
        self,
        user_id: str,
        states: Sequence[str],
        query: Optional[str] = None,
        project_id: Optional[str] = None,
        unsorted_only: bool = False,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        after: Optional[tuple[datetime, str]] = None,
        limit: int = 30,
    ) -> tuple[list[dict[str, Any]], bool]:
        """The Shelf list: browse, search and filter in one query (UC33/34/36).

        One method rather than three because they are one screen. Search is a
        filter like any other, and a search that could not also be narrowed to
        a state or a date would just be a second, worse list.

        Ordered by `created_at` — when the thing was *said* — and not by
        `state_changed_at`, which is when the system last moved it. That is a
        deliberate choice about what the shelf is (D38): capture order reads as
        an archive, whereas decay order puts whatever was most recently taken
        away from you at the top, which is the reading this screen is supposed
        not to have.

        Paginated by keyset, not offset. `(created_at, id)` is a total order
        thanks to the id tiebreak, so a page boundary cannot repeat or skip a
        row when a capture lands mid-scroll — which an `OFFSET` would, and this
        table only ever grows.

        Args:
            user_id: Owner of the items.
            states: States to include. Empty means every state.
            query: Substring to match against `raw_text` and `parsed_text`
                (UC34). Matched case-insensitively via the trigram indexes.
            project_id: Restrict to one project (UC36).
            unsorted_only: Restrict to items with no project at all. Mutually
                exclusive with `project_id`; the caller enforces that.
            created_from: Earliest capture time to include, inclusive.
            created_to: Latest capture time to include, exclusive.
            after: The `(created_at, id)` of the last row of the previous page.
            limit: Rows per page.

        Returns:
            The page, and whether another one exists. `has_more` comes from
            asking for one row past the limit rather than from a second count
            query, which would be a whole extra scan to answer a boolean.
        """
        where = ["i.user_id = %(user_id)s"]
        params: dict[str, Any] = {"user_id": user_id, "limit": limit + 1}

        if states:
            where.append(
                f"i.state = ANY(%(states)s::{settings.db_schema}.item_state[])"
            )
            params["states"] = list(states)

        if query:
            # ESCAPE so a literal % or _ in the search box matches itself
            # instead of turning into a wildcard.
            where.append(
                "(i.raw_text ILIKE %(pattern)s ESCAPE '\\'"
                " OR i.parsed_text ILIKE %(pattern)s ESCAPE '\\')"
            )
            params["pattern"] = f"%{_escape_like(query)}%"

        if unsorted_only:
            where.append("i.project_id IS NULL")
        elif project_id:
            where.append("i.project_id = %(project_id)s")
            params["project_id"] = project_id

        if created_from is not None:
            where.append("i.created_at >= %(created_from)s")
            params["created_from"] = created_from

        if created_to is not None:
            where.append("i.created_at < %(created_to)s")
            params["created_to"] = created_to

        if after is not None:
            # Row comparison, so the index on (user_id, created_at, id) can
            # seek straight to the boundary rather than filter after sorting.
            where.append("(i.created_at, i.id) < (%(cursor_at)s, %(cursor_id)s)")
            params["cursor_at"], params["cursor_id"] = after

        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT i.id::text,
                       coalesce(nullif(i.parsed_text, ''), i.raw_text) AS text,
                       i.raw_text,
                       i.kind::text,
                       i.state::text,
                       i.due_at,
                       i.critical,
                       i.parse_status::text,
                       i.audio_path IS NOT NULL AS has_audio,
                       i.project_id::text,
                       p.name AS project_name,
                       i.created_at,
                       i.state_changed_at
                  FROM {settings.db_schema}.items i
                  LEFT JOIN {settings.db_schema}.projects p ON p.id = i.project_id
                 WHERE {' AND '.join(where)}
                 ORDER BY i.created_at DESC, i.id DESC
                 LIMIT %(limit)s
                """,
                params,
            )
            columns = [c.name for c in result.description or []]
            rows = [dict(zip(columns, row)) for row in await result.fetchall()]

        has_more = len(rows) > limit
        return rows[:limit], has_more

    async def list_projects(self, user_id: str) -> list[dict[str, Any]]:
        """Every project this user has, with how many items sit in each (UC36).

        Returns the count alongside the name because the filter chips are the
        only place projects are visible at all, and a chip that leads to an
        empty list is worse than no chip. With UC11 dropped nothing populates
        `project_id` on its own, so this is normally empty and the chip row
        does not render — which is the honest depiction of that decision, not
        a gap.

        Args:
            user_id: Owner of the projects.

        Returns:
            Projects with a non-zero item count, busiest first.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT p.id::text, p.name, p.slug, count(i.id) AS items
                  FROM {settings.db_schema}.projects p
                  LEFT JOIN {settings.db_schema}.items i ON i.project_id = p.id
                 WHERE p.user_id = %s
                 GROUP BY p.id, p.name, p.slug
                 ORDER BY count(i.id) DESC, p.name ASC
                """,
                (user_id,),
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

    async def link_entities(
        self, user_id: str, item_id: str, entities: Sequence[dict[str, str]]
    ) -> list[dict[str, Any]]:
        """Attach a capture to the people it named (UC45).

        The parse has returned `entities` since the first version and they were
        thrown away every time — the tables were built in migration 001 for
        exactly this (D7), so this is the write that was always missing rather
        than a schema change.

        Resolution is `resolve_entity`, and the snapshot it matches against is
        read once per capture and then kept up to date in memory. That matters
        for a split (UC4): two items from one recording that both say "Priya"
        must land on one row, and they would not if each resolved against the
        state before either was written.

        Args:
            user_id: Owner.
            item_id: The capture to attach.
            entities: `{type, name}` as the parse produced them.

        Returns:
            One entry per link written, each naming the entity it resolved to
            and whether that resolution was a declined guess.
        """
        if not entities:
            return []

        pool = await self._ensure_pool()
        linked: list[dict[str, Any]] = []

        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT id::text, type::text, name, aliases
                  FROM {settings.db_schema}.entities
                 WHERE user_id = %s
                """,
                (user_id,),
            )
            columns = [c.name for c in result.description or []]
            known = [dict(zip(columns, row)) for row in await result.fetchall()]

            for parsed in entities:
                name = (parsed.get("name") or "").strip()
                entity_type = (parsed.get("type") or "person").strip() or "person"
                if not name:
                    continue

                resolved = resolve_entity(name, entity_type, known)
                if resolved is None:
                    continue

                if resolved.entity_id is None:
                    created = await conn.execute(
                        f"""
                        INSERT INTO {settings.db_schema}.entities
                          (user_id, type, name, aliases)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (user_id, type, name)
                          DO UPDATE SET name = EXCLUDED.name
                        RETURNING id::text
                        """,
                        (user_id, entity_type, resolved.name, Json(resolved.aliases)),
                    )
                    row = await created.fetchone()
                    entity_id = row[0]
                    known.append(
                        {
                            "id": entity_id,
                            "type": entity_type,
                            "name": resolved.name,
                            "aliases": list(resolved.aliases),
                        }
                    )
                else:
                    entity_id = resolved.entity_id
                    before = next((e for e in known if e["id"] == entity_id), None)
                    changed = before is None or (
                        before["name"] != resolved.name
                        or list(before["aliases"]) != list(resolved.aliases)
                    )
                    if changed:
                        # A promotion renames the row, which is a rename of the
                        # person page. The alias is what stops that losing the
                        # mentions filed under the shorter name.
                        await conn.execute(
                            f"""
                            UPDATE {settings.db_schema}.entities
                               SET name = %s, aliases = %s
                             WHERE id = %s AND user_id = %s
                            """,
                            (resolved.name, Json(resolved.aliases), entity_id, user_id),
                        )
                        if before is not None:
                            before["name"] = resolved.name
                            before["aliases"] = list(resolved.aliases)

                await conn.execute(
                    f"""
                    INSERT INTO {settings.db_schema}.links
                      (item_id, entity_id, relation)
                    VALUES (%s, %s, 'mentions')
                    ON CONFLICT (item_id, entity_id, relation) DO NOTHING
                    """,
                    (item_id, entity_id),
                )

                linked.append(
                    {
                        "id": entity_id,
                        "name": resolved.name,
                        "type": entity_type,
                        "ambiguous": resolved.ambiguous,
                    }
                )

        return linked

    async def item_people(self, item_id: str, user_id: str) -> list[dict[str, Any]]:
        """Who this item is linked to (UC45, UC46).

        On the detail screen so that a link is visible where it can be
        corrected. Before people were extracted from every capture this was a
        fact you could only see from the other end — you found out a task had
        been filed under the wrong Priya by opening Priya.

        Args:
            item_id: The item.
            user_id: Owner. Scoped on the *item* as well as the entity, so a
                guessed id cannot read somebody else's links.

        Returns:
            The linked entities, by name.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT e.id::text, e.name, e.type::text
                  FROM {settings.db_schema}.links l
                  JOIN {settings.db_schema}.entities e ON e.id = l.entity_id
                  JOIN {settings.db_schema}.items i ON i.id = l.item_id
                 WHERE l.item_id = %s AND i.user_id = %s AND e.user_id = %s
                 ORDER BY e.name ASC
                """,
                (item_id, user_id, user_id),
            )
            columns = [c.name for c in result.description or []]
            return [dict(zip(columns, row)) for row in await result.fetchall()]

    async def link_person(
        self,
        user_id: str,
        item_id: str,
        entity_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Attach an item to a person by hand.

        The other direction from `link_entities`, and deliberately not the same
        function. That one resolves what a *model* heard and is willing to
        guess when the evidence is thin — a token subset in either direction,
        so "Priya" lands on "Priya Sharma" (D43). This one is a person pointing
        at a name they typed, and guessing on top of that would be the app
        overruling them.

        So a typed name matches only where matching is not a guess: the same
        name, ignoring case and spacing, or a name this person is already
        recorded as going by. "priya sharma" is Priya Sharma; "Priya" is not,
        unless she is on file as answering to it. Anything else is a new
        person, and a duplicate that lands anyway is a merge away (UC48, D45).

        Args:
            user_id: Owner.
            item_id: The item to attach.
            entity_id: An existing person, from the picker.
            name: A name typed on the spot. Ignored when `entity_id` is set.

        Returns:
            The entity now linked and whether the link was new, or None if the
            item is not this user's, the person is not, or the name was blank.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            owned = await conn.execute(
                f"SELECT 1 FROM {settings.db_schema}.items"
                " WHERE id = %s AND user_id = %s",
                (item_id, user_id),
            )
            if await owned.fetchone() is None:
                return None

            if entity_id:
                found = await conn.execute(
                    f"""
                    SELECT id::text, name, type::text
                      FROM {settings.db_schema}.entities
                     WHERE id = %s AND user_id = %s
                    """,
                    (entity_id, user_id),
                )
                columns = [c.name for c in found.description or []]
                row = await found.fetchone()
                if row is None:
                    return None
                entity = dict(zip(columns, row))
            else:
                cleaned = " ".join((name or "").split())
                if not cleaned:
                    return None

                known = await conn.execute(
                    f"""
                    SELECT id::text, name, type::text, aliases
                      FROM {settings.db_schema}.entities
                     WHERE user_id = %s AND type = 'person'
                    """,
                    (user_id,),
                )
                columns = [c.name for c in known.description or []]
                wanted = _normalized(cleaned)
                entity = None
                for row in await known.fetchall():
                    candidate = dict(zip(columns, row))
                    names = [candidate["name"], *list(candidate["aliases"])]
                    if any(_normalized(n) == wanted for n in names):
                        entity = {k: candidate[k] for k in ("id", "name", "type")}
                        break

                if entity is None:
                    # `ON CONFLICT` as well as the scan above: the scan settles
                    # case and spacing, the constraint settles two taps racing
                    # the picker's offer to create the same person twice.
                    created = await conn.execute(
                        f"""
                        INSERT INTO {settings.db_schema}.entities
                          (user_id, type, name, aliases)
                        VALUES (%s, 'person', %s, '[]'::jsonb)
                        ON CONFLICT (user_id, type, name)
                          DO UPDATE SET name = EXCLUDED.name
                        RETURNING id::text, name, type::text
                        """,
                        (user_id, cleaned),
                    )
                    columns = [c.name for c in created.description or []]
                    entity = dict(zip(columns, await created.fetchone()))

            linked = await conn.execute(
                f"""
                INSERT INTO {settings.db_schema}.links
                  (item_id, entity_id, relation)
                VALUES (%s, %s, 'mentions')
                ON CONFLICT (item_id, entity_id, relation) DO NOTHING
                """,
                (item_id, entity["id"]),
            )

        return {**entity, "added": bool(linked.rowcount)}

    async def unlink_person(
        self, user_id: str, item_id: str, entity_id: str
    ) -> Optional[dict[str, Any]]:
        """Detach an item from a person by hand.

        With people extracted from every capture rather than only from
        `person_note`s, there is more to be wrong about in both directions —
        a name heard in passing, a "Pansy" that is a cat. Removing a link is
        the correction for the false positive, as the picker is for the miss.

        The person is removed with their last link, the same rule a split
        follows (UC49): a name with nothing behind it is clutter rather than
        data. Nothing said about them is touched, because by then there is
        nothing said about them.

        Args:
            user_id: Owner.
            item_id: The item.
            entity_id: Who to detach.

        Returns:
            What happened, or None if there was no such link to remove.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            removed = await conn.execute(
                f"""
                DELETE FROM {settings.db_schema}.links l
                 WHERE l.item_id = %(item_id)s
                   AND l.entity_id = %(entity_id)s
                   AND EXISTS (
                       SELECT 1 FROM {settings.db_schema}.items i
                        WHERE i.id = l.item_id AND i.user_id = %(user_id)s)
                   AND EXISTS (
                       SELECT 1 FROM {settings.db_schema}.entities e
                        WHERE e.id = l.entity_id AND e.user_id = %(user_id)s)
                """,
                {"item_id": item_id, "entity_id": entity_id, "user_id": user_id},
            )
            if not removed.rowcount:
                return None

            left = await conn.execute(
                f"SELECT count(*) FROM {settings.db_schema}.links WHERE entity_id = %s",
                (entity_id,),
            )
            person_removed = False
            if (await left.fetchone())[0] == 0:
                await conn.execute(
                    f"""
                    DELETE FROM {settings.db_schema}.entities
                     WHERE id = %s AND user_id = %s
                    """,
                    (entity_id, user_id),
                )
                person_removed = True

        return {"entity_id": entity_id, "person_removed": person_removed}

    async def merge_people(
        self, user_id: str, survivor_id: str, absorbed_id: str
    ) -> Optional[dict[str, Any]]:
        """Fold one person into another (UC48).

        The survivor is the page the merge was started from. Every note the
        absorbed row held moves across, its name becomes an alias so tomorrow's
        mention resolves here too, and the row itself goes.

        Atomic: the pool's connection block commits at the end or rolls back,
        and a half-done merge is a person whose notes have moved but whose row
        still exists — which is exactly the state this feature is for undoing.

        Args:
            user_id: Owner; both rows are scoped to them.
            survivor_id: The row that stays.
            absorbed_id: The row that is folded in and deleted.

        Returns:
            The survivor as it now stands plus what was absorbed, or None if
            either row is missing, they are the same row, or they are not the
            same kind of thing.
        """
        if survivor_id == absorbed_id:
            return None

        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT id::text, type::text, name, aliases
                  FROM {settings.db_schema}.entities
                 WHERE user_id = %s AND id = ANY(%s::uuid[])
                """,
                (user_id, [survivor_id, absorbed_id]),
            )
            columns = [c.name for c in result.description or []]
            rows = {
                r["id"]: r
                for r in (dict(zip(columns, row)) for row in await result.fetchall())
            }

            survivor = rows.get(survivor_id)
            absorbed = rows.get(absorbed_id)
            if survivor is None or absorbed is None:
                return None
            if survivor["type"] != absorbed["type"]:
                # A place called Preston is not the person called Preston, and
                # folding one into the other is never what was meant.
                return None

            # Move the links, skipping any the survivor already holds — the
            # unique constraint on (item_id, entity_id, relation) is what makes
            # a note mentioning both people a real case rather than a crash.
            moved = await conn.execute(
                f"""
                UPDATE {settings.db_schema}.links l
                   SET entity_id = %(survivor)s
                 WHERE l.entity_id = %(absorbed)s
                   AND NOT EXISTS (
                       SELECT 1
                         FROM {settings.db_schema}.links x
                        WHERE x.item_id = l.item_id
                          AND x.entity_id = %(survivor)s
                          AND x.relation = l.relation)
                """,
                {"survivor": survivor_id, "absorbed": absorbed_id},
            )
            moved_count = moved.rowcount or 0

            # Whatever is left is a duplicate of a link the survivor already
            # had. It goes with the row.
            await conn.execute(
                f"DELETE FROM {settings.db_schema}.links WHERE entity_id = %s",
                (absorbed_id,),
            )

            aliases = merged_aliases(
                survivor["name"],
                list(survivor["aliases"]),
                absorbed["name"],
                list(absorbed["aliases"]),
            )
            await conn.execute(
                f"""
                UPDATE {settings.db_schema}.entities
                   SET aliases = %s
                 WHERE id = %s AND user_id = %s
                """,
                (Json(aliases), survivor_id, user_id),
            )
            await conn.execute(
                f"DELETE FROM {settings.db_schema}.entities WHERE id = %s AND user_id = %s",
                (absorbed_id, user_id),
            )

        return {
            "survivor_id": survivor_id,
            "absorbed_id": absorbed_id,
            "absorbed_name": absorbed["name"],
            "aliases": aliases,
            "moved": moved_count,
        }

    async def split_person(
        self,
        user_id: str,
        source_id: str,
        item_ids: Sequence[str],
        into_id: Optional[str] = None,
        into_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Move some of a person's notes to somebody else (UC49).

        The other half of UC48: a merge fixes two rows that should be one, this
        fixes one row that should be two. Between them a wrong resolution is
        always recoverable, which is what lets the automatic rules stay willing
        to guess (D45).

        The target is either an existing person or a name typed on the spot. A
        typed name that already belongs to somebody resolves to them rather
        than colliding with the unique constraint — the picker offers to create
        only what does not exist, but two taps can race and the database is the
        thing that decides.

        Args:
            user_id: Owner.
            source_id: The person the notes are leaving.
            item_ids: Which notes. Ones not currently linked to the source are
                ignored rather than being an error.
            into_id: An existing person to move them to.
            into_name: A new name to move them to. Ignored if `into_id` is set.

        Returns:
            What happened, or None if the source or target could not be
            resolved.
        """
        if not item_ids:
            return None

        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT id::text, type::text, name, aliases
                  FROM {settings.db_schema}.entities
                 WHERE user_id = %s AND id = %s
                """,
                (user_id, source_id),
            )
            columns = [c.name for c in result.description or []]
            row = await result.fetchone()
            if row is None:
                return None
            source = dict(zip(columns, row))

            if into_id:
                if into_id == source_id:
                    return None
                found = await conn.execute(
                    f"""
                    SELECT id::text, type::text, name, aliases
                      FROM {settings.db_schema}.entities
                     WHERE user_id = %s AND id = %s
                    """,
                    (user_id, into_id),
                )
                target_row = await found.fetchone()
                if target_row is None:
                    return None
                target = dict(zip(columns, target_row))
            else:
                name = " ".join((into_name or "").split())
                if not name:
                    return None
                if _normalized(name) == _normalized(source["name"]):
                    return None
                created = await conn.execute(
                    f"""
                    INSERT INTO {settings.db_schema}.entities
                      (user_id, type, name, aliases)
                    VALUES (%s, %s, %s, '[]'::jsonb)
                    ON CONFLICT (user_id, type, name)
                      DO UPDATE SET name = EXCLUDED.name
                    RETURNING id::text, type::text, name, aliases
                    """,
                    (user_id, source["type"], name),
                )
                target = dict(zip(columns, await created.fetchone()))

            # Only links that are actually the source's, and only for items
            # this user owns — a note id from somewhere else moves nothing.
            moved = await conn.execute(
                f"""
                UPDATE {settings.db_schema}.links l
                   SET entity_id = %(target)s
                 WHERE l.entity_id = %(source)s
                   AND l.item_id = ANY(%(items)s::uuid[])
                   AND EXISTS (
                       SELECT 1 FROM {settings.db_schema}.items i
                        WHERE i.id = l.item_id AND i.user_id = %(user_id)s)
                   AND NOT EXISTS (
                       SELECT 1
                         FROM {settings.db_schema}.links x
                        WHERE x.item_id = l.item_id
                          AND x.entity_id = %(target)s
                          AND x.relation = l.relation)
                """,
                {
                    "target": target["id"],
                    "source": source_id,
                    "items": list(item_ids),
                    "user_id": user_id,
                },
            )
            moved_count = moved.rowcount or 0

            # A note already linked to both is not moved above; leaving it on
            # the source would keep it on a page it was just taken off.
            await conn.execute(
                f"""
                DELETE FROM {settings.db_schema}.links l
                 WHERE l.entity_id = %(source)s
                   AND l.item_id = ANY(%(items)s::uuid[])
                """,
                {"source": source_id, "items": list(item_ids)},
            )

            kept, alias_moved = aliases_after_split(
                list(source["aliases"]), target["name"], list(target["aliases"])
            )
            if alias_moved:
                await conn.execute(
                    f"""
                    UPDATE {settings.db_schema}.entities
                       SET aliases = %s
                     WHERE id = %s AND user_id = %s
                    """,
                    (Json(kept), source_id, user_id),
                )

            remaining = await conn.execute(
                f"SELECT count(*) FROM {settings.db_schema}.links WHERE entity_id = %s",
                (source_id,),
            )
            left = (await remaining.fetchone())[0]

            source_removed = False
            if left == 0:
                # Everything moved. What is left is a name with nothing behind
                # it, which is clutter rather than data — the notes are all
                # still there, on the other page.
                await conn.execute(
                    f"""
                    DELETE FROM {settings.db_schema}.entities
                     WHERE id = %s AND user_id = %s
                    """,
                    (source_id, user_id),
                )
                source_removed = True

        return {
            "source_id": source_id,
            "source_removed": source_removed,
            "target_id": target["id"],
            "target_name": target["name"],
            "target_created": into_id is None,
            "moved": moved_count,
            "aliases_moved": alias_moved,
        }

    async def list_people(
        self, user_id: str, query: Optional[str] = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Everyone who has ever been mentioned, and how recently (UC47).

        Ordered by the last thing said about them rather than alphabetically:
        the list is for finding somebody, and the person you spoke about this
        morning is a likelier target than the one from a year ago. Name order
        is the tiebreak, and it is what an untouched list falls back to.

        Searches aliases as well as the name, because the alias is precisely
        the name you are likely to type — you look up "Priya", not the "Priya
        Sharma" the row was promoted to.

        Args:
            user_id: Owner.
            query: Substring to match against name and aliases.
            limit: Hard cap; the list is not paginated because it is bounded by
                how many people one person talks about, not by how much they
                capture.

        Returns:
            People, most recently mentioned first.
        """
        where = ["e.user_id = %(user_id)s", "e.type = 'person'"]
        params: dict[str, Any] = {"user_id": user_id, "limit": limit}

        if query:
            # `aliases::text` is a crude way into a jsonb array of strings and
            # it is the right amount of machinery here: this table is bounded
            # by the number of people in a life, not by capture volume.
            where.append(
                "(e.name ILIKE %(pattern)s ESCAPE '\\'"
                " OR e.aliases::text ILIKE %(pattern)s ESCAPE '\\')"
            )
            params["pattern"] = f"%{_escape_like(query)}%"

        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT e.id::text,
                       e.name,
                       e.type::text,
                       e.aliases,
                       count(l.id) AS mentions,
                       max(i.created_at) AS last_mentioned
                  FROM {settings.db_schema}.entities e
                  LEFT JOIN {settings.db_schema}.links l ON l.entity_id = e.id
                  LEFT JOIN {settings.db_schema}.items i ON i.id = l.item_id
                 WHERE {' AND '.join(where)}
                 GROUP BY e.id, e.name, e.type, e.aliases
                 ORDER BY max(i.created_at) DESC NULLS LAST, e.name ASC
                 LIMIT %(limit)s
                """,
                params,
            )
            columns = [c.name for c in result.description or []]
            return [dict(zip(columns, row)) for row in await result.fetchall()]

    async def get_person(
        self, entity_id: str, user_id: str
    ) -> Optional[dict[str, Any]]:
        """One person, for the header of their page (UC46).

        Args:
            entity_id: Who.
            user_id: Owner; scoping this is what stops a guessed id reading
                someone else's contacts.

        Returns:
            The row with its mention count, or None.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT e.id::text,
                       e.name,
                       e.type::text,
                       e.aliases,
                       count(l.id) AS mentions,
                       max(i.created_at) AS last_mentioned,
                       e.created_at
                  FROM {settings.db_schema}.entities e
                  LEFT JOIN {settings.db_schema}.links l ON l.entity_id = e.id
                  LEFT JOIN {settings.db_schema}.items i ON i.id = l.item_id
                 WHERE e.id = %s AND e.user_id = %s
                 GROUP BY e.id, e.name, e.type, e.aliases, e.created_at
                """,
                (entity_id, user_id),
            )
            columns = [c.name for c in result.description or []]
            row = await result.fetchone()
            return dict(zip(columns, row)) if row else None

    async def person_items(
        self,
        entity_id: str,
        user_id: str,
        after: Optional[tuple[datetime, str]] = None,
        limit: int = 30,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Everything ever said about one person (UC46).

        Newest first and keyset-paginated, for the same reasons the Shelf is
        (D38, D39): this list only grows, and the thing you want is usually the
        thing you said last. Every state is included — a person page that hid
        what you had already done about somebody would be answering a different
        question than "what do I know here".

        Args:
            entity_id: Who.
            user_id: Owner. Scoped on the *item*, so a link cannot be used to
                read a row that is not yours.
            after: `(created_at, id)` of the previous page's last row.
            limit: Page size.

        Returns:
            The page, and whether another exists.
        """
        where = ["l.entity_id = %(entity_id)s", "i.user_id = %(user_id)s"]
        params: dict[str, Any] = {
            "entity_id": entity_id,
            "user_id": user_id,
            "limit": limit + 1,
        }
        if after is not None:
            where.append("(i.created_at, i.id) < (%(cursor_at)s, %(cursor_id)s)")
            params["cursor_at"], params["cursor_id"] = after

        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT i.id::text,
                       coalesce(nullif(i.parsed_text, ''), i.raw_text) AS text,
                       i.raw_text,
                       i.kind::text,
                       i.state::text,
                       i.due_at,
                       i.critical,
                       i.parse_status::text,
                       i.audio_path IS NOT NULL AS has_audio,
                       i.created_at
                  FROM {settings.db_schema}.links l
                  JOIN {settings.db_schema}.items i ON i.id = l.item_id
                 WHERE {' AND '.join(where)}
                 ORDER BY i.created_at DESC, i.id DESC
                 LIMIT %(limit)s
                """,
                params,
            )
            columns = [c.name for c in result.description or []]
            rows = [dict(zip(columns, row)) for row in await result.fetchall()]

        has_more = len(rows) > limit
        return rows[:limit], has_more

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

    # ------------------------------------------------- weekly digest (UC31)
    #
    # Both halves are plain SQL, which is the cost rule (`CLAUDE.md`) and also
    # the honest design: the digest is a report on rows, and a model asked to
    # summarise rows can only paraphrase what a query already knows.
    #
    # They are two queries rather than one because they are two different
    # kinds of statement. What decayed is **history** — an append-only fact in
    # `transitions`, true forever, unchanged by anything that happens next.
    # What is about to drop is a **forecast** off `items` as they stand right
    # now, and it moves the moment you touch anything. Joining them would
    # produce one list whose rows meant two different things.

    async def digest_moved(
        self, user_id: str, start: datetime, end: datetime, limit: int = 20
    ) -> list[dict[str, Any]]:
        """What left its old state during a week, and how (UC31).

        Reads `transitions`, not `items`, and that is the point: an item
        shelved by decay on Tuesday and reactivated by hand on Thursday still
        belongs in the week's digest, because the thing being reported is what
        happened, not where the item ended up. `state_now` carries the second
        fact separately.

        Three buckets, and they are not defined symmetrically because they are
        not the same kind of news:

        - `shelved` — `reason = 'decay'`. The system put it away on its own.
        - `dropped` — `reason = 'expiry'`. Likewise, terminally.
        - `done` — anything reaching `done`, however it was said: the tap, the
          notification button, or the state chips on item detail. You finished
          it either way, and this half of the digest is a summary rather than
          an account of the system's own decisions.

        A shelving or a drop the *user* performed is deliberately absent. That
        is not something to be told about — you pressed the button.

        `total` counts per bucket before the limit, so a truncated section can
        still say how much it is not showing.

        Args:
            user_id: Owner of the items.
            start: Inclusive start of the week.
            end: Exclusive end of the week.
            limit: Rows per bucket.

        Returns:
            Newest first, each row carrying its `bucket` and that bucket's
            `total`.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH moved AS (
                    SELECT t.item_id,
                           CASE
                             WHEN t.to_state = 'done' THEN 'done'
                             WHEN t.reason = 'expiry' THEN 'dropped'
                             ELSE 'shelved'
                           END AS bucket,
                           t.created_at AS at,
                           coalesce(nullif(i.parsed_text, ''), i.raw_text) AS text,
                           i.kind::text  AS kind,
                           i.state::text AS state_now,
                           i.due_at
                      FROM {settings.db_schema}.transitions t
                      JOIN {settings.db_schema}.items i ON i.id = t.item_id
                     WHERE i.user_id = %(user_id)s
                       AND t.created_at >= %(start)s
                       AND t.created_at <  %(end)s
                       AND (
                             (t.reason = 'decay'  AND t.to_state = 'shelved')
                          OR (t.reason = 'expiry' AND t.to_state = 'dropped')
                          OR  t.to_state = 'done'
                       )
                ),
                ranked AS (
                    SELECT *,
                           count(*)     OVER (PARTITION BY bucket) AS total,
                           row_number() OVER (
                               PARTITION BY bucket ORDER BY at DESC
                           ) AS rank
                      FROM moved
                )
                SELECT item_id::text AS id, text, kind, bucket, at, state_now,
                       due_at, total
                  FROM ranked
                 WHERE rank <= %(limit)s
                 ORDER BY at DESC
                """,
                {
                    "user_id": user_id,
                    "start": start,
                    "end": end,
                    "limit": limit,
                },
            )
            columns = [c.name for c in result.description or []]
            return [dict(zip(columns, row)) for row in await result.fetchall()]

    async def digest_expiring(
        self, user_id: str, warn_days: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Shelved items close enough to `DROP_AFTER_DAYS` to be worth naming.

        The forecast half of the digest, and the half with something to act
        on: everything here is still recoverable, and reading this list is
        what makes UC19 a decision rather than an accident.

        "Untouched since" is `greatest(state_changed_at, updated_at)`, the same
        expression the expiry sweep uses (D37). Deriving the drop date from it
        here rather than storing one keeps the two from drifting — a warning
        that names a date the sweep disagrees with is worse than no warning.

        Args:
            user_id: Owner of the items.
            warn_days: How far ahead to look.
            limit: Rows returned.

        Returns:
            Soonest to drop first, each row carrying `drops_at` and the `total`
            before the limit.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH shelved AS (
                    SELECT id,
                           coalesce(nullif(parsed_text, ''), raw_text) AS text,
                           kind::text AS kind,
                           due_at,
                           greatest(state_changed_at, updated_at) AS untouched_since,
                           greatest(state_changed_at, updated_at)
                             + make_interval(days => %(drop_after)s) AS drops_at
                      FROM {settings.db_schema}.items
                     WHERE user_id = %(user_id)s
                       AND state = 'shelved'
                ),
                soon AS (
                    SELECT *, count(*) OVER () AS total
                      FROM shelved
                     WHERE drops_at < now() + make_interval(days => %(warn)s)
                )
                SELECT id::text, text, kind, due_at, untouched_since, drops_at,
                       total
                  FROM soon
                 ORDER BY drops_at ASC
                 LIMIT %(limit)s
                """,
                {
                    "user_id": user_id,
                    "drop_after": settings.drop_after_days,
                    "warn": warn_days,
                    "limit": limit,
                },
            )
            columns = [c.name for c in result.description or []]
            return [dict(zip(columns, row)) for row in await result.fetchall()]

    # ---------------------------------------------------- calendar (UC43)

    async def has_calendar_work(self) -> bool:
        """Whether anything is waiting to be written to the calendar.

        Asked before authenticating, and that is the whole point. The tick is
        a short-lived process, so the access token cache dies with it and
        every tick would otherwise trade a JWT for a token just to discover
        there was nothing to sync — 1,440 round trips a day, for nothing.
        This is a covered index lookup on a table with one row per timed item.

        Returns:
            True if a link is dirty or an event is queued for removal.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                SELECT EXISTS (
                    SELECT 1 FROM {settings.db_schema}.calendar_links
                     WHERE sync_state IN ('pending', 'error')
                       AND attempts < %(max_attempts)s
                ) OR EXISTS (
                    SELECT 1 FROM {settings.db_schema}.calendar_deletions
                     WHERE attempts < %(max_attempts)s
                )
                """,
                {"max_attempts": settings.google_calendar_max_attempts},
            )
            row = await result.fetchone()
            return bool(row and row[0])

    async def claim_calendar_links(self) -> list[dict[str, Any]]:
        """Take the next batch of items whose calendar event is out of date.

        The attempt is counted here rather than after the write, for the same
        reason the push claim does it: a crash between "Google accepted it"
        and "we wrote that down" should cost one attempt, not loop forever.

        `wanted` is computed by the same SQL function the trigger uses, so the
        question "should this item have an event" has exactly one answer in
        the system. A row where `wanted` is false and there is no event id is
        already correct and simply gets its link removed.

        Returns:
            Rows carrying the item as it stands and the event as last synced.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH claimed AS (
                    SELECT l.item_id
                      FROM {settings.db_schema}.calendar_links l
                      JOIN {settings.db_schema}.items i ON i.id = l.item_id
                     WHERE l.sync_state IN ('pending', 'error')
                       AND l.attempts < %(max_attempts)s
                     ORDER BY l.last_synced_at ASC NULLS FIRST
                     LIMIT %(limit)s
                ),
                bumped AS (
                    UPDATE {settings.db_schema}.calendar_links l
                       SET attempts = l.attempts + 1
                      FROM claimed
                     WHERE l.item_id = claimed.item_id
                    RETURNING l.item_id
                )
                SELECT i.id::text      AS item_id,
                       i.user_id::text AS user_id,
                       {settings.db_schema}.calendar_summary(
                           i.parsed_text, i.raw_text) AS text,
                       i.raw_text,
                       i.due_at,
                       i.state::text   AS state,
                       {settings.db_schema}.calendar_wanted(
                           i.state, i.due_at) AS wanted,
                       l.google_event_id,
                       l.calendar_id
                  FROM bumped
                  JOIN {settings.db_schema}.calendar_links l
                    ON l.item_id = bumped.item_id
                  JOIN {settings.db_schema}.items i ON i.id = l.item_id
                """,
                {
                    "max_attempts": settings.google_calendar_max_attempts,
                    "limit": settings.google_calendar_batch_limit,
                },
            )
            columns = [c.name for c in result.description or []]
            return [dict(zip(columns, row)) for row in await result.fetchall()]

    async def mark_calendar_synced(
        self, item_id: str, google_event_id: str, calendar_id: str
    ) -> None:
        """Record that the calendar now matches the item.

        Attempts go back to zero here as well as on the trigger. A row that
        needed four goes to get through should not carry those four into the
        next edit, months later, and stall on the first hiccup.

        Args:
            item_id: The item.
            google_event_id: Google's id for its event.
            calendar_id: The calendar the event is on — stored rather than
                assumed, so that changing `GOOGLE_CALENDAR_ID` later cannot
                orphan the events already written to the old one.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                UPDATE {settings.db_schema}.calendar_links
                   SET google_event_id = %s,
                       calendar_id = %s,
                       sync_state = 'synced',
                       last_synced_at = now(),
                       attempts = 0,
                       error_detail = NULL
                 WHERE item_id = %s
                """,
                (google_event_id, calendar_id, item_id),
            )

    async def mark_calendar_failed(self, item_id: str, error: str) -> None:
        """Record why a sync did not happen, leaving it due for another go.

        The row stays claimable — `error` is in the claim's filter — until
        `attempts` runs out. It is never marked `synced`, so a failed write is
        never mistaken for a calendar that agrees.

        Args:
            item_id: The item.
            error: What went wrong, as the caller saw it.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                UPDATE {settings.db_schema}.calendar_links
                   SET sync_state = 'error', error_detail = %s
                 WHERE item_id = %s
                """,
                (error[:500], item_id),
            )

    async def forget_calendar_event(self, item_id: str) -> None:
        """Drop the stored event id but keep the row pending.

        For the case where Google says the event is gone but the item still
        wants one — somebody deleted it in the calendar UI. The app is the
        source of truth (D8), so the answer is to make a new one on the next
        tick, not to accept the deletion. Removing an item from the calendar
        is done by completing or dropping the item.

        Args:
            item_id: The item whose event has disappeared.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                UPDATE {settings.db_schema}.calendar_links
                   SET google_event_id = NULL,
                       sync_state = 'pending',
                       error_detail = NULL
                 WHERE item_id = %s
                """,
                (item_id,),
            )

    async def drop_calendar_link(self, item_id: str) -> None:
        """Forget an item's calendar link entirely.

        Called once the event is off the calendar and none is wanted. The row
        is removed rather than kept as a tombstone: if the item is reactivated
        with a time later, the trigger writes a fresh row and the item gets a
        fresh event, which is what "the event is a projection" means.

        Args:
            item_id: The item.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                DELETE FROM {settings.db_schema}.calendar_links
                 WHERE item_id = %s
                """,
                (item_id,),
            )

    async def claim_calendar_deletions(self) -> list[dict[str, Any]]:
        """Take the next batch of events whose item has been deleted (UC39).

        Returns:
            Rows with the event to remove and the calendar it is on.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                f"""
                WITH claimed AS (
                    SELECT id
                      FROM {settings.db_schema}.calendar_deletions
                     WHERE attempts < %(max_attempts)s
                     ORDER BY requested_at ASC
                     LIMIT %(limit)s
                ),
                bumped AS (
                    UPDATE {settings.db_schema}.calendar_deletions d
                       SET attempts = d.attempts + 1
                      FROM claimed
                     WHERE d.id = claimed.id
                    RETURNING d.id
                )
                SELECT d.id::text, d.google_event_id, d.calendar_id,
                       d.user_id::text AS user_id
                  FROM bumped
                  JOIN {settings.db_schema}.calendar_deletions d
                    ON d.id = bumped.id
                """,
                {
                    "max_attempts": settings.google_calendar_max_attempts,
                    "limit": settings.google_calendar_batch_limit,
                },
            )
            columns = [c.name for c in result.description or []]
            return [dict(zip(columns, row)) for row in await result.fetchall()]

    async def clear_calendar_deletion(self, deletion_id: str) -> None:
        """Forget a deletion that has been carried out.

        Args:
            deletion_id: The outbox row.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                DELETE FROM {settings.db_schema}.calendar_deletions
                 WHERE id = %s
                """,
                (deletion_id,),
            )

    async def fail_calendar_deletion(self, deletion_id: str, error: str) -> None:
        """Record why an event could not be removed.

        Args:
            deletion_id: The outbox row.
            error: What went wrong.
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                UPDATE {settings.db_schema}.calendar_deletions
                   SET last_error = %s
                 WHERE id = %s
                """,
                (error[:500], deletion_id),
            )


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
