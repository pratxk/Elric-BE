# Elric backend — AP invoice processing agent (v1, 0.1.0)

Deterministic-first invoice matching: the LLM reads documents, pure code
decides money. FastAPI + LangGraph + Pydantic v2 + SQLAlchemy. Postgres via
`DATABASE_URL` (Neon-ready); SQLite file for local dev.

## Run locally

```bash
cd ~/workspace/invoice-agent/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in GEMINI_API_KEY when you want extraction
uvicorn app.main:app --reload --port 8000
```

Health: `curl localhost:8000/health` → `{"status":"ok",...}`

## Tests

```bash
pytest -q   # 177 tests; GEMINI_API_KEY is forcibly unset by tests/conftest.py
```

Tests use an isolated SQLite file in /tmp (never the dev `elric.db`) and a
fresh store per test. Extraction is tested with a fake Gemini client only —
**no test makes a real Gemini call, on either key**.

## API (§8)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/invoices/upload` | PDF/PNG/JPEG ≤ 20 MB; hash-deduped (409 on re-upload) |
| GET | `/api/invoices` | optional `?status=` filter |
| GET | `/api/invoices/{id}` | |
| POST | `/api/invoices/{id}/approve` | resumes the paused graph run; bookkeeper posts |
| POST | `/api/invoices/{id}/reject` | resumes the paused graph run as rejected |
| POST | `/api/invoices/{id}/query-vendor` | **draft only — never sends** |
| GET | `/api/invoices/{id}/trace` | SSE audit trail incl. live per-node events |
| GET | `/api/invoices/{id}/ledger` | balanced ledger entries for the invoice |
| GET | `/api/invoices/{id}/payments` | scheduled (never executed) payments |
| POST | `/api/purchase-orders` | seed a PO (vendor_name used for lookup) |
| GET | `/api/purchase-orders` | |
| POST | `/api/goods-receipts` | seed a goods receipt |
| GET | `/api/goods-receipts` | optional `?po_id=` |
| GET | `/api/ledger` | all ledger entries |
| GET | `/api/payments` | all scheduled payments |
| GET | `/api/queue/exceptions` | flagged + on-hold invoices |
| GET | `/api/aging` | buckets current / 30 / 60 / 90+ (`?as_of=`) |
| GET | `/api/metrics` | match/auto-approve/false-approve rates, median cycle time, money in exceptions, overdue totals |
| GET | `/api/forecast` | weekly outflows + discount recommendations (`?weeks=&weekly_budget_inr=&cost_of_capital_pct=`) |
| POST | `/api/forecast/discount` | `{"invoice_id", "terms": "2/10 Net 30"}` → NPV pay-now/pay-later |
| POST | `/api/eval/run` | synthetic-corpus matcher eval + end-to-end graph eval |
| GET | `/api/eval/summary` | latest eval scores (cached) or a fresh run (`?refresh=true`) |
| POST | `/api/auth/register` | `{email, password}` → 201 + session (public) |
| POST | `/api/auth/login` | `{email, password}` → session (public; 5 failures → 15-min lockout) |
| POST | `/api/auth/refresh` | rotates the refresh cookie → new access token (public) |
| POST | `/api/auth/logout` | revokes the refresh token, clears the cookie (public) |
| GET | `/api/auth/me` | current user (access token) |
| POST | `/api/auth/complete-onboarding` | sets `onboarding_completed` (access token) |
| POST | `/api/auth/change-password` | `{current_password, new_password}`; revokes ALL sessions |
| GET | `/api/settings` | the user's settings (defaults created on first read) |
| PUT | `/api/settings` | validated settings update |
| POST | `/api/vendors` | create a vendor in the user's registry |
| GET | `/api/vendors/{id}` | one registry vendor (404 cross-user) |
| PUT | `/api/vendors/{id}` | update a registry vendor |
| DELETE | `/api/vendors/{id}` | delete a registry vendor |
| GET | `/health` | |

## Auth + per-user scoping (v1.1)

