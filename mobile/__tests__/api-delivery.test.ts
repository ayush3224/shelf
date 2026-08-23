/**
 * The delivery half of the API client (UC23, UC17, UC20).
 *
 * What is pinned here is the request shape, because these three are the calls
 * a notification button makes with nobody watching. A wrong path or a body
 * the server reads differently shows up as "the Done button does nothing",
 * with no error anywhere to follow.
 */
import { registerDevice, reactivateItem, snoozeItem } from '../lib/api';
import { supabase } from '../lib/supabase';

const ID = 'b3f0c1a2-0000-4000-8000-000000000001';
const TOKEN = 'ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]';

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

describe('registering a device (UC23)', () => {
  it('posts the token, the platform and a name', async () => {
    const sent = stubApi({ registered: true, devices: 1 });

    await registerDevice({ token: TOKEN, platform: 'android', device_name: 'Pixel' });

    expect(sent[0].url).toContain('/devices');
    expect(sent[0].init.method).toBe('POST');
    expect(parsed(sent[0])).toEqual({
      token: TOKEN,
      platform: 'android',
      device_name: 'Pixel',
    });
  });

  it('carries the session, like every other call (D11)', async () => {
    const sent = stubApi({ registered: true, devices: 1 });

    await registerDevice({ token: TOKEN, platform: 'android' });

    const headers = sent[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer test-token');
  });
});

describe('snoozing (UC17)', () => {
  it('sends an empty body when no duration is chosen', async () => {
    // The notification button names no duration on purpose: the default is
    // one number and it lives on the server.
    const sent = stubApi({ id: ID, state: 'active', changed: true, snooze_count: 1 });

    await snoozeItem(ID);

    expect(sent[0].url).toContain(`/items/${ID}/snooze`);
    expect(sent[0].init.method).toBe('POST');
    expect(parsed(sent[0])).toEqual({});
  });

  it('sends a duration when one is chosen', async () => {
    const sent = stubApi({ id: ID, state: 'active', changed: true, snooze_count: 1 });

    await snoozeItem(ID, 90);

    expect(parsed(sent[0])).toEqual({ minutes: 90 });
  });

  it('passes back the fact that the item had already moved', async () => {
    const stale = {
      id: ID,
      state: 'shelved',
      due_at: null,
      snooze_count: 3,
      changed: false,
    };
    stubApi(stale);

    expect(await snoozeItem(ID)).toEqual(stale);
  });
});

describe('reactivating (UC20)', () => {
  it('sends an empty body, letting the server pick the due time', async () => {
    const sent = stubApi({ id: ID, state: 'active', previous: 'shelved', changed: true });

    await reactivateItem(ID);

    expect(sent[0].url).toContain(`/items/${ID}/reactivate`);
    expect(sent[0].init.method).toBe('POST');
    expect(parsed(sent[0])).toEqual({});
  });

  it('sends a time when the user names one', async () => {
    const sent = stubApi({ id: ID, state: 'active', previous: 'shelved', changed: true });

    await reactivateItem(ID, '2026-08-27T09:00:00.000Z');

    expect(parsed(sent[0])).toEqual({ due_at: '2026-08-27T09:00:00.000Z' });
  });
});
