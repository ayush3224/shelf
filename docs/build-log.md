# Shelf — Build Log

A running, semi-technical record of how this project came to exist and
what has actually been built. Written to be readable months later
without reconstructing anything from memory.

**Update this at the end of every working session.** Add a new entry
under "Sessions", and move anything that changed into the "Current
state" table above it. Keep the reasoning, not just the outcome — the
*why* is the part that's expensive to recover.

Last updated: **22 August 2026**

---

## 1. Where the idea came from

The starting problem wasn't "I need a to-do app." It was narrower and
more specific: **checklists get written and never revisited.** Any
system that requires opening a list to find out what's pending fails on
a meeting-heavy day, which is most days.

Two consequences fell out of that immediately:

- **Delivery must be push, not pull.** If it needs you to go look, it's
  already lost.
- **Capture must be near-zero friction.** Speaking beats typing beats
  opening an app and filling a form.

A third constraint came from the surrounding context: too many parallel
threads across work and side projects. So the system needed a way to
distinguish what's genuinely live from what's merely *not deleted* —
without that distinction costing any ongoing effort.

## 2. The design bet

Everything else follows from one idea:

> **The user never sets a state. Behaviour sets it.**

Four states — `active`, `shelved`, `done`, `dropped` — with only `done`
ever set explicitly. The rest move on their own:

| Transition | Trigger |
|---|---|
| → `active` | captured with a time, or re-mentioned later |
| → `shelved` | captured without a time, or ignored N times |
| → `dropped` | shelved and untouched for M days |
| → `done` | one tap or one word — the only manual one |

The claim being tested is that **silence is signal**. Conventional
systems treat an ignored item as pending forever, which is what turns
them into guilt piles. This one reads repeated silence as "not now" and
acts on it, then says so.

Two supporting decisions:

- **The app opens to capture, never to a list.** Opening to a list is
  what makes a tool feel like a chore.
- **`Today` must be finishable.** Due and overdue only. If it becomes a
  wall, the design has failed.

## 3. Scope

44 use cases were enumerated up front (see `use-cases.md`), grouped into
capture, parsing, state transitions, delivery, review, retrieval,
system, and integrations. 16 are P0.

The full list was written before any code, deliberately — not to build
it all, but to know what shape the data model needed so later phases
wouldn't require a rewrite. That's why `entities` and `links` tables
exist from the first migration despite the graph feature being P2.

Two additions came later in discovery: writing to a personal Google
Calendar (UC43, which replaced a read-only mirror), and an
Obsidian-style people/notes graph (UC44).

## 4. Architecture and why

| Layer | Choice | Reasoning |
|---|---|---|
| Mobile | Expo / React Native | Existing React fluency; two thin native modules cover the gaps |
| Backend | FastAPI on the VPS | Colocated with the database |
| Database | Self-hosted Supabase (Postgres 17) | Already running on the VPS from a prior project |
| Auth | Supabase GoTrue, Google provider | Already configured; no password handling |
| Storage | Supabase Storage, private bucket | Same stack, no extra service |
| LLM | `claude-haiku-4-5` | The parse is extraction + classification |
| TLS | Caddy | Already terminating certificates on the box |

**Cost discipline** is a stated constraint, so it's encoded in
`CLAUDE.md` rather than left to judgement: Haiku only, `max_tokens`
capped at 200, and — the big one — **never send table rows to the
model**. Decay, digests, and list queries are SQL. Steady-state cost
lands under $1/month; the real risk is a retry loop, not the rate card.

Prompt caching was explicitly rejected: captures are sporadic, so the
5-minute cache would be cold on most calls and you'd pay the write
premium for nothing.

## 5. Infrastructure story

The hosting decision moved three times, which is worth recording
because the reasoning generalises:

1. **Hosted Supabase** — blocked, free tier exhausted.
2. **Railway Postgres** — planned, and a migration was rewritten to drop
   `auth.users` and RLS in favour of a bearer token.
