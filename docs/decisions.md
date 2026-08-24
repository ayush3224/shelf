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
*Resolved:* `needs_review` now has the use this predicted — low-confidence
transcription, not failed parses. See D22.

**D14 — The parse's cleaned text gets its own column.**
`parsed_text` (migration 002), not an overwrite of `raw_text`. The raw
capture is what UC38 edits against and UC34 searches; rewriting it means
the user can never see what they actually said. `Today` and the review
deck display `parsed_text` and fall back to `raw_text`. `project_hint` and
`entities` are still returned-but-not-stored. `project_hint` stays that way
(UC11 dropped); `entities` gets stored by UC45.
*Revisit if:* the fallback is never exercised, i.e. parse failures are rare
enough that a null `parsed_text` is dead code.

**D15 — Relative dates resolve against `TZ`, set to `Asia/Kolkata`.**
The server runs in UTC and the user does not; resolving "tomorrow 3pm" in
server time was silently five and a half hours wrong. `TZ` is read from
`.env` (`CAPTURE_TIMEZONE` still works as an alias) and handed to the model
as the reference time.
*Revisit if:* captures start happening in more than one timezone — then the
device should send its offset and the setting should go away.

**D16 — The app never touches Postgres; everything goes through the API.**
Supabase's client is used for auth and nothing else. RLS would make direct
reads safe, but it would not make them *correct* — the state machine, the
`Today` bound and the `transitions` log all live in the API, and a second
writer is how they drift apart.
*Revisit if:* a read is genuinely display-only and the round trip hurts.

**D17 — `Today` is bounded server-side, at the end of the user's day.**
`GET /items/today` returns `active` items with `due_at` before local midnight,
oldest first. The client does no filtering of its own. A bound the client owns
is a bound that widens quietly, and a `Today` that has stopped being finishable
is the design failing (constraint 3).
*Consequence:* something due at 00:20 is not on today's list at 23:50. Surfacing
it at the due moment is UC23's job, not the list's.

**D18 — The session lives in the device keystore, chunked.**
`expo-secure-store` writes to the Android Keystore / iOS keychain, and the
platforms reject large values — a Supabase session with two JWTs clears the
historic ~2KB iOS limit. The storage adapter splits values across numbered keys
and writes the manifest last, so a torn write reads as the old value rather
than a corrupt one. Storing the session unencrypted was the alternative, and
it is a bearer token for the whole API.
*Revisit if:* the session shrinks enough that chunking is dead code.

**D19 — The split re-prompt gets its own, larger token budget.**
`MAX_PARSE_TOKENS` stays at 200 and the common path stays one call. A split
(UC4) returns an array, which does not fit in 200, so `parse_split` is capped
at `MAX_SPLIT_TOKENS` (600) and `MAX_SPLIT_ITEMS` (10) instead. The cost rule
in `CLAUDE.md` is about the *per-capture* budget, and a second call only
happens when the first one sets `split` — a rare capture pays for itself. A
truncated split raises rather than writing half of one; the caller then keeps
the single item it already parsed.
*Revisit if:* `split` fires on captures that are not really multi-item. That is
a prompt problem, not a budget one, and the fix is the prompt.

**D20 — Record the file and transcribe in the cloud. No on-device recognition.**
Android's `SpeechRecognizer` transcribes live from the microphone and does not
produce a file. UC7 and UC42 both require the file — it is the only part of a
capture that cannot be reproduced — so a recorder has to run regardless, and
the microphone is effectively single-consumer: running both reliably across
devices is not something this project should be spending its time on. The one
module that does both, `expo-speech-recognition`, is published against SDK 56
and this app is on SDK 57, which is precisely the out-of-matrix native module
class that broke the EAS builds.
So: `expo-audio` records to `.m4a`, the file is uploaded, and the server
transcribes it. The cost was the objection — on OpenAI this was about
$0.90/month at 20 captures a day, most of the stated budget on its own and more
than the Haiku parse — and it was accepted as the price of a capture that
cannot be lost. D24 later moved the call to Groq and took it to roughly
$0.09/month, which removes the objection rather than answering it.
The API still accepts an on-device `transcript` field and skips the cloud call
when one is present, so a future native module is a client change and nothing
more.
*Revisit if:* transcription cost becomes the dominant line item, or a
maintained SDK-57 speech module appears.
*Consequence:* O4 was closed rather than answered — see the Open section and
D23. `transcript_source` is recorded on every row anyway (`on_device` /
`cloud` / `none`), so adding a second path later is a write rather than a
backfill.

