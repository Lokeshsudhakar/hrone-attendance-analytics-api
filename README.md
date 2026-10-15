# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne Software Engineer Trainee assignment
(contract v2.0, rules R1-R10 in `openapi.yaml`, storage layout in `DATA_MODEL.md`).

All application code lives in a single file: `app/main.py`. It starts with the
expected command:

```bash
uvicorn app.main:app --port 8000
```

Interactive docs are served at `http://localhost:8000/docs`, and a small
console UI at `http://localhost:8000/dashboard` (redirect from `/`) lets you
call every endpoint from a form and see its HTTP status + JSON output side by
side. The UI is a single embedded HTML page served by the same app; it is not
part of the OpenAPI contract and adds no endpoints to the schema.

## What is implemented

Every path and schema in `openapi.yaml`, with the status codes and business rules
the contract specifies:

| Area | Endpoints |
| --- | --- |
| Health | `GET /health` |
| Employees | `POST /employees`, `GET /employees` (filter + cursor pagination) |
| Attendance | `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance` (filters + cursor pagination), `PATCH /attendance/{emp_code}/{date}` (regularize + append-only audit) |
| Analytics (aggregation pipelines) | `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend` |
| Diagnostics | `GET /admin/explain/{endpoint}` (real `explain` with `executionStats`) |
| Dashboard (extra) | `GET /dashboard` — a small read-only HTML page; **not** in the contract |

Rules R1-R10 are enforced server-side: epoch-ms input with sub-second truncation,
PRESENT/ABSENT/LEAVE/ON_DUTY status derivation, `punch_out > punch_in` validation,
late-minute computation against a 9:30 start, exactly-one-open-record, 409 on
double punch-in, optimistic-concurrency regularize, `Decimal` ROUND_HALF_UP
rounding everywhere, R9 headcount, and R10 index-backed reads (no COLLSCAN).

## Indexes

Created idempotently at startup (`ensure_indexes` in the lifespan handler):

- `employees`: unique `emp_code` (duplicate create -> one 201, rest 409);
  `(department, joined_on)` and `(joined_on)` for headcount `$match` stages.
- `attendance_logs`: unique `(emp_code, date)` (the natural key, which makes
  concurrent punch-ins race-safe); `(date desc, emp_code asc)` for the
  `GET /attendance` sort and the month-range `$match` of every pipeline.

`GET /admin/explain/attendance_list` returns the live `explain` output and the
test suite asserts `IXSCAN` (and no `COLLSCAN`) against it.

## Concurrency

- **Punch-in race:** both requests can pass the friendly duplicate check, then both
  `insert_one`. The unique index rejects the second with `DuplicateKeyError`, which
  is mapped to 409. The database decides the winner; there is no check-then-insert
  window.
- **Punch-out race:** the update is conditioned on `punch_out: None`, so only one
  of two concurrent punch-outs wins.
- **Regularize (PATCH):** optimistic concurrency via `history: {$size: seen}`; the
  loser gets 409. Every change appends an immutable audit entry to `history`.

## Dashboard (extra, not in the contract)

`GET /dashboard` serves one self-contained HTML page (embedded in `app/main.py`)
that visualises the API with plain `fetch()` calls: department summary, late
leaderboard, per-employee monthly stats and the latest punches. It holds no
business logic and never talks to MongoDB - it is a read-only client of the
public endpoints, which is why its numbers always match the API exactly.
`/dashboard` is marked `include_in_schema=False`, so the documented API surface
remains exactly the contract's 12 routes.

## Analytics

All four analytics endpoints are MongoDB aggregation pipelines (no Python loops
over raw documents). Notable stages:

- `monthly`: `$match` on the month, `$lookup` + `$unwind` logs, per-status
  counters in `$group`.
- `departments/summary`: groups employees by department **before** the `$lookup`
  (with `preserveNullAndEmptyArrays`) so zero-log employees keep their department
  and count toward `headcount` (R9).
- `leaderboard/late`: `$group` by employee, then `$setWindowFields`
  (`"unbounded"` bounds) to rank with ties sharing a rank (1, 2, 2, 4);
  `limit` filters `rank <= limit` after ranking.
- `departments/{department}/trend`: `$densify` over the date range (exclusive
  upper bound on MongoDB 7, so the bound is `to + 1 day`), 7-day moving average
  via `$setWindowFields`; rates are finalized with exact half-up rounding.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# MongoDB 6.0+ (the grader uses 7.0). Local example:
#   docker run -p 27017:27017 mongo:7
export MONGO_URI="mongodb://localhost:27017"   # Windows: $env:MONGO_URI="..."
export MONGO_DB="hrone_attendance_db"

python sample_seed.py             # loads the nine sample documents

uvicorn app.main:app --port 8000 --reload
```

`MONGO_URI` and `MONGO_DB` come from the environment; a local `.env` is loaded
for convenience but real environment variables always win. MongoDB Atlas free
tier (M0) works too.

## Tests

`test_api.py` is a standalone live-API test suite (~185 checks) covering the
business rules, the concurrency races, the aggregation outputs against
hand-computed expectations, and the explain/IXSCAN assertions:

```bash
# start the server first, then:
python test_api.py http://127.0.0.1:8000
```

Last run in this environment: **186/186 checks passed** (MongoDB 7.0.14 local).

## Review and decisions

- `REVIEW.md`: defects found in the starter kit and how they were addressed.
- `DECISIONS.md`: the five design questions (indexes, punch-in race, ties,
  headcount, one 100x-scale change).

## Not verified

- Atlas connectivity from this sandbox (TLS blocked); local MongoDB 7.0.14 was
  used instead.
- Explain plans at the 100k-document scale; verified on the seeded dataset
  only.
