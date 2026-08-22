-- 002_parsed_text.sql — store the parse's cleaned one-line description
--
-- The parse contract (docs/data-model.md) returns a `text` field: the raw
-- capture with the filler stripped out. It had nowhere to land, so it was
-- being discarded on every capture. `raw_text` stays exactly as captured —
-- it is what UC38 edits against and what UC34 searches — and `parsed_text`
-- is what `Today` and the review deck should display.
--
-- Null means "not parsed, or parsed before this column existed". Callers
-- fall back to `raw_text`.

alter table shelf.items
  add column if not exists parsed_text text;

comment on column shelf.items.parsed_text is
  'Cleaned one-line description from the Haiku parse. Null if parse_status <> ''ok''. Falls back to raw_text for display.';
