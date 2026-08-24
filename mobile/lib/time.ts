/** Due-time formatting for the `Today` list. */

const time = new Intl.DateTimeFormat(undefined, {
  hour: 'numeric',
  minute: '2-digit',
});

const dayMonth = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

const weekdayOnly = new Intl.DateTimeFormat(undefined, { weekday: 'long' });

/**
 * A short, human due label relative to now.
 *
 * Covers the future as well as the past, because `Today` now carries both
 * (D56): due and overdue in the top block, still-to-come under `Later`.
 *
 * The two halves are shaped differently on purpose. Backwards, the clock time
 * stops mattering once something is a week late and the label collapses to
 * "3 weeks ago". Forwards it never stops mattering — a time is the whole
 * content of an upcoming item — so every future label keeps its clock, and
 * only the day part gets shorter as it gets further away.
 */
export function dueLabel(dueAt: string, now: Date = new Date()): string {
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return '';

  const days = Math.round((startOfDay(now) - startOfDay(due)) / 86_400_000);
  const clock = time.format(due);

  if (days === 0) return clock;

  if (days < 0) {
    const ahead = -days;
    if (ahead === 1) return `Tomorrow, ${clock}`;
    // Inside the coming week a weekday names itself unambiguously; past that
    // "Tuesday" could be either of two, so it becomes a date.
    if (ahead < 7) return `${weekdayOnly.format(due)}, ${clock}`;
    if (due.getFullYear() === now.getFullYear()) {
      return `${dayMonth.format(due)}, ${clock}`;
    }
    return `${dayMonth.format(due)} ${due.getFullYear()}, ${clock}`;
  }

  if (days === 1) return `Yesterday, ${clock}`;
  if (days < 7) return `${days} days ago, ${clock}`;
  if (days < 30) {
    const weeks = Math.floor(days / 7);
    return `${weeks} week${weeks === 1 ? '' : 's'} ago`;
  }
  const months = Math.floor(days / 30);
  return `${months} month${months === 1 ? '' : 's'} ago`;
}


/**
 * The day part of a due time, as a bare phrase for the capture toast (D57).
 *
 * Lowercase and unpunctuated so it drops into a sentence — "Saved for
 * tomorrow" — and without a clock time, which is detail the acknowledgement
 * does not need. Empty for an item with no time at all; that one is on the
 * shelf and the toast says so instead.
 */
export function dueDayPhrase(dueAt: string | null, now: Date = new Date()): string {
  if (!dueAt) return '';
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return '';

  const ahead = Math.round((startOfDay(due) - startOfDay(now)) / 86_400_000);
  if (ahead <= 0) return 'today';
  if (ahead === 1) return 'tomorrow';
  if (ahead < 7) return weekdayOnly.format(due);
  if (due.getFullYear() === now.getFullYear()) return dayMonth.format(due);
  return `${dayMonth.format(due)} ${due.getFullYear()}`;
}

const fullDateTime = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  hour: 'numeric',
  minute: '2-digit',
});

/** An absolute due label for the item detail, where there is room for one. */
export function fullDueLabel(dueAt: string | null): string {
  if (!dueAt) return 'No time — on the shelf';
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return 'No time — on the shelf';
  return fullDateTime.format(due);
}

const dateOnly = new Intl.DateTimeFormat(undefined, {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
});

/** When an item was captured, for the detail screen's footer. */
export function capturedLabel(createdAt: string): string {
  const at = new Date(createdAt);
  if (Number.isNaN(at.getTime())) return '';
  return `${dateOnly.format(at)}, ${time.format(at)}`;
}



/**
 * When something was captured, for a Shelf row.
 *
 * Deliberately coarse. `Today` says "Yesterday, 3:41 pm" because a due time is
 * the point of that row; on the Shelf the date is context, not an instruction,
 * and a clock time there reads as a deadline the item does not have.
 */