3. **The Hostinger VPS** — then `docker ps` revealed a *full self-hosted
   Supabase stack already running there* for six weeks. Postgres, auth,
   storage, and TLS all present and paid for. Reverted to the original
   RLS-based migration.

Lesson: inventory what's already running before provisioning anything.

Because `public` on that Postgres already held another project's tables,
Shelf lives in a dedicated **`shelf` schema**. The first attempt ran an
unscoped migration into `public`; it was dropped table-by-table by
explicit name and re-run correctly.

### Networking, the awkward parts

- **The pooler.** Port 5432 on the host is Supavisor, not Postgres, and
  it rejects a plain `postgres` username (`no tenant identifier`). A
  `socat` container on `127.0.0.1:5433` resolves `supabase-db` by name
  on the Docker network — survives restarts and IP changes.
- **Docker bypasses ufw.** Docker writes iptables rules ahead of ufw's
  chain, so `ufw deny 5432` did nothing. The block had to go in
  `DOCKER-USER`, then be persisted with `iptables-persistent`.
- **The odd port didn't work.** The API was first exposed on 8445;
  Caddy was listening and ufw allowed it, but connections failed from
  outside — blocked upstream of the VPS. Abandoned in favour of a
  `handle /api/*` block on **443**, which is better anyway: it works
  from corporate WiFi and mobile networks that filter odd ports.

Backups turned out to already exist — a nightly 02:00 pg dump and a
weekly storage tarball. They live on the same box, though, so an
off-site copy is still worth adding.

## 6. Current state

| Area | Status |
|---|---|
| Schema (`shelf`, 7 tables + view) | ✅ live |
| Audio bucket (`shelf-audio`, private) | ✅ created, unused |
| API at `https://srv1531684.hstgr.cloud/api` | ✅ live on 443 |
| systemd service, survives reboot | ✅ verified |
| Postgres closed to the internet | ✅ verified externally |
| JWT auth, fail-closed | ✅ `/health` is the only public path |
| Text capture + Haiku parse | ✅ kind, due date, critical, entities |
| Timezone handling (IST) | ✅ tested — "tomorrow 3pm" → 09:30Z |
| `GET /items/today`, `POST /items/{id}/done` | ⚠️ built, needs service restart |
| Expo app: capture, today, sign-in | ⚠️ built, never run on a phone |
| Native dep tree vs SDK 57 matrix | ✅ reconciled, `expo-doctor` 21/21 |
| Google OAuth redirect handling | ✅ callback swallowed, not routed |
| Google OAuth config | ❌ not started |
| APK on the phone | ❌ not started |
| Everything Phase 2+ | ❌ not started |

## 7. Pending — immediate

1. Commit the app work; `systemctl restart shelf` to deploy the two new
   routes.
2. Configure Google sign-in across three places that must agree: Google
   Cloud Console, the Supabase Google provider, and the redirect
   allow-list (`shelf://auth-callback`).
3. `eas build:configure`, then
   `eas build --profile preview --platform android`.
4. Install the APK; verify sign-in, a timed capture, and an untimed one.
5. **Use it for two weeks.** This is a gate, not a formality — Phase 1's
   exit criterion is real usage, not passing tests.

## 8. Pending — later phases

- **Phase 2** — notifications, snooze, the decay engine, shelf browsing,
  search, quiet hours.
- **Phase 3** — home-screen widget, full-screen DND-breaking alarm,
  offline capture queue, audio playback, multi-item splitting.
- **Phase 4** — the swipe-deck weekly review, digest via the Batch API.
- **Phase 5** — Google Calendar write (UC43), one-way only.
- **Phase 6** — the people/notes graph (UC44), project inference,
  natural-language query.
- **Phase 7** — lock-screen capture, reminders in a known person's
  voice, voice-call escalation, export.

## 9. Open questions

| # | Question | Current default |
|---|---|---|
| O1 | Ignores before auto-shelve | 3 |
| O2 | Days shelved before auto-drop | 90 |
| O3 | Echo the parse back on capture? | Middle option shipped — state announced, parse not echoed |
| O4 | Hinglish transcription quality | Unmeasured; on-device STT first |

