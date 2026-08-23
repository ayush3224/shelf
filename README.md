# Shelf

Personal voice-first capture. Speak it, and the system decides what it is,
when to surface it, and when to quietly let it go.

Four states — `active`, `shelved`, `done`, `dropped` — and you set none of
them by hand.

## Start here

| File | What's in it |
|------|--------------|
| `CLAUDE.md` | Context for Claude Code. Read first. |
| `PLAN.md` | Build order as five sessions, each with an exit criterion. |
| `docs/use-cases.md` | Every use case with its ID, priority and status. Dropped ones are kept, struck through. |
| `docs/data-model.md` | Schema, state machine, parse contract. |
| `docs/architecture.md` | Components, flows, external services. |
| `docs/decisions.md` | Decisions made, and what's still open. |
| `backend/README.md` | The API. |
| `mobile/README.md` | The Expo app, and the Google sign-in setup. |

## Setup

```bash
cp .env.example .env      # fill in the blanks
```

Then `PLAN.md` — "Already built" says what exists, and session 2 is next.
