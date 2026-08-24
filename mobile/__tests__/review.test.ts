/**
 * The weekly review deck (UC30) — what is in it, and what a swipe means.
 *
 * The gesture cannot be tested, so everything the gesture *decides* is a pure
 * function and this is where it is pinned. Three things matter:
 *
 * - **The deck stays bounded.** It is the two halves of the digest that carry
 *   a decision and nothing else. If completions or already-dropped items ever
 *   leaked in, the review would be a list of things you cannot act on, and the
 *   two-minute exit criterion would go with it.
 * - **`left` writes nothing.** Every card is already shelved, and recording
 *   the non-decision would restart the drop clock (D37) — turning "I looked at
 *   this and did nothing" into "keep it another three months".
 * - **A diagonal resolves to one axis.** Otherwise `done` and `keep` swap at
 *   the corner, which is a mis-swipe you cannot undo.
 */
import { DECISIONS, buildDeck, directionOf } from '../lib/review';
import type { DigestResponse, ExpiringItem, MovedItem } from '../lib/api';
import { dueAgeLabel, shelvedAgeLabel, untouchedLabel } from '../lib/time';

function moved(id: string, text: string, extra: Partial<MovedItem> = {}): MovedItem {
  return {
    id,
    text,
    kind: 'task',
    at: '2026-08-19T15:30:00Z',
    state_now: 'shelved',
    due_at: null,
    ...extra,
  };
}

function expiring(id: string, text: string, extra: Partial<ExpiringItem> = {}): ExpiringItem {
  return {
    id,
    text,
    kind: 'task',
    due_at: null,
    untouched_since: '2026-05-30T12:00:00Z',
    drops_at: '2026-08-28T12:00:00Z',
    ...extra,
  };
}

function week(over: Partial<DigestResponse> = {}): DigestResponse {
  return {
    period_start: '2026-08-16T09:00:00Z',
    period_end: '2026-08-23T09:00:00Z',
    as_of: '2026-08-24T09:00:00Z',
    shelved: [],
    dropped: [],
    done: [],
    expiring: [],
    shelved_total: 0,
    dropped_total: 0,
    done_total: 0,
    expiring_total: 0,
    warn_days: 14,
    ...over,
  };
}

// ---------------------------------------------------------------- the deck

describe('what is in the deck (UC30)', () => {
  it('holds what decayed and what is about to drop, and nothing else', () => {
    const deck = buildDeck(
      week({
        shelved: [moved('a', 'Renew the passport')],
        expiring: [expiring('b', 'Pottery class')],
        dropped: [moved('c', 'Old idea', { state_now: 'dropped' })],
        done: [moved('d', 'Tax return', { state_now: 'done' })],
        shelved_total: 1,
        expiring_total: 1,
        dropped_total: 1,
        done_total: 1,
      }),
    );

    expect(deck.map((card) => card.id)).toEqual(['b', 'a']);
  });

  it('deals the ones with a deadline first', () => {
    // A two-minute review is a review that gets abandoned halfway. The cards
    // that got looked at should be the ones that were about to disappear.
    const deck = buildDeck(
      week({
        shelved: [moved('a', 'No deadline on the decision')],
        expiring: [expiring('b', 'Goes in four days')],
      }),
    );

    expect(deck[0].source).toBe('expiring');
    expect(deck[1].source).toBe('decayed');
  });

  it('deals an item once when it is in both halves', () => {
    // Shelved by decay this week *and* near its drop date — which happens to
    // anything reactivated late in its ninety days. Dealt as the expiring
    // card, because that is the version with the deadline on it.
    const deck = buildDeck(
      week({
        shelved: [moved('a', 'Both at once')],
        expiring: [expiring('a', 'Both at once')],
      }),
    );

    expect(deck).toHaveLength(1);
    expect(deck[0].source).toBe('expiring');
  });

  it('is empty on a quiet week rather than dealing something to fill it', () => {
    expect(buildDeck(week({ done: [moved('d', 'Tax return')], done_total: 1 }))).toEqual([]);
  });

  it('carries both ages, because they say different things', () => {
    const [card] = buildDeck(
      week({
        shelved: [moved('a', 'Ring the landlord', { due_at: '2026-05-01T09:00:00Z' })],
      }),
    );

    expect(card.shelvedAt).toBe('2026-08-19T15:30:00Z');
    expect(card.dueAt).toBe('2026-05-01T09:00:00Z');
  });
});

