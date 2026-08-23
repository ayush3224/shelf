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
| Schema (`shelf`, 7 tables + view) | ✅ live, migrations 001–003 applied |
| Audio bucket (`shelf-audio`, private) | ✅ created, unused |
| API at `https://srv1531684.hstgr.cloud/api` | ✅ live on 443 |
| systemd service, survives reboot | ✅ verified |
| Postgres closed to the internet | ✅ verified externally |
| JWT auth, fail-closed | ✅ `/health` is the only public path |
| Text capture + Haiku parse | ✅ kind, due date, critical, entities |
| Timezone handling (IST) | ✅ tested — "tomorrow 3pm" → 09:30Z |
| `GET /items/today`, `POST /items/{id}/done` | ⚠️ built, needs service restart |
| Expo app: capture, today, sign-in | ⚠️ built, never run on a phone |
| Voice capture, hold-to-record (UC1) | ⚠️ API verified live; UI never run on a phone |
| Audio stored + signed playback (UC7) | ✅ verified live, byte-identical |
| Cloud transcription (UC8) | ✅ verified live — Groq turbo, 1.6s round trip |
| Multi-item splitting (UC4) | ⚠️ built, never run against real Haiku |
| Mobile test suite (jest-expo) | ✅ 55 tests |
| Backend test suite | ✅ 164 tests |
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
| ~~O4~~ | ~~Hinglish transcription quality~~ | **Closed 23 Aug 2026** — English only, so the premise is gone (D23) |

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

### 23 August 2026 — voice capture, retained audio, splitting

Three use cases, one decision that shaped all of them.

**The STT decision (D20).** `SpeechRecognizer` transcribes live from the
microphone and never produces a file. UC7 and UC42 both require the file,
so a recorder has to run regardless — and the microphone is effectively
single-consumer, so running both is a fight. The one module that does
both, `expo-speech-recognition`, is published against SDK 56 while this
app is on 57: exactly the out-of-matrix native module that broke the EAS
builds last session. So the file is recorded with `expo-audio` and the
server transcribes it. That is the escape hatch that was offered, and it
was the right one.

Two things worth being blunt about. The cost is ~$0.90/month at 20
captures a day — most of the stated budget, and more than the Haiku
parse. And **O4 becomes unmeasurable as written**: with no on-device
path there is no on-device failure rate to compare against.
`transcript_source` (`on_device` / `cloud` / `none`) is written on every
row anyway so the comparison works the day a second path exists, and
`transcript_confidence` answers the half of O4 that is still live — how
good the cloud transcript is on code-switched speech.

*(Later the same day: O4 was closed outright. See the next entry.)*

**Ordering is the design.** `POST /capture/audio` stores the recording
*first*, then writes the row, then transcribes, then parses. A failed
upload is a 503, deliberately (D21): the file is still on the device at
that moment, and "saved" would be a lie. Everything after the upload
degrades instead — a failed transcription keeps the audio and no words, a
failed parse keeps the audio and the words. If the row will not write,
the orphaned object is deleted, because an object no row points at is
only a bill.

**Splitting (UC4).** `split` joins the parse contract; only when it is
set does a second Haiku call run, and only that call gets past the
200-token cap (D19, capped at 600 / 10 items). The first item reuses the
row already written before the model call; siblings are inserted
alongside it sharing one `audio_path`, which is the only thing grouping
them. Every sibling keeps the *whole* transcript — UC38 edits against
what was said and UC34 searches it, and a fragment the user never spoke
serves neither. A failed split degrades to the single item already
parsed. It applies to typed captures too; nothing about it is voice-only.

`needs_review` finally has the use D13 predicted for it (D22): a
low-confidence transcript is parsed normally, then flagged.

**Two bugs found by writing the tests, both real:**

- `extension_for` ran `mimetypes.guess_extension` on any content type,
  and that maps `application/octet-stream` to `.bin` — so a non-audio
  upload was stored as an unplayable blob under a name claiming nothing
  was wrong, in the function whose whole job was refusing that. The
  filename is now consulted first and `mimetypes` only for `audio/*`.
