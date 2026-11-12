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
| GET | `/api/v1/users/me` | Bearer | Current user's profile, including `kyc_status` |
| PATCH | `/api/v1/users/me` | Bearer | Update full name |
| GET | `/api/v1/kyc` | Bearer | KYC status, next step, and the DigiLocker details (masked) to check |
| POST | `/api/v1/kyc/digilocker` | Bearer | Start (or reuse the open) DigiLocker session: returns `consent_url` |
| GET | `/api/v1/kyc/digilocker/{id}` | Bearer | Session status; checks with the provider while open |
| GET | `/api/v1/kyc/digilocker/callback?state=` | - (one-time `state`) | DigiLocker sends the browser here; redirects to the app with the outcome |
| POST | `/api/v1/kyc/pan` | Bearer | PAN typed in, when it isn't in DigiLocker or didn't match |
| POST | `/api/v1/kyc/selfie` | Bearer | Start (or reuse the open) selfie session: returns `sdk_token` for the provider's SDK |
| GET | `/api/v1/kyc/selfie/{id}` | Bearer | Selfie result (liveness + match with the Aadhaar photo); checks with the provider while open |
| POST | `/api/v1/kyc/submit` | Bearer | Send for review once every step is done: status becomes `pending_review` |
| POST | `/api/v1/admin/auth/login` | `X-Admin-Client` | Admin portal step 1: email + password of an admin; texts a code |
| POST | `/api/v1/admin/auth/verify` | `X-Admin-Client` | Step 2: challenge + code; returns an access token, sets the session cookie |
| POST | `/api/v1/admin/auth/refresh` | `X-Admin-Client` + cookie | New access token; replaces the cookie |
| POST | `/api/v1/admin/auth/logout` | `X-Admin-Client` + cookie | Ends the session at once |
| GET | `/api/v1/admin/auth/me` | Admin + `X-Admin-Client` | The signed-in admin |
| GET | `/api/v1/admin/users?q=&kyc_status=&status=&limit=&offset=` | Admin | Everyone who signed up, newest first, with active card counts (searches are logged) |
| GET | `/api/v1/admin/users/{user_id}` | Admin | One user: profile, KYC summary (who decided, why) and all cards (each opening is logged) |
| GET | `/api/v1/admin/cards?q=&status=&network=&card_type=&limit=&offset=` | Admin | All saved cards with owners; `q` = last 4 digits, name, email or phone |
| GET | `/api/v1/admin/kyc?status=&limit=&offset=` | Admin | Review queue, longest-waiting first (masked data only) |
| GET | `/api/v1/admin/kyc/{user_id}` | Admin | A user's KYC details, checks and history (each opening is logged) |
| POST | `/api/v1/admin/kyc/{user_id}/approve` | Admin | Approve: status becomes `verified` |
| POST | `/api/v1/admin/kyc/{user_id}/reject` | Admin | Reject with `reason` (+ internal `note`): status becomes `rejected` |
| GET | `/api/v1/cards` | Bearer + KYC | Saved cards, default first |
| POST | `/api/v1/cards/verifications` | Bearer + KYC + `Idempotency-Key` | Start adding a card: ₹1 order + checkout options |
| POST | `/api/v1/cards/verifications/{id}/complete` | Bearer + KYC | Send checkout's payment id + signature: saves the card, refunds ₹1 |
| GET | `/api/v1/cards/verifications/{id}` | Bearer + KYC | Status of a card check |
| PATCH | `/api/v1/cards/{id}/default` | Bearer + KYC | Make a card the default |
| DELETE | `/api/v1/cards/{id}` | Bearer + KYC | Remove a card (and its token at the gateway) |
| POST | `/api/v1/payments/orders` | Bearer + KYC + `Idempotency-Key` | Start a payment with a saved card: order + checkout options |
| POST | `/api/v1/payments/{id}/verify` | Bearer + KYC | Send checkout's payment id + signature |
| GET | `/api/v1/payments/{id}` | Bearer + KYC | Payment status; checks with the gateway while unsettled |
| POST | `/api/v1/webhooks/razorpay` | `X-Razorpay-Signature` | Razorpay events (called by Razorpay, not the app) |

### KYC gate

Cards and payments (and later UPI and bills) need `kyc_status` = `verified`; otherwise
they answer 403 with code `kyc_required`. "Bearer + KYC" in the table above marks them.

    not_submitted -> in_progress -> pending_review -> verified
                          ^               |
                          +-- rejected <--+

