# Receipt Tracker (MVP)

Receipt tracking for US freelancers. Upload a receipt photo or PDF. The app extracts the vendor, date and amount, suggests a Schedule C-style category with a deductible % hint, and exports a CSV or PDF summary you can bring to tax time.

> **General guidance only, not tax advice.** Category and deductible suggestions are simplified defaults. The disclaimer appears in the UI and in both exports.

Stack: FastAPI, SQLAlchemy 2 with SQLite, Jinja templates and plain CSS, Tesseract OCR (free, runs locally), ReportLab for PDF output.
Docs: [`docs/competitors.md`](docs/competitors.md) and [`docs/mvp.md`](docs/mvp.md).

## Features
- Sign up and log in with email and password. Passwords are hashed with bcrypt. Sessions use a signed, HTTP-only cookie. All forms carry CSRF tokens.
- **Login rate limiting.** Failed logins are counted per email and per client IP in SQLite, so the limits survive restarts. After `LOGIN_MAX_FAILURES_PER_EMAIL` (default 5) or `LOGIN_MAX_FAILURES_PER_IP` (default 20) failures within `LOGIN_WINDOW_SECONDS`, logins for that email or IP get HTTP 429 with `Retry-After` until `LOGIN_LOCKOUT_SECONDS` after the last failure. Unknown emails lock the same way, and a dummy bcrypt check runs for them, so neither the lockout nor the response time shows which accounts exist. A successful login clears the per-email counter (not the per-IP one). Behind a proxy, set `TRUSTED_PROXY_HOPS` so the real client IP is read from `X-Forwarded-For`.
- **Password reset** (`/forgot-password` → `/reset-password?token=…`). Tokens are random (`secrets.token_urlsafe(32)`), **stored only as a SHA-256 hash**, single-use, expire after `RESET_TOKEN_TTL_MINUTES` (default 60), and a newer request revokes older links. A reset logs out every existing session for that user and clears the login lockout. The response is identical whether or not the email exists, and requests are silently throttled per IP and per email (per hour).
  - **No email provider is connected** (`app/mailer.py` is an empty interface). With `DEV_MODE=true` the reset link is written to the **server log only**, never shown in the UI. Without `DEV_MODE`, a warning is logged and the link is not delivered anywhere. Uvicorn access-log lines are filtered so `token=` values show as `[redacted]`.
  - Set `PUBLIC_BASE_URL` in production so links don't depend on the request's Host header.
- **Plans and the free-tier limit.** Each user has a `plan` (`free` by default, or `pro`). Free allows `FREE_MONTHLY_RECEIPT_LIMIT` (default 15) new receipts per calendar month (UTC), and Pro is unlimited. Uploads, manual entries and inbound-email receipts all count. Deleting a receipt doesn't free a slot. When a free user hits the limit, the upload is refused with a friendly message (a multi-file upload keeps the files that fit), and existing receipts and exports stay available. The dashboard, upload page and account page show usage. `/upgrade` describes Pro ($6/month or $49/year) with a clear **"Billing isn't available yet"** notice. There is no payment code and no Stripe.
- Upload JPG, PNG, WEBP or PDF files, up to 20 at a time, `MAX_UPLOAD_MB` each. The file type is checked from its magic bytes, not its extension. Files are stored under `DATA_DIR/uploads/<user_id>/` and served only to their owner.
- Pluggable extraction (`app/extraction.py`):
  - `heuristic` (default): uses PDF text when present, otherwise Tesseract OCR, then parses the text with regex rules.
  - `openai` (optional): only runs when `OPENAI_API_KEY` is set. It's paid per use and off by default. If it fails, the app falls back to `heuristic`.
  - `none`: no extraction; you enter everything manually.
  - You can always correct the result on the edit form.
