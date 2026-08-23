/**
 * The audio capture request (UC1, UC7).
 *
 * What is worth pinning here is the shape of the request rather than the
 * response: a multipart upload has one classic way of failing silently, and it
 * fails as an empty parse server-side rather than as an error the client sees.
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

/**
 * Record what was appended to a FormData.
 *
 * Reading the FormData back does not work: React Native's implementation keeps
 * the `{uri, name, type}` file descriptor as an object, while the standard one
 * this test runs under coerces it to "[object Object]". Spying on `append`
 * captures what the code passed, which is the thing under test.
 */
let appended: [string, unknown][] = [];

beforeEach(() => {
  appended = [];
  jest
    .spyOn(FormData.prototype, 'append')
    .mockImplementation((key: string, value: unknown) => {
      appended.push([key, value]);
    });
});

function parts(): [string, unknown][] {
  return appended;
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

  it('sends the file by URI and marks the capture as voice', async () => {
    const sent = stubApi({ id: 'i1', items: [] });
    await captureAudio(recording);

    const entries = Object.fromEntries(parts());
    expect(sent[0].url).toContain('/capture/audio');
    expect(entries.source).toBe('voice');
    expect(entries.audio).toMatchObject({
      uri: recording.uri,
      name: recording.name,
      type: recording.mimeType,
    });
  });

  it('omits the transcript fields when there is no on-device transcript', async () => {
    const sent = stubApi({ id: 'i1', items: [] });
    await captureAudio(recording);

    const keys = parts().map(([key]) => key);
    // Sending an empty transcript would make the server skip the cloud path
    // and store a capture with no words at all.
    expect(keys).not.toContain('transcript');
    expect(keys).not.toContain('transcript_confidence');
  });

  it('passes an on-device transcript through when there is one', async () => {
    const sent = stubApi({ id: 'i1', items: [] });
    await captureAudio(recording, { transcript: 'call the bank', confidence: 0.9 });

    const entries = Object.fromEntries(parts());
    expect(entries.transcript).toBe('call the bank');
    expect(entries.transcript_confidence).toBe('0.9');
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
