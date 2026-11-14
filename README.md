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
pytest -q   # 74 tests; GEMINI_API_KEY is forcibly unset by tests/conftest.py
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
| GET | `/health` | |

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
