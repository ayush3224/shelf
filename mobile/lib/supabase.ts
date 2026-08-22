/**
 * The Supabase client (UC41).
 *
 * Auth only. The app never reads Postgres directly — every row goes through
 * the API, which is the one place that owns the schema and the state machine.
 * What this client is for is getting an access token and keeping it fresh.
 */
import 'react-native-url-polyfill/auto';
import { AppState } from 'react-native';
import { createClient } from '@supabase/supabase-js';

import { SUPABASE_ANON_KEY, SUPABASE_URL } from './config';
import { secureStorage } from './secureStorage';

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: {
    storage: secureStorage,
    storageKey: 'shelf-auth',
    persistSession: true,
    autoRefreshToken: true,
    // There is no browser URL to read a session out of; the OAuth redirect is
    // handled by hand in lib/auth.tsx.
    detectSessionInUrl: false,
    flowType: 'pkce',
  },
});

// Supabase's refresh timer is a setInterval, which Android suspends in the
// background. Without this the first request after a long sleep goes out with
// a stale token.
AppState.addEventListener('change', (state) => {
  if (state === 'active') {
    void supabase.auth.startAutoRefresh();
  } else {
    void supabase.auth.stopAutoRefresh();
  }
});