- `app/+native-intent.tsx` raced its exchange against a `setTimeout` it
  never cleared, so the winner left a live 15-second timer holding its
  closure. Jest's open-handle detector caught it. Now cleared in a
  `finally`.

**Test infrastructure.** jest-expo, and the two auth harnesses from last
session are now real tests rather than scratchpad scripts — plus new
coverage for the multipart upload, whose classic failure (setting
`Content-Type` by hand, so the boundary in the header is not the one in
the body) is silent and server-side. `expo-asset` had to be declared
directly: `expo-audio` needs it as a direct peer, the same floating-peer
class as last session, and `expo-doctor` is what catches it.

**Scope note.** UC7 and UC4 are P1/Phase 3 and were built while P0 still
has UC38 (edit) and UC39 (delete) open, against the working agreement in
`CLAUDE.md`. Built as asked; flagging it because UC38 is what a
`needs_review` flag is supposed to send you to, and it does not exist
yet — the flag currently points nowhere.

**Verified:** 126 backend tests, 29 mobile tests, `ruff`/`black` clean,
`tsc --noEmit` clean, `expo-doctor` 21/21, `expo install --check` up to
date.

**Not verified:** anything on a device. Migration 003 has not been run,
the `shelf-audio` bucket does not exist yet, and `WHISPER_API_KEY` is
unset — so the voice path has never completed once end to end.

### 23 August 2026 (later) — English only, migration 003, and a bucket that would have rejected every capture

**O4 closed.** Not answered — the premise is gone. The user speaks only
English, so there is no code-switching to transcribe and no Hinglish
quality to measure. `language=en` now goes on every Whisper call (D23):
Whisper's detection is a guess from the first seconds of audio, and a
wrong guess does not error, it returns fluent nonsense in the language it
picked. The confidence floor went 0.55 → 0.75 with it — the low value
existed to avoid flagging every code-switched capture, and with one known
language a score under 0.75 now means bad audio, which is what
`needs_review` should have been catching all along.

Stale O4 references cleared out of the migration comment, `db.py`,
`main.py` and two test files, so the column comments no longer claim to
feed a question nobody is asking.

**Migration 003 applied.** Checked first: 001 and 002 were on, 003 was
not, and `shelf.items` held 2 rows. Applied inside a transaction because
`create type` is not idempotent and a partial apply would leave the file
un-rerunnable. Verified after: enum `('on_device','cloud','none')`,
`transcript_source not null default 'none'`, `transcript_confidence`
float4 nullable, partial index `items_audio_idx`, and both existing rows
backfilled to `'none'` — which is the honest value for captures that were
typed.

**The bucket already existed, and it would have rejected every voice
capture.** `shelf-audio` was created 22 Aug: private, 10MB limit,
`allowed_mime_types` of `audio/mp4, audio/mpeg, audio/ogg, audio/webm,
audio/wav, audio/aac`. Nothing was duplicated.

But the recorder reports Android's `.m4a` recordings as `audio/m4a`,
which is not a registered type and is not on that list. Probed it
directly rather than reasoning about it:

```
audio/m4a    -> 400 {"statusCode":"415","error":"invalid_mime_type"}
audio/x-m4a  -> 400 invalid_mime_type
audio/mp4    -> 200
audio/3gpp   -> 400 invalid_mime_type
```

So the very first real capture would have come back 503 "Could not save
the recording" — with the file correctly still on the device, which is at
least the failure mode D21 intended, but for entirely the wrong reason.

Fixed by making the *extension* the single source of truth: the resolved
extension picks the MIME type the upload declares, so a client can report
whichever spelling its platform prefers and the object still lands as
`audio/mp4`. The `mimetypes` fallback is gone with it — it resolved
`audio/3gpp` happily, which only moved the refusal from a clear 400 to an
opaque storage error. `MAX_AUDIO_BYTES` dropped 25MB → 10MB to match the
bucket's own limit, for the same reason: a server guard the store
overrules turns "too long" into something the user cannot act on.

**Verified end to end against the live bucket**, through `storage.py`
rather than around it: upload declaring `audio/m4a` → stored as
`audio/mp4`, signed URL fetched, 88 bytes back byte-identical, `audio/3gpp`
refused at the edge, object deleted, bucket confirmed empty afterwards.

