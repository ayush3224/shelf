/**
 * The Shelf client and its grouping (UC33, UC34, UC36).
 *
 * Two things are worth pinning here and they fail in opposite directions.
 *
 * The query string is one: `state` is a *repeated* parameter, and a client
 * that comma-joins it sends one unknown state and gets a 400 — or worse,
 * sends nothing and silently falls back to the default list while the chips
 * still look selected.
 *
 * The grouping is the other. Sections have to survive pagination: a project's
 * items can straddle a page boundary, so appending a page must extend the
 * section that already exists rather than starting a second one with the same
 * name underneath it.
 */
import { browseItems, projects } from '../lib/api';
import { groupByProject, windowStart } from '../lib/shelf';
import type { ShelfItem } from '../lib/api';
import { supabase } from '../lib/supabase';

jest.mock('expo-secure-store');
jest.mock('expo-auth-session', () => ({
  makeRedirectUri: ({ scheme, path }: { scheme: string; path: string }) =>
    `${scheme}://${path}`,
}));
jest.mock('expo-web-browser', () => ({
  openAuthSessionAsync: jest.fn(async () => ({ type: 'cancel' })),
  warmUpAsync: jest.fn(),
  coolDownAsync: jest.fn(),
}));
jest.mock('expo-file-system', () => ({ File: class {} }));

type Sent = { url: string; init: RequestInit };

function stubApi(body: unknown, status = 200): Sent[] {
  const sent: Sent[] = [];
  globalThis.fetch = jest.fn(async (url: unknown, init: unknown) => {
    sent.push({ url: String(url), init: init as RequestInit });
    return {
      ok: status < 400,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  }) as unknown as typeof fetch;
  return sent;
}

/** The query string of the one request that was made, already parsed. */
function query(sent: Sent[]): URLSearchParams {
  return new URLSearchParams(String(sent[0].url).split('?')[1] ?? '');
}

const page = { items: [], next_cursor: null, has_more: false, states: [] };

function shelfItem(overrides: Partial<ShelfItem> = {}): ShelfItem {
  return {
    id: 'a',
    text: 'Get the pollution certificate',
    raw_text: 'Get the pollution certificate',
    kind: 'task',
    state: 'shelved',
    due_at: null,
    critical: false,
    parse_status: 'ok',
    has_audio: false,
    project_id: null,
    project_name: null,
    created_at: '2026-08-23T11:33:56Z',
    state_changed_at: '2026-08-23T11:33:56Z',
    ...overrides,
  };
}

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  jest.restoreAllMocks();
});

// ------------------------------------------------------------ query string

test('no filters asks for nothing in particular', async () => {
  const sent = stubApi(page);
  await browseItems();

  expect(String(sent[0].url)).toMatch(/\/items$/);
});

test('states are repeated, not comma-joined', async () => {
  const sent = stubApi(page);
  await browseItems({ states: ['shelved', 'done'] });

  expect(query(sent).getAll('state')).toEqual(['shelved', 'done']);
});

test('a search, a window and a page size travel together', async () => {
  const sent = stubApi(page);
  await browseItems({
    q: 'pollution',
    from: '2026-08-01T00:00:00.000Z',
    limit: 30,
  });

  const q = query(sent);
  expect(q.get('q')).toBe('pollution');
  expect(q.get('from')).toBe('2026-08-01T00:00:00.000Z');
  expect(q.get('limit')).toBe('30');
});

test('a cursor is passed back exactly as it was issued', async () => {
  // Base64url can contain `-` and `_`; a client that mangled the encoding
  // would send a token the server refuses and the list would stop paging.
  const cursor = 'MjAyNi0wOC0yM3xhYmM-ZGVm_Z2g';
  const sent = stubApi(page);
  await browseItems({ cursor });

  expect(query(sent).get('cursor')).toBe(cursor);
});

test('an empty search is not sent at all', async () => {
  const sent = stubApi(page);
  await browseItems({ q: '' });

  expect(query(sent).has('q')).toBe(false);
});

test('projects are fetched for the filter chips', async () => {
  const sent = stubApi({ projects: [] });
  await expect(projects()).resolves.toEqual({ projects: [] });
  expect(String(sent[0].url)).toMatch(/\/projects$/);
});

// --------------------------------------------------------------- grouping

test('items with no project fall under Unsorted', () => {
  const sections = groupByProject([shelfItem({ id: 'a' }), shelfItem({ id: 'b' })]);

  expect(sections).toHaveLength(1);
  expect(sections[0].title).toBe('Unsorted');
  expect(sections[0].data.map((i) => i.id)).toEqual(['a', 'b']);
});

test('a project keeps the position of its most recent item', () => {
  // Rows arrive newest first, so first appearance is the ordering.
  const sections = groupByProject([
    shelfItem({ id: 'a', project_id: 'p2', project_name: 'Work' }),
    shelfItem({ id: 'b' }),
    shelfItem({ id: 'c', project_id: 'p1', project_name: 'House' }),
  ]);

  expect(sections.map((s) => s.title)).toEqual(['Work', 'Unsorted', 'House']);
});

test('a later page extends the section it belongs to', () => {
  // The whole reason grouping is the client's job: page two must not open a
  // second "House" underneath the first.
  const first = [
    shelfItem({ id: 'a', project_id: 'p1', project_name: 'House' }),
    shelfItem({ id: 'b' }),
  ];
  const second = [
    shelfItem({ id: 'c', project_id: 'p1', project_name: 'House' }),
    shelfItem({ id: 'd' }),
  ];

  const sections = groupByProject([...first, ...second]);

  expect(sections.map((s) => s.title)).toEqual(['House', 'Unsorted']);
  expect(sections[0].data.map((i) => i.id)).toEqual(['a', 'c']);
  expect(sections[1].data.map((i) => i.id)).toEqual(['b', 'd']);
});

test('two projects sharing a name stay apart', () => {
  // Keyed on the id, not the label. Renaming is a hand edit here, so two
  // projects called the same thing is a thing a person can actually do.
  const sections = groupByProject([
    shelfItem({ id: 'a', project_id: 'p1', project_name: 'House' }),
    shelfItem({ id: 'b', project_id: 'p2', project_name: 'House' }),
  ]);

  expect(sections).toHaveLength(2);
});

test('an empty list groups into nothing', () => {
  expect(groupByProject([])).toEqual([]);
});

// ----------------------------------------------------------- date windows

test('any time asks for no bound at all', () => {
  expect(windowStart(null)).toBeUndefined();
});

test('a window is measured back from now', () => {
  const now = new Date('2026-08-24T12:00:00.000Z');
  expect(windowStart(7, now)).toBe('2026-08-17T12:00:00.000Z');
  expect(windowStart(365, now)).toBe('2025-08-24T12:00:00.000Z');
});
