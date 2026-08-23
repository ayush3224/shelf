# PLAN.md

Build order, as sessions rather than phases. Each has an exit criterion —
don't start the next until the current one is true.

Priorities: **P0** = v1, **P1** = shortly after, **P2** = later or never.
Dropped and deferred use cases are marked in `docs/use-cases.md`; the IDs
are kept so history keeps pointing at the right thing.

---

## Already built

Not a session — the ground the rest stands on.

- Infrastructure: `shelf` schema on the VPS's Supabase, FastAPI behind Caddy
  on 443, systemd, migrations 001–003.
- UC41 auth — Supabase JWT, fail-closed middleware.
- UC5 text capture, UC1 voice capture, UC8 transcription (Whisper on Groq).
- UC9 kind, UC10 due date, UC12 initial state, UC14 critical flag.
- UC4 multi-item splitting, UC7 audio retained and playable.
- UC42 graceful failure — the audio survives a failed transcription or parse.
- UC32 `Today`, UC16 mark done.

**Not yet true:** none of it has been used on a phone for more than a test.
That gate is real — see the exit criterion on session 1.

---

## Session 1 — Item detail ✅ *(done 23 August 2026)*

- UC37 item detail with original audio
- UC38 edit or correct a mis-parsed item
- UC39 delete permanently, storage object included
- UC21 manually move an item between any two states

**Exit:** a mis-parsed item can be fixed, moved or destroyed from the phone
without touching the database. *Built and verified against the live API; the
screen itself has still not been used on a device.*

**The real gate before session 2:** use the app daily for two weeks. This is
not a formality. Phase 1's original exit criterion was real usage, and every
decay constant in session 2 is guesswork until there is behaviour to tune it
against.

---

## Session 2 — Decay engine and push (P0)

The differentiating part, and the last of the P0 work.

- UC23 push notification at due time
- UC17 snooze
- UC18 auto-shelve after N ignored or snoozed pushes
- UC19 auto-drop after M days shelved and untouched
- UC20 reactivate a shelved item — an in-app action, not a spoken re-mention

UC15 (mark done from the notification action) rides with UC23. It is P0 and
a push you cannot answer from the notification shade is half a feature.

Note what dropping UC22 means here: decay is now **silent**. Items shelve and
drop without announcing it, so the only place that becomes visible is the
weekly digest in session 5. And with UC29 dropped, nothing suppresses an
overnight push.

**Exit:** you can ignore the app for a week and the state is still honest —
nothing lingers `active` that you have repeatedly not acted on.

---

## Session 3 — Shelf screen (P0/P1)

Everything that isn't due, and a way to find it.

- UC33 browse the shelf
- UC34 text search across all items
- UC36 filter by state and date range

UC33 was written as "grouped by project". With UC11 dropped, `project_id` is
never populated, so this is a flat list until projects are entered by hand or
UC11 comes back. Filtering by project goes with it.

**Exit:** you can find any item you have ever captured in under ten seconds,
without scrolling `Today`.

---

## Session 4 — People (P1)

- UC45 voice-record a note about a person; the parse extracts who and links it
- UC46 person page — everything ever said about them
- UC47 browse and search people

`entities` and `links` have been in the schema since migration 001 for this
exact purpose (D7), so this is extraction and UI, not a migration.

Recall is **manual**: you look someone up. No calendar triggering, no
proactive surfacing. That is deferred on purpose — it needs session 5's
calendar link, and it should not be built before the manual version has been
used enough to know what is worth surfacing.

**Exit:** you can open a person and read everything you have ever said about
them, and adding to it costs one voice note.

---

## Session 5 — Review and calendar (P1)

- UC30 weekly review as a swipe deck — four directions, four states
- UC31 weekly digest of what decayed and what is about to drop
- UC43 write timed items to a personal Google Calendar
- UC14 critical flag from spoken cues *(already parsed; this is the delivery
  half — what `critical` actually changes)*

UC31 carries more weight than it looks. With UC22 dropped, the digest is the
**only** place decay becomes visible, so it is the thing standing between
"silence is signal" and "items vanish quietly".

The calendar is one-way: the app owns the item, the event is a projection.
Store `google_event_id`, reconcile app → Google only. Never build two-way
merge — it is where this kind of project dies (D8).

**Exit:** Sunday review takes under two minutes, and timed items appear in
the calendar and stay in sync when edited.

---

## Not scheduled

Still real, still unbuilt, no session claimed:

| ID | | |
|----|---|---|
| UC2 | Home-screen widget | deferred pending real usage |
| UC24 | Full-screen DND-breaking alarm | deferred pending real usage |
| UC3 | Lock-screen capture | P2 |
| UC13 | Detect a reference to an existing item | P2 |
| UC25 | Reminder in a known person's voice | P2 |
| UC26 | Voice-call escalation | P2 |
| UC28 | Escalation ladder | P2 |
| UC40 | Export all data | P2 |
| UC44 | Obsidian-style linked graph | P2 — UC45-47 are the practical first cut |

UC2 and UC24 are **deferred, not cancelled**: both are friction-removal, and
whether they are worth two native modules is a question daily use answers and
speculation does not.

---

## Open questions

Locked defaults are in `docs/decisions.md`. These two want real answers once
there is data:

1. `SHELVE_AFTER_IGNORES` — default 3
2. `DROP_AFTER_DAYS` — default 90

The `transitions` table logs every state change with a reason, so after a
month you can query how often a decay-shelved item gets resurrected by hand
and tune both from evidence rather than guessing. Session 2 is what starts
producing that data; until then both numbers are opinion.
