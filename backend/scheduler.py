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

Steps 1-3 run before step 5 so that an item shelving on this tick does not
also get a fresh push on it. Steps 2 and 3 write to `transitions` and nothing
else: UC22 was dropped, so decay is **silent** — nothing is pushed, nothing is
announced, and `transitions` plus the weekly digest (UC31) are the only places
it is visible at all.

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
from datetime import datetime
from typing import Any, Optional

from backend import push
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

    def summary(self) -> str:
        """The considered half of the log line."""
        return (
            f"active={self.active} due={self.due_now} "
            f"shelved={self.shelved}/{self.shelved_expired}expired "
            f"open={self.open_pushes}/{self.open_pushes_overdue}overdue "
            f"queued={self.queued_pushes} devices={self.devices}"
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
            f"failed={self.failed} stalled={self.stalled}] "
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
                WHERE disabled_at IS NULL) AS devices
            """,
            {"days": settings.drop_after_days, "repeat": settings.push_repeat_minutes},
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
