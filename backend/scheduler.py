"""The one-minute tick: push delivery and decay (UC23, UC18, UC19).

Pure SQL and one HTTP call to the push service. No model call ever happens
here — decay, digests and counts are all things Postgres can answer, and
putting rows in front of Haiku is the cost rule this project is built around.

The tick is one ordered script, and the order is the design:

1. **Read the silence.** A push that was delivered and never answered, and is
   now old enough that the next one is due, is written down as `ignored` and
   counted against the item.
2. **Decay** anything whose declines have reached `SHELVE_AFTER_IGNORES`.
3. **Expire** anything that has sat on the shelf past `DROP_AFTER_DAYS`.
4. **Cancel** notifications belonging to items that are no longer `active`.
5. **Enqueue** a push for everything due with nothing outstanding.
6. **Send** what is queued.
7. **Announce the week**, once, on digest day (UC31).
8. **Reconcile the calendar** (UC43): every item whose Google event no longer
   matches it, in either direction — one that has just gained a due time, one
   whose text or time was edited, one that has been completed or dropped and
   whose event should come down.

Steps 1-3 run before step 5 so that an item shelving on this tick does not
also get a fresh push on it. Step 8 runs last, and after the sweeps for the
same reason: an item dropped by step 3 should have its event removed on the
tick that dropped it, not a minute later. Steps 2 and 3 write to `transitions` and nothing
else: UC22 was dropped, so decay is **silent** — nothing is pushed, nothing is
announced, and `transitions` plus the weekly digest (UC31) are the only places
it is visible at all. Step 7 is that digest, and it is the reason this file
cares about the calendar at all: everything else here is driven by how long
something has been waiting, and the digest alone is driven by what day it is.

One property is worth stating plainly, because the whole decay model rests on
it: **an item is never decayed by a push that did not go out.** `sent_at` is
written only when the push service accepted the message, and step 1 only reads
silence from rows that were actually sent. If Expo is down, or no device is
registered, or the token is dead, nothing decays — the tick stalls instead,
loudly, in the log.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backend import digest, gcal, push
from backend.config import capture_tz, settings
from backend.db import Database, close_db, get_db

logger = logging.getLogger(__name__)

# What the notification says, and how long it may be. Android truncates a long
# title itself, but a title that is truncated is a title nobody reads — the
# item's own words are the useful part and they come first.
_MAX_TITLE_CHARS = 90


@dataclass
class Survey:
    """What the tick was looking at before it did anything.

    The counts that say *why* a tick was quiet. "Nothing happened" and
    "something should have happened and did not" produce identical output
    without these, and telling them apart after the fact is the difference
    between reading one log line and re-deriving the state of the database by
    hand.

    Taken before the sweeps run, so the numbers describe the work the tick was
    about to consider rather than what survived it.
    """

    active: int = 0
    due_now: int = 0
    shelved: int = 0
    shelved_expired: int = 0
    open_pushes: int = 0
    open_pushes_overdue: int = 0
    queued_pushes: int = 0
    devices: int = 0
    calendar_dirty: int = 0
    calendar_stalled: int = 0

    def summary(self) -> str:
        """The considered half of the log line."""
        return (
            f"active={self.active} due={self.due_now} "
            f"shelved={self.shelved}/{self.shelved_expired}expired "
            f"open={self.open_pushes}/{self.open_pushes_overdue}overdue "
            f"queued={self.queued_pushes} devices={self.devices} "
            f"cal={self.calendar_dirty}dirty/{self.calendar_stalled}stalled"
        )


@dataclass
class TickResult:
    """What one tick did. Counts, so the log line is greppable."""

    ignored: int = 0
    shelved: int = 0
    dropped: int = 0
    cancelled: int = 0
    queued: int = 0
    sent: int = 0
    failed: int = 0
    stalled: int = 0
    digests_built: int = 0
    digests_sent: int = 0
    calendar_written: int = 0
    calendar_removed: int = 0
    calendar_failed: int = 0
    survey: Survey = field(default_factory=Survey)
    elapsed_ms: int = 0

    @property
    def quiet(self) -> bool:
        """Whether the tick did nothing at all — the usual case."""
        return not any(
            (
                self.ignored,
                self.shelved,
                self.dropped,
                self.cancelled,
                self.queued,
                self.sent,
                self.failed,
                self.digests_built,
                self.digests_sent,
                self.calendar_written,
                self.calendar_removed,
                self.calendar_failed,
            )
        )

    @property
    def undeliverable(self) -> bool:
        """Pushes waiting with nowhere to send them.

        Not an error — the scheduler leaves them queued on purpose rather than
        burning their attempts (D32) — but it is the one quiet state that looks
        exactly like a broken tick from the outside.
        """
        return self.survey.queued_pushes > 0 and self.survey.devices == 0

    def summary(self) -> str:
        """One line for the log."""
        return (
            f"considered[{self.survey.summary()}] "
            f"did[ignored={self.ignored} shelved={self.shelved} "
            f"dropped={self.dropped} cancelled={self.cancelled} "
            f"queued={self.queued} sent={self.sent} "
            f"failed={self.failed} stalled={self.stalled} "
            f"digests={self.digests_built}built/{self.digests_sent}sent "
            f"cal={self.calendar_written}written/{self.calendar_removed}removed"
            f"/{self.calendar_failed}failed] "
            f"{self.elapsed_ms}ms"
        )


# --------------------------------------------------------------- the steps


async def _survey(db: Database) -> Survey:
    """Count what the tick is about to look at.

    One query, run before the sweeps. It exists so a quiet tick can say *why*
    it was quiet: no items due is a different fact from items due with no
    device to send to, and both used to print the same nothing.

    Args:
        db: Database.

    Returns:
        The counts, as of now.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT
              (SELECT count(*) FROM {settings.db_schema}.items
                WHERE state = 'active') AS active,
              (SELECT count(*) FROM {settings.db_schema}.items
                WHERE state = 'active' AND due_at IS NOT NULL
                  AND due_at <= now()) AS due_now,
              (SELECT count(*) FROM {settings.db_schema}.items
                WHERE state = 'shelved') AS shelved,
              (SELECT count(*) FROM {settings.db_schema}.items
                WHERE state = 'shelved'
                  AND greatest(state_changed_at, updated_at)
                      < now() - make_interval(days => %(days)s)) AS shelved_expired,
              (SELECT count(*) FROM {settings.db_schema}.notifications
                WHERE sent_at IS NOT NULL AND responded_at IS NULL) AS open_pushes,
              (SELECT count(*) FROM {settings.db_schema}.notifications
                WHERE sent_at IS NOT NULL AND responded_at IS NULL
                  AND sent_at <= now()
                      - make_interval(mins => %(repeat)s)) AS open_pushes_overdue,
              (SELECT count(*) FROM {settings.db_schema}.notifications
                WHERE sent_at IS NULL AND responded_at IS NULL) AS queued_pushes,
              (SELECT count(*) FROM {settings.db_schema}.push_tokens
                WHERE disabled_at IS NULL) AS devices,
              (SELECT count(*) FROM {settings.db_schema}.calendar_links
                WHERE sync_state IN ('pending', 'error')
                  AND attempts < %(cal_attempts)s) AS calendar_dirty,
              (SELECT count(*) FROM {settings.db_schema}.calendar_links
                WHERE sync_state IN ('pending', 'error')
                  AND attempts >= %(cal_attempts)s) AS calendar_stalled
            """,
            {
                "days": settings.drop_after_days,
                "repeat": settings.push_repeat_minutes,
                "cal_attempts": settings.google_calendar_max_attempts,
            },
        )
        columns = [c.name for c in result.description or []]
        row = await result.fetchone()
        return Survey(**dict(zip(columns, row))) if row else Survey()


async def _sweep_ignored(db: Database) -> int:
    """Write down every push that went unanswered long enough (UC18).

    "Long enough" is `PUSH_REPEAT_MINUTES`: the moment the next push falls due
    is the moment the previous one's silence becomes an answer. That is the
    rule from `docs/data-model.md` — an `ignored` row is written when the next
    push comes due with no response to the last — and it is what makes
    `push_count` mean "declined this many times" rather than "sent this many
    times".

    Scoped to items that are still `active`: a push outstanding on an item the
    user has already dealt with some other way is stale, not ignored, and step
    4 closes it without counting it.

    Args:
        db: Database.

    Returns:
        How many pushes were counted as ignored.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            WITH overdue AS (
                SELECT n.id, n.item_id
                  FROM {settings.db_schema}.notifications n
                  JOIN {settings.db_schema}.items i ON i.id = n.item_id
                 WHERE n.sent_at IS NOT NULL
                   AND n.responded_at IS NULL
                   AND i.state = 'active'
                   AND n.sent_at <= now() - make_interval(mins => %(repeat)s)
            ),
            answered AS (
                UPDATE {settings.db_schema}.notifications n
                   SET responded_at = now(), response = 'ignored'
                  FROM overdue
                 WHERE n.id = overdue.id
                RETURNING n.item_id
            ),
            bumped AS (
                UPDATE {settings.db_schema}.items i
                   SET push_count = i.push_count + counted.n
                  FROM (
                      SELECT item_id, count(*) AS n FROM answered GROUP BY item_id
                  ) counted
                 WHERE i.id = counted.item_id
                RETURNING i.id
            )
            SELECT count(*) FROM bumped
            """,
            {"repeat": settings.push_repeat_minutes},
        )
        row = await result.fetchone()
        return int(row[0]) if row else 0


async def _sweep_decay(db: Database) -> int:
    """Shelve anything that has been declined enough times (UC18).

    Ignoring and snoozing are the same answer — "not now" — so both counters
    feed the one threshold. This is the design bet the whole app rests on:
    repeated non-response is a decision, and the system acts on it instead of
    asking the user to.

    **Silently.** UC22 was dropped, so the `transitions` row is the entire
    record; nothing is sent and nothing is said.

    Args:
        db: Database.

    Returns:
        How many items were shelved.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            WITH decayed AS (
                UPDATE {settings.db_schema}.items
                   SET state = 'shelved'
                 WHERE state = 'active'
                   AND push_count + snooze_count >= %(threshold)s
                RETURNING id
            ),
            logged AS (
                INSERT INTO {settings.db_schema}.transitions
                  (item_id, from_state, to_state, reason)
                SELECT id, 'active', 'shelved', 'decay' FROM decayed
            )
            SELECT count(*) FROM decayed
            """,
            {"threshold": settings.shelve_after_ignores},
        )
        row = await result.fetchone()
        return int(row[0]) if row else 0


async def _sweep_expiry(db: Database) -> int:
    """Drop anything that has sat untouched on the shelf too long (UC19).

    "Untouched" is read as the later of the two timestamps the row already
    carries: when it was shelved, and when it was last written to. Editing a
    shelved item is touching it, and restarting its clock is what stops the
    system throwing away something you were demonstrably still thinking about.

    Silent, like decay.

    Args:
        db: Database.

    Returns:
        How many items were dropped.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            WITH expired AS (
                UPDATE {settings.db_schema}.items
                   SET state = 'dropped'
                 WHERE state = 'shelved'
                   AND greatest(state_changed_at, updated_at)
                       < now() - make_interval(days => %(days)s)
                RETURNING id
            ),
            logged AS (
                INSERT INTO {settings.db_schema}.transitions
                  (item_id, from_state, to_state, reason)
                SELECT id, 'shelved', 'dropped', 'expiry' FROM expired
            )
            SELECT count(*) FROM expired
            """,
            {"days": settings.drop_after_days},
        )
        row = await result.fetchone()
        return int(row[0]) if row else 0


async def _cancel_stale(db: Database) -> int:
    """Close notifications for items that are no longer `active`.

    An item can leave `active` while a push is still outstanding — decayed on
    this very tick, or moved by hand (UC21). Those pushes are not ignored and
    must never be counted as such; a queued one is deleted before it goes out,
    and a delivered one is closed with `responded_at` set and `response` left
    null, which is this table's way of saying "stopped waiting, no answer".

    Args:
        db: Database.

    Returns:
        How many notifications were cancelled or closed.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            WITH dropped AS (
                DELETE FROM {settings.db_schema}.notifications n
                 USING {settings.db_schema}.items i
                 WHERE i.id = n.item_id
                   AND n.sent_at IS NULL
                   AND i.state <> 'active'
                RETURNING n.id
            ),
            closed AS (
                UPDATE {settings.db_schema}.notifications n
                   SET responded_at = now()
                  FROM {settings.db_schema}.items i
                 WHERE i.id = n.item_id
                   AND n.sent_at IS NOT NULL
                   AND n.responded_at IS NULL
                   AND i.state <> 'active'
                RETURNING n.id
            )
            SELECT (SELECT count(*) FROM dropped) + (SELECT count(*) FROM closed)
            """
        )
        row = await result.fetchone()
        return int(row[0]) if row else 0


async def _enqueue_due(db: Database) -> int:
    """Queue a push for everything due with nothing already outstanding (UC23).

    The `NOT EXISTS` is the whole of the "and no notification has been sent
    yet" rule, and it covers the repeat case too: while a push is queued or
    awaiting an answer, the item has one outstanding and gets no second one.
    Once step 1 has written the silence down, the item is free again and this
    queues the next — which is what makes `PUSH_REPEAT_MINUTES` the interval
    between pushes as well as the patience before an ignore.

    Args:
        db: Database.

    Returns:
        How many pushes were queued.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            WITH candidates AS (
                SELECT i.id
                  FROM {settings.db_schema}.items i
                 WHERE i.state = 'active'
                   AND i.due_at IS NOT NULL
                   AND i.due_at <= now()
                   AND NOT EXISTS (
                       SELECT 1
                         FROM {settings.db_schema}.notifications n
                        WHERE n.item_id = i.id
                          AND (n.sent_at IS NULL OR n.responded_at IS NULL)
                   )
            ),
            queued AS (
                INSERT INTO {settings.db_schema}.notifications
                  (item_id, scheduled_for, tier)
                SELECT id, now(), 'push' FROM candidates
                RETURNING id
            )
            SELECT count(*) FROM queued
            """
        )
        row = await result.fetchone()
        return int(row[0]) if row else 0


# ---------------------------------------------------------------- delivery


def _title(text: str) -> str:
    """The item's own words, trimmed to something a shade will show."""
    text = " ".join(text.split())
    if len(text) <= _MAX_TITLE_CHARS:
        return text
    return text[: _MAX_TITLE_CHARS - 1].rstrip() + "…"