**Login: email + password.** Password policy (server-side, 422 on
violation): 12–72 chars, at least one uppercase, one lowercase, one
digit, one symbol. Hashing is **bcrypt, cost 12, via the `bcrypt`
package directly** (passlib is unmaintained and breaks with bcrypt 4+;
the raw package is the maintained path). Plaintext passwords are never
stored, logged, or returned; hashes never appear in any API response.
Login errors are generic — wrong password and unknown email both return
401 `Invalid email or password`. Five failed logins lock the email out
for 15 minutes (DB-backed `login_attempts`, survives restart); register
with a duplicate email is a normal 409. No password reset in v1.1
(that would need an email sender); change-password while logged in only.

**Token contract (pinned):**
- **Access token:** JWT, HS256, 15-minute expiry, returned in JSON as
  `access_token`, sent as `Authorization: Bearer <token>`. Signed with
  `JWT_SECRET` — the server **refuses to start** without it (no silent
  fallback). Implemented on the standard library only
  (`app/infra/jwt.py`); no PyJWT dependency.
- **Refresh token:** opaque random token, 7-day expiry, stored
  server-side only as a SHA-256 hash (`refresh_tokens` table: id,
  user_id, token_hash, family_id, expires_at, revoked_at, created_at),
  delivered in the HttpOnly `elric_refresh` cookie (SameSite=Lax,
  Path=/, **Secure only when the request is HTTPS** — detected from the
  request scheme, so localhost HTTP dev works and HTTPS deploys get the
  flag).
- **Rotation:** `POST /api/auth/refresh` (no body, reads the cookie)
  revokes the presented token and issues a new one in the SAME
  family_id, returning a fresh access token.
- **Reuse detection:** presenting an already-rotated (revoked) or
  expired token revokes the WHOLE family and returns 401, forcing
  re-login. A completely unknown token hash revokes nothing (it may be
  a stale cookie) — just 401.
- **Logout** revokes the presented refresh token and clears the cookie.
- **Change-password** revokes ALL of the user's refresh tokens (all
  sessions, all devices) and returns `{"reauthenticate": true}` — the
  client must discard its access token and redirect to `/login`
  (access tokens are stateless JWTs and cannot be revoked server-side;
  they expire within 15 minutes at most).
- **Guard:** every `/api/*` endpoint except `/health`,
  `POST /api/auth/register`, `POST /api/auth/login`, and
  `POST /api/auth/refresh` requires a valid access token → 401.

**Per-user scoping:** `users`, `settings` (one row per user), and the
`vendors` registry are new tables; `user_id` (nullable) was added to
invoices, purchase orders, goods receipts, ledger entries, payments,
and the audit log. Every API read/write is scoped to the logged-in
user; legacy rows with `user_id` NULL are invisible to everyone (the
dev `elric.db`'s 2 legacy invoices are intentionally invisible now —
not deleted). Cross-user access to a known id returns 404, never 403.

**Settings flow into the pipeline:** the upload snapshots the owner's
settings into graph state (`state["settings"]`); the matcher builds
its `ToleranceConfig` (±pct / flat ₹), duplicate window, and the
auto-approve policy from that snapshot (`policy_from_settings()` —
used by BOTH the matcher node and the approver gate, so the two can
never disagree). Env/config values are only the fallback for runs
without settings (eval, graph tests). Settings: company_name, country
(default IN), state, currency (default INR), auto_approve_limit_inr
(default `AUTO_APPROVE_LIMIT_INR`), tolerance_pct (default 2.0),
tolerance_flat_inr (default 500), duplicate_window_days (default 90).

**Model (v1.1): `gemini-3.8-flash`.** `gemini-2.5-flash` is being
retired and 404s intermittently; the code default (`app/config.py`)
and `.env` / `.env.example` were switched on 2026-11-15. **Deploy
note: set `GEMINI_MODEL=gemini-3.8-flash` in the Render env vars too**
— `.env` is gitignored, so the deployed value does not follow the code.

**Tax-default fix (v1.1, eval §4.2):** a missing per-line
`tax_rate_pct` is now `None` (unknown), never defaulted to 18% Indian
GST. The arithmetic self-check and the matcher's total rule skip their
tax-dependent legs when any line rate is unknown (the subtotal leg
still runs); GST validation likewise skips non-GST invoices instead of
flagging them. See `tests/test_tax_default.py`, which re-checks the
four saved benchmark extractions with zero new LLM calls.

## What works (v1, §16c)

