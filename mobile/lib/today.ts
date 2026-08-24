/**
 * The `Today` screen's pure parts (UC32, D56).
 *
 * Here rather than in the screen so the one rule that matters can be tested
 * without dragging `expo-router` and the navigation tree into a unit test:
 * which block a given item lands in, and when a header appears at all.
 */
import type { TodayItem } from './api';

export type TodaySection = {
  key: 'due' | 'later';
  /** Rendered only when there is something to distinguish it from. */
  title: string;
  data: TodayItem[];
};

/**
 * Split the screen into its blocks, dropping the ones that are empty.
 *
 * The division itself is the server's — this only decides what to draw. A
 * client that re-derived "is this due today" from `due_at` would be deciding
 * where the day ends, and the day ends in the user's timezone on the server
 * (D15), not wherever the phone happens to be.
 *
 * A lone section gets no header. The break between "due" and "later" is the
 * whole reason there are two, so a "Due" heading with nothing under it to
 * contrast against is chrome on a screen that is trying not to have any.
 */
export function todaySections(items: TodayItem[], later: TodayItem[]): TodaySection[] {
  const sections: TodaySection[] = [];
  if (items.length > 0) sections.push({ key: 'due', title: 'Due', data: items });
  if (later.length > 0) sections.push({ key: 'later', title: 'Later', data: later });
  return sections;
}

/** Whether a section's heading should be drawn at all. */
export function showHeaders(sections: TodaySection[]): boolean {
  return sections.length > 1;
}
