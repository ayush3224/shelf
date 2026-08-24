# Data model

Postgres via Supabase. Written for a single user, but `user_id` is on
every row so that assumption is cheap to unwind later.

## State machine

```
                  ┌─────────┐
      captured ───│ active  │──── done (explicit) ──▶ done
      with time   └────┬────┘
                       │ ignored/snoozed N times (decay)
                       ▼
                  ┌─────────┐
   captured ─────▶│ shelved │──── untouched M days ──▶ dropped
   without time   └────┬────┘
                       │ re-mentioned in a new note
                       └──────────▶ active
```

`done` and `dropped` are terminal but reversible by hand (UC21).
Everything else moves on its own.

`push_count` and `snooze_count` are the decay counter, and they count
*this* stretch of being active: entering `active` from any other state
resets both, by trigger (migration 004, D35). Without that, an item
taken back off the shelf would arrive carrying the ignores that put it
there and shelve again on its first push.

## Tables

### `items`
The core row. One per captured thing.

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid pk | |
| `user_id` | uuid | |
| `kind` | enum | `task` \| `note` \| `person_note` |
| `state` | enum | `active` \| `shelved` \| `done` \| `dropped` |
| `raw_text` | text | transcript or typed input, never rewritten |
| `parsed_text` | text null | cleaned one-line description from the parse; falls back to `raw_text` for display |
| `audio_path` | text null | Supabase Storage key; kept until delete. Split siblings (UC4) share one |
| `transcript_source` | enum | `on_device` \| `cloud` \| `none` — which path produced `raw_text` (migration 003) |
| `transcript_confidence` | real null | Transcriber confidence in [0,1]; null if it reported none |
| `project_id` | uuid null | fk `projects` |
| `due_at` | timestamptz null | presence decides initial state |
| `critical` | bool | drives full-screen alarm / call tier |
| `push_count` | int | pushes sent since last user response |
| `snooze_count` | int | |
| `parse_status` | enum | `ok` \| `failed` \| `needs_review` (UC42) |
| `source` | enum | `voice` \| `text` \| `widget` |
| `state_changed_at` | timestamptz | with `updated_at`, drives the drop timer (D37) |
| `created_at` / `updated_at` | timestamptz | |

Indexes: `(user_id, state, due_at)`, `(user_id, state_changed_at)`,
trigram GIN on `raw_text` **and on `parsed_text`** for UC34, `(audio_path)`
where non-null — that key is how a split's siblings are found — and
`(user_id, created_at desc, id desc)` for the Shelf's keyset paging (migration
005, D39). The two search indexes are a pair on purpose: `raw_text` is what was
said and `parsed_text` is what is displayed, and searching one without the
other silently misses half of what the user is looking at.

### `transitions`
Append-only audit of every state change. **This table is how you tune
the decay constants later** — don't skip it.

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid pk | |
| `item_id` | uuid | |
| `from_state` / `to_state` | enum | |
| `reason` | enum | `manual` \| `decay` \| `expiry` \| `reactivation` \| `completion` |
| `created_at` | timestamptz | |

### `projects`
`id`, `user_id`, `name`, `slug`.

### `entities` *(UC44 — create now, populate later)*
| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid pk | |
| `type` | enum | `person` \| `org` \| `place` |
| `name` | text | |
| `aliases` | jsonb | other names the same entity goes by |

### `links` *(UC44)*
Bidirectional edges between items and entities.
`id`, `item_id`, `entity_id`, `relation`, `created_at`.
Unique on `(item_id, entity_id, relation)`.

> Creating `entities` and `links` in the first migration is deliberate.
> The graph UI is P2, but retrofitting these tables after months of
> items exist means a backfill pass over every note. Cheap now,
> expensive later.

### `notifications`
`id`, `item_id`, `scheduled_for`, `tier` (`push`\|`alarm`\|`call`),
`sent_at`, `responded_at`, `response` (`done`\|`snooze`\|`ignored`).

Plus, from migration 004: `attempts`, `last_error`, `ticket_id`.

An `ignored` row is written by the scheduler when the next push comes
due with no response to the previous one — "next push due" being
`sent_at + PUSH_REPEAT_MINUTES` (D33). That write is what increments
`push_count` and eventually triggers decay.

