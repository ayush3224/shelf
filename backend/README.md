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

Access the interactive API docs at `http://localhost:8001/docs`.

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
| `DEFAULT_USER_ID` | Single-user UUID | UUID |
| `SHELVE_AFTER_IGNORES` | Decay constant | `3` |
| `DROP_AFTER_DAYS` | Drop timeout in days | `90` |
| `QUIET_HOURS_START` / `_END` | No notifications outside these hours | `22`, `7` |

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

Store a captured item.

**Request:**
```json
{
  "text": "captured text",
  "source": "voice"
}
```

**Response:**
```json
{
  "id": "uuid",
  "status": "captured"
}
```

`source` must be one of: `voice`, `text`, `widget`.

Items are created with `parse_status = 'needs_review'` and initial state `shelved`.

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

Run tests with pytest (add test files as needed):
```bash
pytest
```

Test client available in `backend.main` for unit tests.
