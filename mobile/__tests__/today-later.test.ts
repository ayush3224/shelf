/**
 * `Today`'s two blocks, and the toast that names them (UC32, D56, D57).
 *
 * The bug these are written against: an active item due on a future day was on
 * no screen in the app, and the capture toast said "it's on Today" about it
 * anyway. Both halves were wrong in the same direction — the app claimed a
 * placement it had not made — so both are pinned here.
 */
import type { CaptureResponse, TodayItem } from '../lib/api';
import { landedMessage, landing } from '../lib/landing';
import { showHeaders, todaySections } from '../lib/today';
import { dueDayPhrase, dueLabel } from '../lib/time';

// A Monday afternoon, so "tomorrow" and "in three days" are different weekdays.
const NOW = new Date('2026-08-24T14:00:00');

function item(id: string, dueAt: string): TodayItem {
  return {
    id,
    text: "Clip the dog's nails",
    raw_text: "clip the dog's nails tomorrow at half eight",
    kind: 'task',
    state: 'active',
    due_at: dueAt,
    critical: false,
    parse_status: 'ok',
    overdue: false,
    has_audio: false,
  };
}

describe('the two blocks', () => {
  it('shows no headers when there is only one block', () => {
    const only = todaySections([item('a', '2026-08-24T15:00:00Z')], []);
    expect(only).toHaveLength(1);
    expect(showHeaders(only)).toBe(false);
  });

  it('shows headers as soon as there are two', () => {
    const both = todaySections(
      [item('a', '2026-08-24T15:00:00Z')],
      [item('b', '2026-08-27T15:00:00Z')],
    );
    expect(both.map((s) => s.key)).toEqual(['due', 'later']);
    expect(showHeaders(both)).toBe(true);
  });

  it('drops the due block entirely when the day is clear', () => {
    const sections = todaySections([], [item('b', '2026-08-27T15:00:00Z')]);
    expect(sections.map((s) => s.key)).toEqual(['later']);
    // One section, so no header — the "Nothing due" line above it is what
    // says which block this is.
    expect(showHeaders(sections)).toBe(false);
  });

  it('is empty when there is nothing at all', () => {
    expect(todaySections([], [])).toEqual([]);
  });
});

describe('due labels reaching into the future', () => {
  it('keeps the clock time on every future label', () => {
    // A time is the whole content of an upcoming item; a label without one
    // would be an item you cannot act on from the row.
    expect(dueLabel('2026-08-25T08:30:00', NOW)).toBe('Tomorrow, 8:30 AM');
    expect(dueLabel('2026-08-27T08:30:00', NOW)).toBe('Thursday, 8:30 AM');
  });

  it('stops naming weekdays once one could mean either of two', () => {
    // Day and month order is the locale's, not ours — these read as the test
    // runner's en-US does.
    expect(dueLabel('2026-09-03T08:30:00', NOW)).toBe('Sep 3, 8:30 AM');
    expect(dueLabel('2027-01-04T08:30:00', NOW)).toBe('Jan 4 2027, 8:30 AM');
  });

  it('still reads the past the way it always did', () => {
    expect(dueLabel('2026-08-24T09:00:00', NOW)).toBe('9:00 AM');
    expect(dueLabel('2026-08-23T09:00:00', NOW)).toBe('Yesterday, 9:00 AM');
    expect(dueLabel('2026-07-24T09:00:00', NOW)).toBe('1 month ago');
  });
});

describe('where a capture landed', () => {
  it('places an item by its time, not only by its state', () => {
    // Both are `active`. That was the whole reason the toast was wrong.
    expect(landing('active', '2026-08-24T18:00:00', NOW)).toBe('today');
    expect(landing('active', '2026-08-25T08:30:00', NOW)).toBe('later');
    expect(landing('shelved', null, NOW)).toBe('shelf');
  });

  it('counts an overdue item as being on Today', () => {
    expect(landing('active', '2026-08-20T08:00:00', NOW)).toBe('today');
  });

  it('names the day rather than only the block', () => {
    expect(dueDayPhrase('2026-08-25T08:30:00', NOW)).toBe('tomorrow');
    expect(dueDayPhrase('2026-08-27T08:30:00', NOW)).toBe('Thursday');
    expect(dueDayPhrase('2026-09-30T08:30:00', NOW)).toBe('Sep 30');
    expect(dueDayPhrase(null, NOW)).toBe('');
  });
});

function response(over: Partial<CaptureResponse>): CaptureResponse {
  return {
    id: 'x',
    status: 'ok',
    parse_status: 'ok',
    state: 'active',
    kind: 'task',
    due_at: null,
    critical: false,
    text: "Clip the dog's nails",
    project_hint: null,
    entities: [],
    split: false,
    items: [],
    ...over,
  } as CaptureResponse;
}

describe('the toast', () => {
  it('no longer says Today about something that is not on Today', () => {
    const message = landedMessage(
      response({ state: 'active', due_at: '2026-08-25T08:30:00' }),
      NOW,
    );
    expect(message).toBe("Saved — it's under Later, due tomorrow.");
    expect(message).not.toContain('on Today');
  });

  it('still says Today about something that is', () => {
    expect(
      landedMessage(response({ state: 'active', due_at: '2026-08-24T18:00:00' }), NOW),
    ).toBe("Saved — it's on Today.");
  });

  it('says the shelf for an item with no time', () => {
    expect(landedMessage(response({ state: 'shelved', due_at: null }), NOW)).toBe(
      "Saved — it's on the shelf.",
    );
  });

  it('keeps the flag on a shaky transcript, wherever it landed', () => {
    expect(
      landedMessage(
        response({
          parse_status: 'needs_review',
          state: 'active',
          due_at: '2026-08-25T08:30:00',
        }),
        NOW,
      ),
    ).toBe(
      "Saved — it's under Later, due tomorrow. The words were hard to make out, so check it.",
    );
  });

  it('keeps the audio and says so when the parse failed outright', () => {
    expect(landedMessage(response({ parse_status: 'failed', state: 'shelved' }), NOW)).toBe(
      "Saved. Couldn't read it — it's on the shelf, with your words kept.",
    );
  });

  it('counts a split across all three destinations (UC4)', () => {
    const message = landedMessage(
      response({
        split: true,
        items: [
          { id: '1', state: 'active', kind: 'task', text: 'a', due_at: '2026-08-24T18:00:00', critical: false },
          { id: '2', state: 'active', kind: 'task', text: 'b', due_at: '2026-08-25T08:30:00', critical: false },
          { id: '3', state: 'shelved', kind: 'note', text: 'c', due_at: null, critical: false },
        ],
      }),
      NOW,
    );
    expect(message).toBe('Saved 3 things — 1 on Today, 1 under Later, 1 on the shelf.');
  });

  it('says the shelf plainly when a split put nothing anywhere else', () => {
    expect(
      landedMessage(
        response({
          split: true,
          items: [
            { id: '1', state: 'shelved', kind: 'note', text: 'a', due_at: null, critical: false },
            { id: '2', state: 'shelved', kind: 'note', text: 'b', due_at: null, critical: false },
          ],
        }),
        NOW,
      ),
    ).toBe('Saved 2 things to the shelf.');
  });
});
