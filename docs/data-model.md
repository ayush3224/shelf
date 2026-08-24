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

### `entities` *(populated since 24 August 2026 — UC45)*
| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid pk | |
| `type` | enum | `person` \| `org` \| `place` |
| `name` | text | |
| `aliases` | jsonb | other names the same entity goes by |

### `links` *(UC45)*
Bidirectional edges between items and entities.
`id`, `item_id`, `entity_id`, `relation`, `created_at`.
Unique on `(item_id, entity_id, relation)` — which is what makes re-linking a
capture idempotent rather than double-counting its mentions.

**Written for every kind of item, not just `person_note`s** (24 August 2026,
D46). A task that names somebody is linked to them, so it is on the Shelf and
on their page at once; nothing in this table ever asked what `kind` the item
was. Links can also be written and removed by hand from item detail
(`POST`/`DELETE /items/{id}/people`), and that path resolves a typed name only
by exact name or a recorded alias — never by the token subset `resolve_entity`
uses, because a name somebody typed is not a guess to be improved on. Removing
an entity's last link removes the entity, the same rule UC49 follows.

**Name resolution** (`resolve_entity` in `backend/db.py`, D43) decides which
row a parsed name belongs to: the same name, then a recorded alias, then a
token subset in either direction — *only when exactly one entity matches*. Two
Priyas on file means a bare "Priya" resolves to neither and gets its own row.
A fuller name promotes: a row called "Priya" that meets "Priya Sharma" is
renamed, keeping "Priya" in `aliases` so the earlier mentions stay attached.
`aliases` is therefore load-bearing rather than decorative — it is both how
past mentions survive a rename and what UC47's search matches on.

**Both operations that correct a resolution move aliases too** (UC48, UC49,
D45). A merge takes the absorbed person's name as an alias, or the next mention
would recreate the row just folded away. A split gives up any alias that names
the target, or the next bare mention resolves straight back onto the row just
corrected. Neither reads the notes to decide: they match names, and identity is
the owner's call.

> Creating `entities` and `links` in the first migration is deliberate.
> The graph UI is P2, but retrofitting these tables after months of
> items exist means a backfill pass over every note. Cheap now,
> expensive later.
>
> **This paid off on 24 August 2026.** UC45-47 shipped with no migration at
> all: the tables, the constraints and `links_entity_idx` were already there,
> so the whole module was extraction and UI. Neither table has a text index
> and neither needs one — `entities` is bounded by how many people are in a
> life, not by capture volume, which is also why UC47's list is unpaginated.

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

### `digests` *(UC31, migration 006)*
| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid pk | |
| `user_id` | uuid | |
| `period_start` / `period_end` | timestamptz | the week covered, half-open |
| `shelved` / `dropped` / `expiring` | int | the counts as built, for the notification body |
| `empty` | bool | nothing to report; the row is written, no push goes out |
| `sent_at` | timestamptz null | null with `empty` false means still outstanding |
| `attempts` / `last_error` / `ticket_id` | | same send bookkeeping as `notifications` |

Unique on `(user_id, period_start)`. **That constraint is the feature**: the
tick runs every minute for the twenty-four hours a digest is fresh, and the
week is announced once because the database refuses the second one — not
because the code remembers.

**The digest's content is not stored here.** What decayed is already in
`transitions` and what is about to drop is a property of `items` as they stand;
both are recomputed on every read of `GET /digest`, so the screen is correct
before the first digest has ever been sent and cannot show a stale copy of an
old one. This table records only which weeks were *announced*.

The halves are in different tenses and it matters: `shelved`/`dropped`/`done`
are history and will read the same in a year, `expiring` is a forecast that
moves the moment anything is touched. That is why the stored `expiring` count
and the one on screen can differ — the notification is a snapshot, the screen
is live.

**What counts as news** (D50). `shelved` reads `reason = 'decay'` and `dropped`
reads `reason = 'expiry'` — the transitions the system made on its own, which
is the whole reason this screen exists. `done` reads any transition into
`done`, however it was said. A shelving or a drop the *user* performed is in
`transitions` and deliberately not in the digest: it was never silent. Only the
first three counts feed `empty`; completions are worth reading and are not
worth a push.

## Config constants

Keep these in one config module, not scattered as literals.

```python
SHELVE_AFTER_IGNORES = 3     # UC18 — tune from `transitions` (O1)
DROP_AFTER_DAYS      = 90    # UC19 — tune from `transitions` (O2)
PUSH_REPEAT_MINUTES  = 240   # UC23 — and therefore the real speed of decay (D33, D40, O5)
SNOOZE_MINUTES       = 30    # UC17 — the default the notification button uses
MAX_SNOOZE_MINUTES   = 10080 # a week; beyond this is refused, not clamped
PUSH_BATCH_LIMIT     = 20    # sends per tick
PUSH_MAX_ATTEMPTS    = 5     # then the row stalls; it is never marked sent (D32)
QUIET_HOURS          = (22, 7)   # UC29 dropped — kept, unused
MAX_PARSE_TOKENS     = 200
MAX_SPLIT_TOKENS     = 600   # UC4 — the array re-prompt only (D19)
MAX_SPLIT_ITEMS      = 10
TRANSCRIPT_CONFIDENCE_FLOOR = 0.5   # below this → needs_review (D22, D27)
DIGEST_DAY           = "sunday"  # UC31 — with DIGEST_HOUR, when the week ends
DIGEST_HOUR          = 9         # local, in CAPTURE_TIMEZONE, not the server's UTC
DIGEST_WARN_DAYS     = 14        # two digest cycles, so nothing drops with one warning
DIGEST_MAX_AGE_HOURS = 24        # past this a digest is abandoned, not sent late (D48)
DIGEST_MAX_ATTEMPTS  = 5
DIGEST_LIST_LIMIT    = 20        # rows per section; the count above it is the true total
REVIEW_THRESHOLD     = 90        # UC30 — px a drag travels before it is an answer (D51)
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
decides `state` (UC12). `entities` are resolved and written to `entities` and
`links` on **every** capture whatever its `kind` (UC45, D46). `project_hint` is
still returned to the caller and not stored — UC11 was dropped.

Relative expressions ("tomorrow at 3pm") are resolved against `TZ`, not
against the server clock. The server runs in UTC; the user does not.
