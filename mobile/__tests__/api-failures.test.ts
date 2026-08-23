/**
 * How a failed request is reported.
 *
 * This exists because the client used to answer every throw with
 * "No connection." — so a malformed body, an unreadable file part and a flat
 * network were indistinguishable, and the one bug that produced them all sent
 * the investigation to the server. The classification is the fix, so it is
 * what gets pinned here.
 */
import { ApiError, capture, captureAudio, today } from '../lib/api';
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
  uri: 'file:///data/user/0/com.shelf.app/cache/Audio/recording-abc.m4a',
  name: 'recording-abc.m4a',
  mimeType: 'audio/m4a',
};

/** Make fetch throw `error`. */
function fetchThrows(error: unknown) {
  globalThis.fetch = jest.fn(async () => {
    throw error;
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  // The classifier logs the real throw; keep the suite output readable.
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());
afterAll(async () => {
  await supabase.auth.stopAutoRefresh();
});

describe('a genuine network failure', () => {
  it('is classified as transport', async () => {
    fetchThrows(new TypeError('Network request failed'));
    await expect(captureAudio(recording)).rejects.toMatchObject({
      kind: 'transport',
      message: 'No connection.',
    });
  });

  it.each([
    'Unable to resolve host "srv1531684.hstgr.cloud"',
    'Connection reset by peer',
    'SSL handshake aborted',
  ])('recognises %s as transport', async (message) => {
    fetchThrows(new Error(message));
    await expect(captureAudio(recording)).rejects.toMatchObject({
      kind: 'transport',
    });
  });
});

describe('a failure before dispatch', () => {
  it('is classified as client, not as no-connection', async () => {
    fetchThrows(new TypeError("Cannot read property 'uri' of undefined"));
    const error = await captureAudio(recording).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe('client');
    expect(error.isLocalFailure).toBe(true);
    // The old behaviour, and the reason the last investigation went to the VPS.
    expect(error.message).not.toBe('No connection.');
  });

  it('keeps the original throw instead of discarding it', async () => {
    const original = new RangeError('body too large to serialise');
    fetchThrows(original);
    const error = await captureAudio(recording).catch((e) => e);

    expect(error.cause).toBe(original);
    expect(error.diagnostic).toContain('RangeError');
    expect(error.diagnostic).toContain('body too large to serialise');
  });

  it('logs the real exception', async () => {
    const spy = jest.spyOn(console, 'error');
    fetchThrows(new Error('some native module blew up'));
    await captureAudio(recording).catch(() => undefined);

    expect(spy).toHaveBeenCalled();
    const logged = spy.mock.calls.flat().map(String).join(' ');
    expect(logged).toContain('/capture/audio');
    expect(logged).toContain('some native module blew up');
  });
});

describe('a timeout', () => {
  it('is classified as timeout, not transport', async () => {
    globalThis.fetch = jest.fn((_url: unknown, init: unknown) => {
      const { signal } = init as { signal: AbortSignal };
      return new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () =>
          reject(new Error('Aborted')),
        );
      });
    }) as unknown as typeof fetch;

    jest.useFakeTimers();
    try {
      const pending = today().catch((e) => e);
      await jest.advanceTimersByTimeAsync(31_000);
      const error = await pending;
      expect(error.kind).toBe('timeout');
      expect(error.message).toBe('The server took too long.');
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('a server error', () => {
  it('keeps the status and the server detail', async () => {
    globalThis.fetch = jest.fn(async () => ({
      ok: false,
      status: 503,
      json: async () => ({ detail: 'Could not save the recording.' }),
      text: async () => '',
    })) as unknown as typeof fetch;

    await expect(captureAudio(recording)).rejects.toMatchObject({
      kind: 'http',
      status: 503,
      message: 'Could not save the recording.',
    });
  });

  it('does not report an unreadable 200 body as a network failure', async () => {
    globalThis.fetch = jest.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError('Unexpected token < in JSON');
      },
    })) as unknown as typeof fetch;

    const error = await capture('hello').catch((e) => e);
    expect(error.kind).toBe('http');
    expect(error.message).toBe('The server sent something unreadable.');
  });
});

describe('the text and audio paths', () => {
  it('are classified the same way, so one cannot mislead about the other', async () => {
    fetchThrows(new TypeError('Network request failed'));
    const textError = await capture('hello').catch((e) => e);
    const audioError = await captureAudio(recording).catch((e) => e);

    expect(textError.kind).toBe('transport');
    expect(audioError.kind).toBe('transport');
  });

  it('still sends text as JSON — only the audio path is multipart', async () => {
    const sent: RequestInit[] = [];
    globalThis.fetch = jest.fn(async (_url: unknown, init: unknown) => {
      sent.push(init as RequestInit);
      return { ok: true, status: 200, json: async () => ({ id: 'x' }) };
    }) as unknown as typeof fetch;

    await capture('call the bank');
    const headers = sent[0].headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
    expect(typeof sent[0].body).toBe('string');
  });
});