137 backend tests, 29 mobile tests, lint and typecheck clean.

**Still blocking a real capture:** `WHISPER_API_KEY` is unset, so
transcription would fail — a capture would be stored with its audio and
no words, which is UC42 working as designed but is not a working feature.

### 23 August 2026 (later still) — transcription moved to Groq

It was pointed at OpenAI. The module was written provider-agnostic
against the OpenAI-compatible `/audio/transcriptions` shape, but the
defaults were `api.openai.com/v1` and `whisper-1`, so as configured it
was OpenAI. Now Groq (D24).

Checked the endpoint rather than assuming it: base
`https://api.groq.com/openai/v1`, `language` accepted as ISO-639-1, and —
the part that actually mattered — `verbose_json` really does return
`segments` with `avg_logprob` and `no_speech_prob`. That is what the
confidence score is computed from, so if Groq had not returned segments
the whole `needs_review` path would have quietly become dead code. It
does, so the scoring is unchanged.

**Model: `whisper-large-v3`, not turbo.** Turbo is the one that makes the
"10x cheaper" figure true ($0.04/hr against OpenAI's $0.36); large-v3 is
$0.111/hr, about 3x. Turbo is a distilled variant and slightly less
accurate, and in this system the transcript *is* the captured thought —
there is no second chance to hear it. The gap between the two is about
$0.06 a month. `GROQ_MODEL=whisper-large-v3-turbo` switches it if that
trade ever looks different.

**Groq bills a 10-second minimum per request.** Most captures are
shorter, so per-capture cost does not fall below 10 seconds however brief
the recording. The ~$0.09/month estimate already assumes it; the earlier
$0.90 OpenAI figure did not need to, since OpenAI bills by the second.

Env vars renamed `WHISPER_*` → `GROQ_*` so the key says whose it is. The
live `.env` had no `WHISPER_*` lines to migrate — the key was never set —
so the block was added fresh. `CLAUDE.md`'s stack table said "Android
on-device SpeechRecognizer primary; cloud Whisper fallback", which has
been wrong since D20; corrected to name Groq and point at D20.

One robustness fix found while wiring it: the transcriber was being
handed the client's reported content type and a filename built by slicing
the last four characters off the storage key. Both are now taken from the
resolved `StoredAudio` — the extension is what tells the transcriber how
to decode the audio, `audio/m4a` is not a registered type, and the slice
gets `.webm` wrong.

140 backend tests, lint clean. **Not verified against Groq**: no
`GROQ_API_KEY` is set, so no real transcription has run.

### 23 August 2026 (last) — turbo, and retrying a 429

Most of this had already landed in the previous entry — Groq, `language=en`,
`GROQ_API_KEY`. Two things actually changed.

**Model is now `whisper-large-v3-turbo`.** The earlier entry picked
large-v3 on the reasoning that the transcript *is* the captured thought
and $0.06/month was not worth trading accuracy for. That reasoning was
built on the wrong premise: the free tier covers this volume outright, so
neither model costs anything. With cost off the table the choice is
latency, and turbo is the faster one. `GROQ_MODEL=whisper-large-v3`
switches back if transcripts disappoint.

**A 429 is now retried rather than surrendered (D25).** Worth being
precise about what this changes: a failed transcription never failed the
capture — the row and the audio are written before the transcriber is
called, so the capture survived as `parse_status = 'failed'` with no
words (UC42). The safety net was already there. What was missing was any
attempt to avoid needing it.

Three attempts, full-jitter backoff from 1s, honouring `retry-after` when
the server sends a sane one. Retried: 429, 408, 5xx, dropped connections.
Not retried: 400/401/413/415 — they will say the same thing twice — and a
200 carrying non-JSON, which is a broken host rather than a busy one.

The part that needed care is the deadline. Three attempts at a 60s
per-request timeout is 180s, and the app gives up at 90s
(`mobile/lib/api.ts`). Retrying past that converts a recoverable failure
into one the app has already stopped waiting for: the audio would still
be safe, but the user would be told the server was unreachable rather
than that the words did not come. So the whole call is bounded at 75s,
each attempt gets whatever is left of it, and a retry is only taken if
enough budget remains for the attempt to finish.

That last clause came out of a failing test. The first version checked
only `wait < remaining`, which would sleep 0.9s of a 1.0s budget and then
fire a request that could not possibly complete — buying nothing and
making the caller wait for it. There is now a 2s reserve.

**The two Groq limits, neither of which binds here.** The 10-second
minimum per request means a 3-second capture bills as 10; irrelevant
while the free tier covers the volume, and it would only matter on a paid
tier with very short, very frequent captures. The 25MB free-tier upload
cap sits above `MAX_AUDIO_BYTES` (10MB, matched to the storage bucket)
and well above what `MAX_RECORDING_MS` can produce — two minutes of AAC
is under 2MB. The bucket is the binding limit, not Groq. Both are now
recorded next to the constant rather than only in a decision.

164 backend tests, 29 mobile tests, lint and typecheck clean.

**Still not verified against Groq:** no `GROQ_API_KEY` is set, so the
retry path has been exercised against stubs and never against a real 429.

### 23 August 2026 — first real capture, end to end

`GROQ_API_KEY` landed, so the voice path ran for real for the first time.

**Migration 003 and the bucket were already done** earlier today and were
re-verified rather than re-applied — 003 is not idempotent (`create type`
has no `if not exists`), so re-running it would have errored rather than
no-opped. Columns, enum and partial index all present; `shelf-audio`
private, 10MB cap, created 22 Aug in Studio. Nothing duplicated.

Confirmed the bucket accepts what the app actually sends: the recorder
reports `audio/m4a`, the server canonicalises to `audio/mp4`, and only
the latter is on the bucket's allow-list — the raw spelling is still
rejected, which is exactly why that normalisation exists. No format the
API accepts would be refused by the bucket, and the server's 10MB cap
equals the bucket's, so the server refuses first and the error stays
legible.

**The recording was real**, not a stub blob: `espeak-ng` synthesised the
phrase and `ffmpeg` encoded it to mono 44.1kHz AAC in an MPEG-4
container — the same shape `expo-audio`'s HIGH_QUALITY preset produces on
Android. 4.68s, 83,707 bytes. (Both tools were installed on the VPS for
this; neither is a runtime dependency.)

Spoken:  *"Remind me to call the insurance people tomorrow at three in
the afternoon."*
Groq returned: *"Remind me to call the insurance people tomorrow at 3 in
the afternoon."*

Only "three" → "3", which is Whisper's own number normalisation. Turbo
was accurate enough that the accuracy worry behind the original
large-v3 choice looks unfounded on clean English.

The rest of the chain, all in **1.57 seconds** through Caddy:

- object in the bucket, 83,707 bytes, `mimetype=audio/mp4`
- row written: `kind=task`, `state=active`, `parse_status=ok`,
  `source=voice`, `parsed_text='Call insurance company'`
- `due_at = 2026-08-24 09:30 UTC` — tomorrow 15:00 IST. D15's timezone
  handling holding all the way through a live capture, which is the bug
  that would otherwise have made every reminder 5.5 hours wrong
- `transcript_source='cloud'`, `transcript_confidence=0.874729` — above
  the 0.75 floor, so `parse_status` stayed `ok` rather than
  `needs_review`, which is D22/D23 behaving as intended
- `GET /items/{id}/audio` signed a URL, the fetch came back 200 with
  `content-type: audio/mp4` and a **byte-identical sha256**; the same
  endpoint without a token returned 401
- no retries and no warnings in the log — the D25 backoff was never
  needed, so it remains stub-tested only

The item correctly did *not* appear on `Today`: it is due tomorrow, and
D17 bounds the list at the end of the user's day. The pre-existing typed
item showed `has_audio: false`.

**Cleaned up:** row deleted, object deleted, `shelf.items` back to its
prior 2 rows, bucket empty at every prefix.

**Still not verified:** the app itself. Every part of this went through
`curl`, so the recorder, the hold-to-record gesture, the permission
prompt and the playback button have still never run on a phone.

### 23 August 2026 — "cannot reach the server" was not about the server

Voice capture failed on the phone with a message about the server, and
nothing arrived at Caddy or in the service log. The message was the
problem: it was a lie, and not an incidental one.

`lib/api.ts` caught every throw from `fetch` with a bare `catch {}` — the
exception discarded, not logged — and reported `No connection.` for all
of them. A malformed body, an unreadable file part, and a flat network
produced identical text. So the reported symptom was never evidence of a
network fault, and it sent the last hour of investigation at the VPS,
which had already been verified end to end the same day.

**Eliminated by inspection, so they can stop being suspects:**

- *A client-side size or MIME guard rejecting the upload.* There is none.
  `captureAudio` has no guards at all; every size and format check is
  server-side.
- *A malformed FormData body.* The `{uri, name, type}` shape matches
  React Native 0.86's own `FormData.getParts()` exactly — a value with a
  `uri` becomes a file part with `content-disposition` and
  `content-type` headers, which is what is being built.
- *A hand-set `Content-Type` breaking the boundary.* Correctly omitted
  for multipart; RN sets it with the generated boundary.
- *`setAudioModeAsync` disturbing the recorder before the URI is read.*
  Android's implementation only assigns `useForegroundService` on each
  recorder. It does not touch `filePath`, so the ordering in `finish()`
  is safe. (Separately: Android's `AudioMode` has no `allowsRecording`
  field at all — that one is iOS-only and is a no-op here.)
- *The server rejecting it.* A real `.m4a` went through `POST
  /capture/audio` earlier the same day and completed in 1.57s.

**Not eliminated, and not confirmable from here:** the file at
`recorder.uri` not being readable when `fetch` reaches for it. React
Native surfaces that as `TypeError: Network request failed` — the same
string a flat network gives — because the native networking module
raises both through one channel. There is no way to tell them apart after
the fact, which is why the fix does not try to.

One concrete finding feeding that hypothesis: expo-audio's Android
recorder exposes `uri` as `filePath?.let { ... } ?: ""` — an **empty
string, never null** — and `filePath` is only assigned inside
`setRecordingOptions`, which returns early without assigning it when
`hasRecordingPermissions()` is false at prepare time. So there is a
silent path to an empty URI with no exception raised anywhere.

**What changed:**

- `ApiError` now carries `kind` (`http` | `timeout` | `transport` |
  `client`), the original `cause`, and a one-line `diagnostic`. `client`
  means the request never left the device — the distinction the old code
  could not express.
- The real exception is logged with a `[shelf/api]` prefix, greppable in
  `adb logcat`. It is no longer discarded.
- The screen says which machine has the problem. A local failure now
  reads "The app could not send that recording — it is still here",
  followed by the actual exception, rather than anything about servers.
- A **preflight**: the recording's file is checked for existence and
  non-zero size *before* it is handed to `fetch`, so the ambiguous case
  is caught while it is still legible. An empty URI is named as a
  permission problem specifically.
- `stop()` returns a discriminated `StopResult` — `recording` /
  `too-short` / `unusable` — instead of `Recording | null`. A null could
  not distinguish "you tapped instead of holding" from "there is no file
  on disk", and the screen was showing the former for both.
- The decision lives in a pure `classifyRecording()`, testable without a
  renderer. `react-test-renderer` wants React `^19.2.8` against the SDK
  57 pin of `19.2.3`, and bumping React to test a hook is not a trade
  worth making.
- Duration is checked before the file, deliberately: a sub-100ms press
  can leave a zero-byte file legitimately, and "the recording is empty"
  is worse advice than "hold the button" for a mis-tap.

**The text path is unchanged** apart from the `request()` refactor, where
`multipart` defaults falsy and `application/json` is still set — pinned
by a test. If text capture also fails on this build, the problem is all
outbound requests rather than the upload path, and that is worth knowing
either way.

**Latent, not hit today:** `mimeFor` can return `audio/3gpp`, which the
server refuses because the bucket cannot store it. Unreachable while the
recorder uses the HIGH_QUALITY preset (`.m4a`); switching to LOW_QUALITY
would fail every capture with a 400.

55 mobile tests (26 new), 164 backend tests, lint and typecheck clean.
**No root cause yet** — this makes the next failure name itself instead
of blaming the server.