def _body(due_at: Optional[datetime], repeat: bool) -> str:
    """The line under the title: when it was due, and whether we have asked.

    Deliberately flat. This is a reminder, not a report on the item's history —
    and it is emphatically not an announcement of a state change, which is the
    thing UC22 dropped.

    Args:
        due_at: When the item fell due.
        repeat: Whether an earlier push for this item went unanswered.

    Returns:
        One short line.
    """
    if due_at is None:
        return "Still open" if repeat else "Due"
    when = due_at.astimezone(capture_tz()).strftime("%-I:%M %p").lower()
    return f"Still open · due {when}" if repeat else f"Due {when}"


async def _claim(db: Database) -> list[dict[str, Any]]:
    """Take the next batch of queued pushes, counting the attempt up front.

    The attempt is counted before the send rather than after, so that a crash
    between "Expo accepted it" and "we wrote that down" costs one attempt
    instead of looping forever.

    Only items whose owner has a live device are claimed. A push with nowhere
    to go is left queued untouched: burning attempts on a user who has not
    installed the build yet would stall the item permanently for a reason that
    has nothing to do with them.

    Args:
        db: Database.

    Returns:
        Rows with everything a message needs.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            WITH claimed AS (
                SELECT n.id
                  FROM {settings.db_schema}.notifications n
                  JOIN {settings.db_schema}.items i ON i.id = n.item_id
                 WHERE n.sent_at IS NULL
                   AND n.responded_at IS NULL
                   AND n.scheduled_for <= now()
                   AND n.attempts < %(max_attempts)s
                   AND i.state = 'active'
                   AND EXISTS (
                       SELECT 1
                         FROM {settings.db_schema}.push_tokens t
                        WHERE t.user_id = i.user_id
                          AND t.disabled_at IS NULL
                   )
                 ORDER BY n.scheduled_for
                 LIMIT %(limit)s
            ),
            bumped AS (
                UPDATE {settings.db_schema}.notifications n
                   SET attempts = n.attempts + 1
                  FROM claimed
                 WHERE n.id = claimed.id
                RETURNING n.id
            )
            SELECT n.id::text AS notification_id,
                   i.id::text AS item_id,
                   i.user_id::text AS user_id,
                   coalesce(nullif(i.parsed_text, ''), i.raw_text) AS text,
                   i.due_at,
                   i.critical,
                   i.push_count
              FROM bumped
              JOIN {settings.db_schema}.notifications n ON n.id = bumped.id
              JOIN {settings.db_schema}.items i ON i.id = n.item_id
            """,
            {
                "max_attempts": settings.push_max_attempts,
                "limit": settings.push_batch_limit,
            },
        )
        columns = [c.name for c in result.description or []]
        return [dict(zip(columns, row)) for row in await result.fetchall()]


