"""The digest's arithmetic: which seven days are "the week" (UC31).

No database. What is under test here is the one thing the scheduler and the
screen have to agree on — if the tick announces one week and `GET /digest`
opens another, the notification is a lie and nothing else in the feature can
be trusted.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend import digest
from backend.config import digest_weekday, settings

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def sunday_at_nine(monkeypatch):
    """The documented default, pinned so a changed .env cannot move the tests."""
    monkeypatch.setattr(settings, "digest_day", "sunday")
    monkeypatch.setattr(settings, "digest_hour", 9)
    monkeypatch.setattr(settings, "capture_timezone", "Asia/Kolkata")
    monkeypatch.setattr(settings, "digest_max_age_hours", 24)


def local(year, month, day, hour=0, minute=0, tz=IST) -> datetime:
    """A wall-clock instant in the capture timezone."""
    return datetime(year, month, day, hour, minute, tzinfo=tz)


# ------------------------------------------------------------ the weekday


def test_the_week_ends_at_the_most_recent_digest_hour():
    # Wednesday 27 August 2026. The Sunday before is the 23rd.
    start, end = digest.period_for(local(2026, 8, 26, 15, 30))
    assert end == local(2026, 8, 23, 9)
    assert start == local(2026, 8, 16, 9)


def test_the_week_is_exactly_seven_days():
    start, end = digest.period_for(local(2026, 8, 26, 15, 30))
    assert end - start == timedelta(days=7)


def test_the_digest_hour_itself_belongs_to_the_new_week():
    start, end = digest.period_for(local(2026, 8, 23, 9, 0))
    assert end == local(2026, 8, 23, 9)


def test_before_the_hour_on_digest_day_the_week_has_not_ended_yet():
    """Sunday at eight in the morning is still last week.

    The half-day where this is easy to get backwards: it *is* the digest day,
    but the moment has not arrived, and a digest built here would cover a week
    that is still running.
    """
    _, end = digest.period_for(local(2026, 8, 23, 8, 59))
    assert end == local(2026, 8, 16, 9)


def test_the_instant_before_the_next_one_still_belongs_to_this_week():
    _, end = digest.period_for(local(2026, 8, 30, 8, 59))
    assert end == local(2026, 8, 23, 9)


def test_the_answer_does_not_depend_on_the_caller_s_timezone():
    """The tick passes UTC and the API passes local time. Same week, either way."""
    as_utc = digest.period_for(local(2026, 8, 26, 15, 30).astimezone(timezone.utc))
    as_local = digest.period_for(local(2026, 8, 26, 15, 30))
    assert as_utc == as_local


def test_another_digest_day_moves_the_whole_week(monkeypatch):
    monkeypatch.setattr(settings, "digest_day", "friday")
    start, end = digest.period_for(local(2026, 8, 26, 15, 30))
    assert end == local(2026, 8, 21, 9)
    assert start == local(2026, 8, 14, 9)


def test_an_unrecognised_digest_day_falls_back_to_sunday(monkeypatch, caplog):
    monkeypatch.setattr(settings, "digest_day", "sunnday")
    assert digest_weekday() == 6
    assert "sunnday" in caplog.text


# -------------------------------------------------------------------- DST


def test_the_digest_hour_stays_nine_across_a_dst_change(monkeypatch):
    """Nine in the morning, both sides of the clocks going back.

    The reason `period_for` builds its answer from a date and a wall-clock
    time rather than by subtracting seven days from an instant. In Europe/London
    the last Sunday of October is 25 hours long, so `end - timedelta(days=7)`
    would put the start of the week at 8am — a digest silently covering an
    extra hour, once a year, in a way nobody would ever reproduce.
    """
    monkeypatch.setattr(settings, "capture_timezone", "Europe/London")
    london = ZoneInfo("Europe/London")

    # Sunday 25 October 2026 is the change day itself: the clocks went back
    # at 2am, so 9am that morning is GMT and 9am the Sunday before was BST.
    start, end = digest.period_for(local(2026, 10, 25, 12, 0, tz=london))

    assert (end.hour, end.utcoffset()) == (9, timedelta(0))
    assert (start.hour, start.utcoffset()) == (9, timedelta(hours=1))
    # Converted to UTC before subtracting: two aware datetimes sharing one
    # tzinfo subtract as wall clocks, which would report seven days flat and
    # hide the very hour this test exists for.
    elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    assert elapsed == timedelta(days=7, hours=1)


# ------------------------------------------------------------- staleness


def test_a_digest_is_fresh_for_a_day():
    end = local(2026, 8, 23, 9)
    assert not digest.is_stale(end, end + timedelta(hours=23))


def test_a_digest_older_than_the_window_is_stale():
    end = local(2026, 8, 23, 9)
    assert digest.is_stale(end, end + timedelta(hours=25))


# -------------------------------------------------------------- headline


def _week(shelved=0, dropped=0, expiring=0, done=0) -> digest.Digest:
    end = local(2026, 8, 23, 9)
    return digest.Digest(
        period_start=end - timedelta(days=7),
        period_end=end,
        as_of=end,
        shelved_total=shelved,
        dropped_total=dropped,
        done_total=done,
        expiring_total=expiring,
    )


def test_the_headline_names_only_what_happened():
    assert _week(shelved=3, expiring=2).headline() == "3 shelved · 2 about to drop"


def test_the_headline_keeps_the_three_counts_in_order():
    assert (
        _week(shelved=1, dropped=2, expiring=3).headline()
        == "1 shelved · 2 dropped · 3 about to drop"
    )


def test_a_week_with_nothing_in_it_is_empty():
    assert _week().empty
    assert not _week(expiring=1).empty


def test_a_week_of_nothing_but_completions_sends_nothing():
    """You already know what you finished; nobody needs interrupting about it.

    Completions are on the screen because reading them is worth the space, but
    what justifies a push is the part of the week that happened without you.
    """
    week = _week(done=5)
    assert week.empty
    assert week.headline() == "Nothing moved"


def test_something_about_to_drop_is_enough_to_be_worth_saying():
    """The forecast half alone justifies a digest.

    A week in which nothing decayed but two things are about to be thrown away
    is precisely the week worth interrupting for — everything on that list is
    still recoverable, and only until it is not.
    """
    week = _week(expiring=2)
    assert not week.empty
    assert week.headline() == "2 about to drop"
