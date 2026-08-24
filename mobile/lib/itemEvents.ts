/**
 * Item changes, announced to whatever list is showing them.
 *
 * The detail screen is reached from four different lists, and every action on
 * it — edit, state move, snooze, reactivate, delete — invalidates the row that
 * was tapped to get there. `Today` never noticed because it refetches on focus
 * (and removes optimistically for the one action it owns), but the Shelf
 * deliberately does *not* refetch on focus: it is a list you scroll, and a
 * reload that silently resets the scroll position loses your place. So a
 * deleted item sat on the Shelf until a pull-to-refresh, an edited one kept its
 * old words, and an item moved to `done` kept saying "Shelved".
 *
 * Refetching on focus would fix all three and cost the thing the Shelf was
 * built to protect. Patching the row in place costs nothing and fixes them in
 * the same shape, which is why this is a change feed rather than an
 * invalidation: the screen that made the change already knows what the row
 * became, and every other screen only has to be told.
 *
 * Deliberately not a state library. One module-scope `Set`, no provider, no
 * dependency — the whole contract is "somebody changed an item, here it is
 * afterwards", and anything larger would be scaffolding around a five-line
 * idea.
 */
import { useEffect } from 'react';

import type { ItemDetail, ItemState } from './api';

/**
 * What happened to one item.
 *
 * An update carries the item as it now stands rather than a diff, because the
 * server is what decided it — an edit that adds a time also moves the state
 * (UC12), and a client assembling that itself is a client that will eventually
 * assemble it wrong.
 *
 * Linking is its own pair of events rather than a flavour of `updated`: who an
 * item names does not change the item, it changes which *lists* the item
 * belongs to. Only a person page cares, and it is the one screen a link can
 * add a row to or take one from.
 */
export type ItemChange =
  | { type: 'deleted'; id: string }
  | { type: 'updated'; id: string; item: ItemDetail }
  | { type: 'linked'; id: string; entityId: string }
  | { type: 'unlinked'; id: string; entityId: string };

/**
 * The fields every list holds for a row.
 *
 * `ShelfItem` and `PersonItem` both widen this — the Shelf carries its project
 * columns, the person page does not — so patching is written against the
 * overlap and leaves whatever else a row is carrying alone.
 */
export type ListedItem = {
  id: string;
  text: string;
  raw_text: string;
  kind: 'task' | 'note' | 'person_note';
  state: ItemState;
  due_at: string | null;
  critical: boolean;
  parse_status: 'ok' | 'failed' | 'needs_review';
  has_audio: boolean;
};

type Listener = (change: ItemChange) => void;

const listeners = new Set<Listener>();

/** Tell every mounted list what just happened to an item. */
export function publishItemChange(change: ItemChange): void {
  // Copied before iterating: a listener that unsubscribes on the way through
  // would otherwise mutate the set being walked.
  for (const listener of [...listeners]) {
    try {
      listener(change);
    } catch {
      // A screen that cannot apply a change must not stop the next screen
      // from applying it. The change already happened on the server; this is
      // only the news of it.
    }
  }
}

/** Subscribe a list to item changes for as long as it is mounted. */
export function useItemChanges(onChange: Listener): void {
  useEffect(() => {
    listeners.add(onChange);
    return () => {
      listeners.delete(onChange);
    };
  }, [onChange]);
}

/**
 * Fold a change into a list of rows.
 *
 * Pure, and generic over the row type, so the Shelf and a person page share
 * one answer to "what does this list look like now" instead of each growing
 * their own.
 *
 * `keeps` is what stops a patched row lingering in a list it no longer belongs
 * to: marking a shelved item `done` while the Shelf is filtered to `Shelved`
 * has to remove it, not relabel it. A list that shows every state passes
 * nothing and keeps everything.
 *
 * Link changes pass straight through: whether an item belongs on somebody's
 * page is a question about the list, not about the row, so the screen that
 * cares answers it itself.
 *
 * Returns the original array when no row matched, so a change to an item this
 * list has never heard of does not re-render it.
 */
export function applyItemChange<T extends ListedItem>(
  rows: T[],
  change: ItemChange,
  keeps: (row: T) => boolean = () => true,
): T[] {
  if (change.type === 'deleted') {
    return rows.some((row) => row.id === change.id)
      ? rows.filter((row) => row.id !== change.id)
      : rows;
  }

  // A link change says nothing about the row itself; the screen that cares
  // decides whether the item belongs to it at all, which is not a patch.
  if (change.type !== 'updated') return rows;

  const { item } = change;
  if (!rows.some((row) => row.id === item.id)) return rows;

  const next: T[] = [];
  for (const row of rows) {
    if (row.id !== item.id) {
      next.push(row);
      continue;
    }
    const patched: T = {
      ...row,
      text: item.text,
      raw_text: item.raw_text,
      kind: item.kind,
      state: item.state,
      due_at: item.due_at,
      critical: item.critical,
      parse_status: item.parse_status,
      has_audio: item.has_audio,
    };
    if (keeps(patched)) next.push(patched);
  }
  return next;
}
