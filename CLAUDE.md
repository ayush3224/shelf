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
- Mentioned again in a new note → back to `active` (reactivation).
- Only `done` is ever set explicitly, by one tap or one word.

Every automatic transition is **announced, never silent**.

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
| Native     | Two thin modules: full-screen-intent alarm, home-screen widget |
| Backend    | FastAPI (Python) on Railway |
| DB / Auth / Storage | Supabase (Postgres, Auth, Storage for audio) |
| LLM        | Claude `claude-haiku-4-5` via the Anthropic API |
| STT        | Android on-device `SpeechRecognizer` primary; cloud Whisper fallback |
| Scheduler  | Railway cron, 1-minute tick |
| Calendar   | Google Calendar API (OAuth) — phase 5 |
| Voice call | CallMeBot — phase 6, optional |

## Cost rules (hard)

These are non-negotiable; they're why the monthly bill stays under $1.

- **Haiku only.** The parse is classification + extraction. Never route it
  to Sonnet or Opus.
- **`max_tokens` capped at 200** on the parse call. Output is 5x input.
- **Never send table rows to the model.** Decay, digests, `Today`,
  overdue, counts — all SQL. For natural-language queries, generate SQL
  with one small call and execute it; do not load rows into context.
- **No prompt caching.** Captures are sporadic; the 5-min cache would be
  cold on most calls and you'd pay the write premium for nothing.
- **Batch API for the weekly digest.** It can wait; it's 50% off.

## Conventions

- Python: `ruff` + `black`, type hints on all public functions.
- TS: strict mode on, no `any`.
- Migrations: numbered SQL files in `/migrations`, never edit an applied one.
- Secrets in `.env`, never committed. `.env.example` stays current.
- Commits: conventional commits (`feat:`, `fix:`, `chore:`).
- One PR per phase milestone, not per file.

## Where things are

- `PLAN.md` — phased build order. Start here.
- `docs/use-cases.md` — all 44 use cases with IDs and priorities.
- `docs/data-model.md` — schema, states, transition rules.
- `docs/architecture.md` — components, data flow, external services.
- `docs/decisions.md` — decisions made, and open questions.

## Working agreement

- Reference use cases by ID (UC1, UC18) in commits and PRs.
- If a use case is ambiguous, ask before implementing an interpretation.
- Don't build P1/P2 work while P0 is incomplete.
- Update `docs/decisions.md` when a real decision gets made.
- After completing work in a session, append an entry to
  docs/build-log.md and update its Current state table.