**D21 — A failed audio upload fails the request.**
`POST /capture/audio` stores the recording before it writes the row, and
returns 503 if the store refuses. The alternative — save the row, drop the
audio, report success — is the one lie the capture path must not tell: the file
is still on the device at that moment, and a retry keeps it. Everything *after*
the upload degrades instead of failing: a failed transcription writes a row
with the audio and no words, and a failed parse writes one with the audio and
the words (UC42). If the row itself will not write, the orphaned object is
deleted, because an object no row points at is only a bill.
*Revisit if:* UC6 lands an offline queue — then the client owns the retry and
this becomes a queue-and-return.

**D22 — `needs_review` means "the words are doubtful", not "the parse failed".**
D13 reserved the status and left it unused. It now has its use: a cloud
transcript below `TRANSCRIPT_CONFIDENCE_FLOOR` is parsed normally and then
flagged, and `Today` marks those rows. A failed parse is still `failed`. The
two are different problems — one needs the text corrected, the other needs the
parse redone — and collapsing them would lose which.

**D23 — Transcription language is pinned to English, not detected.**
The user speaks only English. Whisper's language detection is a guess made
from the first seconds of audio, and a wrong guess does not error — it returns
fluent, confident nonsense in the language it picked. Sending `language=en`
removes that failure mode entirely, and costs nothing.
The confidence floor moves with it, 0.55 → 0.75. The low value existed to
avoid flagging every code-switched capture as doubtful; with a single known
language Whisper sits well above 0.75 on clean speech, so a score below it now
means bad audio — noise, distance, a clipped recording — which is exactly what
`needs_review` should catch (D22).
*Revisit if:* captures start happening in another language. Clearing
`GROQ_LANGUAGE` restores detection, and the floor must come back down with it
or every non-English capture gets flagged.

**D24 — Whisper is served by Groq, not OpenAI, on `whisper-large-v3-turbo`.**
Same model family, same request shape. OpenAI's `whisper-1` is $0.36/hour of
audio; Groq serves `whisper-large-v3` at $0.111/hour and
`whisper-large-v3-turbo` at $0.04/hour — and Groq's free tier covers this
volume outright, so at ~20 captures a day the bill is zero either way.
That makes the model choice latency, not cost, and turbo is the faster of the
two. `GROQ_MODEL=whisper-large-v3` switches to the more accurate one if the
transcripts ever disappoint.
Two limits worth knowing, neither of which binds here:
- **Groq bills a 10-second minimum per request.** Most captures are shorter, so
  per-capture cost never falls below 10 seconds. Irrelevant while the free tier
  covers the volume; it would matter on a paid tier with very short, very
  frequent captures.
- **The free tier caps uploads at 25MB.** `MAX_AUDIO_BYTES` is 10MB, matched to
  the storage bucket, and `MAX_RECORDING_MS` caps a capture at two minutes —
  under 2MB of AAC. The bucket is the binding limit, not Groq.
The endpoint is OpenAI-compatible, so `GROQ_API_BASE` is the only thing naming
the provider; moving again is a one-line change. The env vars were renamed
`WHISPER_*` → `GROQ_*` so the key says whose it is.
*Revisit if:* turbo's accuracy disappoints on real captures, or the volume
outgrows the free tier.

**D25 — A rate-limited transcription is retried, not surrendered.**
A 429 is the transcriber saying "later", not "no". Giving up on the first one
is survivable — the row and the audio are already written, so the capture
survives with `parse_status = 'failed'` and no words (UC42) — but words are the
point, and a few seconds of backoff usually buys them.
Three attempts, full-jitter exponential backoff from 1s, honouring `retry-after`
when the server sends a sane one. Retried: 429, 408, 5xx, and dropped
connections — all the same transient class. Not retried: 400, 401, 413, 415 and
friends, which will say the same thing twice, and a 200 carrying non-JSON, which
is a broken host rather than a busy one.
The whole call is bounded by a 75s deadline, under the app's own 90s upload
timeout, and a retry is only taken if enough of that budget remains for the
attempt to actually finish. Retrying past the client's timeout would convert a
recoverable failure into one the app had already stopped waiting for: the audio
would still be safe, but the user would be told the server was unreachable
rather than that the words did not come.
*Revisit if:* 429s become common rather than incidental. That is the free tier
being outgrown, and the answer is a paid tier, not more retries.

