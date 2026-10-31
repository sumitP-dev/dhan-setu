# Dhan Setu — Backend

FastAPI backend for Dhan Setu (card, UPI and BBPS payments). All routes live under `/api/v1`.

Stack: Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, PostgreSQL, Redis, Pydantic v2, pytest.

## Layout

```
app/
  main.py              app factory: CORS, error handlers, routers
  api.py               collects every module's router under /api/v1
  core/                config (.env), database, redis, logging, security
  common/              exceptions (error shape), enums
  modules/<feature>/   models, schemas, repository, service, router, dependencies
                       done: health, auth, users, notifications (SMS stub)
                       empty: cards, upi, bbps, payments, ledger, webhooks
  models.py            imports all models so Alembic can see them
alembic/               migrations
tests/                 pytest (SQLite in memory, fake Redis); helpers.py has shared fakes
```

## Setup

Needs [uv](https://docs.astral.sh/uv/). It installs Python 3.12 and the exact locked dependency versions (`uv.lock`).

```bash
uv sync                   # creates .venv with app + dev dependencies
cp .env.example .env      # then edit DATABASE_URL / REDIS_URL
```

Add a dependency with `uv add <package>` (or `uv add --dev <package>`). Commit `uv.lock`.

## Run tests

Tests don't need PostgreSQL or Redis, and they never read your `.env`. Warnings count as failures.

```bash
uv run pytest
```

## Code quality

Run all of these before every commit. Settings live in `pyproject.toml`.

```bash
uv run ruff format          # auto-format
uv run ruff check --fix     # lint (incl. security rules), auto-fix what's safe
uv run mypy                 # strict type check, with the pydantic plugin
uv run pytest               # tests
```

## Local PostgreSQL and Redis (macOS, Homebrew)

```bash
brew install postgresql@17 redis
brew services start postgresql@17      # also starts at login
brew services start redis

# App role + databases (choose your own password, put it in .env)
psql -h localhost -d postgres -c "CREATE ROLE dhansetu LOGIN PASSWORD '<password>'"
psql -h localhost -d postgres -c "CREATE DATABASE dhansetu OWNER dhansetu"
```

Stop them with `brew services stop postgresql@17 redis`.

## Docker (alternative to Homebrew)

```bash
docker compose up -d        # postgres:16 (db dhan_setu, user dhansetu) + redis:7
```

Set `POSTGRES_PASSWORD` in `.env` first, and point `DATABASE_URL` at
`postgresql+asyncpg://dhansetu:<password>@localhost:5432/dhan_setu`.
Stop Homebrew's services first (they use the same ports), or set `POSTGRES_PORT` / `REDIS_PORT`.

Build the API image:

```bash
docker build -t dhan-setu-backend .
docker run --env-file .env -p 8000:8000 dhan-setu-backend
```

## Run the server

Needs PostgreSQL and Redis running locally.

```bash
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8000
```

- Health: http://localhost:8000/api/v1/health
- Readiness (checks DB + Redis): http://localhost:8000/api/v1/health/ready
- API docs (non-production only): http://localhost:8000/docs

`--host 0.0.0.0` lets the Android emulator reach the server at `http://10.0.2.2:8000`.

## VS Code

Open `dhan-setu-backend/` as the folder, or open `../dhan-setu.code-workspace`
(File > Open Workspace from File...) to work on backend and mobile side by side.
Accept the recommended extensions when prompted (Python, Debugger, Ruff, Mypy, REST Client).

- **Run and Debug** (Cmd+Shift+D): `FastAPI: run & debug` starts the server on port 8000 with
  breakpoints and auto-reload; `Pytest` and `Pytest: current file` debug the tests.
- **Testing** (beaker icon): run or debug single tests.
- Files are formatted and imports sorted on save, with the same Ruff settings as the command line.
- If the interpreter isn't picked up: Cmd+Shift+P > "Python: Select Interpreter" > `.venv`.

## Trying the API by hand

`requests.http` walks through register -> verify OTP -> login -> /users/me -> refresh -> logout.
Open it with the REST Client extension and click "Send Request" above each block, top to bottom.
OTP codes appear in the server log (`[SMS stub] verify_phone OTP for +91******3210: 123456`);
paste the code into `@otp` at the top of the file. Codes expire after 5 minutes.

## Migrations

```bash
uv run alembic revision --autogenerate -m "add users"
uv run alembic upgrade head
```

Register new models in `app/models.py` so autogenerate picks them up.

## API

Full interactive docs: http://localhost:8000/docs

| Method | Path | Auth | What it does |
|---|---|---|---|
| GET | `/api/v1/health` | - | Process is up |
| GET | `/api/v1/health/ready` | - | PostgreSQL and Redis reachable |
| POST | `/api/v1/auth/register` | - | Create account (pending), texts a verify_phone OTP. No tokens yet |
| POST | `/api/v1/auth/otp/send` | - | Text a code: `verify_phone` (pending accounts) or `login` (active accounts) |
| POST | `/api/v1/auth/otp/verify` | - | Check the code. `verify_phone` activates the account. Returns user + tokens |
| POST | `/api/v1/auth/login` | - | Email **or** mobile + password, returns user + tokens |
| POST | `/api/v1/auth/refresh` | - | Swap refresh token for a new pair |
| POST | `/api/v1/auth/logout` | - | Revoke this device's refresh token |
| POST | `/api/v1/auth/change-password` | Bearer | Change password, signs out other devices |
| GET | `/api/v1/users/me` | Bearer | Current user's profile |
| PATCH | `/api/v1/users/me` | Bearer | Update full name |

### How auth works

- **Access token**: JWT, 15 minutes. Send as `Authorization: Bearer <token>`.
  When it expires the API returns 401 with code `token_expired`: call `/auth/refresh`.
  Any other 401 (`invalid_token`, `invalid_refresh_token`) means: log in again.
- **Sign-up**: `register` -> SMS code -> `otp/verify` (purpose `verify_phone`) -> tokens.
  Until then the account is `pending_verification` and login returns 403 `phone_not_verified`.
- **OTP**: 6 digits, valid 5 minutes, 5 wrong tries kill the code, one SMS per number per 30s
  (429 `otp_cooldown` + `Retry-After`). Stored only as an HMAC. `otp/send` answers the same
  whether or not the number is registered.
- **Dev OTP**: the SMS provider is a stub that writes the code to the server log. With
  `DEBUG=true` (never in production) responses also include `dev_otp`.
- **Refresh token**: random, 7 days, single use. Every refresh returns a new one.
  Only its SHA-256 hash is stored. Re-using an old one is treated as theft and ends
  that device's session. **Clients must never run two refreshes at the same time.**
- **Brute force**: 5 wrong passwords lock that account for 15 minutes, whether you use its
  email or mobile (429 + `Retry-After`).
- **Account status**: `pending_verification`, `active` or `blocked` (403 `account_disabled`).
- **Roles**: `customer` (default), `merchant`, `admin`. Protect a route with
  `Depends(require_roles(UserRole.ADMIN))` from `app.modules.auth.dependencies`.
- Mobile numbers are accepted as `9876543210`, `+91 98765 43210`, etc. and stored as `+919876543210`.
- Passwords: 8+ characters, at least one letter and one number, at most 72 bytes.

## Rules

- Store datetimes with `UTCDateTime` columns and create them with `utcnow()`.
- Inject the database session as `db: DbSession`. It commits before the response is sent and rolls back on any error.

- Errors always look like `{"error": {"code": "...", "message": "..."}}`.
- Money is integer paise. Never floats.
- Never store card numbers or CVV, only the gateway token, last4, network and expiry.
- Payment-create endpoints require an `Idempotency-Key` header.
- Secrets only in `.env` (git-ignored).
