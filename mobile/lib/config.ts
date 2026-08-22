/**
 * Environment-derived configuration.
 *
 * Expo inlines `EXPO_PUBLIC_*` at build time, so these are read as literal
 * property accesses — `process.env[name]` does not work.
 */

function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(
      `${name} is not set. Copy mobile/.env.example to mobile/.env and fill it in.`,
    );
  }
  return value;
}

/** Shelf API base, without a trailing slash. */
export const API_BASE_URL = required(
  'EXPO_PUBLIC_API_BASE_URL',
  process.env.EXPO_PUBLIC_API_BASE_URL,
).replace(/\/+$/, '');

export const SUPABASE_URL = required(
  'EXPO_PUBLIC_SUPABASE_URL',
  process.env.EXPO_PUBLIC_SUPABASE_URL,
);

export const SUPABASE_ANON_KEY = required(
  'EXPO_PUBLIC_SUPABASE_ANON_KEY',
  process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY,
);

/** URI scheme from app.json; the OAuth redirect comes back on it. */
export const APP_SCHEME = 'shelf';
