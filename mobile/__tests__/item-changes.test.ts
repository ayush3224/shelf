/**
 * Item changes reaching the lists that show them.
 *
 * The bug was narrow and the fix is not: deleting from the detail screen left
 * the row on the Shelf until a pull-to-refresh, because the Shelf does not
 * refetch on focus and nothing told it. Delete was just the visible one —
 * editing the words and moving the state had the identical gap, and each would
 * have been reported separately a week apart.
 *
 * So what is asserted here is the shared piece: a change is applied to a row
 * whatever kind of change it is, a row that no longer matches the list's own
 * question leaves rather than lingering mislabelled, and a list that has never
 * heard of the item is handed back untouched — which is what stops every
 * mounted screen re-rendering on every edit.
 */
import { act, renderHook } from '@testing-library/react-native';

import {
  applyItemChange,
  publishItemChange,
  useItemChanges,
} from '../lib/itemEvents';
import type { ItemChange } from '../lib/itemEvents';
import type { ItemDetail, ShelfItem } from '../lib/api';

function row(id: string, over: Partial<ShelfItem> = {}): ShelfItem {
  return {
    id,
    text: `item ${id}`,
    raw_text: `item ${id}`,
    kind: 'task',
    state: 'shelved',
    due_at: null,
    critical: false,
    parse_status: 'ok',
    has_audio: false,
    project_id: null,
    project_name: null,
    created_at: '2026-08-24T09:00:00Z',
    state_changed_at: '2026-08-24T09:00:00Z',
    ...over,
  };
}

function detail(id: string, over: Partial<ItemDetail> = {}): ItemDetail {
  return {
    id,
    text: `item ${id}`,
    raw_text: `item ${id}`,
    parsed_text: `item ${id}`,
    kind: 'task',
    state: 'shelved',
    due_at: null,
    critical: false,
    parse_status: 'ok',
    source: 'voice',
    has_audio: false,
    transcript_source: 'cloud',
    transcript_confidence: 0.9,
    created_at: '2026-08-24T09:00:00Z',
    updated_at: '2026-08-24T09:00:00Z',
    people: [],
    on_calendar: false,
    calendar_sync_state: null,
    calendar_stalled: false,
    ...over,
  };
}

const onShelf = (r: ShelfItem) =>
  ['shelved', 'done', 'dropped'].includes(r.state);

// ------------------------------------------------------------------ deleting

describe('a deleted item', () => {
  it('leaves the list', () => {
    const rows = [row('a'), row('b')];
    const next = applyItemChange(rows, { type: 'deleted', id: 'a' });
    expect(next.map((r) => r.id)).toEqual(['b']);
  });

  it('leaves a list that is filtered to states it did not match', () => {
    // Delete is unconditional: the row is gone from the database, so no
    // predicate gets a say in whether it stays on screen.
    const rows = [row('a', { state: 'active' })];
    expect(applyItemChange(rows, { type: 'deleted', id: 'a' }, onShelf)).toEqual([]);
  });
});

// ------------------------------------------------------------------- editing

describe('an edited item', () => {
  it('shows its new words without a refetch', () => {
    const rows = [row('a', { text: 'Call teh bank' })];
    const next = applyItemChange(rows, {
      type: 'updated',
      id: 'a',
      item: detail('a', { text: 'Call the bank' }),
    });
    expect(next[0].text).toBe('Call the bank');
  });

  it('keeps the columns only the list has', () => {
    // `ItemDetail` knows nothing about projects; a patch that spread it whole
    // would blank the section this row is grouped under.
    const rows = [row('a', { project_id: 'p1', project_name: 'Flat' })];
    const next = applyItemChange(rows, { type: 'updated', id: 'a', item: detail('a') });
    expect(next[0].project_name).toBe('Flat');
    expect(next[0].state_changed_at).toBe('2026-08-24T09:00:00Z');
  });
});

// ------------------------------------------------------------ state changes

describe('a state change', () => {
  it('relabels a row the list still wants', () => {
    const rows = [row('a', { state: 'shelved' })];
    const next = applyItemChange(
      rows,
      { type: 'updated', id: 'a', item: detail('a', { state: 'done' }) },
      onShelf,
    );
    expect(next).toHaveLength(1);
    expect(next[0].state).toBe('done');
  });

  it('removes a row the list no longer wants', () => {
    // Reactivating from the detail screen: the Shelf is everything but
    // `active`, so the row belongs on `Today` now and nowhere here.
    const rows = [row('a'), row('b')];
    const next = applyItemChange(
      rows,
      { type: 'updated', id: 'a', item: detail('a', { state: 'active', due_at: '2026-08-25T09:00:00Z' }) },
      onShelf,
    );
    expect(next.map((r) => r.id)).toEqual(['b']);
  });

  it('keeps every state on a list that asked for every state', () => {
    // A person page shows what you have already dealt with, so nothing that
    // happens to an item takes it off somebody's page.
    const rows = [row('a')];
    const next = applyItemChange(rows, {
      type: 'updated',
      id: 'a',
      item: detail('a', { state: 'dropped' }),
    });
    expect(next).toHaveLength(1);
  });
});

// -------------------------------------------------------------- not this list

describe('a change to an item this list has never shown', () => {
  it('hands the same array back', () => {
    const rows = [row('a')];
    expect(applyItemChange(rows, { type: 'updated', id: 'zzz', item: detail('zzz') })).toBe(rows);
    expect(applyItemChange(rows, { type: 'deleted', id: 'zzz' })).toBe(rows);
  });
});

// --------------------------------------------------------------- link changes

describe('a link change', () => {
  it('leaves the rows alone', () => {
    // Who an item names does not change the item — it changes which lists it
    // belongs to, and only a person page can answer that about itself.
    const rows = [row('a')];
    expect(applyItemChange(rows, { type: 'linked', id: 'a', entityId: 'e1' })).toBe(
      rows,
    );
    expect(applyItemChange(rows, { type: 'unlinked', id: 'a', entityId: 'e1' })).toBe(
      rows,
    );
  });
});

// ------------------------------------------------------------------ delivery

describe('the change feed', () => {
  it('reaches every mounted list', () => {
    const seen: ItemChange[][] = [[], []];
    renderHook(() => {
      useItemChanges((c) => seen[0].push(c));
      useItemChanges((c) => seen[1].push(c));
    });

    act(() => publishItemChange({ type: 'deleted', id: 'a' }));

    expect(seen[0]).toHaveLength(1);
    expect(seen[1]).toHaveLength(1);
  });

  it('stops reaching a list once it unmounts', () => {
    const seen: ItemChange[] = [];
    const { unmount } = renderHook(() =>
      useItemChanges((c) => seen.push(c)),
    );

    act(() => publishItemChange({ type: 'deleted', id: 'a' }));
    unmount();
    act(() => publishItemChange({ type: 'deleted', id: 'b' }));

    expect(seen.map((c) => c.id)).toEqual(['a']);
  });

  it('still reaches the others when one of them throws', () => {
    // The change already happened on the server. A screen that cannot apply
    // it is a bad render, not a reason to leave the next screen stale.
    const seen: string[] = [];
    renderHook(() => {
      useItemChanges(() => {
        throw new Error('bad render');
      });
      useItemChanges((c) => seen.push(c.id));
    });

    act(() => publishItemChange({ type: 'deleted', id: 'a' }));
    expect(seen).toEqual(['a']);
  });
});
