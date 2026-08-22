/**
 * Session state and Google sign-in (UC41).
 *
 * Single user, but the token still decides everything: the API reads `user_id`
 * from the JWT's `sub` and nothing else (D11), so the only job here is to get
 * a real session and keep it somewhere the OS protects.
 */
import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { makeRedirectUri } from 'expo-auth-session';
import * as WebBrowser from 'expo-web-browser';
import type { Session } from '@supabase/supabase-js';

import { APP_SCHEME } from './config';
import { supabase } from './supabase';

type AuthState = {
  session: Session | null;
  /** True until the stored session has been read back off the device. */
  loading: boolean;
  signingIn: boolean;
  error: string | null;
  signInWithGoogle: () => Promise<void>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

const redirectTo = makeRedirectUri({ scheme: APP_SCHEME, path: 'auth-callback' });

function message(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

/**
 * Turn the URL the browser handed back into a session.
 *
 * PKCE returns `?code=`; a project still configured for the implicit flow
 * returns the tokens in the fragment. Both are handled because which one you
 * get is a server-side setting, and failing on the other is a confusing
 * "sign-in did nothing" rather than an error.
 */
async function sessionFromRedirect(url: string): Promise<void> {
  const parsed = new URL(url);

  const errorDescription =
    parsed.searchParams.get('error_description') ?? parsed.searchParams.get('error');
  if (errorDescription) throw new Error(errorDescription);

  const code = parsed.searchParams.get('code');
  if (code) {
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (error) throw error;
    return;
  }

  const fragment = new URLSearchParams(parsed.hash.replace(/^#/, ''));
  const access_token = fragment.get('access_token');
  const refresh_token = fragment.get('refresh_token');
  if (access_token && refresh_token) {
    const { error } = await supabase.auth.setSession({ access_token, refresh_token });
    if (error) throw error;
    return;
  }

  throw new Error('Google sent us back without a session.');
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [signingIn, setSigningIn] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (active) setSession(data.session);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    const { data } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next);
    });

    return () => {
      active = false;
      data.subscription.unsubscribe();
    };
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      session,
      loading,
      signingIn,
      error,
      async signInWithGoogle() {
        setSigningIn(true);
        setError(null);
        try {
          const { data, error: oauthError } = await supabase.auth.signInWithOAuth({
            provider: 'google',
            options: { redirectTo, skipBrowserRedirect: true },
          });
          if (oauthError) throw oauthError;
          if (!data.url) throw new Error('Supabase returned no authorization URL.');

          const result = await WebBrowser.openAuthSessionAsync(data.url, redirectTo);
          if (result.type === 'cancel' || result.type === 'dismiss') return;
          if (result.type !== 'success') {
            throw new Error('The sign-in window closed unexpectedly.');
          }

          await sessionFromRedirect(result.url);
        } catch (e) {
          setError(message(e, 'Sign-in failed.'));
        } finally {
          setSigningIn(false);
        }
      },
      async signOut() {
        setError(null);
        await supabase.auth.signOut();
      },
    }),
    [session, loading, signingIn, error],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside an AuthProvider');
  return context;
}

/** The redirect URI Supabase must have on its allow-list. Surfaced for setup. */
export const OAUTH_REDIRECT_URI = redirectTo;
