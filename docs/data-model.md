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
| `audio_path` | text null | Supabase Storage key; kept until delete |
| `project_id` | uuid null | fk `projects` |
| `due_at` | timestamptz null | presence decides initial state |
| `critical` | bool | drives full-screen alarm / call tier |
| `push_count` | int | pushes sent since last user response |
| `snooze_count` | int | |
| `parse_status` | enum | `ok` \| `failed` \| `needs_review` (UC42) |
| `source` | enum | `voice` \| `text` \| `widget` |
| `state_changed_at` | timestamptz | drives the drop timer |
| `created_at` / `updated_at` | timestamptz | |

Indexes: `(user_id, state, due_at)`, `(user_id, state_changed_at)`,
full-text GIN on `raw_text` for UC34.

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

An `ignored` row is written by the scheduler when the next push comes
due with no response to the previous one. That write is what increments
`push_count` and eventually triggers decay.

### `calendar_links` *(UC43)*
`item_id`, `google_event_id`, `calendar_id`, `last_synced_at`,
`sync_state` (`pending`\|`synced`\|`error`).

One-way: app → Google. Never merge back.

## Config constants

Keep these in one config module, not scattered as literals.

```python
SHELVE_AFTER_IGNORES = 3     # UC18 — tune from `transitions`
DROP_AFTER_DAYS      = 90    # UC19 — tune from `transitions`
QUIET_HOURS          = (22, 7)
MAX_PARSE_TOKENS     = 200
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

Where each field lands on `items`: `kind`, `due_at` and `critical` map to
their own columns, `text` to `parsed_text` (migration 002), and `due_at`
decides `state` (UC12). `project_hint` and `entities` are returned to the
caller but not yet stored — they need UC11 and UC44 respectively.

Relative expressions ("tomorrow at 3pm") are resolved against `TZ`, not
against the server clock. The server runs in UTC; the user does not.