- **Extraction** (`app/services/extraction_service.py`): real Gemini
  structured output (JSON schema, per-field confidence) with the document
  sent as an inlineData part (vision for scans) and the text layer in the
  prompt; arithmetic self-check with exactly one re-extract, then escalate
  (raises `ExtractionFailedError` — never guesses). Vendor names are
  fuzzy-resolved against the registry built from stored POs/invoices so
  extracted invoices share registry vendor ids. No key → clean
  `GeminiConfigurationError` and the invoice parks `on_hold`.
- **Deterministic matcher** (`app/domain/matching/`): Hungarian line-item
  alignment (pure Python, no scipy), Jaro-Winkler vendor matching,
  config-driven tolerances (±2% or ₹500), exact + near-duplicate detection,
  India GST validation (CGST/SGST/IGST splits, HSN slabs, recompute from
  lines). All 6 exception codes flow to the exception queue.
- **Exception handler**: per-code root-cause analysis with cited evidence
  + recommended fix (LLM-backed when a client is configured, deterministic
  templates otherwise — the queue never blocks on the LLM) and a
  context-aware vendor-query draft (drafts only, never sent).
- **HITL approvals**: `interrupt()` at the approval queue; approve/reject
  endpoints really resume the paused run (`Command(resume=...)`) and the
  bookkeeper posts afterwards. Rejected runs end rejected, nothing posts.
- **Bookkeeper**: balanced double-entry posting (Dr 5000 / Cr AP-2000) to
  SQLite, idempotent per invoice; payment scheduled on the
  discount-optimized date (2/10 Net 30 NPV) or the due date. Payments are
  scheduled, never executed.
- **Aging**: buckets current / 1–30 / 31–60 / 61–90 / 90+ from due dates,
  served by `/api/aging`; overdue totals feed `/api/metrics`.
- **Metrics**: real numbers from the store — match rate, auto-approve
  rate, false-approve rate (from the last saved eval run), median cycle
  time (upload → terminal audit), money caught in exceptions, aging totals.
- **Eval harness** (`app/eval/`): seeded synthetic corpus (7 scenarios);
  `POST /api/eval/run` reports matcher numbers AND graph numbers (corpus
  run end-to-end through the LangGraph with mocked extraction).
  False-approve rate 0 on both. Benchmark loader stays code-only — the HF
  dataset is never downloaded.
- **Discount optimizer + cash-flow forecast**: parses "2/10 Net 30"-style
  terms, NPV vs cost of capital, per-invoice pay-now/pay-later
  recommendation; weekly projected outflows with over-budget flags.
- **SSE trace**: the graph run is streamed (`stream_mode="updates"`) and
  every node event is written to the append-only audit log as it happens;
  the trace endpoint streams that stored trail.
- **Lifecycle state machine** (`app/domain/lifecycle.py`): received →
  extracted → validated → matched → approved | flagged → posted →
  scheduled → paid; duplicate / on_hold / rejected as side states.
  Extraction failure routes straight to audit — nothing downstream runs
  on unextracted data.
- **Migrations** (`app/infra/db.py::migrate`): `create_all()` never adds
  columns to an existing table, so every new column is also registered in
  `_MIGRATIONS` and added with ALTER TABLE on boot. A shipped `elric.db`
  from before a column existed boots cleanly instead of 500ing.

## What's stubbed / still needs live verification

- **Real Gemini extraction**: the client + fallback exist and are wired in,
  but no live call was made during this build (quota rule — both keys
  preserved). Live-verify with a real key + a real invoice PDF: upload →
  extracted fields → match → approve → ledger.
- **Benchmark loader**: `app/eval/benchmark.py` is loader code only; the HF
  dataset is never downloaded implicitly.
- **Postgres checkpointer**: falls back to MemorySaver locally (no
  `psycopg`/libpq in the dev venv); Postgres when `DATABASE_URL` is set
  and the driver is installed.
- **v1.1 features** (§16b): vendor scoring, anomaly detection, policy
  interpreter, NL analytics, email triage, FX, ERP connectors — not built.
- **Auth**: phase 2 (§13); APIs are auth-shaped (`approver` on decisions)
  but unauthenticated in v1.

## Docker

```bash
docker build -t elric-backend .
docker run -p 8000:8000 --env-file .env elric-backend
```

Multi-stage, `python:3.12-slim`, `uv` installs, non-root user, HEALTHCHECK.
Target ~250 MB.

## Config

See `.env.example`. Secrets only via env vars — never in code or git.
