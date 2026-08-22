# Architecture

## Components

```
┌──────────────────────────────┐
│  Expo / React Native (Android)│
│  · Capture (home)             │
│  · Today                      │
│  · Shelf                      │
│  · Item detail                │
│  · Review deck                │
│  native: full-screen alarm,   │
│          home-screen widget   │
└───────────┬──────────────────┘
            │ HTTPS
┌───────────▼──────────────────┐      ┌──────────────────┐
│  FastAPI on Railway           │─────▶│  Anthropic API   │
│  · /capture  · /items         │      │  claude-haiku-4-5│
│  · /transitions  · /query     │      └──────────────────┘
│  · cron: 1-min tick           │
└───────────┬──────────────────┘      ┌──────────────────┐
            │                          │ Google Calendar  │
┌───────────▼──────────────────┐      │  (phase 5)       │
│  Supabase                     │      └──────────────────┘
│  Postgres · Auth · Storage    │
└──────────────────────────────┘
```

## Capture flow

1. User holds the mic button; app records to a local file.
2. On-device `SpeechRecognizer` transcribes. If confidence is low or
   the recognizer fails, mark for cloud fallback.
3. App uploads `{audio, transcript?, source}` to `/capture`.
4. Backend stores audio in Supabase Storage, writes the `items` row
   immediately with `parse_status = needs_review`.
5. Backend calls Haiku with the parse contract (see `data-model.md`).
6. On success: fill `kind`, `due_at`, `critical`, entities;
   set `state = active` if `due_at` present else `shelved`;
   `parse_status = ok`.
7. On failure: leave the row, keep the audio, `parse_status = failed`.
   **The capture is never lost** (UC42).

Note step 4 — the row is written *before* the model call. Parsing is an
enrichment, not a gate.

## Scheduler (1-minute tick)

Pure SQL, no model calls:

- Items where `due_at <= now()` and not yet notified → enqueue push.
- Prior notification unanswered → write `response = 'ignored'`,
  increment `push_count`.
- `push_count >= SHELVE_AFTER_IGNORES` → transition to `shelved`,
  reason `decay`, and send the announcement (UC22).
- `state = 'shelved'` and `state_changed_at < now() - DROP_AFTER_DAYS`
  → transition to `dropped`, reason `expiry`.
- Respect `QUIET_HOURS` for everything except `critical`.

## Delivery tiers

| Tier | Trigger | Mechanism |
|------|---------|-----------|
| Push | normal item due | FCM notification with done/snooze actions |
| Alarm | `critical`, or already ignored twice | native full-screen intent, bypasses DND |
| Call | opt-in, must-not-miss (P2) | CallMeBot HTTP GET |

## Natural-language query (UC35)

One Haiku call turns the question into SQL against a fixed, read-only
view. Execute it, return rows. **Never** load table contents into the
prompt — the model sees the schema, not the data.

Guard: allow-list of tables, `SELECT` only, hard `LIMIT`, statement
timeout.

## Google Calendar (UC43)

- OAuth via Google, refresh token in Supabase.
- App writes; Google never writes back.
- Item created with `due_at` → create event, store `google_event_id`.
- Item edited → patch event. Item done/dropped → delete event.
- Reconciliation job nightly for anything with `sync_state = 'error'`.

## External services & cost

| Service | Purpose | Expected cost |
|---------|---------|---------------|
| Anthropic API | parse, NL query | < $1 / month at ~20 captures/day |
| Supabase | DB, auth, storage | free tier |
| Railway | API + cron | ~$5 / month |
| Google Calendar | UC43 | free |
| CallMeBot | UC26 | free, or $15/mo dedicated |

The infrastructure costs more than the model. Optimise for build time,
not tokens — but keep the cost rules in `CLAUDE.md` anyway, because the
failure mode isn't steady state, it's a retry loop at 3am.

## Not doing

- Multi-user, sharing, collaboration.
- Two-way calendar sync.
- iOS (until Android is genuinely used daily).
- A web dashboard.
- Prompt caching.