How documents are collected:

1. `POST /kyc/digilocker` -> open `consent_url`; the user logs in to DigiLocker and allows
   sharing Aadhaar and PAN.
2. DigiLocker sends the browser to `/kyc/digilocker/callback?state=...` (a one-time token;
   only its SHA-256 is stored). The backend fetches the documents from the provider itself,
   then redirects to `KYC_APP_RETURN_URL?status=completed|pending|...`. If the browser
   never comes back, the app polls `GET /kyc/digilocker/{id}`.
3. Checks: UIDAI's signature on the Aadhaar; PAN must be a personal PAN whose name and
   date of birth match the Aadhaar; one PAN per account. If the PAN is missing or doesn't
   match, `next_step` is `pan` and the user can type it in (`POST /kyc/pan`, 5 tries).
4. Stored: name, date of birth, address and PAN encrypted with AES-256-GCM
   (`KYC_ENCRYPTION_KEY`), Aadhaar last 4 digits, a masked PAN and a keyed PAN hash.
   Never stored: the full Aadhaar number, the Aadhaar XML, photos.
5. Selfie (`next_step` = `selfie`): `POST /kyc/selfie` returns an `sdk_token`; the app
   runs the provider's selfie SDK, then polls `GET /kyc/selfie/{id}`. The backend fetches
   the result itself: the person must be live and the face-match score against the Aadhaar
   photo at least `KYC_SELFIE_MIN_MATCH_SCORE` (default 80). Running DigiLocker again
   clears a passed selfie, since the photo may have changed. Then `next_step` is `submit`.

6. `POST /kyc/submit` (`next_step` = `submit`) -> `pending_review`. Details can't change
   while under review.

Admin review (`/admin/kyc`, role `admin`):

- There is no API that grants the admin role. On the server run
  `.venv/bin/python -m app.cli make-admin <email>` (the account must be active);
  `remove-admin <email>` takes it away, effective on the admin's next request.
- Approve or reject (`name_mismatch`, `photo_mismatch`, `document_issue`, `other` with a
  note). The user sees the reason's message, never the note. Two admins deciding at once:
  one wins, the other gets 409. An admin can't decide on their own KYC.
- A rejection frees the PAN (so its real owner isn't blocked); the user starts again
  from DigiLocker.
- Every decision is in `kyc_events` with the admin's id; every opening of a user's
  details is in `kyc_access_logs`.

Only `KycService.transition()` changes the status: under a row lock, checked against the
allowed changes, recorded in `kyc_events`. `KYC_PROVIDER=fake` (default outside
production) or `none` (KYC off; required in production until a real provider is set up).

### Admin portal sign-in

"Admin" in the table means an admin portal access token; customer-app tokens never work
there (each token names who it is for), and admin tokens never work in the app.

1. `POST /admin/auth/login` with email + password. Only active admin accounts pass; a
   customer, a wrong password and an unknown email all get the same 401. A code is texted
   to the admin's phone (`admin_login` codes can't be swapped with app login codes).
2. `POST /admin/auth/verify` with the `challenge_token` and the code. Returns a 10-minute
   access token (keep it in memory only) and sets an httpOnly, SameSite=Strict cookie
   holding the refresh token (`__Host-` prefixed and Secure outside local http).
3. `POST /admin/auth/refresh` before the access token expires. The cookie is replaced each
   time; an old cookie seen again ends the session (copied cookie), except within 10
   seconds, when another tab just refreshed (409 `refresh_in_progress`: retry).
4. A session ends after `ADMIN_SESSION_IDLE_MINUTES` (30) without a refresh,
   `ADMIN_SESSION_MAX_HOURS` (8) in total, on logout, or when the account is blocked or
   loses the admin role, and every access token of that session stops working at once.

Cross-site protection: the cookie endpoints need the header
`X-Admin-Client: dhan-setu-admin` and, when the browser sends one, an `Origin` listed in
`ADMIN_ORIGINS`. CORS (with credentials) is open only to those origins. Every sign-in is
kept in `admin_sessions` (time, IP, browser, how it ended).

### Admin views of users and cards

Read-only. `q` searches phone numbers when it looks like one (4+ digits), otherwise names
and emails; `%` and `_` are taken literally. Card responses never include gateway tokens
or customer ids. Searches and opening a user are recorded in `admin_audit_logs` (admin,
action, user, search text).

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

### Payment gateway

`PAYMENT_GATEWAY` in `.env` picks the gateway (code in `app/modules/payments/gateway/`):