// -------------------------------------------------------------- the swipes

describe('what a swipe means (UC30)', () => {
  it('maps four directions to four states', () => {
    expect(DECISIONS.up.state).toBe('done');
    expect(DECISIONS.right.state).toBe('active');
    expect(DECISIONS.down.state).toBe('dropped');
    expect(DECISIONS.left.state).toBe('shelved');
  });

  it('writes nothing when you leave a card alone', () => {
    // The one that matters. Every card here is already shelved, so recording
    // "leave it" would touch the row and restart its drop clock (D37) —
    // silently converting a non-decision into another three months.
    expect(DECISIONS.left.writes).toBe(false);
    expect(DECISIONS.up.writes).toBe(true);
    expect(DECISIONS.right.writes).toBe(true);
    expect(DECISIONS.down.writes).toBe(true);
  });
});

describe('reading a drag', () => {
  it('ignores anything shorter than the threshold', () => {
    expect(directionOf(40, 0, 90)).toBeNull();
    expect(directionOf(0, -40, 90)).toBeNull();
    expect(directionOf(0, 0, 90)).toBeNull();
  });

  it('reads a committed drag on either axis', () => {
    expect(directionOf(120, 5, 90)).toBe('right');
    expect(directionOf(-120, 5, 90)).toBe('left');
    expect(directionOf(5, -120, 90)).toBe('up');
    expect(directionOf(5, 120, 90)).toBe('down');
  });

  it('resolves a diagonal to one axis outright', () => {
    // Blending would make `done` and `keep` interchangeable at the corner,
    // and three of the four answers are not ones you want to give by accident.
    expect(directionOf(200, 150, 90)).toBe('right');
    expect(directionOf(150, 200, 90)).toBe('down');
  });

  it('does not answer a long diagonal whose dominant axis is still short', () => {
    // 80 across and 79 down: it has travelled far enough overall to feel like
    // a swipe and not far enough in any direction to be one.
    expect(directionOf(80, 79, 90)).toBeNull();
  });

  it('gives a perfect diagonal to the horizontal', () => {
    // Left and right are the two reversible answers, so a genuine tie goes to
    // them rather than to `done` or `drop`.
    expect(directionOf(120, 120, 90)).toBe('right');
    expect(directionOf(-120, -120, 90)).toBe('left');
  });
});

// ------------------------------------------------------------- the ageing

describe('how old a card says it is', () => {
  const now = new Date('2026-08-24T12:00:00Z');

  it('reports the neglect, not just the shelving', () => {
    // The pair that decides the swipe. Shelved four days ago, overdue for
    // nearly four months — and only the second number is evidence.
    expect(shelvedAgeLabel('2026-08-20T09:00:00Z', now)).toBe('Shelved 4 days ago');
    expect(dueAgeLabel('2026-05-01T09:00:00Z', now)).toBe('Due 3 months ago');
  });

  it('says nothing about a due date that never existed', () => {
    // Most of the shelf: no time means shelved from the start (UC12), and
    // inventing an age to report would be worse than the gap.
    expect(dueAgeLabel(null, now)).toBe('');
  });

  it('can say a card is not overdue at all', () => {
    expect(dueAgeLabel('2026-08-24T18:00:00Z', now)).toBe('Due today');
    expect(dueAgeLabel('2026-08-27T09:00:00Z', now)).toBe('Due in 3 days');
  });

  it('does not claim an edit was a shelving', () => {
    // `untouched_since` is greatest(state_changed_at, updated_at) — editing a
    // shelved item restarts its clock (D37) — so it is the last time the item
    // was touched at all, which is not always the day it was put away.
    expect(untouchedLabel('2026-05-30T12:00:00Z', now)).toBe('Untouched for 86 days');
  });
});
