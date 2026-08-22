# Shelf — mobile

Expo / React Native (TypeScript). Phase 1 only: capture typed text, and finish
what's due today.

Two screens. `Capture` is the launch screen (D9) and `Today` is one tap away.

| Screen | Use cases |
|--------|-----------|
| Capture | UC5 text entry, UC12 initial state (assigned server-side) |
| Today | UC32 due + overdue, UC16 mark done |
| Sign in | UC41 Google via Supabase |

Not here, on purpose: audio capture (UC1/UC8), the widget, alarms, shelf
browsing, edit and delete. Those are later phases in `PLAN.md`.

## Setup

```bash
cd mobile
npm install
cp .env.example .env      # fill in the three values
npm start
```

`.env` needs:

| Variable | Value |
|----------|-------|
| `EXPO_PUBLIC_API_BASE_URL` | `https://srv1531684.hstgr.cloud/api` |
| `EXPO_PUBLIC_SUPABASE_URL` | same as `SUPABASE_URL` in the root `.env` |
| `EXPO_PUBLIC_SUPABASE_ANON_KEY` | same as `SUPABASE_ANON_KEY` in the root `.env` |

`EXPO_PUBLIC_*` is inlined into the bundle, so nothing secret goes in it. The
anon key is publishable by design; the service key stays on the API.

## Google sign-in, one-time config

Sign-in will fail until all three of these agree on the same URLs.

1. **Google Cloud Console** — OAuth 2.0 client (Web application). Authorised
   redirect URI:
   `https://<your-supabase-host>/auth/v1/callback`
2. **Supabase → Authentication → Providers → Google** — enable it, paste the
   client ID and secret.
3. **Supabase → Authentication → URL Configuration → Redirect URLs** — add both:
   - `shelf://auth-callback` — development and production builds
   - `exp://127.0.0.1:8081/--/auth-callback` — Expo Go, with your LAN IP if the
     device is not the host

The app prints nothing about which one it used; if sign-in returns you to the
app with no session, it is almost always a missing entry in step 3.

## Builds

`expo-secure-store` and the `shelf://` scheme need a development build — Expo
Go falls back to `exp://` URLs and cannot register a custom scheme.

```bash
npx expo run:android          # local dev build
npx expo export --platform android   # bundle check, no device needed
npm run typecheck
```

## Layout

```
app/
  _layout.tsx          session gate: tabs behind a session, sign-in behind none
  sign-in.tsx          UC41
  (tabs)/
    _layout.tsx        Capture first (D9)
    index.tsx          Capture — UC5
    today.tsx          Today — UC32, UC16
lib/
  api.ts               Shelf API client; bearer token on every request
  auth.tsx             session state, Google OAuth hand-off
  supabase.ts          Supabase client — auth only, never reads Postgres
  secureStorage.ts     keystore-backed session storage, chunked
  config.ts            EXPO_PUBLIC_* with a real error when unset
  theme.ts / time.ts
```

The app never talks to Postgres. Every row goes through the API, which is the
one place that owns the schema and the state machine.

## Known edges

- **No offline queue.** A capture that fails to send keeps your text in the box
  and says so. Queue-and-sync is UC6, phase 3.
- **`Today` ends at local midnight.** Something due at 00:20 shows up after the
  day rolls over, not before. Firing at the due moment is the push notification's
  job (UC23, phase 2).
