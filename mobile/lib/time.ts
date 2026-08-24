/** Due-time formatting for the `Today` list. */

const time = new Intl.DateTimeFormat(undefined, {
  hour: 'numeric',
  minute: '2-digit',
});

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/**
 * A short, human due label relative to now.
 *
 * `Today` only ever holds due and overdue items, so this covers today and the
 * past — anything else would mean the server's bound is wrong.
 */
export function dueLabel(dueAt: string, now: Date = new Date()): string {
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return '';

  const days = Math.round((startOfDay(now) - startOfDay(due)) / 86_400_000);
  const clock = time.format(due);

  if (days === 0) return clock;
  if (days === 1) return `Yesterday, ${clock}`;
  if (days < 7) return `${days} days ago, ${clock}`;
  if (days < 30) {
    const weeks = Math.floor(days / 7);
    return `${weeks} week${weeks === 1 ? '' : 's'} ago`;
  }
  const months = Math.floor(days / 30);
  return `${months} month${months === 1 ? '' : 's'} ago`;
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


const dayMonth = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });

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