async def _tokens_for(db: Database, user_ids: list[str]) -> dict[str, list[str]]:
    """Live push tokens, grouped by user.

    Args:
        db: Database.
        user_ids: Owners to look up.

    Returns:
        User id to their live tokens.
    """
    if not user_ids:
        return {}
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT user_id::text, token
              FROM {settings.db_schema}.push_tokens
             WHERE user_id = ANY(%s) AND disabled_at IS NULL
            """,
            (user_ids,),
        )
        tokens: dict[str, list[str]] = {}
        for user_id, token in await result.fetchall():
            tokens.setdefault(user_id, []).append(token)
        return tokens


async def _mark_sent(
    db: Database, notification_id: str, ticket_id: Optional[str]
) -> None:
    """Record that a push actually left.

    This is the write the whole decay model hangs off: only a notification
    with `sent_at` can ever be read as ignored.

    Args:
        db: Database.
        notification_id: The row to mark.
        ticket_id: Expo's receipt id, for tracing a push the phone never showed.
    """
    async with db.connection() as conn:
        await conn.execute(
            f"""
            UPDATE {settings.db_schema}.notifications
               SET sent_at = now(), ticket_id = %s, last_error = NULL
             WHERE id = %s
            """,
            (ticket_id, notification_id),
        )


async def _mark_failed(db: Database, notification_ids: list[str], error: str) -> None:
    """Record why a push did not leave, without marking it sent.

    Args:
        db: Database.
        notification_ids: Rows that failed.
        error: What went wrong, as the sender saw it.
    """
    if not notification_ids:
        return
    async with db.connection() as conn:
        await conn.execute(
            f"""
            UPDATE {settings.db_schema}.notifications
               SET last_error = %s
             WHERE id = ANY(%s::uuid[])
            """,
            (error[:500], notification_ids),
        )


async def _count_stalled(db: Database) -> int:
    """Queued pushes that have run out of attempts.

    They are not marked sent and never will be, so nothing decays from them —
    but they are also not going to be delivered, which is worth saying out loud
    every tick rather than discovering in a month.

    Args:
        db: Database.

    Returns:
        How many rows have given up.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT count(*)
              FROM {settings.db_schema}.notifications
             WHERE sent_at IS NULL
               AND responded_at IS NULL
               AND attempts >= %s
            """,
            (settings.push_max_attempts,),
        )
        row = await result.fetchone()
        return int(row[0]) if row else 0


async def _send_queued(db: Database) -> tuple[int, int]:
    """Deliver the queued pushes (UC23).

    One message per device per notification, all in one request to Expo.

    Args:
        db: Database.

    Returns:
        How many notifications were sent, and how many failed to go out.
    """
    claimed = await _claim(db)
    if not claimed:
        return (0, 0)

    tokens = await _tokens_for(db, sorted({row["user_id"] for row in claimed}))

    messages: list[push.PushMessage] = []
    # Parallel to `messages`: which notification each one belongs to. Expo
    # answers positionally, so this is how a ticket finds its way home.
    owners: list[str] = []

    for row in claimed:
        for token in tokens.get(row["user_id"], []):
            messages.append(
                push.PushMessage(
                    token=token,
                    title=_title(row["text"] or "Something you captured"),
                    body=_body(row["due_at"], repeat=row["push_count"] > 0),
                    data={
                        "itemId": row["item_id"],
                        "notificationId": row["notification_id"],
                    },
                )
            )
            owners.append(row["notification_id"])

    if not messages:
        # `_claim` only takes rows whose owner has a live token, so this means
        # the token was disabled between the two queries. Next tick.
        return (0, 0)

    try:
        tickets = await push.send(messages)
    except push.PushError as e:
        logger.error("Push service refused the batch: %s", e)
        await _mark_failed(db, [row["notification_id"] for row in claimed], str(e))
        return (0, len(claimed))

    accepted: dict[str, Optional[str]] = {}
    refused: dict[str, str] = {}
    alive: list[str] = []

    for notification_id, ticket in zip(owners, tickets):
        if ticket.ok:
            accepted.setdefault(notification_id, ticket.ticket_id)
            alive.append(ticket.token)
            continue
        refused[notification_id] = ticket.message or "refused"
        if ticket.token_is_dead:
            # The app is gone from that device. Registering again is the only
            # thing that brings the token back (UC23).
            await db.disable_push_token(ticket.token, ticket.error or "refused")
            logger.warning("Disabled a dead push token: %s", ticket.error)

    await db.note_push_success(alive)

    for notification_id, ticket_id in accepted.items():
        await _mark_sent(db, notification_id, ticket_id)

    failed = [n for n in refused if n not in accepted]
    for notification_id in failed:
        await _mark_failed(db, [notification_id], refused[notification_id])
        logger.warning(
            "Push for notification %s did not go out: %s",
            notification_id,
            refused[notification_id],
        )

    return (len(accepted), len(failed))


# ------------------------------------------------- the weekly digest (UC31)
#
# The only step here driven by the calendar rather than by elapsed time, and
# the only one whose absence is invisible: if pushes stop, you notice within a
# day; if the digest stops, you notice a month later, having quietly lost the
# one window onto silent decay. Hence the log line on every path, including
# the one where there was nothing to say.


async def _digest_users(db: Database, period_start: datetime) -> list[str]:
    """Whose week still needs building.

    Users with a live device and no digest row for this week yet. Two filters
    doing two jobs: the device is there because a digest that cannot be
    delivered is not worth computing — and unlike an item reminder it is not
    worth keeping for later either (`digest.is_stale`) — and the `NOT EXISTS`
    is there because this runs every minute forever. Asking one indexed
    question is what keeps the steady-state cost of a weekly feature at one
    cheap query per tick rather than a full digest build every sixty seconds.

    Args:
        db: Database.
        period_start: Start of the week being considered.

    Returns:
        User ids, usually none.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT DISTINCT t.user_id::text
              FROM {settings.db_schema}.push_tokens t
             WHERE t.disabled_at IS NULL
               AND NOT EXISTS (
                   SELECT 1
                     FROM {settings.db_schema}.digests d
                    WHERE d.user_id = t.user_id
                      AND d.period_start = %s
               )
            """,
            (period_start,),
        )
        return [row[0] for row in await result.fetchall()]


async def _record_digest(
    db: Database, user_id: str, week: "digest.Digest"
) -> Optional[str]:
    """Claim this week for this user, exactly once.

    The unique constraint on `(user_id, period_start)` is what makes the tick
    idempotent: it runs every minute all Sunday, and every run after the first
    hits the conflict and does nothing. Doing this in the database rather than
    with a "have I already?" read is the difference between a rule and a race.

    An empty week is still written down, so that a digest arriving late in the
    day — because something decayed at noon — cannot happen: the week was
    already considered and closed.

    Args:
        db: Database.
        user_id: Whose week.
        week: The built digest, for its counts.

    Returns:
        The new row's id, or None if this week was already claimed.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            INSERT INTO {settings.db_schema}.digests
              (user_id, period_start, period_end, shelved, dropped, expiring, empty)
            VALUES (%(user_id)s, %(start)s, %(end)s,
                    %(shelved)s, %(dropped)s, %(expiring)s, %(empty)s)
            ON CONFLICT (user_id, period_start) DO NOTHING
            RETURNING id::text
            """,
            {
                "user_id": user_id,
                "start": week.period_start,
                "end": week.period_end,
                "shelved": week.shelved_total,
                "dropped": week.dropped_total,
                "expiring": week.expiring_total,
                "empty": week.empty,
            },
        )
        row = await result.fetchone()
        return row[0] if row else None


async def _claim_digests(db: Database) -> list[dict[str, Any]]:
    """Take the outstanding digests, counting the attempt up front.

    Same shape as `_claim` for item pushes and for the same reason: a crash
    between "Expo accepted it" and "we wrote that down" costs one attempt
    rather than looping.

    Args:
        db: Database.

    Returns:
        Rows with everything the message needs.
    """
    async with db.connection() as conn:
        result = await conn.execute(
            f"""
            WITH claimed AS (
                SELECT d.id
                  FROM {settings.db_schema}.digests d
                 WHERE d.sent_at IS NULL
                   AND d.empty = false
                   AND d.attempts < %(max_attempts)s
                   AND EXISTS (
                       SELECT 1
                         FROM {settings.db_schema}.push_tokens t
                        WHERE t.user_id = d.user_id
                          AND t.disabled_at IS NULL
                   )
                 ORDER BY d.period_end
            ),
            bumped AS (
                UPDATE {settings.db_schema}.digests d
                   SET attempts = d.attempts + 1
                  FROM claimed
                 WHERE d.id = claimed.id
                RETURNING d.id
            )
            SELECT d.id::text AS digest_id,
                   d.user_id::text AS user_id,
                   d.period_start,
                   d.period_end,
                   d.shelved,
                   d.dropped,
                   d.expiring
              FROM bumped
              JOIN {settings.db_schema}.digests d ON d.id = bumped.id
            """,
            {"max_attempts": settings.digest_max_attempts},
        )
        columns = [c.name for c in result.description or []]
        return [dict(zip(columns, row)) for row in await result.fetchall()]


def _digest_body(row: dict[str, Any]) -> str:
    """The line under the digest's title: the counts, as they were built.

    Args:
        row: A claimed digest row.

    Returns:
        One short line.
    """
    parts = [
        (row["shelved"], "shelved"),
        (row["dropped"], "dropped"),
        (row["expiring"], "about to drop"),
    ]
    return " · ".join(f"{n} {label}" for n, label in parts if n) or "Nothing moved"


async def _send_digests(db: Database, now: datetime) -> tuple[int, int]:
    """Build this week's digest if it is due, and deliver what is outstanding.

    Two halves, deliberately separate. Building claims the week and can only
    happen once; sending retries. A digest whose send keeps failing is *not*
    retried forever — past `DIGEST_MAX_AGE_HOURS` it is abandoned, because a
    summary of last week delivered on Wednesday is not late news, it is wrong
    news. That is the opposite of the rule for item pushes (D32), and the
    difference is that a due item is still due whenever the reminder lands.

    Args:
        db: Database.
        now: The instant the tick is running at.

    Returns:
        How many digests were built, and how many were sent.
    """
    start, end = digest.period_for(now)
    built = 0

    # Outside the window there is nothing to build, and saying so before
    # touching a user is what keeps this a once-a-week feature rather than a
    # once-a-minute one. Past the window the week is abandoned: the tick was
    # not running when it ended, and no row is written, so the next digest day
    # starts clean.
    for user_id in [] if digest.is_stale(end, now) else await _digest_users(db, start):
        week = await digest.build(db, user_id, now)
        digest_id = await _record_digest(db, user_id, week)
        if digest_id is None:
            continue
        built += 1
        logger.info(
            "digest for %s week ending %s: %s%s",
            user_id,
            week.period_end.date(),
            week.headline(),
            " (not sent — nothing to report)" if week.empty else "",
        )

    claimed = await _claim_digests(db)
    if not claimed:
        return (built, 0)

    fresh = [row for row in claimed if not digest.is_stale(row["period_end"], now)]
    for row in claimed:
        if row not in fresh:
            logger.warning(
                "Abandoning the digest for week ending %s: it is older than "
                "%sh and a stale summary is worse than none",
                row["period_end"].date(),
                settings.digest_max_age_hours,
            )
    if not fresh:
        return (built, 0)

    tokens = await _tokens_for(db, sorted({row["user_id"] for row in fresh}))

    messages: list[push.PushMessage] = []
    owners: list[str] = []
    for row in fresh:
        for token in tokens.get(row["user_id"], []):
            messages.append(
                push.PushMessage(
                    token=token,
                    title="Your week on the shelf",
                    body=_digest_body(row),
                    data={"digest": row["period_start"].date().isoformat()},
                    channel_id=settings.digest_channel_id,
                    # No Done and no Snooze: those act on one item and this is
                    # about several. Tapping it opens the digest.
                    category_id="",
                    high_priority=False,
                )
            )
            owners.append(row["digest_id"])

    if not messages:
        return (built, 0)

    try:
        tickets = await push.send(messages)
    except push.PushError as e:
        logger.error("Push service refused the digest batch: %s", e)
        await _mark_digest_failed(db, [row["digest_id"] for row in fresh], str(e))
        return (built, 0)

    accepted: dict[str, Optional[str]] = {}
    refused: dict[str, str] = {}
    for digest_id, ticket in zip(owners, tickets):
        if ticket.ok:
            accepted.setdefault(digest_id, ticket.ticket_id)
            continue
        refused[digest_id] = ticket.message or "refused"
        if ticket.token_is_dead:
            await db.disable_push_token(ticket.token, ticket.error or "refused")

    for digest_id, ticket_id in accepted.items():
        await _mark_digest_sent(db, digest_id, ticket_id)
    for digest_id in (d for d in refused if d not in accepted):
        await _mark_digest_failed(db, [digest_id], refused[digest_id])
        logger.warning("The digest did not go out: %s", refused[digest_id])

    return (built, len(accepted))


async def _mark_digest_sent(
    db: Database, digest_id: str, ticket_id: Optional[str]
) -> None:
    """Record that a digest actually left.

    Args:
        db: Database.
        digest_id: The row to mark.
        ticket_id: Expo's receipt id.
    """
    async with db.connection() as conn:
        await conn.execute(
            f"""
            UPDATE {settings.db_schema}.digests
               SET sent_at = now(), ticket_id = %s, last_error = NULL
             WHERE id = %s
            """,
            (ticket_id, digest_id),
        )


async def _mark_digest_failed(db: Database, digest_ids: list[str], error: str) -> None:
    """Record why a digest did not leave, without marking it sent.

    Args:
        db: Database.
        digest_ids: Rows that failed.
        error: What went wrong.
    """
    if not digest_ids:
        return
    async with db.connection() as conn:
        await conn.execute(
            f"""
            UPDATE {settings.db_schema}.digests
               SET last_error = %s
             WHERE id = ANY(%s::uuid[])
            """,
            (error[:500], digest_ids),
        )


# --------------------------------------------------- the calendar (UC43)

# One-way, and this is the only place it happens. The database decides *what*
# is out of date — a trigger marks a link pending whenever an item's time,
# text or state moves (migration 007) — and this decides what to do about it.
# Nothing here reads Google's copy: if they disagree, the item wins (D8).


async def _sync_calendar(db: Database) -> tuple[int, int, int]:
    """Bring the calendar back in line with the items (UC43).

    Three shapes of work, all of them falling out of one question the database
    already answered — should this item have an event?

    - **Yes, and it has none.** Create one and remember its id.
    - **Yes, and it has one.** Patch it. The item's time or text has moved.
    - **No, and it has one.** Take it down: the item was completed, dropped,
      or had its due time cleared.

    Plus the case the link table cannot hold, because the item is gone: UC39
    deletes leave their event id in `calendar_deletions` on the way out, and
    those are drained here too.

    The token is fetched **before** anything is claimed. A claim counts an
    attempt against every row it takes, and an expired key or a revoked share
    would otherwise spend the whole backlog's attempts in one tick on a
    failure that has nothing to do with any individual item.

    Args:
        db: Database.

    Returns:
        How many events were written, removed, and failed.
    """
    if not gcal.enabled():
        return 0, 0, 0

    # Nothing to sync is the overwhelmingly common case, and this step is the
    # only one in the tick that would pay a network round trip to find that
    # out: the tick is a short-lived process, so the token cache never
    # survives to a second tick.
    if not await db.has_calendar_work():
        return 0, 0, 0

    try:
        await gcal.access_token()
    except gcal.CalendarError as e:
        # Nothing is claimed, so nothing spends an attempt. The log is the
        # whole point of this branch: a calendar that silently stops syncing
        # looks exactly like a calendar with nothing to sync.
        logger.error("calendar: cannot authenticate, skipping this tick: %s", e)
        return 0, 0, 0

    default_calendar = settings.google_calendar_id
    written = removed = failed = 0

    for row in await db.claim_calendar_links():
        item_id = row["item_id"]
        # The calendar the event actually lives on, which is not necessarily
        # the configured one: changing GOOGLE_CALENDAR_ID must not orphan the
        # events already written to the old calendar.
        target = row["calendar_id"] or default_calendar

        try:
            if not row["wanted"]:
                if row["google_event_id"]:
                    await gcal.delete_event(target, row["google_event_id"])
                    removed += 1
                await db.drop_calendar_link(item_id)
                continue

            event = gcal.CalendarEvent(
                item_id=item_id,
                text=row["text"] or "",
                due_at=row["due_at"],
                raw_text=row["raw_text"] or "",
            )

            if row["google_event_id"]:
                await gcal.patch_event(target, row["google_event_id"], event)
                await db.mark_calendar_synced(item_id, row["google_event_id"], target)
            else:
                event_id = await gcal.create_event(default_calendar, event)
                await db.mark_calendar_synced(item_id, event_id, default_calendar)
            written += 1

        except gcal.CalendarError as e:
            if e.gone:
                # The event was deleted in Google and the item still wants
                # one. The app is the source of truth, so it comes back on the
                # next tick (D8) — the way to take something off the calendar
                # is to complete or drop the item. `attempts` is deliberately
                # not reset, so a pathological create/delete loop is bounded.
                logger.info(
                    "calendar: event for item %s is gone; recreating next tick",
                    item_id,
                )
                await db.forget_calendar_event(item_id)
                continue

            failed += 1
            await db.mark_calendar_failed(item_id, str(e))
            log = logger.error if not e.retryable else logger.warning
            log("calendar: item %s did not sync: %s", item_id, e)

    for row in await db.claim_calendar_deletions():
        try:
            await gcal.delete_event(row["calendar_id"], row["google_event_id"])
        except gcal.CalendarError as e:
            failed += 1
            await db.fail_calendar_deletion(row["id"], str(e))
            log = logger.error if not e.retryable else logger.warning
            log("calendar: event %s not removed: %s", row["google_event_id"], e)
            continue
        await db.clear_calendar_deletion(row["id"])
        removed += 1

    return written, removed, failed


# ----------------------------------------------------------------- the tick


async def tick(db: Optional[Database] = None) -> TickResult:
    """Run one pass of the scheduler.

    Args:
        db: Database to use. Defaults to the process-wide one.

    Returns:
        Counts of what each step did.
    """
    db = db or get_db()
    started = time.monotonic()
    result = TickResult()

    result.survey = await _survey(db)
    logger.debug("survey: %s", result.survey.summary())

    for name, step in (
        ("ignored", _sweep_ignored),
        ("shelved", _sweep_decay),
        ("dropped", _sweep_expiry),
        ("cancelled", _cancel_stale),
        ("queued", _enqueue_due),
    ):
        count = await step(db)
        setattr(result, name, count)
        logger.debug("step %s: %s", name, count)

    result.sent, result.failed = await _send_queued(db)
    logger.debug("step send: sent=%s failed=%s", result.sent, result.failed)

    result.digests_built, result.digests_sent = await _send_digests(
        db, datetime.now(timezone.utc)
    )
    logger.debug(
        "step digest: built=%s sent=%s", result.digests_built, result.digests_sent
    )

    (
        result.calendar_written,
        result.calendar_removed,
        result.calendar_failed,
    ) = await _sync_calendar(db)
    logger.debug(
        "step calendar: written=%s removed=%s failed=%s",
        result.calendar_written,
        result.calendar_removed,
        result.calendar_failed,
    )

    result.stalled = await _count_stalled(db)
    result.elapsed_ms = int((time.monotonic() - started) * 1000)

    return result


async def run_once() -> TickResult:
    """One tick, with the connection pool opened and closed around it.

    This is what cron runs. A tick is milliseconds of work, so paying for a
    pool per invocation is cheaper than keeping a second long-lived process
    around to avoid it.

    Returns:
        What the tick did.
    """
    db = get_db()
    try:
        result = await tick(db)
    finally:
        await close_db()

    # Every tick logs, including the quiet ones. Silence used to mean either
    # "nothing to do" or "it died before it got anywhere", and those two
    # readings cost twenty minutes to tell apart once. A line a minute is
    # nothing to journald and it is the difference between reading the log and
    # reconstructing the database by hand.
    logger.info("tick: %s", result.summary())

    if result.stalled:
        logger.error(
            "%s queued pushes have exhausted their attempts and will not be "
            "delivered; nothing will decay from them",
            result.stalled,
        )
    if result.survey.calendar_stalled:
        logger.error(
            "%s calendar link(s) have exhausted their attempts; those items "
            "are not on the calendar and will not be until they are edited",
            result.survey.calendar_stalled,
        )
    if result.undeliverable:
        logger.warning(
            "%s push(es) queued with no registered device; leaving them queued "
            "rather than spending their attempts. Open the app to register one",
            result.survey.queued_pushes,
        )

    return result


def main() -> None:
    """Entry point for the cron tick.

    Anything that escapes is logged with its traceback and exits non-zero, so
    a broken tick shows up twice: as a stack trace in `journalctl -u
    shelf-tick`, and as a failed unit in `systemctl status`. Letting it
    propagate bare would still print, but a tick that ran and did nothing and
    a tick that died have to be distinguishable at a glance.

    Set `DEBUG=1` for a line per step.
    """
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_once())
    except Exception:
        logger.exception("tick failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
