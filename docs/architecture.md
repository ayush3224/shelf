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
┌───────────▼───────────────────┐      ┌──────────────────┐
│  FastAPI on a VPS (systemd)   │─────▶│  Anthropic API   │
│  · /capture  · /items         │      │  claude-haiku-4-5│
│  · /devices  · /snooze        │      └──────────────────┘
│  · shelf-tick.timer: 1-min    │      ┌──────────────────┐
│                               │─────▶│  Expo push → FCM │
└───────────┬───────────────────┘      └──────────────────┘
            │                          ┌──────────────────┐
┌───────────▼───────────────────┐      │ Google Calendar  │
│  Supabase                     │      │  (session 5)     │
│  Postgres · Auth · Storage    │      └──────────────────┘
└───────────────────────────────┘
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

`backend/scheduler.py`, run by `shelf-tick.timer` — a systemd `oneshot`, not
Railway cron, because the API has never actually run on Railway (D36). Pure
SQL and one HTTPS call to the push service; no model call ever.

The order is the design:

1. **Read the silence.** A push that was delivered and is still unanswered
   `PUSH_REPEAT_MINUTES` later → `response = 'ignored'`, `push_count + 1`.
2. **Decay.** `push_count + snooze_count >= SHELVE_AFTER_IGNORES` →
   `shelved`, reason `decay`. **Silently** — UC22 was dropped.
3. **Expire.** `shelved` and untouched past `DROP_AFTER_DAYS` →
   `dropped`, reason `expiry`. Also silent. "Untouched" is the later of
   `state_changed_at` and `updated_at` (D37).
4. **Cancel.** Notifications belonging to items that are no longer `active` —
   queued ones deleted, delivered ones closed with no response.
5. **Enqueue.** Everything due with nothing outstanding gets a row.
6. **Send.** One message per registered device, in one request to Expo.

Steps 1-3 run before step 5 so an item shelving on this tick does not also get
a fresh push on it. Ignoring and snoozing feed the same threshold — both are
"not now" (UC18) — the difference being that a snooze *answers* the push and an
ignore is read out of the silence.

**Nothing decays from a push that did not go out** (D32). `sent_at` is written
only when Expo accepted the message, step 1 only reads rows that carry one, and
a send that keeps failing stalls after `PUSH_MAX_ATTEMPTS` rather than being
marked sent. A broken delivery path costs reminders, never state.

UC29 (quiet hours) was dropped too, so nothing suppresses an overnight push.
`QUIET_HOURS` remains in the config module but is unused; leaving it there is
cheaper than removing it and re-deriving it if the decision reverses.

With both dropped, the `transitions` table and the weekly digest (UC31) are
the only places decay is observable at all.

## Push delivery (UC23, UC15, UC17)

```
scheduler ──▶ exp.host/--/api/v2/push/send ──▶ FCM ──▶ device
                        ▲
                 FCM V1 service account key
                 lives in the EAS dashboard
```

The app is Expo, so the address of a device is an `ExponentPushToken` and
Expo's service is what stands in front of FCM. The server holds no Google
credential at all; talking to FCM directly would mean managing that key twice.

- The app posts its token to `POST /devices` **on every launch**, because Expo
  reissues it on reinstall or a data clear, and a stale token is a reminder
  that goes nowhere with nothing to show for it.
- The Done and Snooze buttons come from a notification *category* the app
  registers and the server names on every message. Both names live in config on
  the server and in `lib/notifications.ts` on the device; if they drift, the
  notification still arrives and simply has no buttons.
- Both actions foreground the app (D34) — a response given while the app is
  killed never reaches a listener otherwise.
- `DeviceNotRegistered` from Expo disables the token; the item is not punished
  for it.

## Delivery tiers

| Tier | Trigger | Mechanism |
|------|---------|-----------|
| Push | normal item due | Expo → FCM notification with done/snooze actions — **built** |
| Alarm | `critical`, or already ignored twice | native full-screen intent, bypasses DND |
| Call | opt-in, must-not-miss (P2) | CallMeBot HTTP GET |

## ~~Natural-language query (UC35)~~ — dropped

Was to be one Haiku call turning a question into SQL against the read-only
`v_items_query` view. Dropped 23 August 2026 (owner's decision). Retrieval is
UC33 (browse) and UC34 (search), both plain SQL with no model call.

`v_items_query` still exists in migration 001. It is now unused; it costs
nothing and would be needed again if this ever came back.

## Retrieval (UC33, UC34, UC36)

One endpoint, `GET /items`, behind one screen. Browse, search and filter are
the same query with different arguments, because they are the same screen with
different chips pressed — splitting them would mean a search you could not
narrow, which is a second and worse list.

```
GET /items?q=&state=&project=&from=&to=&cursor=&limit=
```

Two defaults, and they deliberately differ:

- **No parameters** → everything that is not `active`. The Shelf is defined by
  what `Today` already owns.
- **A search** → *all four* states. You are looking for a thing you said, and
  whether it happens to be due today is not something you should have had to
  guess before typing. An explicit `state` beats both, which is what lets a
  chip narrow a search back down.

Ordered by `created_at desc, id desc` (D38) and paged by an opaque keyset
cursor over that pair (D39) — never an offset. Search is `ILIKE` over
`raw_text` and `parsed_text` through the two trigram GIN indexes, escaped so a
typed `%` is a percent sign rather than the whole table. No model call is
involved at any point; UC35 was dropped and this is what replaced it.

Grouping by project is done on the device, not in the response. A group can
straddle a page boundary, so a response shaped as sections would have to either
cut a group or give up paging. With UC11 dropped nothing populates
`project_id`, so in practice there is one group, "Unsorted", and the section
headers do not render at all.

## People (UC45-47)

`entities` and `links` have been in the schema since migration 001 (D7). The
module is extraction plus UI on top of them, not a migration:

**Built 24 August 2026, with no migration** — the tables were already there.

- The parse has always returned `entities` — `{type, name}` for people, orgs
  and places named in a capture — and always discarded them. UC45 is that
  write: `resolve_entity` picks the row, then a `links` row records the
  mention. Linking happens on every write path including each half of a split
  (UC4), and it is **enrichment, never a gate**: a capture whose people cannot
  be resolved keeps its words, exactly as a failed parse does (D6, UC42).
- Resolution never guesses between two candidates (D43). The snapshot of known
  entities is read once per capture and updated in memory as it goes, so two
  split siblings that both say "Priya" land on one row rather than colliding on
  the unique constraint.
- UC46 reads back the other way: `GET /people/{id}`, every item linked to one
  entity, **newest first** (the owner changed this from oldest-first on 24
  August 2026) and every state, keyset-paginated like the Shelf (D39).
- UC47 is `GET /people`, ordered by who was mentioned most recently and
  searching aliases as well as names. Unpaginated on purpose: this table is
  bounded by how many people are in a life, not by capture volume.

**Recall is manual and stays that way for now.** No calendar triggering, no
proactive surfacing. That needs UC43 and a delivery tier, and it should not
be built before the manual version has been used.

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
