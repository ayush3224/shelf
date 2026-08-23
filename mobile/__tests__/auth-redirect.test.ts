/**
 * The OAuth redirect, end to end minus the OS and the network (UC41).
 *
 * Runs the real `@supabase/auth-js` against the real chunked keystore adapter;
 * only `expo-secure-store` and `fetch` are stand-ins. That is what makes this
 * worth having — the parts most likely to break (the PKCE verifier surviving a
 * round trip through chunked storage, and the session doing the same) are the
 * parts actually exercised.
 */
import * as SecureStore from 'expo-secure-store';

import {
  AUTH_CALLBACK_PATH,
  OAUTH_REDIRECT_URI,
  completeAuthRedirect,
  isAuthCallbackUrl,
} from '../lib/auth';
import { secureStorage } from '../lib/secureStorage';
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

const store = SecureStore as unknown as {
  __reset(): void;
  __keys(): string[];
  __raw(key: string): string | undefined;
  KEYSTORE_VALUE_LIMIT: number;
};

/** A Google-provider session at a realistic size, not a toy one. */
function session() {
  return {
    access_token: `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.${'a'.repeat(1200)}.sig`,
    refresh_token: 'r'.repeat(64),
    provider_token: `ya29.${'g'.repeat(180)}`,
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: {
      id: '11111111-2222-3333-4444-555555555555',
      aud: 'authenticated',
      role: 'authenticated',
      email: 'someone@example.com',
      app_metadata: { provider: 'google', providers: ['google'] },
      user_metadata: {
        full_name: 'Someone',
        avatar_url: `https://lh3.googleusercontent.com/a/${'p'.repeat(180)}`,
        picture: `https://lh3.googleusercontent.com/a/${'p'.repeat(180)}`,
        sub: '1'.repeat(21),
      },
      identities: [{ id: '1'.repeat(21), provider: 'google', identity_data: {} }],
      created_at: new Date(0).toISOString(),
    },
  };
}

/** Point `fetch` at a token endpoint that returns `body`, recording the calls. */
function stubToken(body: unknown, status = 200) {
  const calls: { url: string; body: Record<string, unknown> | null }[] = [];
  globalThis.fetch = jest.fn(async (url: unknown, init: unknown) => {
    const request = init as { body?: string };
    const parsed = request?.body ? JSON.parse(request.body) : null;
    calls.push({ url: String(url), body: parsed });
    return {
      ok: status < 400,
      status,
      headers: { get: () => 'application/json' },
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  }) as unknown as typeof fetch;
  return calls;
}

/** Start a PKCE flow so a verifier is in the keystore, as sign-in would. */
async function startFlow() {
  stubToken({});
  const { data, error } = await supabase.auth.signInWithOAuth({
    provider: 'google',
    options: { redirectTo: OAUTH_REDIRECT_URI, skipBrowserRedirect: true },
  });
  expect(error).toBeNull();
  return data;
}

// Supabase starts a refresh ticker on initialise; without this Jest reports
// the suite as leaking a handle.
afterAll(async () => {
  await supabase.auth.stopAutoRefresh();
});

beforeEach(async () => {
  store.__reset();
  await supabase.auth.signOut().catch(() => undefined);
  store.__reset();
});

describe('isAuthCallbackUrl', () => {
  // The redirect's shape depends on the build, so matching against
  // OAUTH_REDIRECT_URI alone would only ever catch one of these.
  it.each([
    ['shelf://auth-callback?code=abc', true, 'standalone / dev build'],
    ['shelf:///auth-callback?code=abc', true, 'triple-slashed'],
    ['exp://127.0.0.1:8081/--/auth-callback?code=abc', true, 'Expo Go'],
    ['shelf://auth-callback#access_token=x', true, 'implicit-flow fragment'],
    ['shelf://auth-callback/', true, 'trailing slash'],
    ['shelf://auth-callback', true, 'no parameters'],
    ['shelf://today', false, 'a real route'],
    ['shelf://', false, 'bare scheme'],
    ['exp://127.0.0.1:8081/--/today', false, 'Expo Go, real route'],
  ])('%s → %s (%s)', (url, expected) => {
    expect(isAuthCallbackUrl(url as string)).toBe(expected);
  });

  it('agrees with the redirect URI the OAuth request actually advertises', () => {
    expect(OAUTH_REDIRECT_URI).toContain(AUTH_CALLBACK_PATH);
    expect(isAuthCallbackUrl(OAUTH_REDIRECT_URI)).toBe(true);
  });
});

describe('completeAuthRedirect', () => {
  it('exchanges the code with the verifier it stored, and keeps the session', async () => {
    await startFlow();
    const stored = store.__keys().find((k) => k.endsWith('-code-verifier'));
    expect(stored).toBeDefined();
    const verifier = JSON.parse((await secureStorage.getItem(stored as string)) as string);

    const granted = session();
    const calls = stubToken(granted);
    await completeAuthRedirect('shelf://auth-callback?code=THE_CODE');

    const exchange = calls.find((c) => c.url.includes('grant_type=pkce'));
    expect(exchange).toBeDefined();
    expect(exchange?.body?.auth_code).toBe('THE_CODE');
    // The verifier is stored with an optional `/redirectType` suffix.
    expect(verifier.startsWith(String(exchange?.body?.code_verifier))).toBe(true);

    const { data } = await supabase.auth.getSession();
    expect(data.session?.access_token).toBe(granted.access_token);
    expect(data.session?.user.id).toBe(granted.user.id);
  });

  it('round-trips a realistic session through the chunked keystore', async () => {
    await startFlow();
    stubToken(session());
    await completeAuthRedirect('shelf://auth-callback?code=THE_CODE');

    const raw = (await secureStorage.getItem('shelf-auth')) as string;
    expect(raw).toBeTruthy();
    // If this ever fails, the session shrank and D18's chunking is dead code.
    expect(raw.length).toBeGreaterThan(store.KEYSTORE_VALUE_LIMIT);
    expect(JSON.parse(store.__raw('shelf-auth') as string).chunks).toBeGreaterThan(1);
  });

  it('rejects a second exchange of the same code', async () => {
    await startFlow();
    stubToken(session());
    await completeAuthRedirect('shelf://auth-callback?code=THE_CODE');

    // This is why the callback must not also be a route: two exchangers race
    // for a single-use code and the winner deletes the verifier.
    await expect(
      completeAuthRedirect('shelf://auth-callback?code=THE_CODE'),
    ).rejects.toThrow();
  });

  it('surfaces an error redirect rather than doing nothing', async () => {
    await expect(
      completeAuthRedirect('shelf://auth-callback?error_description=access_denied'),
    ).rejects.toThrow(/access_denied/);
  });

  it('rejects a redirect carrying neither a code nor tokens', async () => {
    await expect(completeAuthRedirect('shelf://auth-callback')).rejects.toThrow(
      /without a session/,
    );
  });
});
