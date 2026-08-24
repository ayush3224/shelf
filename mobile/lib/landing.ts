/**
 * What the capture screen says happened (UC1, UC5, D57).
 *
 * Pure and separate from the screen because this is the one line the owner
 * reads after every single capture, and it has been wrong: it said "it's on
 * Today" for anything timed, which for a future date named a screen the item
 * was not on — and, until D56, no screen it *was* on either.
 *
 * The rule mirrors the server's split rather than inventing one. If the two
 * ever disagree the toast is lying again, so it is written once, here.
 */
import type { CaptureResponse } from './api';
import { dueDayPhrase } from './time';

/** The three places a capture can end up, and the only three. */
export type Landing = 'today' | 'later' | 'shelf';

/**
 * Which block on which screen this item is now sitting in.
 *
 * `active` alone is not enough to answer: an item due next Tuesday is just as
 * `active` as one due in an hour and lands somewhere else. The cut-off is the
 * end of the local day — the same instant the server divides on (D15), which
 * is the user's day and, on the one phone this app has, the same one.
 */
export function landing(
  state: string,
  dueAt: string | null,
  now: Date = new Date(),
): Landing {
  if (state !== 'active' || !dueAt) return 'shelf';
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return 'shelf';
  const endOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return due < endOfDay ? 'today' : 'later';
}

/** Where one item went, as a clause that finishes "Saved — …". */
function landedClause(state: string, dueAt: string | null, now?: Date): string {
  switch (landing(state, dueAt, now)) {
    case 'today':
      return "it's on Today";
    case 'later':
      // The day is the useful half. Naming the block without the date would
      // confirm a place and leave the thing actually worth confirming — that
      // the date it heard is the date you said — unsaid.
      return `it's under Later, due ${dueDayPhrase(dueAt, now)}`;
    default:
      return "it's on the shelf";
  }
}

/** Where the capture went, in one line. State is announced, never silent. */
export function landedMessage(result: CaptureResponse, now?: Date): string {
  if (result.parse_status === 'failed') {
    return "Saved. Couldn't read it — it's on the shelf, with your words kept.";
  }

  const items = result.items ?? [];
  if (items.length > 1) {
    // UC4: say how many things came out of one note, because the count is the
    // part that would otherwise be a surprise. Each destination is counted
    // separately — "saved 3 things" over a silent split across two blocks is
    // the same missing information this line exists to supply.
    const tally: Record<Landing, number> = { today: 0, later: 0, shelf: 0 };
    for (const item of items) tally[landing(item.state, item.due_at, now)] += 1;

    const heard = `Saved ${items.length} things`;
    if (tally.today === 0 && tally.later === 0) return `${heard} to the shelf.`;

    const parts: string[] = [];
    if (tally.today) parts.push(`${tally.today} on Today`);
    if (tally.later) parts.push(`${tally.later} under Later`);
    if (tally.shelf) parts.push(`${tally.shelf} on the shelf`);
    return `${heard} — ${parts.join(', ')}.`;
  }

  const clause = landedClause(result.state, result.due_at, now);
  if (result.parse_status === 'needs_review') {
    return `Saved — ${clause}. The words were hard to make out, so check it.`;
  }
  return `Saved — ${clause}.`;
}