`sent_at` is set **only** when the push service accepted the message.
Everything that stops a push leaving records itself in `attempts` and
`last_error` instead, and the ignore sweep never looks at a row without
`sent_at` — so an item cannot be decayed by a reminder the user never
received (D32). After `PUSH_MAX_ATTEMPTS` the row stalls rather than
being marked sent.

`responded_at` set with `response` **null** is a third thing, distinct
from both an answer and a silence: the item left `active` some other way
(finished elsewhere, moved by hand) and the push was cancelled rather
than ignored. Counting those as ignores would decay items the user had
just touched.

### `push_tokens` *(UC23, migration 004)*
Where a reminder is actually sent.

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid pk | |
| `user_id` | uuid | reassigned on conflict, see below |
| `token` | text unique | `ExponentPushToken[...]` |
| `platform` | text | `android` \| `ios` \| `web` |
| `device_name` | text null | for telling two phones apart |
| `created_at` / `updated_at` | timestamptz | |
| `last_success_at` | timestamptz null | last push the service accepted |
| `disabled_at` / `disabled_reason` | | set on `DeviceNotRegistered` |

Keyed on the **token**, not the user: an Expo push token identifies an
install, and the same install can be signed in as someone else tomorrow.
Registering an existing token therefore reassigns the row instead of
adding a second one, which would push the same phone twice. It also
clears `disabled_at` — a token we had written off has just proved
otherwise by turning up again.

### `calendar_links` *(UC43)*
`item_id`, `google_event_id`, `calendar_id`, `last_synced_at`,
`sync_state` (`pending`\|`synced`\|`error`).

One-way: app → Google. Never merge back.

## Config constants

Keep these in one config module, not scattered as literals.

```python
SHELVE_AFTER_IGNORES = 3     # UC18 — tune from `transitions` (O1)
DROP_AFTER_DAYS      = 90    # UC19 — tune from `transitions` (O2)
PUSH_REPEAT_MINUTES  = 60    # UC23 — and therefore the real speed of decay (D33, O5)
SNOOZE_MINUTES       = 30    # UC17 — the default the notification button uses
MAX_SNOOZE_MINUTES   = 10080 # a week; beyond this is refused, not clamped
PUSH_BATCH_LIMIT     = 20    # sends per tick
PUSH_MAX_ATTEMPTS    = 5     # then the row stalls; it is never marked sent (D32)
QUIET_HOURS          = (22, 7)   # UC29 dropped — kept, unused
MAX_PARSE_TOKENS     = 200
MAX_SPLIT_TOKENS     = 600   # UC4 — the array re-prompt only (D19)
MAX_SPLIT_ITEMS      = 10
TRANSCRIPT_CONFIDENCE_FLOOR = 0.5   # below this → needs_review (D22, D27)
DIGEST_DAY           = "sunday"
```

## Parse contract

The Haiku call returns exactly this shape, nothing else:

```json
{
  "kind": "task | note | person_note",
  "text": "cleaned one-line description",
  "due_at": "ISO-8601 or null",
  "critical": false,
  "project_hint": "string or null",
  "entities": [{"type": "person", "name": "..."}],
  "split": false
}
```

`split: true` means the note contained several items (UC4) and the
caller should re-prompt for an array. Keep the common path cheap.

The re-prompt returns `{"items": [ ... ]}`, each element the same shape minus
`split`. It is the only call allowed past `MAX_PARSE_TOKENS` — an array does
not fit in 200 — and is capped at `MAX_SPLIT_TOKENS` / `MAX_SPLIT_ITEMS`
instead (D19). One row is written per item; every one carries the same
`audio_path`, and that shared key is the only thing grouping them. Each gets
its own `due_at` and therefore its own initial state (UC12), but they all keep
the *whole* transcript in `raw_text`: UC38 edits against what was actually
said and UC34 searches it, and neither is served by a fragment the user never
spoke. A split that fails degrades to the single item already parsed.

Where each field lands on `items`: `kind`, `due_at` and `critical` map to
their own columns, `text` to `parsed_text` (migration 002), and `due_at`
decides `state` (UC12). `project_hint` and `entities` are returned to the
caller but not yet stored — they need UC11 and UC44 respectively.

Relative expressions ("tomorrow at 3pm") are resolved against `TZ`, not
against the server clock. The server runs in UTC; the user does not.
