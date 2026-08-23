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