O1 and O2 are answerable from data rather than opinion: the
`transitions` table logs every state change with a reason, so after a
month of use you can query how often decay-shelved items get
resurrected and tune both from evidence.

## 10. Known debt

- No off-site backup copy.
- Committing directly to `main` rather than the per-phase PR flow
  `CLAUDE.md` describes. Fine for a single-committer repo — the doc
  should probably be relaxed to match.
- `parsed_text` is stored but not exposed in `v_items_query`.
- Postgres password was exposed in a chat transcript; rotation
  outstanding.
- An APK built this way doesn't auto-update — every change means a
  rebuild and reinstall.

---

## Sessions

### 22 August 2026 — discovery through first working backend and app

Started from three parked ideas (voice capture, a people-memory system,
an automated crypto trader) and picked the first.

Journey design, then form factor, then the 44 use cases, then the repo
scaffold. Infrastructure moved from hosted Supabase → Railway → the VPS
as facts emerged. Migration written, mis-scoped, cleaned up, re-run.

Backend built in three Claude Code sessions: skeleton and capture, then
a psycopg placeholder fix (asyncpg-style `$1` against a psycopg3 driver
— zero placeholders, six parameters), then auth plus the parse step.
The timezone bug found in that last session was the significant one:
relative dates were resolving in UTC, which would have made every
reminder 5.5 hours wrong and quietly killed the usage test.