export function capturedOnLabel(createdAt: string, now: Date = new Date()): string {
  const at = new Date(createdAt);
  if (Number.isNaN(at.getTime())) return '';

  const days = Math.round((startOfDay(now) - startOfDay(at)) / 86_400_000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days} days ago`;
  if (at.getFullYear() === now.getFullYear()) return dayMonth.format(at);
  return `${dayMonth.format(at)} ${at.getFullYear()}`;
}

/**
 * The week a digest covers, as one short phrase (UC31).
 *
 * `period_end` is exclusive — the digest hour on digest day — so the last day
 * it actually covers is the one before. Printing the exclusive bound would put
 * a Sunday on a week that ends on Saturday night, which is the kind of
 * off-by-one nobody notices and everybody half-distrusts.
 */
export function weekLabel(periodStart: string, periodEnd: string): string {
  const from = new Date(periodStart);
  const to = new Date(new Date(periodEnd).getTime() - 86_400_000);
  if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) return '';
  return `${dayMonth.format(from)} – ${dayMonth.format(to)}`;
}

/**
 * How long something has left before it is dropped (UC31).
 *
 * Rounded **down**, deliberately. This is the only warning there is, so it has
 * to err towards sounding earlier than the sweep will actually fire: something
 * with thirty hours left reads as "tomorrow", and a label that said "in 2
 * days" would be promising time the item does not have.
 */
export function dropsInLabel(dropsAt: string, now: Date = new Date()): string {
  const at = new Date(dropsAt);
  if (Number.isNaN(at.getTime())) return '';

  const days = Math.floor((at.getTime() - now.getTime()) / 86_400_000);
  if (days <= 0) return 'Drops today';
  if (days === 1) return 'Drops tomorrow';
  return `Drops in ${days} days`;
}

/**
 * How overdue something is, for a review card (UC30).
 *
 * The label that decides the swipe. "Shelved 4 days ago" says how long the
 * system has had it put away; this says how long *you* have been not doing it,
 * and they are frequently very different numbers — an item captured in May and
 * shelved on Tuesday reads as four days old and is four months overdue.
 *
 * Empty for an item that never had a time, which is most of the shelf: without
 * a due date there is no age to report and inventing one would be worse than
 * the gap.
 */
export function dueAgeLabel(dueAt: string | null, now: Date = new Date()): string {
  if (!dueAt) return '';
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return '';

  const days = Math.round((startOfDay(now) - startOfDay(due)) / 86_400_000);
  if (days === 0) return 'Due today';
  if (days === 1) return 'Due yesterday';
  if (days === -1) return 'Due tomorrow';
  if (days < 0) return `Due in ${-days} days`;
  if (days < 30) return `Due ${days} days ago`;
  const months = Math.floor(days / 30);
  return `Due ${months} month${months === 1 ? '' : 's'} ago`;
}

/**
 * How long ago the system put something away (UC30).
 *
 * Paired with `dueAgeLabel` on a review card, never alone: on its own it
 * flatters the item, because the shelving is always more recent than the
 * neglect that caused it.
 */
export function shelvedAgeLabel(at: string, now: Date = new Date()): string {
  const when = new Date(at);
  if (Number.isNaN(when.getTime())) return '';

  const days = Math.round((startOfDay(now) - startOfDay(when)) / 86_400_000);
  if (days <= 0) return 'Shelved today';
  if (days === 1) return 'Shelved yesterday';
  if (days < 30) return `Shelved ${days} days ago`;
  const months = Math.floor(days / 30);
  return `Shelved ${months} month${months === 1 ? '' : 's'} ago`;
}

/**
 * How long something has sat on the shelf untouched (UC30, UC31).
 *
 * Deliberately *not* worded as "shelved N days ago". The underlying value is
 * `greatest(state_changed_at, updated_at)` — editing a shelved item restarts
 * its drop clock (D37) — so it is the last time the item was touched at all,
 * which is not always the day it was put away.
 */
export function untouchedLabel(since: string, now: Date = new Date()): string {
  const when = new Date(since);
  if (Number.isNaN(when.getTime())) return '';

  const days = Math.round((startOfDay(now) - startOfDay(when)) / 86_400_000);
  if (days <= 0) return 'Untouched today';
  if (days === 1) return 'Untouched for a day';
  return `Untouched for ${days} days`;
}