**D26 — The multipart file part is an `expo-file-system` `File`, not React
Native's `{uri, name, type}`.**
Expo installs its own WinterCG `fetch` over the global
(`expo/src/winter/runtime.native.ts` — `install('fetch', ...)`, unless
`EXPO_PUBLIC_USE_RN_FETCH` is set). Its multipart encoder accepts a part only
if it is a string, a `Blob`, or an object exposing `bytes()`; anything else
throws `Unsupported FormDataPart implementation` before the request is
dispatched. Expo's own source says it plainly: "`uri` is not supported for
React Native's FormData".
So the RN file-part shape — which is correct, and only correct, for RN's
XHR-based `fetch` — silently could not be sent. `File` satisfies the encoder
directly: `bytes()` supplies the body, `name` and `type` become the part's
`filename` and `content-type`, and it still streams from disk.
*Consequence for tests:* asserting what the client passes to `FormData.append`
proves nothing, because the failure is in what the *encoder* will accept. The
body is now tested by running it through `convertFormDataAsync` itself.
*Revisit if:* the app ever sets `EXPO_PUBLIC_USE_RN_FETCH=1`, which restores
RN's fetch and inverts this — then the RN shape becomes the correct one.

**D27 — The confidence floor is 0.5, calibrated on real recordings.**
The 0.75 floor was set from synthetic speech — clean, close-miked, no room.
Real captures from a phone land at **0.70-0.76 while being word-perfect**, so
half of them were arriving flagged `needs_review` for no reason, which makes
the flag noise and teaches the user to ignore it.
0.5 keeps the signal for what `needs_review` is actually for — mumbling,
distance, a clipped recording — without penalising ordinary conditions.
*Lesson worth keeping:* a threshold calibrated on generated audio is
calibrated on the wrong distribution. This one should have started permissive
and tightened against real data, not the reverse.
*Revisit if:* genuinely bad captures start landing above 0.5, or the recording
setup changes.

**D28 — The stored format comes from the file's bytes, not its labels.**
Real captures were being stored as `.mp3` with `audio/mpeg`. They were not
MP3s: the bytes are `ftyp`/`mp42`, i.e. AAC in an MPEG-4 container, exactly
what the recorder writes. Android's `MimeTypeMap` maps the `m4a` extension to
`audio/mpeg` (AOSP lists `m4a` under that type), `expo-file-system` derives
`File.type` from that lookup, and `extension_for` trusted the declared type
over the filename. The bucket allows `audio/mpeg`, so nothing errored — the
file just lied about itself from then on.
Resolution order is now: **the bytes, then the filename, then the declared
content type.** The container is not a matter of opinion, so it is read rather
than asked about; the filename is next because whoever wrote the file knew
what they wrote; the declared type is last precisely because it is the one
that was wrong.
*Why it mattered even though nothing broke:* playback served `audio/mpeg` for
an MP4, and Groq received a `.mp3` filename for AAC. Both worked only because
ExoPlayer and ffmpeg sniff content rather than trusting labels — the same
thing the server now does, instead of relying on everyone downstream to
compensate.
*Revisit if:* a format arrives that the sniffer does not recognise; it returns
None rather than guessing, and the filename still carries it.

**D29 — An edit re-derives state, except from a terminal one.**
UC12 says `due_at` decides the state, and the commonest thing UC38 repairs is a
parse that missed a time — so supplying that time and leaving the item shelved
would be the wrong answer to the correction the user just made. Clearing the
time shelves it again, symmetrically.
`done` and `dropped` are exempt. An edit to the wording of a finished item
should not un-finish it; resurrecting something is a decision, and UC21's chips
are where decisions get made. Any move an edit does cause is logged with reason
`manual` — a state change nobody recorded is a hole in the data O1 and O2 get
tuned from.
*Revisit if:* corrections start being made mostly on terminal items, which
would mean the review deck is surfacing the wrong things.

**D30 — Delete removes the row first, the object second.**
A failed object delete leaves storage to pay for. A failed row delete would
leave an item whose recording is already gone — and "the audio is never lost"
(UC42) is the promise the rest of the system is built on, so that is the worse
state to risk. The object delete is best-effort and never raises: the row is
already gone by then, and failing the request would report a delete that did
happen as one that did not, sending the user to try again on nothing.
`transitions` rows cascade with the item. That loses a little of the history
the decay constants are tuned from, but keeping an audit trail for something
the user asked to erase is not what "delete permanently" means.

**D31 — Finishing an item moved from the row to the check circle.**
Tapping anywhere on a `Today` row used to mark it done. The row title is now
the way into the detail screen, and a row that both finishes and navigates
cannot do either predictably — so `done` is the circle, the title opens, and
the play button plays. Three targets, each with its own affordance.
This costs UC16 a little precision, which is why the circle carries a generous
`hitSlop`. It buys UC38 and UC39 a way in that does not need a long-press or a
swipe to discover.
*Revisit if:* finishing an item starts feeling fiddly in daily use — the answer
would be a swipe for done, not putting navigation back on the whole row.

