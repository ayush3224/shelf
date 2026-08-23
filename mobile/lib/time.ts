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
