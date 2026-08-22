# Use cases

Canonical list. Reference by ID in commits, PRs, and issues.

P0 = v1 · P1 = shortly after · P2 = later

## Capture

| ID | Use case | Pri |
|----|----------|-----|
| UC1 | Record a voice note from the app's home screen | P0 |
| UC2 | Record from a home-screen widget without opening the app | P1 |
| UC3 | Record from lock screen / quick-settings tile | P2 |
| UC4 | Speak several items in one note; system splits them | P1 |
| UC5 | Type an item instead of speaking | P0 |
| UC6 | Capture offline; queue and sync on reconnect | P1 |
| UC7 | Original audio retained and playable on the item | P1 |

## Parse & classify

| ID | Use case | Pri |
|----|----------|-----|
| UC8 | Transcribe speech to text | P0 |
| UC9 | Classify as task / note / person-note | P0 |
| UC10 | Extract due date-time from natural language | P0 |
| UC11 | Infer project or tag from content | P1 |
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
| UC20 | Reactivate by mentioning the item in a new note | P1 |
| UC21 | Manually move an item between any two states | P0 |
| UC22 | Announce every automatic transition | P1 |

## Reminders & delivery

| ID | Use case | Pri |
|----|----------|-----|
| UC23 | Push notification at due time | P0 |
| UC24 | Full-screen alarm that breaks DND for critical items | P1 |
| UC25 | Reminder spoken in a known person's recorded voice | P2 |
| UC26 | Voice call for must-not-miss items | P2 |
| UC27 | *(superseded by UC43)* | — |
| UC28 | Escalation ladder: push → alarm → call | P2 |
| UC29 | Quiet hours; nothing fires overnight except critical | P1 |

## Review

| ID | Use case | Pri |
|----|----------|-----|
| UC30 | Weekly review as a swipe deck — four directions, four states | P1 |
| UC31 | Weekly digest of what decayed and what's about to drop | P1 |
| UC32 | Daily `Today` list, bounded to due and overdue | P0 |

## Retrieval

| ID | Use case | Pri |
|----|----------|-----|
| UC33 | Browse the shelf, grouped by project | P0 |
| UC34 | Text search across all items | P0 |
| UC35 | Natural-language query over notes | P1 |
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

## Integrations

| ID | Use case | Pri |
|----|----------|-----|
| UC43 | Write to personal Google Calendar from the app | P1 |
| UC44 | Obsidian-style linked network of people and notes | P2 |

**Totals:** 43 active use cases (UC27 folded into UC43), 16 at P0.