**D32 — A push that did not go out can never decay an item.**
`sent_at` is written only when the push service has accepted the message, and
the ignore sweep only reads silence from rows that carry one. Everything that
can go wrong on the way — Expo down, no device registered yet, a token the OS
has retired — records itself in `attempts` and `last_error` and leaves
`sent_at` null.
The consequence is chosen rather than tolerated: after `PUSH_MAX_ATTEMPTS` a
queued push **stalls**, and the item stops being reminded about. That is the
failure mode to want. The alternative — mark it sent and move on — means the
system shelves things for its own outage and tells the user nothing, which is
the exact opposite of "silence is signal": it is the system inventing silence
on the user's behalf. A stall is loud in the log and costs nothing but a
missed reminder.
*Revisit if:* stalls happen for any reason other than a genuinely dead device.

**D33 — `PUSH_REPEAT_MINUTES` is the real speed of decay, and it is 60.**
*Superseded on 24 August 2026 by D40, which raises it to 240. The first half of this entry — that the constant exists at all and that it is what sets the pace — still stands; only the number moved.*
`docs/data-model.md` said an `ignored` row is written "when the next push comes
due with no response to the previous one" — which quietly assumes a repeat
interval that no constant had ever named. It is now `PUSH_REPEAT_MINUTES`, one
hour, and it does double duty: it is how long an unanswered push waits before
the next one, and therefore how long silence takes to become an ignore.
With `SHELVE_AFTER_IGNORES` at 3 this means an item nobody touches is pushed at
the due moment, an hour later, and an hour after that, and is on the shelf
about two hours after it fell due. That is fast. It is deliberately fast: the
whole bet is that repeated non-response is a decision, and a threshold slow
enough to be polite is one that never fires. It is also a guess, which is why
it joins O1 and O2 as something to tune from `transitions` rather than from
opinion (O5).
A snooze counts the same as an ignore, and answers the push rather than being
left to time out — so `push_count` stays "declined by silence" and
`snooze_count` stays "declined out loud", and the threshold reads the sum.
*Revisit if:* things you care about are shelved before the day they were due
is over. That means an hour is too short, not that the model is wrong (D2).
*That is exactly what happened, on inspection rather than in the field — see
D40.*

**D34 — Both notification actions open the app.**
`expo-notifications` documents the cost of the quiet version plainly: with
`opensAppToForeground: false`, a response given while the app is *killed*
reaches no listener at all. A Done button that silently does nothing after a
reboot is worse than one that flashes the app open for half a second, because
the first teaches you not to trust the button and the second only annoys you.
So Done and Snooze both foreground the app, act, and land on `Today` — where
the row being gone is the confirmation. A failure, or an item that has moved on
since the push, gets a dialog; success gets none.
Answering without leaving the shade needs a native background handler. That is
the same class of work as UC24's alarm and waits for the same evidence: daily
use showing it is worth it.
*Revisit if:* the flash of the app opening is the thing that stops you using
the buttons.

