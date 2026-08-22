# Shelf Backend API

FastAPI service for the Shelf voice capture application.

## Setup

### Prerequisites

- Python 3.10+
- PostgreSQL (via Supabase)

### Installation

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your actual values
   ```

## Running the Service

### Development

```bash
source venv/bin/activate
python -m backend.main
```

The API will be available at `http://localhost:8001`.

There are no interactive docs — `/docs`, `/redoc` and `/openapi.json` are
disabled on purpose (D12). The endpoints are documented below.

### Production with systemd

1. Copy the unit file:
   ```bash
   sudo cp shelf.service /etc/systemd/system/
   ```

2. Update paths in the unit file to match your deployment location.

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable shelf
   sudo systemctl start shelf
   ```

## Configuration

All configuration is read from `.env`:

| Variable | Purpose | Example |
|----------|---------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://user:pass@host:5432/db` |
| `DB_SCHEMA` | Database schema name | `shelf` |
| `API_PORT` | HTTP server port | `8001` |
| `SUPABASE_JWT_SECRET` | HS256 secret used to verify access tokens | secret string |
| `SUPABASE_JWT_AUD` | Expected `aud` claim; empty disables the check | `authenticated` |
| `ANTHROPIC_API_KEY` | Key for the parse call | `sk-ant-...` |
| `ANTHROPIC_MODEL` | Parse model — Haiku only (D5) | `claude-haiku-4-5` |
| `TZ` | Timezone relative dates resolve against (alias: `CAPTURE_TIMEZONE`) | `Asia/Kolkata` |
| `SHELVE_AFTER_IGNORES` | Decay constant | `3` |
| `DROP_AFTER_DAYS` | Drop timeout in days | `90` |
| `QUIET_HOURS_START` / `_END` | No notifications outside these hours | `22`, `7` |

## Authentication (UC41)

Every endpoint except `GET /health` requires a Supabase access token:

```
Authorization: Bearer <supabase access token>
```

The token is verified with `SUPABASE_JWT_SECRET` (HS256, `exp` and `aud`
checked) and its `sub` claim is used as `user_id` on every row. There is no
configured fallback identity. A missing, malformed, expired, wrongly signed,
or subject-less token gets `401`; so does a well-formed token whose subject
is not a Supabase user.

Auth runs as middleware over everything not in `backend.auth.PUBLIC_PATHS`,
so new routes are protected by default.

## API Endpoints

### `GET /health`

Check API and database connectivity.

**Response:**
```json
{
  "status": "ok",
  "db_connected": true
}
```

### `POST /capture`

Store a captured item, then enrich it with one Haiku call (UC5, UC9, UC10,
UC12, UC14).

**Request:**
```json
{
  "text": "Call the insurance guy tomorrow at 3pm, it's urgent",
  "source": "voice"
}
```

**Response:**
```json
{
  "id": "uuid",
  "status": "captured",
  "parse_status": "ok",
  "state": "active",
  "kind": "task",
  "due_at": "2026-08-23T15:00:00Z",
  "critical": true,
  "text": "Call insurance agent about claim",
  "project_hint": null,
  "entities": [{"type": "person", "name": "Ravi"}]
}
```

`source` must be one of: `voice`, `text`, `widget`.

The row is written **before** the model call (D6). The parse then sets
`kind`, `due_at`, `critical`, `state` (`active` if `due_at` is set, else
`shelved`) and flips `parse_status` to `ok`.

If the parse fails the row stays exactly as captured with
`parse_status = 'failed'` and the endpoint still returns `200` — a capture
is never lost (UC42).

`text` is stored as `items.parsed_text` (migration 002); `raw_text` keeps
the capture exactly as it arrived. `project_hint` and `entities` are
returned but **not stored** — project inference is UC11 and entity linking
is UC44, both phase 6.

Relative dates ("tomorrow at 3pm") resolve against `TZ`, not the server
clock (D15).

## Database

The schema is created by the migration in `/migrations/001_init.sql`. Run this against your Supabase project before starting the service.

All operations are scoped to the `DB_SCHEMA` (default: `shelf`).

## Development

### Code Style

- Format with `black` and lint with `ruff`:
  ```bash
  black backend/
  ruff check backend/
  ```

- Type hints required on all public functions.

### Testing

```bash
./venv/bin/python -m pytest        # 32 tests, model call stubbed
./venv/bin/python -m pytest -m live  # opt-in: real Haiku call
```

The default run stubs the Anthropic call, so it costs nothing and is
deterministic. `-m live` runs `tests/test_timezone_live.py`, which proves
against the real model that "tomorrow at 3pm" resolves to 15:00 IST and
not 15:00 UTC.

### Migrations

Numbered SQL in `/migrations`, applied in order. Never edit one that has
already run — add the next number instead.
