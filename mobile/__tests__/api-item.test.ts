/**
 * The item detail client (UC37, UC38, UC21, UC39).
 *
 * The part worth pinning is the `due_at` distinction: omitting it must leave
 * the time alone and sending `null` must clear it. `undefined` and `null` mean
 * different things on this endpoint, and a client that collapses them turns
 * "fix the wording" into "and also shelve it".
 */
import {
  addItemPerson,
  addToCalendar,
  deleteItem,
  editItem,
  item,
  removeFromCalendar,
  removeItemPerson,
  setItemState,
} from '../lib/api';
import { supabase } from '../lib/supabase';

const ID = 'b3f0c1a2-0000-4000-8000-000000000001';

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

const parsed = (sent: Sent) => JSON.parse(sent.init.body as string);

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());
afterAll(async () => {
  await supabase.auth.stopAutoRefresh();
});

describe('loading an item', () => {
  it('gets it by id', async () => {
    const sent = stubApi({ id: ID, text: 'Call the bank' });
    await item(ID);
    expect(sent[0].url).toContain(`/items/${ID}`);
    expect(sent[0].init.method).toBeUndefined(); // a GET
  });
});

describe('editing (UC38)', () => {
  it('sends only the text when only the text changed', async () => {
    const sent = stubApi({ id: ID });
    await editItem(ID, { text: 'Call the insurer' });

    expect(sent[0].init.method).toBe('PATCH');
    // The absent key is the whole point: present-and-null would clear the time.
    expect(parsed(sent[0])).toEqual({ text: 'Call the insurer' });
    expect('due_at' in parsed(sent[0])).toBe(false);
  });

  it('sends due_at: null when the time is being cleared', async () => {
    const sent = stubApi({ id: ID });
    await editItem(ID, { due_at: null });

    expect(parsed(sent[0])).toEqual({ due_at: null });
  });

  it('sends a new due time when one is set', async () => {
    const sent = stubApi({ id: ID });
    await editItem(ID, { due_at: '2026-08-24T09:30:00.000Z' });

    expect(parsed(sent[0]).due_at).toBe('2026-08-24T09:30:00.000Z');
  });

  it('can change both at once', async () => {
    const sent = stubApi({ id: ID });
    await editItem(ID, { text: 'Call the insurer', due_at: null });

    expect(parsed(sent[0])).toEqual({ text: 'Call the insurer', due_at: null });
  });

  it('surfaces a refused edit with the server detail', async () => {
    stubApi({ detail: 'text cannot be blank' }, 400);
    await expect(editItem(ID, { text: ' ' })).rejects.toMatchObject({
      status: 400,
      message: 'text cannot be blank',
    });
  });
});

describe('manual state moves (UC21)', () => {
  it.each(['active', 'shelved', 'done', 'dropped'] as const)(
    'posts %s',
    async (state) => {
      const sent = stubApi({ id: ID, state, previous: 'active', changed: true });
      await setItemState(ID, state);

      expect(sent[0].url).toContain(`/items/${ID}/state`);
      expect(sent[0].init.method).toBe('POST');
      expect(parsed(sent[0])).toEqual({ state });
    },
  );
});

describe('delete (UC39)', () => {
  it('sends a DELETE and reports whether audio went too', async () => {
    const sent = stubApi({ id: ID, deleted: true, audio_deleted: true });
    const result = await deleteItem(ID);

    expect(sent[0].url).toContain(`/items/${ID}`);
    expect(sent[0].init.method).toBe('DELETE');
    expect(result.audio_deleted).toBe(true);
  });

  it('surfaces a 404 rather than reporting success', async () => {
    stubApi({ detail: 'No such item' }, 404);
    await expect(deleteItem(ID)).rejects.toMatchObject({ status: 404 });
  });
});


// ------------------------------------------ linking people by hand (UC45, D45)
//
// The wire shape is the whole risk here: the server takes exactly one of
// `person_id` and `name`, and a client that sent both — or sent `name: ''` for
// a picked person — would get a 400 on the correction path, which is the path
// that exists precisely because something already went wrong.

describe('linking a person to an item', () => {
  it('sends a picked person as an id', async () => {
    const sent = stubApi({ id: ID, people: [], changed: true });
    await addItemPerson(ID, { id: 'e1' });

    expect(sent[0].url).toContain(`/items/${ID}/people`);
    expect(sent[0].init.method).toBe('POST');
    expect(parsed(sent[0])).toEqual({ person_id: 'e1' });
  });

  it('sends a typed name as a name, and nothing else', async () => {
    const sent = stubApi({ id: ID, people: [], changed: true });
    await addItemPerson(ID, { name: 'Priya Sharma' });

    // Not `{ person_id: undefined, name: ... }`: the server reads
    // both-or-neither as a client bug and refuses it.
    expect(parsed(sent[0])).toEqual({ name: 'Priya Sharma' });
  });

  it('unlinks by id, on the item rather than on the person', async () => {
    const sent = stubApi({ id: ID, people: [], person_removed: false });
    await removeItemPerson(ID, 'e1');

    expect(sent[0].url).toContain(`/items/${ID}/people/e1`);
    expect(sent[0].init.method).toBe('DELETE');
  });

  it('returns who the item is linked to afterwards', async () => {
    stubApi({
      id: ID,
      people: [{ id: 'e1', name: 'Priya Sharma', type: 'person' }],
      changed: true,
      person_removed: false,
    });
    const result = await addItemPerson(ID, { name: 'Priya Sharma' });

    expect(result.people.map((p) => p.name)).toEqual(['Priya Sharma']);
    expect(result.changed).toBe(true);
  });
});

describe('the calendar (UC43, D59)', () => {
  const pending = {
    id: ID,
    on_calendar: true,
    changed: true,
    sync_state: 'pending',
    queued: false,
  };

  it('adds with a POST and no body to get wrong', async () => {
    const sent = stubApi(pending);
    const result = await addToCalendar(ID);

    expect(sent[0].url).toContain(`/items/${ID}/calendar`);
    expect(sent[0].init.method).toBe('POST');
    // The item id is the whole request. What goes in the event is read from
    // the row by the tick, never sent from here.
    expect(sent[0].init.body).toBeUndefined();
    // Pending, not synced: the answer is "written down", not "on Google".
    expect(result.sync_state).toBe('pending');
  });

  it('removes with a DELETE on the same path', async () => {
    const sent = stubApi({ ...pending, on_calendar: false, queued: true });
    const result = await removeFromCalendar(ID);

    expect(sent[0].url).toContain(`/items/${ID}/calendar`);
    expect(sent[0].init.method).toBe('DELETE');
    expect(result.queued).toBe(true);
  });
});