**D35 — Reactivating resets the decay count and settles a due time.**
Two things that look like details and are not.
The **count reset** is what makes UC20 an escape hatch at all: an item brought
back carrying the three ignores that shelved it re-shelves on its first push,
and the user has no way to keep it. It is done by trigger (migration 004)
rather than at the call site, because the scheduler, UC20, UC21 and an edit
that re-derives state (D29) all move items into `active`, and a rule enforced
in four places is a rule that will be missed in a fifth.
The **due time** is what makes it visible: `Today` is bounded on `due_at` (D17)
and the scheduler only pushes what is due, so `active` with no time is a state
nothing in this app would ever surface again. Reactivating without one sets it
to now; a time still in the future is left alone, and a time in the past is
replaced, because the point of reactivating is that you want it now and not
that you want to be told it was overdue in March.
Every route into `active` is *one implementation*: `set_state` (UC21's chips)
hands the move to `reactivate_item` rather than doing it itself. That was not
tidiness — on its own, the chip left `due_at` null and produced exactly the
invisible item described above. Crossing `shelved → active` is therefore
logged as `reactivation` whichever control asked for it, which matters because
that edge *is* the evidence O1 is tuned from and `manual` would hide it among
the ordinary corrections. Un-finishing a `done` item is not that, and stays
`manual`.

**D36 — The one-minute tick is a systemd timer, not Railway cron.**
`CLAUDE.md` says "Railway cron, 1-minute tick". The API has never actually run
on Railway — it is FastAPI under systemd behind Caddy on a VPS — so the tick is
`shelf-tick.timer` calling `python -m backend.scheduler`, once a minute, as a
`oneshot`. Same idea, in the form this host has.
A separate process rather than a loop inside the API: a tick that shares the
web process shares its restarts and its memory, and cannot be run by hand
against the real database while the API is up, which is exactly what it needed
to be during this session. systemd will not start a second copy of a unit that
is still running, so overlapping ticks are not a case that has to be handled.
*Revisit if:* the deployment ever moves to Railway, at which point this becomes
a cron entry and nothing else changes.

**D37 — "Untouched" on the shelf includes being edited.**
UC19 drops a `shelved` item after `DROP_AFTER_DAYS`. `docs/data-model.md` said
the clock was `state_changed_at`; the rule is now the later of
`state_changed_at` and `updated_at`.
The difference matters for exactly one case, and it is the case that would
hurt: an item you shelved in March and corrected the wording of last week is
one you were demonstrably still thinking about, and throwing it away on the
90th day after the shelving would be the system ignoring the clearest possible
evidence to the contrary. Nothing writes to a shelved row on its own, so this
only ever moves in response to a person.

**D38 — The Shelf is ordered by capture time, not by decay time.**
UC33's rows are sorted on `created_at` — when you *said* it — rather than on
`state_changed_at`, when the system last moved it.
Both were defensible and the tie was broken on what the screen is *for*. Decay
order puts whatever was most recently taken away from you at the top, which
makes the Shelf a feed of things the system has been quietly deciding against
you: a backlog with a running total. Capture order makes it an archive of
things you have said, which is the reading the screen is supposed to have, and
it is also the clock a person can actually search against — nobody remembers
when an item decayed, everybody remembers roughly when they said it. The date
filter (UC36) bounds the same column for the same reason, so one clock governs
the whole screen.
*Consequence:* an item that decayed onto the shelf this morning after being
captured a year ago sits a year down the list. That is correct here, and the
place decay is *meant* to become visible is the weekly digest (UC31) — which
UC22's dropping already made load-bearing.

**D39 — The Shelf pages by keyset, from the first commit.**
`GET /items` takes an opaque cursor holding the last row's `(created_at, id)`,
never an offset.
It was cheap to do now and expensive to retrofit, and offset paging is wrong
here in a way that shows up as a bug report rather than as slowness: a capture
landing while you scroll shifts every row down by one, so the item at the page
boundary is served twice and the one after it is never served at all. The `id`
in the key is not decoration — a split (UC4) writes several rows in the same
transaction with the same timestamp, so `created_at` alone is not a total order
and is exactly where that duplication would happen.
*Consequence:* there is no "jump to page 7" and no total count, and the screen
does not offer either. `has_more` comes from fetching one row past the limit
rather than from a second counting query.

**D40 — `PUSH_REPEAT_MINUTES` is 240, not 60.**
Raised from one hour to four on 24 August 2026, which moves a shelving from
about two hours after the due time to most of a working day.
D33 set it to an hour and argued that fast was deliberate: a threshold slow
enough to be polite never fires. The argument holds; the number did not follow
from it. At 60, three ignores fit comfortably inside one long meeting — the
item falls due, is pushed while you are in a room you cannot answer from,
pushed again, pushed a third time, and is on the shelf before you come out. The
system reads that as a decision, and it is not one: **a single unavailable
stretch is an interruption, not a pattern of avoidance.** What UC18 is supposed
to detect is the second thing, and telling them apart is exactly what the
interval is for.
At 240 the same three ignores span twelve hours. An item due at 9am is shelved
at 9pm, having been offered at 9, 1 and 5 — three genuinely separate chances,
across a whole day, each in a different part of it. Declining all three is
recognisably avoidance rather than a busy afternoon.
*What this does not change:* the direction, or D2. Silence is still signal and
still acts on its own; this is a claim about how much silence is enough to be
sure, not about whether silence counts.
*Cost:* an item now sits `active` for up to twelve hours after you have in
practice ignored it, and `Today` carries it for that whole time. That is the
trade, and it is the right way round — `Today` showing something for an extra
few hours is recoverable, and a thing shelved out from under you during a
meeting is the failure the whole design is trying not to have.
*Revisit if:* `transitions` shows decay-shelved items being reactivated by hand
at any real rate (O5), which would mean four hours is still too short — or if
nothing ever decays at all, which would mean it is now too long.

**D41 — The tab bar's height is the inset plus a constant, never a literal.**
`app/(tabs)/_layout.tsx` set `tabBarStyle.height` to `60`. It is now
`BAR_CONTENT_HEIGHT + insets.bottom`, read from `useSafeAreaInsets()`.
The literal made every tab in the app unreachable on the device, from the app's
first commit until 24 August 2026, and nothing anywhere reported it.

React Navigation's `getTabBarHeight` adds the bottom safe-area inset — but only
on the branch where it computes the height itself:

```js
const customHeight = 'height' in flattenedStyle ? flattenedStyle.height : undefined;
if (typeof customHeight === 'number') return customHeight;  // literal: inset NOT added
return TABBAR_HEIGHT_UIKIT + inset;                          // default: inset added
```

It then applies `paddingBottom: insets.bottom` to the container regardless, and
our `tabBarStyle` — merged last — did not override it. On a phone with gesture
navigation that leaves `60 - 6 - 48 = 6dp` of content box for a 13px label:
a strip of `#FFFFFF` a hairline tall against a `#FBFAF8` page. Not a missing
tab bar — an invisible one. `Today` and `Shelf` were both unreachable, and the
only reason it went unnoticed for two days is that everything tested until now
was reachable from the capture screen or from a notification.

*Why no test caught it.* In a test the insets are zero, so `60 - 6 - 0 = 54`
and the same code is correct. This is the shape of bug that survives a green
suite: **the environment supplies the broken input, and the environment is the
one thing the test replaces.** `__tests__/tab-bar-insets.test.tsx` now renders
the real tree behind a 48dp inset and asserts the label has room; it reports 6
against the old code.

*The rule.* A safe-area inset is not space a component may spend. Anything that
sets its own height next to one adds the inset to a constant rather than
subtracting it from a total — and if a component cannot say what its height is
without knowing the inset, it should not be setting a height at all.
*Revisit if:* never, in this direction. If the bar needs to be a different
size, `BAR_CONTENT_HEIGHT` is the number to change.

**D42 — A container that holds text sizes to the text. Twice now.**
The Shelf's filter chips were cut off mid-label on the device. Same family as
D41, different mechanism, and the second one in two days — so this is written
as a rule rather than a fix.
There was no literal height here. React Native's `ScrollView` defaults to
`flexGrow: 1, flexShrink: 1` (`styles.baseHorizontal`), and the Shelf stacks
three of them in a column beside a `SectionList`, which is a fourth. All four
are willing to give up height, so when the page's content exceeds the screen
they shrink *proportionally* — and the chip rows, holding the least, lose their
labels first. Fixed with `flexGrow: 0, flexShrink: 0`: sizing to content is not
the default, it has to be asked for.
*The audit this prompted.* Every fixed height in `app/` and `lib/` was checked.
Three are circles — the mic button, the play button, the done circle — where a
fixed `width` and `height` with a matching `borderRadius` is the shape itself,
and those stay. Two were text in a box: the primary buttons on the capture and
sign-in screens, `height: 52` around a 16px label. Not visibly broken at the
default font scale, and clipped the moment the system font size is raised. Both
are now `minHeight: 52` with vertical padding — the tap target as a floor, with
the label free to push past it.
*The rule.* A fixed height is for a shape. If a box contains text, its height is
an outcome: `minHeight` plus padding, never `height`. And if a box contains text
*and* has flexible siblings, it also has to say it will not shrink.
*Why the suite kept missing these.* Both bugs are layout, and jest does no
layout. The tests added for D41 and D42 assert the *inputs* to the layout —
that the inset is added, that the rows do not shrink — because that is the
honest limit of what a renderer without a layout engine can check. It catches a
regression; it would not have caught either bug from scratch. Only a phone does
that, which is the real conclusion of both entries.

**D43 — A name resolves to a person only when exactly one thing matches.**
`resolve_entity` (in `backend/db.py`) turns a name the parse produced into an
`entities` row: the same name, then a recorded alias, then a token subset in
either direction — and **only when exactly one candidate matches** at that step.
The asymmetry is the whole design. A wrong *merge* is silent: what you said
about Priya Nair is filed on Priya Sharma's page, and nothing on any screen
ever looks odd, so you find out by being wrong about somebody in person. A
wrong *split* is two Priyas in a list you are looking at. So when the rule is
uncertain it splits, and an unresolved name gets its own row rather than
linking to nothing — the note has to be findable under some name.
A fuller name **promotes**: a row called "Priya" that meets "Priya Sharma" is
renamed and keeps "Priya" in `aliases`. That is what stops a rename orphaning
every mention filed under the shorter name, and it is why `aliases` is
load-bearing rather than decorative — UC47 searches it too, because the alias
is usually the name you actually remember.
*One deliberate exception:* an alias beats the subset rule even when the subset
rule would find the match ambiguous. Once "Priya" is an alias of Priya Sharma,
a later Priya Nair does not make bare "Priya" ambiguous again. An alias is a
resolution that already happened out of real usage; a subset is an inference
being made now. Letting one new namesake invalidate a binding built over a year
would make the system worse the longer it is used. The residual risk is O6.
*Known limit, not papered over:* "Sharma Priya" has the same tokens as "Priya
Sharma" but neither set is a *proper* subset of the other, so it becomes its own
row. Matching on token equality would fix that case and merge genuinely
different orderings elsewhere; this fails in the visible direction.
*Revisit if:* the People list starts filling with near-duplicates. That means
the subset rule is too strict, not that the direction is wrong.

**D44 — People gets the fourth tab, and that is the ceiling.**
UC47 is a tab rather than a screen inside the Shelf.
People is a second *index* over the same items, not a subset of the Shelf. You
go to the Shelf when you remember roughly *when* you said something and to
People when you remember *who*, and neither list contains the other — a
person-note that is due today appears under People and never on the Shelf,
which excludes `active` by definition. Nesting it would imply a containment
that is not true and would put a tap in front of the one flow whose entire
justification (manual, deliberate recall) is being quick.
*What this costs.* The bar is now one capture surface and three retrieval ones,
which is the wrong balance for an app whose bet is that capture stays cheap.
Four is therefore the ceiling, and it is written down in
`app/(tabs)/_layout.tsx` as well as here. If a fifth ever wants in the answer is
not a fifth tab: fold `Shelf` and `People` into one find-it screen with a scope
toggle, because both are the same act — searching your own history — indexed by
time in one case and by person in the other.
*Revisit if:* a fifth retrieval surface is proposed, or daily use shows People
being opened rarely enough that a tab is not paying for itself.

**D45 — Manual correction is the answer to a wrong resolution, so the
heuristic is deliberately allowed to guess.**
UC48 (merge) and UC49 (split) close O6. With both on the person page, the
automatic rules in D43 no longer have to be *right* — they have to be
**recoverable**, which is a much weaker and much more achievable property.
This changes what D43 is for, and D43 itself is unchanged on purpose. Its
rules — same name, then alias, then a unique token subset, and the
alias-beats-ambiguity asymmetry — all stay exactly as they are. The argument
for leaving them alone is that tuning a heuristic against cases you have not
seen yet is guesswork, whereas judging identity is something the owner can do
instantly and correctly for their own life. The right division of labour is
therefore: **the machine files, the person adjudicates.** A resolution that
guesses and is occasionally wrong is now strictly better than one that refuses
and is always incomplete, because the cost of a wrong guess has gone from
permanent to two taps.
*What this licenses.* Nothing new — but it means future proposals to make
resolution cleverer should be weighed against whether the correction path is
adequate, not against whether the guess is defensible. If merge and split cover
it, the guess is fine.
*Direction is fixed, not chosen.* The person whose page you are on survives a
merge. Making it a parameter would mean reasoning about direction mid-task,
which is how you get it backwards; "the page you are on stays" is a rule that
survives being half-remembered. To keep the other name, start from the other
page.
*Aliases follow the notes.* On a split, an alias of the source that names the
target stops being the source's. This is the difference between a correction
that holds and one that undoes itself: "Priya" became an alias of Priya Sharma
because a bare mention landed there, and if those notes move to a Priya of
their own while the alias stays, the next bare "Priya" resolves straight back
onto the row just corrected. The rule matches names only and deliberately does
not read the notes to infer which alias a note was responsible for — that is a
guess about identity, and identity is now the owner's call.
*Only the merge asks.* It removes a row. A split relocates mentions and is
undone by moving them back, so a dialog in front of it is ceremony rather than
a safeguard.
*Emptying a person removes them.* A split that moves every note leaves a name
with nothing behind it, which is clutter and not data — the notes are all still
there, on the other page.

---

## Open

**O6 — Correcting a person by hand. CLOSED, 24 August 2026.**
Answered by building it: UC48 merge and UC49 split, both on the person page
(D45). All three paths that could leave a row wanting reconciliation — a
declined ambiguous name, a name the subset rule cannot see as the same person,
and the residual risk of alias precedence — now have the same two-tap fix.
*What survives:* the question of whether the automatic rules should be tuned at
all. The answer is now "probably not" — with correction available they only
need to be recoverable, and the owner judges identity faster and better than a
heuristic can. *Reopen if:* correcting turns out to be frequent enough to be a
chore, which would mean the resolution rules are wrong in a way worth measuring
rather than a way worth shrugging at.

**O1 — `SHELVE_AFTER_IGNORES`.** Default 3. After a month, query
`transitions` for items that were shelved by decay and later
reactivated — a high rate means the number is too low.

**O2 — `DROP_AFTER_DAYS`.** Default 90. Same method: how often do you
resurrect something from the shelf after 60+ days?

**O5 — `PUSH_REPEAT_MINUTES`.** Now 240, raised from 60 on 24 August
2026 (D40, superseding D33). The constant nobody had named, and the one that
actually sets how fast decay runs.

*Why it moved before any data arrived.* This is still an open question and the
new number is still a guess — but the old one was answerable without data,
because it was wrong on its own terms rather than wrong by some margin only
usage could reveal. At 60, three ignores fit inside a single long meeting: an
item could fall due, be pushed three times into a room you cannot answer from,
and be shelved before you came out. UC18 exists to detect **repeated
avoidance**, and one stretch of being unavailable is not that. The interval is
precisely the mechanism that distinguishes them, so an interval shorter than a
meeting cannot do its job no matter what the data eventually says. At 240 the
three pushes span twelve hours — 9am, 1pm, 5pm for a thing due at 9 — and
declining all three is recognisably a pattern.

*What still needs data.* Whether four hours is itself right. Same method as O1
and O2: after a month, ask `transitions` how many decay-shelved items were
reactivated by hand, and how long after the due time they shelved. A high
reactivation rate is still more likely to be this number than
`SHELVE_AFTER_IGNORES` — but now the plausible error is in the other direction
too, and **nothing decaying at all** is the signal that four hours is too long.
The clock on collecting that evidence restarts here: the constant changed, so
transitions written before 24 August 2026 were produced under a different
pace and are not comparable.

**O3 — Echo-back on capture.** Should the app confirm what it
understood ("Got it — call insurance, Tuesday 3pm") or stay silent and
trust the parse? Echo catches errors but taxes every single capture.
Leaning silent, with a correction affordance in `Today` instead.
*Phase 1 ships the middle option:* the capture screen says where the item
landed ("Saved — it's on Today", "Saved to the shelf") but not what the model
thought it heard. That is state being announced, not the parse being echoed.
Still open, because the correction affordance it leans on is UC38 and is not
built yet.

**O4 — Hinglish transcription quality. CLOSED, 23 August 2026.**
Closed because the premise is gone, not because it was answered: the user
speaks only English, so there is no code-switching to transcribe and no
Hinglish quality to measure. The question was always "does recognition cope
with Hinglish, and should the cloud become primary" — with the language pinned
to English (D23) and the cloud path already primary (D20), neither half has
anything left to decide.
*What survives:* `transcript_source` and `transcript_confidence` are still
written on every row. They are no longer answering O4, but they are what would
show transcription quality degrading, and `needs_review` now means bad audio
rather than an unexpected language.
*Reopen if:* captures start happening in another language, or in more than
one.

**D46 — People are extracted from every capture, not only from
`person_note`s.** *24 August 2026, owner's decision.*
"Call Priya about the invoice" is a task *and* a fact about Priya. `kind` is a
single value and cannot be both, so leaving the link to depend on it meant one
of the two was always lost — the item was either a task you would never find
from Priya's page, or a note about Priya that never came due.

`links` never required that choice. An item now appears on the Shelf and on a
person's page at the same time, and a person's page shows every linked item
regardless of kind, naming the kind on the row when it is not a `person_note`.
The change is one line of the parse prompt and one line of the person row; the
schema was already right (D7).

*The cost is precision.* A wider net catches more and mis-catches more: a name
said in passing, a "Pansy" who turns out to be a cat, and every miss the model
was already making now spread over three times as many captures. That is
answered the way D45 answered it for merge and split rather than by tuning the
extraction — a link can be added or removed by hand on item detail, so a wrong
one costs a tap. Correctable beats correct.

*Manual linking is deliberately not `resolve_entity`.* That function resolves
what a model heard and guesses by token subset when the evidence is thin (D43).
A name a person typed is not a guess to improve on, so `link_person` matches
only where matching is not a guess — the same name whatever its case and
spacing, or a name already recorded as an alias. "priya sharma" is Priya
Sharma; "Priya" is not, unless she is on file as answering to it. A duplicate
that lands anyway is one merge away (UC48).

*Removing the last link removes the person*, the rule a split already follows
(UC49): a name with nothing behind it is clutter rather than data. Nothing said
is deleted either way — this corrects the filing, not the capture, and losing
the words is UC39 and a different button.

*Revisit if:* person pages fill with noise faster than the correction gesture
can clear it. That would mean the extraction needs a bar to clear, not that
`kind` should be deciding again.
