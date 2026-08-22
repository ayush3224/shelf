# PLAN.md

Build order. Each phase has an exit criterion — don't start the next
phase until the current one is true.

Priorities: **P0** = v1, **P1** = shortly after, **P2** = later or never.

---

## Phase 0 — Foundations

Repo, infra, nothing user-facing.

- Supabase project; run `/migrations/001_init.sql`
- Supabase Auth, single user (UC41)
- FastAPI skeleton on Railway, `/health` endpoint
- Expo app skeleton, builds to device
- `.env.example`, CI running lint + tests

**Exit:** app on your phone talks to your API talks to your DB.

---

## Phase 1 — The core loop (P0)

The smallest thing that's genuinely useful.

- UC1 capture from home screen
- UC5 text entry fallback
- UC8 transcription (on-device first)
- UC9 classify kind
- UC10 extract due date/time
- UC12 initial state assignment
- UC32 `Today` list
- UC15/16 mark done
- UC38 edit a mis-parsed item
- UC39 delete
- UC42 graceful failure — keep audio, flag item

**Exit:** you capture by voice and things show up in `Today` at the right
time. Use it for two weeks before building anything else. If you don't
reach for it, stop and figure out why — no further phase fixes that.

---

## Phase 2 — Making it self-maintaining (P0/P1)

The decay engine. This is the differentiating part.

- UC23 push notification at due time
- UC17 snooze
- UC18 auto-shelve after N ignores
- UC19 auto-drop after M days
- UC21 manual state moves
- UC22 announce every automatic transition
- UC33 browse the shelf
- UC34 text search
- UC29 quiet hours

**Exit:** you can ignore the app for a week and the state is still honest.

---

## Phase 3 — Friction removal (P1)

- UC2 home-screen widget
- UC24 full-screen alarm (native module, full-screen intent)
- UC6 offline capture + sync queue
- UC7 audio playback on item detail
- UC14 critical flag from spoken cues
- UC4 multi-item splitting

**Exit:** capture is one tap and critical things are unmissable.

---

## Phase 4 — Review (P1)

- UC30 swipe deck, four directions → four states
- UC31 weekly digest (Batch API)
- UC36 filters
- UC37 item detail

**Exit:** Sunday review takes under two minutes.

---

## Phase 5 — Calendar (P1)

- UC43 write to personal Google Calendar

App is source of truth; the calendar event is a projection the app owns.
Store `google_event_id` on the item; reconcile one way only. Don't build
two-way merge — it's where this kind of project dies.

**Exit:** timed items appear in your calendar and stay in sync when edited.

---

## Phase 6 — Graph (P2)

- UC44 Obsidian-style people/notes network
- UC11 project inference
- UC13 dedup / update-existing detection
- UC35 natural-language query (text-to-SQL)
- UC20 reactivation by re-mention

The `entities` and `links` tables exist from Phase 0 — this phase builds
the extraction and UI on top of them.

**Exit:** a person page shows everything you've ever said about them.

---

## Phase 7 — Optional (P2)

- UC3 lock-screen capture
- UC25 reminder in a known person's voice
- UC26 voice call escalation
- UC28 escalation ladder
- UC40 export

---

## Open questions

Locked defaults are in `docs/decisions.md`. These two want real answers
once you have data:

1. `SHELVE_AFTER_IGNORES` — default 3
2. `DROP_AFTER_DAYS` — default 90

The `transitions` table logs every state change with a reason, so after
a month you can query what your actual ignore-then-still-do-it rate is
and tune both numbers from evidence rather than guessing.