- `fake` (default): settles payments in memory, no keys or internet needed. Tests always use it.
  The app refuses to start with it in production.
- `razorpay`: needs `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET`.
  Only `rzp_test_` keys are accepted outside production, and only `rzp_live_` keys in production.

Card numbers and CVVs never reach this server: users type them into Razorpay's checkout.

### How adding a card works

1. `POST /cards/verifications` creates a ₹1 order. The app opens Razorpay checkout with the
   returned `checkout` options and `save: 1`.
2. `POST /cards/verifications/{id}/complete` with checkout's payment id and signature. The
   server checks the signature, then asks Razorpay for the payment and its saved card. Card
   details always come from Razorpay, never from the app.
3. The card is saved (credit/debit, not expired) and the ₹1 refunded straight away.
   Only saved cards can be used to pay.

- `status`: `created` -> `verified` or `failed` (`failure_code` + `failure_message` say why).
  `charged` means the ₹1 was taken but Razorpay didn't answer while saving: call
  `/complete` again. Completing again is always safe.
- Limits: 5 card checks per user per day, 10 saved cards (`CARD_VERIFICATIONS_PER_DAY`,
  `MAX_CARDS_PER_USER`). Small test charges are how stolen cards get tried.
- If a refund can't be confirmed it stays `pending` for the reconciliation job (Prompt 10).

### How a card payment works

1. `POST /payments/orders` with `amount_paise` (whole paise, at least 100) and a saved
   `card_id`, plus an `Idempotency-Key`. The same key and body return the same payment;
   the same key with a different body is refused (409 `idempotency_key_reused`).
2. The app opens Razorpay checkout with the returned `checkout` options.
3. `POST /payments/{id}/verify` with checkout's payment id and signature. The server checks
   the signature against the stored order and asks Razorpay for the payment.
4. Only the chosen saved card is accepted. A payment made with any other card is never
   captured, or refunded if it already was (`failure_code: card_not_allowed`).
5. While a payment is unsettled, `GET /payments/{id}` checks with Razorpay (at most every
   `PAYMENT_SYNC_INTERVAL_SECONDS`), so it settles even if the app closed before verifying.

Statuses: `created -> pending -> success -> refunded`, and `created/pending -> failed`.
`failed -> pending` is allowed because a user can retry on the same order in checkout.
Every change is recorded in `payment_events`. Unpaid payments fail after
`PAYMENT_EXPIRY_MINUTES`. Single payments are capped at `MAX_PAYMENT_AMOUNT_PAISE`.

### Razorpay webhook

Razorpay calls `POST /api/v1/webhooks/razorpay` when payments and refunds change, so
payments and card checks finish even if the app closed before verifying.

- The `X-Razorpay-Signature` header (HMAC of the raw body with `RAZORPAY_WEBHOOK_SECRET`) must
  match, or the request is refused (400). Bodies over 256 KB are refused (413).
- A webhook is only a trigger: amounts and statuses in it are never used. The server asks
  Razorpay for the order or refund and applies that, with the same code as verify and polling.
- Each event is handled once: duplicates are recognised by event id and by a hash of the body
  (the event id isn't signed, so a replayed body under a new id is still a duplicate).
- If Razorpay can't be reached while handling, the response is 503 and nothing is recorded,
  so Razorpay's retry is handled normally. Signed but unusable events get 200 and are kept in
  `webhook_events` as `failed`. Payloads aren't stored (they hold customer details).
- Events used: `payment.authorized`, `payment.captured`, `payment.failed`, `order.paid`,
  `refund.created`, `refund.processed`, `refund.failed`.

To receive webhooks locally, Razorpay needs a public URL: run a tunnel (e.g. `ngrok http 8000`)
and add `https://<tunnel>/api/v1/webhooks/razorpay` in Dashboard (Test Mode) -> Webhooks, with
the same secret as `RAZORPAY_WEBHOOK_SECRET`. Without webhooks, polling still settles payments.

## Rules

- Store datetimes with `UTCDateTime` columns and create them with `utcnow()`.
- Inject the database session as `db: DbSession`. It commits before the response is sent and rolls back on any error.

- Errors always look like `{"error": {"code": "...", "message": "..."}}`.
- Money is integer paise. Never floats.
- Never store card numbers or CVV, only the gateway token, last4, network and expiry.
- Payment-create endpoints require an `Idempotency-Key` header.
- Secrets only in `.env` (git-ignored).
