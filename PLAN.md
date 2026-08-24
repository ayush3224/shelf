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

## Session 2 — Decay engine and push ✅ *(built 23 August 2026)*

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

*Built and verified server-side against the live database and the real clock:
`pytest -m db` walks a real item from due, through three ignored pushes, to
`shelved` with reason `decay`, and a real row backdated ninety days to
`dropped` with reason `expiry` — nothing announced either. The failure paths
are proven too, including a real round trip to Expo: a push that does not
leave never marks `sent_at` and therefore cannot decay anything (D32).*

**Still outstanding:** the last hop. A real notification has not yet appeared
on the phone, because the build on the device predates `expo-notifications` —
there is nothing on it to register a token. That needs an EAS build installed,
and then the two-minute test: capture something due shortly, watch it arrive,
press Done. Until that has happened the exit criterion is not met.

The **real gate** stated below — two weeks of daily use — was not met before
this session, and the constants are still guesses because of it. `PUSH_REPEAT_MINUTES`
is a new one (D33, O5) and it is the one that decides how fast decay actually
runs. It has since been raised from 60 to 240 (D40) — not because data arrived,
but because an hour was refutable without it: three ignores fitted inside one
meeting.

---

## Session 3 — Shelf screen ✅ *(built 24 August 2026)*

Everything that isn't due, and a way to find it.

- UC33 browse the shelf
- UC34 text search across all items
- UC36 filter by state and date range
- UC20's reactivate button, which finally has somewhere to live

UC33 was written as "grouped by project". With UC11 dropped, `project_id` is
never populated, so the grouping is **built but dormant**: rows carry their
project, the client sections on it, and with nothing to section the headers do
not render. Entering a project by hand is all it takes to light it up — and
`GET /projects` exists so the filter chips appear the moment one does.

Ordering is capture time, not decay time (D38), and paging is keyset from the
first request (D39). Both were choices about what this screen *is*: an archive
of what you have said, not a ledger of what the system has taken away from you.

**Exit:** you can find any item you have ever captured in under ten seconds,
without scrolling `Today`.

*Built and verified against the live database through the running API: a real
capture landed on the shelf, was found by a word from the middle of it, was
reactivated over HTTPS with the transition logged as `reactivation`, and was
deleted again — eight rows before, eight rows after. `pytest -m db` runs the
browse SQL itself against a real Postgres on its own schema, including the two
properties that only fail in production: a typed `%` matching itself rather
than the whole table, and a page boundary that neither repeats nor skips a row
when a capture lands mid-scroll.*

**Still outstanding:** the same last hop as session 2. None of this has been
touched on a phone — the screen is verified through the API and under jest, not
under a thumb. The first attempt to look at it on the device found the tab bar
had been invisible since the app's first commit (D41): `tabBarStyle.height` was
a literal, so the bottom safe-area inset ate the labels and `Today` and `Shelf`
were both unreachable. Fixed, with a test that renders behind a real inset —
but it still needs a build and one look.

---

## Session 4 — People ✅ *(built 24 August 2026)*

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

*Built with **no migration**, as predicted — `entities`, `links`, their
constraints and `links_entity_idx` were all in place from 001, so the whole
module was extraction and UI. That bet (D7) is now paid off rather than
theoretical.*

*The hard part was not the schema, it was deciding when two names are one
person (D43): same name, then a recorded alias, then a token subset — and only
ever when exactly one candidate matches. "Priya" and "Priya Sharma" merge, with
the fuller name promoted and the shorter kept as an alias so earlier mentions
stay attached. Two Priyas on file and a bare "Priya" resolves to neither,
because a wrong merge is silent and a wrong split is visible.*

*People takes the fourth tab (D44), and four is written down as the ceiling.*

*Manual correction followed the same day: **UC48 merge** and **UC49 split**,
both on the person page, closing O6. That inverts what the resolution rules are
for — with a correction path they only need to be recoverable, not right, so
D43 is deliberately left as it is rather than tuned (D45). The machine files,
the owner adjudicates.*

**Still outstanding:** as ever, none of this has been touched on a phone.

---

## Session 5 — Review and calendar (P1) — *in progress*

- UC31 weekly digest of what decayed and what is about to drop ✅ *(built
  24 August 2026)*
- UC30 weekly review as a swipe deck — four directions, four states
- UC43 write timed items to a personal Google Calendar
- UC14 critical flag from spoken cues *(already parsed; this is the delivery
  half — what `critical` actually changes)*

UC31 carries more weight than it looks. With UC22 dropped, the digest is the
**only** place decay becomes visible, so it is the thing standing between
"silence is signal" and "items vanish quietly". It is built first for that
reason: it is the piece that makes everything already shipped defensible,
rather than the piece that adds the most.

*Built with migration 006 and no model call.* The digest is two SQL queries
recomputed on read; `shelf.digests` stores only which weeks were announced
(D47), and the `CLAUDE.md` rule about batching it was struck rather than
implemented, because there turned out to be nothing to batch. The tick gained
a seventh step, the only one driven by the calendar rather than by how long
something has waited. Its own route and its own notification channel, not a
fifth tab (D49); a stale digest is abandoned rather than delivered late, which
is the opposite of the rule for item pushes and for a stated reason (D48).

*Verified live.* Both halves through the running API over a real capture — a
decay transition inside the week appearing under "Shelved", a real row four
days from its drop date appearing under "About to drop" with a `drops_at` the
expiry sweep would agree with. The tick's own path was exercised against the
live schema with the push service stubbed: built once, refused the second time
by the unique constraint, one row marked sent, five devices messaged.
`pytest -m db` covers the rest against a real Postgres, including the window
boundaries and the case that matters most — a decay the owner immediately
undid still counts as part of the week.

**Still outstanding on UC31:** the real Expo round trip. Five devices are
registered against the live schema and an unannounced Monday "your week" push
is not one to send on the owner's behalf; the first real one is due the coming
Sunday at 9am IST, and the message that will go out has been printed and read.
And the same last hop as every session before it — none of this has been seen
on a phone.

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

Locked defaults are in `docs/decisions.md`. These three want real answers once
there is data:

1. `SHELVE_AFTER_IGNORES` — default 3
2. `DROP_AFTER_DAYS` — default 90
3. `PUSH_REPEAT_MINUTES` — **240** since 24 August 2026, and the one that
   actually sets the pace: with a threshold of 3, an item nobody touches is
   on the shelf about twelve hours after it fell due. It was 60, which put
   that at two hours — inside a single long meeting, which is an interruption
   and not the repeated avoidance UC18 is meant to catch (D40, superseding
   D33; O5)

The `transitions` table logs every state change with a reason, so after a
month you can query how often a decay-shelved item gets resurrected by hand
and tune both from evidence rather than guessing. Session 2 is what starts
producing that data; until then both numbers are opinion.
