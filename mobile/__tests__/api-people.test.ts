/**
 * The People client (UC46, UC47).
 *
 * Two things are worth pinning. The search term has to be encoded — names
 * contain spaces and the occasional apostrophe, and an unencoded query string
 * is the kind of thing that works for "Priya" and breaks for "Priya O'Neill".
 * And the person page's cursor has to survive the round trip: base64url
 * contains `-` and `_`, and a client that mangles it stops paging silently.
 */
import { mergePerson, people, person, splitPerson } from '../lib/api';
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

const PERSON = {
  id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  name: 'Priya Sharma',
  type: 'person',
  aliases: ['Priya'],
  mentions: 2,
  last_mentioned: '2026-08-23T11:00:00Z',
};

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

test('an unfiltered list asks for nothing in particular', async () => {
  const sent = stubApi({ people: [] });
  await people();

  expect(String(sent[0].url)).toMatch(/\/people$/);
});

test('a search term is encoded, spaces and all', async () => {
  const sent = stubApi({ people: [] });
  await people("Priya O'Neill");

  expect(String(sent[0].url)).toContain('?q=Priya%20O');
  expect(String(sent[0].url)).not.toContain(' ');
});

test('an empty search is not sent as a filter', async () => {
  const sent = stubApi({ people: [] });
  await people('');

  expect(String(sent[0].url)).toMatch(/\/people$/);
});

test('a person comes back with their aliases and count', async () => {
  stubApi({ people: [PERSON] });
  const result = await people();

  expect(result.people[0].aliases).toEqual(['Priya']);
  expect(result.people[0].mentions).toBe(2);
});

test('a person page is fetched by id', async () => {
  const sent = stubApi({ person: PERSON, items: [], next_cursor: null, has_more: false });
  await person(PERSON.id);

  expect(String(sent[0].url)).toMatch(new RegExp(`/people/${PERSON.id}$`));
});

test('a cursor is handed back exactly as it was issued', async () => {
  // Base64url carries `-` and `_`; mangling it stops paging with no error.
  const cursor = 'MjAyNi0wOC0yM3xhYmM-ZGVm_Z2g';
  const sent = stubApi({ person: PERSON, items: [], next_cursor: null, has_more: false });
  await person(PERSON.id, cursor);

  const qs = new URLSearchParams(String(sent[0].url).split('?')[1] ?? '');
  expect(qs.get('cursor')).toBe(cursor);
});

// ------------------------------------------------ correcting a person

test('a merge names the person being folded in', async () => {
  const sent = stubApi({
    person: PERSON,
    absorbed_id: 'other',
    absorbed_name: 'Priya N.',
    moved: 2,
  });
  await mergePerson(PERSON.id, 'other');

  expect(String(sent[0].url)).toMatch(new RegExp(`/people/${PERSON.id}/merge$`));
  expect(JSON.parse(sent[0].init.body as string)).toEqual({ absorb: 'other' });
  expect(sent[0].init.method).toBe('POST');
});

test('a split to an existing person sends into_id', async () => {
  const sent = stubApi({
    target: PERSON,
    source: PERSON,
    source_removed: false,
    target_created: false,
    moved: 1,
    aliases_moved: [],
  });
  await splitPerson(PERSON.id, ['item-1'], { id: 'target-1' });

  expect(JSON.parse(sent[0].init.body as string)).toEqual({
    item_ids: ['item-1'],
    into_id: 'target-1',
  });
});

test('a split to a new person sends into_name and never both', async () => {
  // The server refuses both-or-neither, so a client that sent both would turn
  // a correction into a 400 at the moment the user is trying to fix something.
  const sent = stubApi({
    target: PERSON,
    source: PERSON,
    source_removed: false,
    target_created: true,
    moved: 2,
    aliases_moved: ['Priya'],
  });
  await splitPerson(PERSON.id, ['a', 'b'], { name: 'Priya Nair' });

  const body = JSON.parse(sent[0].init.body as string);
  expect(body).toEqual({ item_ids: ['a', 'b'], into_name: 'Priya Nair' });
  expect(body).not.toHaveProperty('into_id');
});

test('a split reports the aliases that followed the notes', async () => {
  stubApi({
    target: { ...PERSON, name: 'Priya Nair' },
    source: PERSON,
    source_removed: false,
    target_created: true,
    moved: 1,
    aliases_moved: ['Priya'],
  });
  const result = await splitPerson(PERSON.id, ['a'], { name: 'Priya Nair' });

  expect(result.aliases_moved).toEqual(['Priya']);
});

test('a split that empties the source says so', async () => {
  // The screen has to navigate away; the page it is on no longer exists.
  stubApi({
    target: PERSON,
    source: null,
    source_removed: true,
    target_created: true,
    moved: 3,
    aliases_moved: [],
  });
  const result = await splitPerson(PERSON.id, ['a', 'b', 'c'], { name: 'Anil' });

  expect(result.source_removed).toBe(true);
  expect(result.source).toBeNull();
});
