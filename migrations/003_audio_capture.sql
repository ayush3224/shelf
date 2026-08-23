-- 003_audio_capture.sql — voice capture, retained audio, split siblings
--
-- UC1 (record from the app), UC7 (audio retained and playable), UC4 (one note
-- containing several items), UC42 (a failed transcription or parse must never
-- lose the capture).
--
-- `audio_path` already exists from 001; what was missing is any record of how
-- the words were produced.

-- Which path produced the words. Written on every row — including 'none' for
-- typed captures, so that "never transcribed" and "transcribed on-device" are
-- not the same value. Only 'cloud' is produced today (D20); the column exists
-- so that adding a second path later is a write, not a backfill.
create type shelf.transcript_source as enum ('on_device', 'cloud', 'none');

alter table shelf.items
  add column if not exists transcript_source shelf.transcript_source
    not null default 'none';

-- Whisper's per-segment average log-probability, collapsed to one number in
-- [0,1]. Null when the transcriber gave no confidence signal, or when nothing
-- was transcribed. Low values are what promote a row to `needs_review` — the
-- use D13 reserved for this status and never had.
alter table shelf.items
  add column if not exists transcript_confidence real;

comment on column shelf.items.transcript_source is
  'Which path produced raw_text: on_device, cloud, or none.';
comment on column shelf.items.transcript_confidence is
  'Transcriber confidence in [0,1], or null if it reported none.';

-- A split note (UC4) writes one row per item, all sharing the audio_path of
-- the single recording they came from — that shared key *is* the grouping, so
-- it needs to be worth looking up. Partial: typed captures have no audio.
create index if not exists items_audio_idx on shelf.items (audio_path)
  where audio_path is not null;
