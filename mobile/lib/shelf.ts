/**
 * The Shelf's pure parts (UC33, UC36).
 *
 * Here rather than in the screen so they can be tested without dragging
 * `expo-router` and the whole navigation tree into a unit test. Both are
 * decisions worth pinning down: one is what makes grouping survive pagination,
 * the other is the only place a filter chip becomes a timestamp.
 */
import type { ShelfItem } from './api';

export type ShelfSection = { key: string; title: string; data: ShelfItem[] };

/**
 * Bucket rows into project sections, newest project first.
 *
 * Order comes from first appearance, and rows arrive newest-capture-first, so
 * a project's position is set by its most recent item and stays put as later
 * pages append to it. That is what lets grouping and keyset pagination
 * coexist: a server returning ready-made sections would have to either cut a
 * group at the page boundary or give up paging.
 *
 * Keyed on `project_id` rather than the name, so two projects that happen to
 * share a label stay apart — names are hand-entered here, so that is a case a
 * person can actually create.
 */
export function groupByProject(items: ShelfItem[]): ShelfSection[] {
  const sections: ShelfSection[] = [];
  const byKey = new Map<string, ShelfSection>();

  for (const item of items) {
    const key = item.project_id ?? 'unsorted';
    let section = byKey.get(key);
    if (!section) {
      // "Unsorted" is not pinned anywhere: with UC11 dropped it holds
      // everything, and a permanent header over the whole list is noise.
      section = { key, title: item.project_name ?? 'Unsorted', data: [] };
      byKey.set(key, section);
      sections.push(section);
    }
    section.data.push(item);
  }

  return sections;
}

/**
 * The `from` bound for a "last N days" chip, or undefined for any time.
 *
 * A window back from now rather than two date pickers: the question this
 * screen actually gets asked is "recently" or "ages ago", and a pair of
 * pickers is a lot of chrome for a screen that is trying not to have any.
 */
export function windowStart(
  days: number | null,
  now: Date = new Date(),
): string | undefined {
  if (days === null) return undefined;
  return new Date(now.getTime() - days * 86_400_000).toISOString();
}
