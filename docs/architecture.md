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

1. User holds the mic button; the app records to a local `.m4a`.
   Microphone permission is asked here, on the first hold, not at launch.
2. App uploads `{audio, transcript?, source}` to `POST /capture/audio`.
   `transcript` is for an on-device result; nothing produces one today
   (**D20**), so in practice it is absent and step 4 does the work.
3. Backend stores the audio in Supabase Storage **first**. If that fails the
   request fails with 503 — the file is still on the device, and reporting
   success would be the one lie this path must not tell (D21).
4. Backend writes the `items` row with `audio_path`, `transcript_source` and
   `parse_status = failed` (D13).
5. Backend transcribes (`whisper-large-v3-turbo` on Groq, D24) unless the
   client sent a transcript. A 429 or a flaky connection is retried with
   backoff inside a 75s deadline (D25); if it still fails, the row keeps the
   audio and no words and stops here (UC42).
6. Backend calls Haiku with the parse contract (see `data-model.md`).
7. If the parse sets `split`, a second Haiku call returns an array and one row
   is written per item, all sharing the one `audio_path` (UC4, D19). A failed
   split degrades to the single item already parsed.
8. On success: fill `kind`, `due_at`, `critical`; set `state = active` if
   `due_at` is present else `shelved`; `parse_status = ok`, or
   `needs_review` if the transcript was low-confidence (D22).
9. On failure: leave the row, keep the audio, `parse_status = failed`.
   **The capture is never lost** (UC42).

Note the order. Steps 3 and 4 come before any model call: the recording is the
only part of a capture that cannot be reproduced, and parsing is an enrichment,
not a gate.

Playback (UC7) is `GET /items/{id}/audio`, which mints a short-lived signed
URL per request rather than storing one on the row — the bucket holds the
user's voice.

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
| Groq (Whisper STT) | transcription | free tier covers ~20 captures/day — see D20, D24 |
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