Three loose ends closed: systemd unit corrected (`Type=notify` would
have hung, since uvicorn doesn't send readiness notifications), Caddy
moved to `/api` on 443, and Postgres firewalled via `DOCKER-USER`.
Rebooted cleanly — all 14 containers and the service came back unaided.

Expo app built last: capture screen, Today list, Google sign-in, session
chunked across secure-store keys because a Supabase session with two
JWTs exceeds the platform's ~2KB per-value limit.

**Ended:** backend live and healthy, app built but not yet on a phone.

### 23 August 2026 — EAS native dependency reconciliation

EAS builds were failing on what looked like an unsatisfiable native
version triangle: RN 0.86.2 → reanimated ≥4.6 → worklets ≥0.12, against
expo-modules-core 57.0.12 which caps worklets at 0.10.

The middle link was false. React Native declares no dependency or peer
on reanimated at all — the arrow runs the other way. Reanimated 4.6.0
declares `react-native: "0.83 - 0.87"`; it *supports* 0.86, it is not
*required by* it. 4.6.0 is also the first release to move to
`worklets 0.12.x`, targeting the next SDK. Reanimated 4.5.1 — the SDK 57
pin — declares `0.83 - 0.86` and `worklets 0.10.x`, satisfying RN 0.86.2
and expo-modules-core's `^0.10.0` ceiling at once. The triangle was only
unsatisfiable because 4.6.0 was in it.

`expo-modules-core@57.0.12` was never the problem: it is not a direct
dependency, it is pinned exactly by `expo@57.0.15`, and its worklets
range is the SDK 57 native ABI boundary doing its job.

Nothing in `app/` or `lib/` imports reanimated or gesture-handler. They
cannot be dropped, though — `react-native-drawer-layout@4.2.10`, pulled
by `expo-router`, hard-depends on both.

Two real drift points, neither where the reported conflict pointed:

- reanimated declared `4.1.2` against an SDK pin of `4.5.1`. Its peers
  are loose (`react-native: "*"`, `worklets: ">=0.5.0"`) so npm resolved
  it happily against worklets 0.10.4 — a pairing Expo never builds. The
  reanimated↔worklets boundary is native C++; the loose semver range
  does not actually protect it. Installs clean, fails at link time.
- gesture-handler 3.2.1 sat in `node_modules` while absent from
  `package.json` — npm auto-installed it as an optional peer of
  `expo-router` (`*`), so it floated to latest, a major version past the
  `~2.32.0` pin. Same for worklets at 0.10.4 vs 0.10.1.

The floating-peer case is the one worth remembering: `expo install
--check` reads `package.json`, so it flagged only reanimated and stayed
silent on both undeclared packages. Auto-installed peers are invisible
to the official check and re-drift on every `npm install`.

Fix: pinned reanimated to 4.5.1, declared gesture-handler `~2.32.0` and
worklets `0.10.1` explicitly, dropped an empty `overrides: {}` left over
from earlier attempts, and reinstalled from a deleted lockfile.

**Verified:** `npm ls` clean with every package deduped to one copy,
`expo install --check` up to date, `tsc --noEmit` clean, `expo-doctor`
21/21. Not yet through an actual EAS build.

### 23 August 2026 — the OAuth redirect was never a route

Google sign-in completed and the app landed on expo-router's "Unmatched
route". The framing was either/or — is `openAuthSessionAsync` failing to
intercept the redirect, or is Android also delivering it to the router?
It is neither, because on Android there is no interception to fail.

`expo-web-browser` has no native auth-session implementation on Android:
`_authSessionIsNativelySupported()` is literally `Platform.OS !==
'android'`. It falls back to a polyfill whose entire redirect mechanism
is `Linking.addEventListener('url', ...)`. `expo-router` subscribes to
that same event in `build/link/linking.js`. The redirect arrives as an
ordinary Android intent on the `shelf` scheme and *both* subscribers see
it — no consumption, no priority, no interception anywhere.

So both things happen at once. The exchange runs and succeeds, and the
router independently tries to navigate to `/auth-callback`, finds no
route, and paints "Unmatched route" over a sign-in that worked.

Fixed with `app/+native-intent.tsx`, whose `redirectSystemPath` returns
`null` for the callback URL — expo-router documents a falsy return as
"stay on the current path". Chose that over adding an
`app/auth-callback.tsx` route for two reasons. First, a route would be a
second exchange racing the one in `signInWithGoogle` for a single-use
code; `_exchangeCodeForSession` calls `removePKCEVerifier` on success, so
the loser fails with a missing-verifier error and reports a working
sign-in as broken. Verified: the second exchange of the same code
rejects. Second, nothing needs to navigate to `(tabs)` by hand —
`_saveSession` fires `SIGNED_IN`, which flips the `Stack.Protected`
guards in `_layout.tsx`. A route would duplicate the exchange and fight
the guard to render a screen no one ever sees.

Cold start needed handling too, and is the one case the old code could
not have survived: if Android kills the process behind the Custom Tab,
the `openAuthSessionAsync` promise and its listener die with it and the
redirect arrives as the app's *initial* URL. The verifier is in the
keystore, so `redirectSystemPath` does the exchange itself when
`initial` is true — bounded at 15s, because expo-router awaits this
before rendering and RN's `fetch` has no timeout of its own.

`isAuthCallbackUrl` matches on the last path segment rather than against
`redirectTo`, because the redirect shape varies by build
(`shelf://auth-callback`, `shelf:///auth-callback`,
`exp://127.0.0.1:8081/--/auth-callback`). Worth knowing: for a custom
scheme the WHATWG parser puts `auth-callback` in `host`, leaving
`pathname` empty — a pathname-based match silently matches nothing.

**Verified** without a device, by running the real `@supabase/auth-js`
and the real chunked keystore adapter with only `expo-secure-store` and
`fetch` faked: the verifier is written and read back through the chunked
adapter, the exchange POSTs the code from the redirect URL with that
verifier, `getSession()` returns the session afterwards, and a realistic
Google session (2590B) round-trips across 2 chunks — over the ~2KB
keystore limit, so the chunking is genuinely load-bearing. Separately:
non-auth links pass through untouched, a warm redirect is swallowed
without exchanging, a cold start exchanges exactly once, and neither a
rejected exchange nor a hung network can throw or stall the launch.

Still unproven on hardware: that Android delivers the intent at all,
which depends on the generated intent-filter. The harnesses live in the
session scratchpad, not the repo — no test infrastructure exists for
`mobile/` yet and adding jest-expo was out of scope for a bug fix.
