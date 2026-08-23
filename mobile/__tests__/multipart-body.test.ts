/**
 * The multipart body, run through the encoder that actually consumes it.
 *
 * The previous version of this suite spied on `FormData.append` and asserted
 * the `{uri, name, type}` object was passed. That test passed while the device
 * failed, because it asserted *what the client sent itself* rather than
 * whether anything downstream could use it. It had encoded the bug as the
 * expected behaviour.
 *
 * Expo replaces the global `fetch` with its own WinterCG implementation
 * (`expo/src/winter/runtime.native.ts`), whose encoder accepts a part only if
 * it is a string, a `Blob`, or an object exposing `bytes()`. So the real
 * assertion is: hand the body to *that* encoder and see whether it survives.
 */
import { convertFormDataAsync } from 'expo/src/winter/fetch/convertFormData';
import { installFormDataPatch } from 'expo/src/winter/FormData';
import RNFormData from 'react-native/Libraries/Network/FormData';

import { captureAudio } from '../lib/api';
import { supabase } from '../lib/supabase';

// Reproduce the device's stack rather than the test runner's.
//
// Jest supplies a standards-compliant FormData, which coerces a non-Blob object
// to "[object Object]" on append — so the file part never reaches the encoder
// and a test using it proves nothing. On the device it is React Native's
// FormData, which keeps objects verbatim in `_parts`.
installFormDataPatch(RNFormData as unknown as typeof FormData);
globalThis.FormData = RNFormData as unknown as typeof FormData;

/**
 * Hand the encoder the pairs Expo's patch would.
 *
 * `installFormDataPatch` adds `entries()` with `??=`, which does not take
 * effect under this transform, so it is supplied here instead: it yields each
 * `[name, value]` out of `_parts` untouched, which is all the patch does.
 * Everything either side of this line is the real implementation — React
 * Native's `append`, and Expo's encoder.
 */
function encode(form: FormData) {
  const { _parts } = form as unknown as { _parts: [string, unknown][] };
  return convertFormDataAsync({ entries: () => _parts } as unknown as FormData);
}

const URI = 'file:///data/user/0/com.shelf.app/cache/Audio/recording-abc.m4a';
const AUDIO = new Uint8Array([0, 0, 0, 32, 102, 116, 121, 112, 77, 52, 65, 32]);

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

// Mirrors expo-file-system's File: implements the Blob interface without
// extending Blob, and exposes bytes()/name/type — which is exactly the surface
// Expo's encoder looks for.
jest.mock('expo-file-system', () => ({
  File: class {
    readonly uri: string;
    readonly name: string;
    readonly type = 'audio/mp4';
    readonly size = AUDIO_LENGTH;
    readonly exists = true;
    constructor(uri: string) {
      if (!uri.startsWith('file://')) throw new Error(`bad uri: ${uri}`);
      this.uri = uri;
      this.name = uri.split('/').pop() as string;
    }
    async bytes() {
      return new Uint8Array([0, 0, 0, 32, 102, 116, 121, 112, 77, 52, 65, 32]);
    }
  },
}));
const AUDIO_LENGTH = 12;

const recording = { uri: URI, name: 'recording-abc.m4a', mimeType: 'audio/m4a' };

/** Capture the FormData the client builds, without sending it. */
function captureBody(): { body: () => FormData } {
  let sent: FormData | undefined;
  globalThis.fetch = jest.fn(async (_url: unknown, init: unknown) => {
    sent = (init as RequestInit).body as FormData;
    return { ok: true, status: 200, json: async () => ({ id: 'x', items: [] }) };
  }) as unknown as typeof fetch;
  return {
    body: () => {
      if (!sent) throw new Error('nothing was sent');
      return sent;
    },
  };
}

/** Bytes as latin-1, so binary content can be searched as text. */
function decode(bytes: Uint8Array): string {
  let out = '';
  for (const byte of bytes) out += String.fromCharCode(byte);
  return out;
}

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

describe("Expo's multipart encoder", () => {
  it('accepts the body captureAudio builds', async () => {
    const sent = captureBody();
    await captureAudio(recording);

    // The assertion that matters. Before the fix this threw
    // "Unsupported FormDataPart implementation".
    await expect(encode(sent.body())).resolves.toBeDefined();
  });

  it('rejects the React Native {uri, name, type} shape', async () => {
    // Pinning the actual constraint, so nobody "simplifies" back to it. RN's
    // shape is correct for RN's XHR fetch — which this app does not use.
    const form = new FormData();
    form.append('audio', {
      uri: URI,
      name: 'recording-abc.m4a',
      type: 'audio/m4a',
    } as unknown as Blob);

    await expect(encode(form)).rejects.toThrow(
      'Unsupported FormDataPart implementation',
    );
  });

  it('writes the file bytes into the body', async () => {
    const sent = captureBody();
    await captureAudio(recording);
    const { body } = await encode(sent.body());

    expect(decode(body)).toContain(decode(AUDIO));
  });

  it('names the part and gives it a content type the bucket accepts', async () => {
    const sent = captureBody();
    await captureAudio(recording);
    const { body } = await encode(sent.body());
    const text = decode(body);

    expect(text).toContain('name="audio"');
    expect(text).toContain('filename="recording-abc.m4a"');
    // Canonical spelling: the bucket's allow-list rejects audio/m4a.
    expect(text).toContain('content-type: audio/mp4');
  });

  it('carries the scalar fields alongside the file', async () => {
    const sent = captureBody();
    await captureAudio(recording, { transcript: 'call the bank', confidence: 0.9 });
    const { body } = await encode(sent.body());
    const text = decode(body);

    expect(text).toContain('name="source"');
    expect(text).toContain('voice');
    expect(text).toContain('name="transcript"');
    expect(text).toContain('call the bank');
    expect(text).toContain('name="transcript_confidence"');
    expect(text).toContain('0.9');
  });

  it('omits the transcript fields when there is no on-device transcript', async () => {
    const sent = captureBody();
    await captureAudio(recording);
    const { body } = await encode(sent.body());

    expect(decode(body)).not.toContain('name="transcript"');
  });

  it('closes the body with the terminating boundary', async () => {
    const sent = captureBody();
    await captureAudio(recording);
    const { body, boundary } = await encode(sent.body());

    expect(decode(body).endsWith(`--${boundary}--\r\n`)).toBe(true);
  });
});

describe('a URI that will not open', () => {
  it('fails as a client error before anything is dispatched', async () => {
    const dispatched = jest.fn();
    globalThis.fetch = dispatched as unknown as typeof fetch;

    const error = await captureAudio({ ...recording, uri: '/no/scheme.m4a' }).catch(
      (e) => e,
    );

    expect(error.kind).toBe('client');
    expect(error.message).toBe('The app could not open that recording.');
    expect(dispatched).not.toHaveBeenCalled();
  });
});
