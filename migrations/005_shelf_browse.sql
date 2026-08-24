-- 005_shelf_browse.sql — the indexes the Shelf screen reads through
-- (UC33 browse, UC34 search, UC36 filter).
--
-- Two gaps, both of which only show up once the table has grown past the
-- point where a sequential scan is invisible. This table only grows.
--
-- 1. Search reads both texts. Migration 001 indexed `raw_text` for UC34 and
--    migration 002 then added `parsed_text` without one, so half of every
--    search — and it is the half that is actually displayed — was a scan.
--    `raw_text OR parsed_text` over two GIN indexes is a BitmapOr, which is
--    what we want; a single index over the concatenation would be one scan
--    but would have to be rebuilt on every edit to either column.
--
-- 2. Browse is keyset-paginated on `(created_at, id)` across every state.
--    The `items_due_idx` and `items_shelf_idx` from 001 are partial — one
--    state each — so neither can serve a list that spans shelved, done and
--    dropped at once, let alone all four when a search is running.
--
-- Trigram search needs at least three characters to use an index at all; a
-- one- or two-letter query falls back to a scan by design. The API declines
-- to search on fewer than two characters rather than pretending otherwise.

create index if not exists items_parsed_text_idx
  on shelf.items using gin (parsed_text gin_trgm_ops);

-- Ordered to match the browse query exactly, so the sort is read off the
-- index rather than performed. `id` is in the key as the tiebreak that makes
-- the cursor total: two items captured in the same millisecond would
-- otherwise be an unstable boundary, and a page could repeat or skip one.
create index if not exists items_browse_idx
  on shelf.items (user_id, created_at desc, id desc);

comment on index shelf.items_parsed_text_idx is
  'UC34 — search reads raw_text and parsed_text together; 001 indexed only the first.';
comment on index shelf.items_browse_idx is
  'UC33/UC36 — keyset pagination over every state, which the partial state indexes cannot serve.';
