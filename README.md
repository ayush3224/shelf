# Shelf

Personal voice-first capture. Speak it, and the system decides what it is,
when to surface it, and when to quietly let it go.

Four states — `active`, `shelved`, `done`, `dropped` — and you set none of
them by hand.

## Start here

| File | What's in it |
|------|--------------|
| `CLAUDE.md` | Context for Claude Code. Read first. |
| `PLAN.md` | Phased build order with exit criteria. |
| `docs/use-cases.md` | All 44 use cases, with IDs and priorities. |
| `docs/data-model.md` | Schema, state machine, parse contract. |
| `docs/architecture.md` | Components, flows, external services. |
| `docs/decisions.md` | Decisions made, and what's still open. |

## Setup

```bash
cp .env.example .env      # fill in the blanks
```

Then Phase 0 in `PLAN.md`.
