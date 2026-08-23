/**
 * `app/+native-intent.tsx` — what it swallows, and when it exchanges.
 *
 * On Android `openAuthSessionAsync` intercepts nothing: it polyfills over a
 * plain `Linking` listener that expo-router subscribes to as well. This file
 * is the only thing stopping the router treating the OAuth callback as a
 * route, so its behaviour is worth pinning down.
 */
import * as SecureStore from 'expo-secure-store';

import { redirectSystemPath } from '../app/+native-intent';
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

const store = SecureStore as unknown as { __reset(): void };

let exchanges = 0;
let hang = false;

function stubNetwork() {
  globalThis.fetch = jest.fn(async (url: unknown) => {
    if (String(url).includes('grant_type=pkce')) {
      exchanges += 1;
      if (hang) return new Promise(() => {}); // never resolves
      const granted = {
        access_token: 'tok',
        refresh_token: 'ref',
        token_type: 'bearer',
        expires_in: 3600,
        expires_at: Math.floor(Date.now() / 1000) + 3600,
        user: {
          id: 'u1',
          aud: 'authenticated',
          role: 'authenticated',
          app_metadata: {},
          user_metadata: {},
          created_at: new Date(0).toISOString(),
        },
      };
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => granted,
        text: async () => JSON.stringify(granted),
      };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({}),
      text: async () => '{}',
    };
  }) as unknown as typeof fetch;
}

/** Seed a PKCE verifier, as a sign-in would before the process was killed. */
async function seedVerifier() {
  await supabase.auth.signInWithOAuth({
    provider: 'google',
    options: { redirectTo: 'shelf://auth-callback', skipBrowserRedirect: true },
  });
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
  exchanges = 0;
  hang = false;
  stubNetwork();
});

describe('non-auth deep links', () => {
  it.each(['shelf://today', 'exp://127.0.0.1:8081/--/today', 'shelf://'])(
    'passes %s through to the router unchanged',
    async (path: string) => {
      await expect(redirectSystemPath({ path, initial: false })).resolves.toBe(path);
    },
  );
});

describe('a warm redirect', () => {
  it('is swallowed so the router never shows "Unmatched route"', async () => {
    await expect(
      redirectSystemPath({ path: 'shelf://auth-callback?code=WARM', initial: false }),
    ).resolves.toBeNull();
  });

  it('does not touch the code — signInWithGoogle owns that exchange', async () => {
    await redirectSystemPath({ path: 'shelf://auth-callback?code=WARM', initial: false });
    expect(exchanges).toBe(0);
  });
});

describe('a cold start', () => {
  // Android killed the process behind the Custom Tab, so the promise and the
  // listener that would have handled this are gone. Nothing else can do it.
  it('exchanges exactly once and leaves a live session', async () => {
    await seedVerifier();
    exchanges = 0;

    await expect(
      redirectSystemPath({ path: 'shelf://auth-callback?code=COLD', initial: true }),
    ).resolves.toBeNull();
    expect(exchanges).toBe(1);

    const { data } = await supabase.auth.getSession();
    expect(data.session?.access_token).toBe('tok');
  });

  it('returns null rather than throwing when the exchange fails', async () => {
    // expo-router's docs are explicit that throwing here can crash the app.
    await expect(
      redirectSystemPath({
        path: 'shelf://auth-callback?error_description=denied',
        initial: true,
      }),
    ).resolves.toBeNull();
  });

  it('gives up on a hung network instead of stalling the splash', async () => {
    jest.useFakeTimers();
    try {
      await seedVerifier();
      hang = true;

      const pending = redirectSystemPath({
        path: 'shelf://auth-callback?code=HUNG',
        initial: true,
      });
      // Nothing resolves this but the handler's own 15s cap.
      await jest.advanceTimersByTimeAsync(20_000);
      await expect(pending).resolves.toBeNull();
    } finally {
      jest.useRealTimers();
    }
  });
});
