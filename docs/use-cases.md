# Use cases

Canonical list. Reference by ID in commits, PRs, and issues.

P0 = v1 · P1 = shortly after · P2 = later · — = dropped or deferred

Dropped rows are struck through and kept, never deleted: the IDs appear in
commits, decisions and the build log, and reusing one would silently repoint
that history at something else.

## Capture

| ID | Use case | Pri |
|----|----------|-----|
| UC1 | Record a voice note from the app's home screen | P0 |
| UC2 | Record from a home-screen widget without opening the app — **deferred** pending real usage; not cancelled | — |
| UC3 | Record from lock screen / quick-settings tile | P2 |
| UC4 | Speak several items in one note; system splits them | P1 |
| UC5 | Type an item instead of speaking | P0 |
| ~~UC6~~ | ~~Capture offline; queue and sync on reconnect~~ — **dropped** by the owner, not a technical limit. A capture with no connection fails and is retried by hand. | — |
| UC7 | Original audio retained and playable on the item | P1 |

## Parse & classify

| ID | Use case | Pri |
|----|----------|-----|
| UC8 | Transcribe speech to text | P0 |
| UC9 | Classify as task / note / person-note | P0 |
| UC10 | Extract due date-time from natural language | P0 |
| ~~UC11~~ | ~~Infer project or tag from content~~ — **dropped** by the owner, not a technical limit. `project_hint` stays returned-but-unstored. | — |
| UC12 | Set initial state — active if timed, shelved if not | P0 |
| UC13 | Detect reference to an existing item; update, don't duplicate | P2 |
| UC14 | Flag critical from spoken cues | P1 |

## State transitions

| ID | Use case | Pri |
|----|----------|-----|
| UC15 | Mark done from a notification action | P0 |
| UC16 | Mark done in the app | P0 |
| UC17 | Snooze a reminder | P0 |
| UC18 | Auto-shelve after N ignored or snoozed pushes | P0 |
| UC19 | Auto-drop after M days shelved and untouched | P1 |
| UC20 | Reactivate a shelved item — an in-app action on the shelf, **not** a spoken re-mention | P1 |
| UC21 | Manually move an item between any two states | P0 |
| ~~UC22~~ | ~~Announce every automatic transition~~ — **dropped** by the owner, not a technical limit. Decay now happens silently; see the note under "Dropped scope". | — |

## Reminders & delivery

| ID | Use case | Pri |
|----|----------|-----|
| UC23 | Push notification at due time | P0 |
| UC24 | Full-screen alarm that breaks DND for critical items — **deferred** pending real usage; not cancelled | — |
| UC25 | Reminder spoken in a known person's recorded voice | P2 |
| UC26 | Voice call for must-not-miss items | P2 |
| UC27 | *(superseded by UC43)* | — |
| UC28 | Escalation ladder: push → alarm → call | P2 |
| ~~UC29~~ | ~~Quiet hours; nothing fires overnight except critical~~ — **dropped** by the owner, not a technical limit. Pushes may fire overnight. | — |

## Review