- Rule-based category suggestion mapped to IRS Schedule C lines (`app/categories.py`). Meals count at 50%, personal expenses at 0%, and anything uncategorized needs review.
- Receipt list filtered by year and category, plus edit and delete. Dashboard shows totals and estimated deductible amounts by category.
- Exports: `/export.csv?year=YYYY` and `/export.pdf?year=YYYY` (the tax summary).
- Privacy: a privacy page, "delete all my receipts", and "delete my account" (requires the password). No sharing and no trackers.
- Email forwarding is a **stub only**: `POST /api/inbound-email` (see below).

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
# optional, for photo OCR: sudo apt-get install tesseract-ocr   (macOS: brew install tesseract)
export SECRET_KEY=dev-secret DATA_DIR=./data
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
pytest -q
```
Add `DEV_MODE=true` locally to see password-reset links in the server log.

### Marking a user Pro (testing; no billing exists)
Uses the same `DATA_DIR` / `DATABASE_URL` as the app:
```bash
python -m app.cli set-plan you@example.com pro    # or: free
python -m app.cli show you@example.com
python -m app.cli list
```
On Render, run it from the service's Shell tab.

Without Tesseract, PDFs that contain text are still extracted. For image receipts you enter the details by hand.

With Docker:
```bash
docker build -t receipt-tracker .
docker run -p 8000:8000 -e SECRET_KEY=dev -v "$PWD/data:/var/data" receipt-tracker
```

## Configuration (env vars)
| Var | Default | Notes |
|---|---|---|
| `DATA_DIR` | `./data` | Holds **all** state: `receipts.db` (SQLite) and `uploads/`. Point this at a persistent disk in production. |
| `DATABASE_URL` | `sqlite:///$DATA_DIR/receipts.db` | Can be overridden (for example with Postgres later; untested). |
| `SECRET_KEY` | random per process | **Required in production.** Without it, sessions reset on every restart. |
| `SECURE_COOKIES` | `false` | Set to `true` behind HTTPS (the Render config already does). |
| `EXTRACTOR` | `heuristic` | `heuristic` \| `openai` \| `none` |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | unset, `gpt-4o-mini` | Optional and paid. Receipt images are sent to OpenAI, so update the privacy page if you enable it. |
| `MAX_UPLOAD_MB` | `10` | |
| `INBOUND_EMAIL_TOKEN` | unset | Enables the inbound-email stub. The endpoint returns 503 when unset. |
| `INBOUND_EMAIL_DOMAIN` | `in.example.com` | Domain shown in each user's forwarding address. |
| `DEV_MODE` | `false` | Dev-only conveniences. Currently: log password-reset links when no email provider is configured. **Never enable in production.** |
| `PUBLIC_BASE_URL` | unset | For example `https://receipts.example.com`. Used to build reset links. Falls back to the request URL. |
| `LOGIN_MAX_FAILURES_PER_EMAIL` | `5` | Failed logins per email within the window before lockout. `0` disables. |
| `LOGIN_MAX_FAILURES_PER_IP` | `20` | Failed logins per client IP within the window before lockout. `0` disables. |
| `LOGIN_WINDOW_SECONDS` | `900` | Sliding window for counting failures. |
| `LOGIN_LOCKOUT_SECONDS` | `900` | Lockout length, counted from the most recent failure. |
| `TRUSTED_PROXY_HOPS` | `0` | Reverse proxies in front of the app. `0` ignores `X-Forwarded-For`. `N` uses the Nth address from the right. `render.yaml` sets `1`. |
| `RESET_TOKEN_TTL_MINUTES` | `60` | Reset link lifetime. |
| `RESET_MAX_REQUESTS_PER_IP` / `RESET_MAX_REQUESTS_PER_EMAIL` | `10` / `3` | Reset requests per hour. Extra requests get the same response but send nothing. |
| `FREE_MONTHLY_RECEIPT_LIMIT` | `15` | New receipts per calendar month (UTC) on the Free plan. |

## Deploy to Render
1. Push this folder to a Git repo.
2. In Render, choose **New → Blueprint** and select the repo. `render.yaml` creates a Docker web service with Tesseract included, `DATA_DIR=/var/data`, a generated `SECRET_KEY`, and health checks on `/healthz`.

**Free tier vs. persistent disk (decision for D):**
- `render.yaml` defaults to `plan: free`. On free instances the filesystem is **ephemeral**: the SQLite DB and uploaded receipts are **wiped on every redeploy, restart, or idle spin-down** (after about 15 minutes without traffic). Use it for demos with fake data only.
- To keep data you need a **paid** instance (`plan: starter`) **plus a paid persistent disk**. Uncomment the `disk:` block, which is already set to mount at `/var/data` = `DATA_DIR`. **Don't enable this without D's OK, because it costs money.** With a disk attached, the service is limited to a single instance and deploys have a few seconds of downtime.
- `TRUSTED_PROXY_HOPS=1` assumes Render's edge appends the real client IP as the last `X-Forwarded-For` entry. After the first deploy, check the login rate-limit IP keys against your own IP. If the value is wrong, all users could share one IP bucket.
- Don't set `DEV_MODE` on Render. Set `PUBLIC_BASE_URL` to the service URL.
- If the app can't write to the mounted disk (the container runs as the non-root user `appuser`), remove the `USER appuser` line from the Dockerfile.
- The longer-term option is managed Postgres plus object storage (S3 or R2) for files. That's deferred.

## Inbound email (stub)
Nothing is connected to a real mail provider. The endpoint shows the shape an adapter (Postmark, SendGrid, Mailgun) would post to:
```bash
curl -X POST http://localhost:8000/api/inbound-email \
  -H "X-Inbound-Token: $INBOUND_EMAIL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"to":"r-<alias>@in.example.com","from":"me@x.com","subject":"Fwd: receipt",
       "attachments":[{"filename":"r.pdf","content_base64":"<base64>"}]}'
```
Each user's alias is shown on the Account page.

## Layout
```
app/main.py         routes, auth, uploads, exports, account deletion, inbound stub
app/extraction.py   pluggable extractors and parsing
app/categories.py   Schedule C categories, deduction %, suggestion rules
app/exports.py      CSV and PDF summary
app/db.py           models (User, Receipt, ThrottleEvent, PasswordResetToken) + tiny column migration
app/security.py     login rate limiter, client IP, reset tokens
app/plans.py        plans, monthly usage counter, free-tier limit
app/mailer.py       outgoing-email interface (no provider connected)
app/cli.py          admin CLI: set-plan / show / list
app/templates/      Jinja UI;  app/static/style.css
tests/              pytest (auth, isolation, CSRF, upload/extract, exports, deletion, inbound, OCR,
                    rate limiting, password reset, free-tier limit, CLI, migration)
```

## Known limitations
- No email is ever sent: password reset only works end-to-end locally with `DEV_MODE=true` (link in the server log). A real provider needs D's OK.
- No email verification and no billing. Pro can only be set with the CLI.
- Sign-up still says "An account with that email exists", which reveals registered emails. The login and reset flows don't.
- Database changes use a small built-in "add missing columns" step, not a full migration tool such as Alembic.
- Rate limits are per process DB. That's fine for SQLite on one instance. `TRUSTED_PROXY_HOPS=1` on Render should be checked after the first deploy (see Deploy).
- The heuristic parser is basic. Expect to correct some receipts by hand.
- Error pages are plain JSON. Amounts are USD only.
