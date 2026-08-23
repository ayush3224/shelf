/**
 * The audio capture request (UC1, UC7) — headers, routing and error mapping.
 *
 * The body itself is tested in `multipart-body.test.ts`, by running it through
 * Expo's real encoder. It is deliberately not tested here any more: this suite
 * used to assert that `append` received `{uri, name, type}`, which is to say it
 * asserted the client agreed with itself. It passed while every capture on the
 * device failed, because nothing here ever asked whether the thing being sent
 * could be encoded.
 */
import { audioUrl, captureAudio } from '../lib/api';
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
jest.mock('expo-file-system', () => ({
  // Written with plain fields: a TypeScript parameter property transpiles to
  // something jest's out-of-scope check rejects inside a mock factory.
  File: class {
    uri: string;
    name: string;
    type = 'audio/mp4';
    constructor(uri: string) {
      this.uri = uri;
      this.name = uri.split('/').pop() as string;
    }
    async bytes() {
      return new Uint8Array([1, 2, 3]);
    }
  },
}));

const recording = {
  uri: 'file:///data/user/0/com.shelf.app/cache/capture-1.m4a',
  name: 'capture-1.m4a',
  mimeType: 'audio/m4a',
};

type Sent = { url: string; init: RequestInit };

/** Answer every API call with `body`, recording what was sent. */
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

beforeEach(() => {
  jest
    .spyOn(supabase.auth, 'getSession')
    .mockResolvedValue({
      data: { session: { access_token: 'test-token' } },
      error: null,
    } as never);
});

afterEach(() => {
  jest.restoreAllMocks();
});

afterAll(async () => {
  await supabase.auth.stopAutoRefresh();
});

describe('captureAudio', () => {
  it('does not set Content-Type, so the multipart boundary survives', async () => {
    const sent = stubApi({ id: 'i1', items: [] });
    await captureAudio(recording);

    const headers = sent[0].init.headers as Record<string, string>;
    // Setting this by hand names a boundary that is not in the body, and the
    // server then parses zero parts — a silent, confusing failure.
    expect(headers['Content-Type']).toBeUndefined();
    expect(headers.Authorization).toBe('Bearer test-token');
  });

  it('posts to the audio route', async () => {
    const sent = stubApi({ id: 'i1', items: [] });
    await captureAudio(recording);
    expect(sent[0].url).toContain('/capture/audio');
    expect(sent[0].init.method).toBe('POST');
  });

  it('reports a refused upload as an ApiError carrying the server detail', async () => {
    stubApi({ detail: 'Could not save the recording.' }, 503);
    await expect(captureAudio(recording)).rejects.toMatchObject({
      status: 503,
      message: 'Could not save the recording.',
    });
  });
});

describe('audioUrl', () => {
  it('asks for the item it was given', async () => {
    const sent = stubApi({ id: 'i1', url: 'https://signed', expires_in: 3600 });
    const result = await audioUrl('i1');

    expect(sent[0].url).toContain('/items/i1/audio');
    expect(result.url).toBe('https://signed');
  });
});
