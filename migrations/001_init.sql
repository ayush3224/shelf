-- 001_init.sql — Shelf initial schema
-- Self-hosted Supabase. `public` is in use by another project, so
-- everything here lives in the `shelf` schema.
-- See docs/data-model.md for rationale.

create schema if not exists shelf;

create extension if not exists "pgcrypto";
create extension if not exists "pg_trgm";

-- ---------------------------------------------------------------- enums

create type shelf.item_kind    as enum ('task', 'note', 'person_note');
create type shelf.item_state   as enum ('active', 'shelved', 'done', 'dropped');
create type shelf.parse_status as enum ('ok', 'failed', 'needs_review');
create type shelf.item_source  as enum ('voice', 'text', 'widget');
create type shelf.entity_type  as enum ('person', 'org', 'place');
create type shelf.notif_tier   as enum ('push', 'alarm', 'call');
create type shelf.notif_response as enum ('done', 'snooze', 'ignored');
create type shelf.sync_state   as enum ('pending', 'synced', 'error');
create type shelf.transition_reason as enum
  ('manual', 'decay', 'expiry', 'reactivation', 'completion');

-- ------------------------------------------------------------- projects

create table shelf.projects (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  name       text not null,
  slug       text not null,
  created_at timestamptz not null default now(),
  unique (user_id, slug)
);

-- ---------------------------------------------------------------- items

create table shelf.items (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references auth.users(id) on delete cascade,
  kind             shelf.item_kind    not null default 'task',
  state            shelf.item_state   not null default 'shelved',
  raw_text         text               not null default '',
  audio_path       text,
  project_id       uuid references shelf.projects(id) on delete set null,
  due_at           timestamptz,
  critical         boolean            not null default false,
  push_count       int                not null default 0,
  snooze_count     int                not null default 0,
  parse_status     shelf.parse_status not null default 'needs_review',
  source           shelf.item_source  not null default 'voice',
  state_changed_at timestamptz        not null default now(),
  created_at       timestamptz        not null default now(),
  updated_at       timestamptz        not null default now()
);

-- the scheduler's hot path: what's due, what's decaying
create index items_due_idx on shelf.items (user_id, state, due_at)
  where state = 'active';
create index items_shelf_idx on shelf.items (user_id, state, state_changed_at)
  where state = 'shelved';
create index items_project_idx on shelf.items (project_id);

-- UC34 text search
create index items_text_idx on shelf.items using gin (raw_text gin_trgm_ops);

-- --------------------------------------------------------- transitions
-- Append-only. This is how the decay constants get tuned later (O1/O2).

create table shelf.transitions (
  id         uuid primary key default gen_random_uuid(),
  item_id    uuid not null references shelf.items(id) on delete cascade,
  from_state shelf.item_state,
  to_state   shelf.item_state not null,
  reason     shelf.transition_reason not null,
  created_at timestamptz not null default now()
);

create index transitions_item_idx   on shelf.transitions (item_id, created_at desc);
create index transitions_reason_idx on shelf.transitions (reason, created_at desc);

-- ---------------------------------------------- entities & links (UC44)
-- Built now, populated in phase 6. Backfilling these later means
-- re-parsing every note ever captured.

create table shelf.entities (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  type       shelf.entity_type not null default 'person',
  name       text not null,
  aliases    jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  unique (user_id, type, name)
);

create table shelf.links (
  id         uuid primary key default gen_random_uuid(),
  item_id    uuid not null references shelf.items(id) on delete cascade,
  entity_id  uuid not null references shelf.entities(id) on delete cascade,
  relation   text not null default 'mentions',
  created_at timestamptz not null default now(),
  unique (item_id, entity_id, relation)
);

create index links_entity_idx on shelf.links (entity_id);

-- --------------------------------------------------------- notifications

create table shelf.notifications (
  id            uuid primary key default gen_random_uuid(),
  item_id       uuid not null references shelf.items(id) on delete cascade,
  scheduled_for timestamptz not null,
  tier          shelf.notif_tier not null default 'push',
  sent_at       timestamptz,
  responded_at  timestamptz,
  response      shelf.notif_response
);

create index notifications_pending_idx
  on shelf.notifications (scheduled_for)
  where sent_at is null;

create index notifications_unanswered_idx
  on shelf.notifications (item_id, sent_at)
  where responded_at is null;

-- -------------------------------------------------- calendar links (UC43)
-- One-way: app writes to Google, never reads back.

create table shelf.calendar_links (
  item_id         uuid primary key references shelf.items(id) on delete cascade,
  google_event_id text,
  calendar_id     text,
  last_synced_at  timestamptz,
  sync_state      shelf.sync_state not null default 'pending',
  error_detail    text
);

create index calendar_links_retry_idx on shelf.calendar_links (sync_state)
  where sync_state in ('pending', 'error');

-- ------------------------------------------------------------- triggers

create or replace function shelf.touch_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger items_touch
  before update on shelf.items
  for each row execute function shelf.touch_updated_at();

-- state_changed_at drives the drop timer; keep it honest automatically
create or replace function shelf.touch_state_changed_at() returns trigger as $$
begin
  if new.state is distinct from old.state then
    new.state_changed_at = now();
  end if;
  return new;
end;
$$ language plpgsql;

create trigger items_state_touch
  before update on shelf.items
  for each row execute function shelf.touch_state_changed_at();

-- ------------------------------------------------------------------ RLS

alter table shelf.projects       enable row level security;
alter table shelf.items          enable row level security;
alter table shelf.entities       enable row level security;
alter table shelf.transitions    enable row level security;
alter table shelf.links          enable row level security;
alter table shelf.notifications  enable row level security;
alter table shelf.calendar_links enable row level security;

create policy own_projects on shelf.projects
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy own_items on shelf.items
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy own_entities on shelf.entities
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy own_transitions on shelf.transitions
  for all using (exists (
    select 1 from shelf.items i where i.id = transitions.item_id
      and i.user_id = auth.uid()));

create policy own_links on shelf.links
  for all using (exists (
    select 1 from shelf.items i where i.id = links.item_id
      and i.user_id = auth.uid()));

create policy own_notifications on shelf.notifications
  for all using (exists (
    select 1 from shelf.items i where i.id = notifications.item_id
      and i.user_id = auth.uid()));

create policy own_calendar_links on shelf.calendar_links
  for all using (exists (
    select 1 from shelf.items i where i.id = calendar_links.item_id
      and i.user_id = auth.uid()));

-- ------------------------------------------------- read-only view (UC35)
-- The only surface the text-to-SQL path is allowed to touch.

create view shelf.v_items_query as
  select i.id, i.kind, i.state, i.raw_text, i.due_at, i.critical,
         i.created_at, i.state_changed_at, p.name as project
  from shelf.items i
  left join shelf.projects p on p.id = i.project_id;

-- ------------------------------------------------------------ API grants
-- PostgREST only exposes schemas listed in PGRST_DB_SCHEMAS. If you want
-- the Supabase REST API over these tables, add `shelf` to that env var in
-- /root/supabase/docker/.env and restart supabase-rest. Not required if
-- FastAPI is the only database client.

grant usage on schema shelf to anon, authenticated, service_role;
grant all on all tables in schema shelf to anon, authenticated, service_role;
grant all on all sequences in schema shelf to anon, authenticated, service_role;
alter default privileges in schema shelf
  grant all on tables to anon, authenticated, service_role;