| ID | Use case | Pri |
|----|----------|-----|
| UC30 | Weekly review as a swipe deck — four directions, four states. **Bounded to what carries a decision**: what decayed this week and what is about to drop (owner's decision, 24 August 2026, D50) | P1 |
| UC31 | Weekly digest of what decayed and what's about to drop — **and what you completed and what was dropped**, as counts with expandable lists (widened by the owner 24 August 2026, D50). Those two are summary, not decisions, and never become cards | P1 |
| UC32 | Daily `Today` screen. The list proper stays bounded to due and overdue, and stays the thing that has to be finishable; below it a separate **`Later`** block holds active items dated further out, which before 24 August 2026 were on no screen at all (D56) | P0 |

## Retrieval

| ID | Use case | Pri |
|----|----------|-----|
| UC33 | Browse the shelf, grouped by project | P0 |
| UC34 | Text search across all items | P0 |
| ~~UC35~~ | ~~Natural-language query over notes~~ — **dropped** by the owner, not a technical limit. Retrieval is UC33 and UC34. | — |
| UC36 | Filter by state, project, date range | P1 |
| UC37 | Item detail with original audio | P1 |

## System

| ID | Use case | Pri |
|----|----------|-----|
| UC38 | Edit or correct a mis-parsed item | P0 |
| UC39 | Delete an item permanently | P0 |
| UC40 | Export all data | P2 |
| UC41 | Auth — single user, private | P0 |
| UC42 | Graceful failure: keep raw audio, flag for review | P0 |

## People

The second thing this system is for: remembering what was said about whom.
`entities` and `links` have existed since migration 001 for exactly this —
they were built early so that adding people later would be a write rather
than a backfill over every note ever captured (D7).

| ID | Use case | Pri |
|----|----------|-----|
| UC45 | Record a capture that names a person; the parse extracts who and links the item to them — **from every capture, not just `person_note`s** (widened by the owner 24 August 2026, D46), and a link can be added or removed by hand on item detail **or from the person page itself** (D58) | P1 |
| UC46 | Person page — every linked item **of any kind**, newest first (was "oldest to newest"; changed by the owner 24 August 2026 — the page is opened to see where things stand, and oldest-first buries that under the history) | P1 |
| UC47 | Browse and search people | P1 |
| UC48 | Merge two people — fold one into the other, their name becoming an alias | P1 |
| UC49 | Split a person — move some of their notes to another person, existing or new | P1 |

**Recall is manual, deliberately.** You go and look someone up. There is no
calendar triggering and no proactive surfacing — no "you are meeting Ravi in
an hour, here is what you said last time". That is a real idea and it is
explicitly deferred, not forgotten: it needs UC43's calendar link and a
delivery tier, and it should not be built before the manual version has been
used enough to know what is worth surfacing.

UC44 (the Obsidian-style linked graph) stays P2 and unscheduled. UC45-47 are
the practical first cut of the same data: the same tables, without the graph
UI.

**UC48 and UC49 are why the automatic matching is allowed to be imperfect.**
Added 24 August 2026, after UC45-47 shipped. With correction two taps from the
person page, a wrong resolution costs a gesture rather than being permanent, so
`resolve_entity` is deliberately left willing to guess rather than tuned to
refuse (D45). The owner judges identity; the machine only files.

**UC45 covers every capture, not just `person_note`s** (24 August 2026, D46).
"Call Priya about the invoice" is a task and a fact about Priya, and `kind` is
one value that cannot be both — the links table never asked. The same trade
follows: a wider net misses in both directions, so the link itself is
correctable by hand on item detail, which is where you are standing when you
notice it is wrong.

**And from the person page, which is the other place you notice** (24 August
2026, D58). A wrong link shows up as something on Priya's page that is not
about Priya; the repair used to be two screens away. It removes the link and
nothing else — the item, its words and its recording are untouched, and linking
back undoes it, so it does not confirm. The single exception is the last note on
somebody who goes by other names: emptying a person removes them, and their
aliases are corrections the owner made that relinking does not restore. That
one asks, and the server refuses it until it has been asked.

## Integrations

| ID | Use case | Pri |
|----|----------|-----|
| UC43 | Write to personal Google Calendar from the app | P1 |
| UC44 | Obsidian-style linked network of people and notes | P2 |

**UC43 is built** (24 August 2026) and is one-way by construction (D8). An
item projects an event while it has a due time and has not ended: `active` and
`shelved` both keep one, `done` and `dropped` take it down (D54). Nothing is
ever read back — an event deleted by hand inside Google Calendar is recreated
on the next tick (D55), and the way to take something off the calendar is to
complete or drop the item.

UC27 was folded into this and stays folded: a reminder that lands as a calendar
entry is this, and there is no second delivery tier hiding in it.

## Dropped scope

Five use cases were dropped on 23 August 2026. All owner decisions, none
technical:

| ID | Was | Consequence to be aware of |
|----|-----|----------------------------|
| UC6 | Offline capture queue | A capture made with no connection fails; the words stay in the box and the recording stays on the device, but sending is a manual retry |
| UC11 | Project inference | `projects` and `items.project_id` exist and stay empty; UC33's "grouped by project" becomes a flat list |
| UC22 | Announce every transition | Decay is silent — items shelve and drop without saying so |
| UC29 | Quiet hours | Nothing suppresses an overnight push |
| UC35 | Natural-language query | — |

**UC22 is the one with teeth.** "Every automatic transition is announced,
never silent" was a stated principle in `CLAUDE.md`, and dropping it means
UC18 and UC19 move items without telling anyone. The weekly digest (UC31)
becomes the only place decay is visible, which makes it load-bearing rather
than a nicety. `CLAUDE.md` has been updated to match; reversing this means
reversing that line too.

**Totals:** 43 active (UC48 and UC49 added 24 August 2026; UC27 folded into
UC43; UC6, UC11, UC22, UC29, UC35 dropped), of which 19 were P0 and 15 remain —
UC15, UC17, UC18 and UC23 are the outstanding ones. UC2 and UC24 are deferred
rather than dropped.
