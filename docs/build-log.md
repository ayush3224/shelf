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
