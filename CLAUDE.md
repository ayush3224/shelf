# CLAUDE.md

Context for Claude Code. Read this before doing anything in this repo.

## What this is

A personal voice-first capture app. The user speaks; the system parses,
classifies, stores, and pushes things back at the right time. Single user
(the owner). Not a product, not multi-tenant.

Working name: **Shelf** — rename freely, it's a placeholder.

## The one idea that matters

**The user never sets a state. Behaviour sets it.**

Four states: `active`, `shelved`, `done`, `dropped`.

- Captured with a time → `active`. Captured without → `shelved`.
- Ignored or snoozed N times → auto-`shelved` (decay).
- `shelved` and untouched for M days → auto-`dropped`.
- Reactivated by hand from the shelf → back to `active` (UC20).
- Only `done` is ever set explicitly, by one tap or one word.

Automatic transitions used to be announced. **UC22 was dropped on 23 August
2026** (owner's decision), so decay is now silent: items shelve and drop
without saying so. The weekly digest (UC31) becomes the only place it is
visible, which makes that feature load-bearing rather than a nicety. If
silent decay turns out to feel like things vanishing, reversing it means
reviving UC22 and restoring this line.

If a proposed feature requires the user to do admin work to keep state
accurate, it is the wrong feature. Push back on it.

## Design constraints (do not violate without asking)

1. **Capture must be ≤2 taps.** Anything that adds friction to capture
   loses more than it gains.
2. **The app opens to the capture screen.** Never to a list.
3. **`Today` must be finishable.** It shows due + overdue only. If it
   becomes a wall, the design has failed.
4. **Raw audio is never discarded** until the item is deleted. If
   transcription or parsing fails, keep the audio and flag the item —
   never lose the capture.
5. **Silence is signal.** Repeated non-response is a decision; act on it.

## Stack

| Layer      | Choice |
|------------|--------|
| Mobile     | Expo / React Native, TypeScript |
| Native     | None yet. The full-screen alarm (UC24) and widget (UC2) are deferred pending real usage |
| Backend    | FastAPI (Python) on a VPS, behind Caddy |
| DB / Auth / Storage | Supabase (Postgres, Auth, Storage for audio) |
| LLM        | Claude `claude-haiku-4-5` via the Anthropic API |
| Push       | `expo-notifications` → Expo push service → FCM (UC23) |
| STT        | Whisper (`whisper-large-v3-turbo`) on Groq, free tier. On-device `SpeechRecognizer` was dropped — see D20 |
| Scheduler  | `shelf-tick.timer` — systemd, 1-minute tick on the VPS (D36) |
| Calendar   | Google Calendar API (OAuth) — session 5 |
| Voice call | CallMeBot — unscheduled (UC26, P2) |

## Cost rules (hard)

These are non-negotiable; they're why the monthly bill stays under $1.

- **Haiku only.** The parse is classification + extraction. Never route it
  to Sonnet or Opus.
- **`max_tokens` capped at 200** on the parse call. Output is 5x input.
- **Never send table rows to the model.** Decay, digests, `Today`,
  overdue, counts, search — all SQL. (This rule used to carve out
  natural-language queries; UC35 was dropped, so there is no longer any
  path that puts rows in front of the model at all.)
- **No prompt caching.** Captures are sporadic; the 5-min cache would be
  cold on most calls and you'd pay the write premium for nothing.
- **Batch API for the weekly digest.** It can wait; it's 50% off.

## Conventions

- Python: `ruff` + `black`, type hints on all public functions.
- TS: strict mode on, no `any`.
- Migrations: numbered SQL files in `/migrations`, never edit an applied one.
- Secrets in `.env`, never committed. `.env.example` stays current.
- Commits: conventional commits (`feat:`, `fix:`, `chore:`).
- **Commit directly to `main`.** This is deliberate, not laziness: one
  committer, no review to wait for, and a PR that only ever merges itself is
  ceremony. Commit per coherent change, not per file. Revisit if a second
  person ever commits.

## Where things are

- `PLAN.md` — build order as five sessions. Start here.
- `docs/use-cases.md` — every use case with its ID, priority and status.
  Dropped ones are struck through and kept; the IDs are never reused.
- `docs/data-model.md` — schema, states, transition rules.
- `docs/architecture.md` — components, data flow, external services.
- `docs/decisions.md` — decisions made, and open questions.

## Working agreement

- Reference use cases by ID (UC1, UC18) in commits.
- If a use case is ambiguous, ask before implementing an interpretation.
- Follow the session order in `PLAN.md`. It already puts the remaining P0
  work (session 2) ahead of the P1 sessions, so "no P1 before P0 is done"
  is the same rule stated once instead of twice.
- Update `docs/decisions.md` when a real decision gets made.
- After completing work in a session, append an entry to
  docs/build-log.md and update its Current state table.
