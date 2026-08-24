"""When the weekly digest covers, and what week it is talking about (UC31).

Just the arithmetic. The digest's *content* is two SQL queries in
`backend/db.py`, its *delivery* is a step in `backend/scheduler.py`, and this
module is the one thing both of them have to agree on: which seven days are
"the week".

The rule is a single sentence — **the week ending at the most recent digest
moment that has already passed** — and it is worth having in one place because
the two callers ask it from opposite directions. The tick asks "has a new week
ended since the last digest I sent?"; the screen asks "which week am I looking
at?". Both have to answer with the same pair of timestamps or the notification
would announce one week and open another.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from backend.config import capture_tz, digest_weekday, settings
from backend.db import Database


def _moment_on(day: date) -> datetime:
    """The digest instant on a given local day.

    Built by combining a date with a wall-clock time rather than by adding
    `timedelta`s to an instant. The difference only shows up under DST, and
    only as the digest sliding an hour — but a "9am Sunday" that is sometimes
    8am is exactly the kind of bug nobody reports and nobody can reproduce.

    Args:
        day: The local calendar day.

    Returns:
        That day at the configured hour, in the capture timezone.
    """
    hour = min(max(settings.digest_hour, 0), 23)
    return datetime.combine(day, time(hour=hour), tzinfo=capture_tz())


def period_for(now: datetime) -> tuple[datetime, datetime]:
    """The week the current digest covers.

    Args:
        now: The instant to answer as of. Any timezone; it is converted.

    Returns:
        `(start, end)`, half-open. `end` is the most recent digest moment at or
        before `now`; `start` is seven days earlier.
    """
    local = now.astimezone(capture_tz())
    back = (local.weekday() - digest_weekday()) % 7
    day = local.date() - timedelta(days=back)

    # `back` lands on the right weekday but says nothing about the hour: on
    # the digest day itself, before the digest hour, the week that ended is
    # the one before.
    if _moment_on(day) > local:
        day -= timedelta(days=7)

    return (_moment_on(day - timedelta(days=7)), _moment_on(day))


def is_stale(period_end: datetime, now: datetime) -> bool:
    """Whether a digest for this week has waited too long to be worth sending.

    An item reminder that leaves late is still true — the thing is still due.
    A digest that leaves late is a summary of a week you are already halfway
    through, arriving as if it were news. So the two have opposite failure
    modes and only one of them retries indefinitely (D32 covers the other).

    Args:
        period_end: End of the week the digest covers.
        now: The instant to judge from.

    Returns:
        True if the digest should be abandoned rather than sent.
    """
    return now - period_end > timedelta(hours=settings.digest_max_age_hours)


@dataclass(frozen=True)
class Digest:
    """One week, as both halves of the report (UC31).

    Assembled once and read twice: the scheduler needs the counts to write a
    notification body, and the screen needs the rows. Building it in one place
    is what stops the push saying "3 shelved" over a screen that lists four.
    """

    period_start: datetime
    period_end: datetime
    as_of: datetime
    shelved: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    expiring: list[dict[str, Any]] = field(default_factory=list)
    shelved_total: int = 0
    dropped_total: int = 0
    expiring_total: int = 0

    @property
    def empty(self) -> bool:
        """Nothing decayed and nothing is near dropping.

        The condition under which no push goes out. A weekly notification that
        reliably says "nothing happened" is how the one that matters gets
        swiped away unread, and the screen is still there for anyone who wants
        to check.
        """
        return not (self.shelved_total or self.dropped_total or self.expiring_total)

    def headline(self) -> str:
        """The notification body: the three counts, and only the ones above zero.

        Returns:
            One short line, e.g. "3 shelved · 2 about to drop".
        """
        parts = [
            (self.shelved_total, "shelved"),
            (self.dropped_total, "dropped"),
            (self.expiring_total, "about to drop"),
        ]
        said = [f"{n} {label}" for n, label in parts if n]
        return " · ".join(said) if said else "Nothing moved"


async def build(db: Database, user_id: str, now: datetime) -> Digest:
    """Assemble the digest for the week that has most recently ended.

    Two queries, no model call. That is the cost rule (`CLAUDE.md`: decay,
    digests and counts are all SQL) and it is also the whole of what a digest
    is — Postgres already knows what moved, and a paraphrase of a list is not
    worth a token or a failure mode.

    Args:
        db: Database.
        user_id: Whose week.
        now: The instant to build as of.

    Returns:
        The week, with both halves and the counts before truncation.
    """
    start, end = period_for(now)
    limit = settings.digest_list_limit

    moved = await db.digest_decayed(user_id, start, end, limit=limit)
    expiring = await db.digest_expiring(user_id, settings.digest_warn_days, limit=limit)

    shelved = [row for row in moved if row["reason"] == "decay"]
    dropped = [row for row in moved if row["reason"] == "expiry"]

    return Digest(
        period_start=start,
        period_end=end,
        as_of=now,
        shelved=shelved,
        dropped=dropped,
        expiring=expiring,
        # `total` is a window count carried on every row of its group, so any
        # row of the group answers for all of them — and an empty group has no
        # row to ask, which is exactly when the answer is zero.
        shelved_total=int(shelved[0]["total"]) if shelved else 0,
        dropped_total=int(dropped[0]["total"]) if dropped else 0,
        expiring_total=int(expiring[0]["total"]) if expiring else 0,
    )
