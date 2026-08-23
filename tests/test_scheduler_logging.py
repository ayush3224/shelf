"""What a tick says about itself.

This is not decoration. A tick that ran and found nothing to do and a tick
that died on its way to the database used to produce byte-identical output —
nothing at all — and telling them apart meant reconstructing the state of the
database by hand. These tests pin the difference.
"""

import logging

import pytest

from backend import scheduler
from backend.scheduler import Survey, TickResult


def test_the_summary_says_what_was_considered_as_well_as_what_was_done():
    """Counts alone cannot explain a quiet tick; the survey can."""
    result = TickResult(
        survey=Survey(active=1, due_now=1, open_pushes=1, devices=1),
        elapsed_ms=18,
    )

    line = result.summary()

    # The question this line has to answer: one item is due, so why was
    # nothing queued? Because a push for it is already open.
    assert "due=1" in line
    assert "open=1" in line
    assert "queued=0" in line
    assert "devices=1" in line
    assert "18ms" in line


def test_a_tick_with_nothing_to_do_is_still_a_tick_that_ran():
    """`quiet` describes the work, not the logging."""
    assert TickResult().quiet is True
    assert TickResult(queued=1).quiet is False


def test_pushes_queued_with_no_device_are_called_out():
    """The one quiet state that looks exactly like a broken tick.

    Nothing is sent, nothing is retried, nothing errors — by design (D32) —
    so it has to announce itself or it reads as a scheduler that has stopped.
    """
    assert TickResult(survey=Survey(queued_pushes=2, devices=0)).undeliverable is True
    assert TickResult(survey=Survey(queued_pushes=2, devices=1)).undeliverable is False
    assert TickResult(survey=Survey(queued_pushes=0, devices=0)).undeliverable is False


def test_a_failing_tick_exits_non_zero_with_a_traceback(monkeypatch, caplog):
    """So a broken tick shows up in `systemctl status`, not just in the journal.

    A bare propagating exception would still print, but the unit would need
    reading to know it had failed at all.
    """

    async def explode():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(scheduler, "run_once", explode)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exit_info:
            scheduler.main()

    assert exit_info.value.code == 1
    assert "tick failed" in caplog.text
    assert "database is on fire" in caplog.text
    assert "Traceback" in caplog.text
