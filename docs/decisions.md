# Decisions

Append as you go. Each entry: what, why, and what would change it.

---

**D1 — Four states, not a priority system.**
`active / shelved / done / dropped`. Priority fields turn into a second
thing to maintain. State is derived from behaviour instead.
*Revisit if:* you find yourself wanting to rank within `active`.

**D2 — States change by decay, not by admin.**
Ignoring is a decision. The system reads it as one and shelves the item,
then tells you.
*Revisit if:* things you actually care about keep getting shelved. That
means `SHELVE_AFTER_IGNORES` is too low, not that the model is wrong.

**D3 — Native app, not a chat bot.**
A browsable place is a hard requirement, not a nice-to-have.
*Considered and rejected:* Telegram-only. Faster to ship, but no widget,
no full-screen alarm, and nowhere to browse.

**D4 — Expo / React Native over native Kotlin.**
Existing React fluency wins over the marginal native gain. Two thin
native modules cover the gaps (alarm, widget).
*Revisit if:* the alarm module fights you for more than a weekend.

**D5 — Haiku 4.5 for all model calls.**
The parse is extraction and classification. Larger models add cost, not
accuracy, on this task.

**D6 — Row written before the model call.**
Parsing is enrichment. A failed parse must never lose a capture.

**D7 — `entities` and `links` tables from migration 001.**
The graph UI is P2, but the tables are cheap now and a backfill later
is not.

**D8 — Calendar is one-way (app → Google).**
Two-way merge is where side projects go to die. The app owns the item;
the event is a projection.

**D9 — App opens to capture, never to a list.**
Opening to a list is what makes a system feel like a chore.

**D10 — Weekly review is a swipe deck, not a list.**
Four swipe directions map to the four states. A deck can be finished;
a list can't.

**D11 — Auth is the Supabase JWT, and it is the only source of `user_id`.**
The bearer token is verified with `SUPABASE_JWT_SECRET` (HS256) and the
`sub` claim *is* the user. `DEFAULT_USER_ID` is gone — a configured
fallback identity is a way to write rows as the wrong user and never
notice. Auth is enforced in middleware, so it is fail-closed: every new
route is protected unless it is added to `PUBLIC_PATHS`. `/health` is the
only entry there.
*Revisit if:* a route needs to be public for a reason other than liveness.

**D12 — `/docs`, `/redoc` and `/openapi.json` are off.**
Single-user private API. An unauthenticated endpoint that describes every
route is a gift to nobody.
*Revisit if:* a second client needs a generated schema — generate it from
the app object offline instead of serving it.

**D13 — `parse_status` starts at `failed` and is promoted to `ok`.**
The row is written before the model call (D6), so between the insert and
the parse the honest value is `failed`. Starting optimistic means a crash
mid-parse leaves a row that claims it was parsed. Fail-closed is cheaper
than an audit later (UC42).
*Revisit if:* `needs_review` gets a real use — it's currently unused, and
belongs to low-confidence transcription, not to failed parses.

**D14 — The parse's cleaned text gets its own column.**
`parsed_text` (migration 002), not an overwrite of `raw_text`. The raw
capture is what UC38 edits against and UC34 searches; rewriting it means
the user can never see what they actually said. `Today` and the review
deck display `parsed_text` and fall back to `raw_text`. `project_hint` and
`entities` are still returned-but-not-stored — those need UC11 and UC44.
*Revisit if:* the fallback is never exercised, i.e. parse failures are rare
enough that a null `parsed_text` is dead code.

**D15 — Relative dates resolve against `TZ`, set to `Asia/Kolkata`.**
The server runs in UTC and the user does not; resolving "tomorrow 3pm" in
server time was silently five and a half hours wrong. `TZ` is read from
`.env` (`CAPTURE_TIMEZONE` still works as an alias) and handed to the model
as the reference time.
*Revisit if:* captures start happening in more than one timezone — then the
device should send its offset and the setting should go away.

---

## Open

**O1 — `SHELVE_AFTER_IGNORES`.** Default 3. After a month, query
`transitions` for items that were shelved by decay and later
reactivated — a high rate means the number is too low.

**O2 — `DROP_AFTER_DAYS`.** Default 90. Same method: how often do you
resurrect something from the shelf after 60+ days?

**O3 — Echo-back on capture.** Should the app confirm what it
understood ("Got it — call insurance, Tuesday 3pm") or stay silent and
trust the parse? Echo catches errors but taxes every single capture.
Leaning silent, with a correction affordance in `Today` instead.

**O4 — Hinglish transcription quality.** On-device recognition may
struggle with code-switching. Measure the real failure rate in Phase 1
before deciding whether the cloud fallback becomes the primary.
