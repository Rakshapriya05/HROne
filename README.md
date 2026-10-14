# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne engineering assignment: punch-in/out, manual corrections with an audit trail, and four MongoDB-aggregation analytics endpoints. All application code is in `app/main.py`, as the assignment requires.

## Status: what is and is not verified

Please read this first; it is the honest state of the project.

- **Passing here:** business rules R1-R5, validation (422/503 paths), employees, punch-in/out, listing, PATCH and audit history, and the analytics endpoints. The analytics tests ran against a small reference interpreter of the aggregation operators (`tests/mini_mongo.py`) and against an independent pure-Python oracle.
- **Not yet run against a real MongoDB server:** the sandbox this was built in had none. `tests/test_mongo_integration.py` (concurrency, unique indexes, `IXSCAN`/no-`COLLSCAN` explain plans) and the `[mongodb]` variant of every other test **skip until you point them at MongoDB** (commands below). Until that run is green, the aggregation pipelines are verified for logic but not for real-server behaviour.

## Features

- Employees: create (unique `emp_code`, enforced by a unique index), list with filter and pagination.
- Attendance: punch-in (IST attendance date, overnight-shift rule, late minutes), punch-out (work hours, overtime, half day, works across midnight), filtered and paginated listing.
- Regularization (`PATCH`): recomputes derived fields, appends one audit entry with only the fields that changed, optimistic concurrency (409 for the loser).
- Analytics (MongoDB aggregation pipelines): employee monthly summary, department summary, late leaderboard with competition ranking, department daily trend with 7-day moving average.
- `GET /admin/explain/{endpoint}`: `executionStats` explain of the exact query each endpoint runs.
- Indexes are created idempotently at startup.

Stack: Python 3.11+, FastAPI, PyMongo (synchronous), Pydantic v2, MongoDB 6.0+ (7.0 recommended). Tests: pytest, FastAPI `TestClient`, mongomock.

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
Copy-Item .env.example .env           # then edit MONGO_URI / MONGO_DB if needed
```

**MongoDB, option A: local Docker**

```powershell
docker run -d --name attendance-mongo -p 27017:27017 mongo:7
```

**MongoDB, option B: Atlas free tier (M0)**: create a cluster, add a database user, allow your IP, then set in `.env`:
`MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority`

Real environment variables override `.env`. Never commit `.env`.

## Run

```powershell
python sample_seed.py                  # optional: loads the 9 sample documents (it clears both collections first)
uvicorn app.main:app --port 8000 --reload
```

Swagger UI: <http://localhost:8000/docs> (OpenAPI JSON at `/openapi.json`). Readiness: `GET /health`.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -v                                     # no MongoDB needed; real-MongoDB tests are skipped with a reason
$env:TEST_MONGO_URI = "mongodb://localhost:27017"      # with MongoDB running: now they all execute
python -m pytest -v -rs
```

Tests use a throw-away database `attendance_test_<random>` that is dropped afterwards; they never touch `MONGO_DB`. `-rs` prints why anything was skipped.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /health` | 200 only if MongoDB answers a ping, else 503 |
| `POST /employees`, `GET /employees` | Create (409 on duplicate `emp_code`); list by department, sorted by `emp_code` |
| `POST /attendance/punch-in` | Create the day's record (R1, R2); exactly one 201 under races |
| `POST /attendance/punch-out` | Close the latest open record (R3-R5); exactly one 200 under races |
| `GET /attendance` | Filter by employee, dates, status; sorted `date` desc then `emp_code`; paginated |
| `PATCH /attendance/{emp_code}/{date}` | Regularize one record; recompute; append to `history` |
| `GET /analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| `GET /analytics/departments/summary` | Per-department monthly summary |
| `GET /analytics/leaderboard/late` | Late-comers ranking (ties share a rank) |
| `GET /analytics/departments/{department}/trend` | One row per day, with 7-day moving average |
| `GET /admin/explain/{endpoint}` | MongoDB explain for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend` |

## Interpretations of ambiguous points

- **Moving average:** the mean of the attendance rates as reported (already rounded to 4 decimals), then rounded half-up.
- **`avg_work_hours`, `late_count`, `leave_count`, `on_duty_count`:** weekend records are included; only `present_days` is limited to Mon-Fri (R7 says weekend records still count toward late and overtime totals).
- **Trend range limit:** at most 92 days inclusive (so 1 Jul to 30 Sep is allowed, 93 days is 422).
- **Punch-in on a date that already has an `ABSENT`/`LEAVE` record:** 409 (one record per employee per day).
- **Punch-out:** if the latest record is already closed it answers 409 before the 24-hour 422 check.
- **PATCH "changes nothing":** judged on `status`, `punch_in`, `punch_out`; derived fields are still recomputed and appear in `history` only if their value changed.
- **Rounding:** MongoDB `$round` and Python `round()` are half-to-even, so half-up is done with `Decimal` or integer arithmetic.
- **Trend calendar:** built with `$range` inside the pipeline instead of `$densify`, so a range with no records at all still returns every day.

## Known limitations

- Real-MongoDB verification is pending (see Status).
- Monthly `working_days` is computed by a small calendar helper rather than inside the pipeline; the data aggregation itself is in MongoDB.
- Unrecognised `emp_code` values in `GET /attendance` simply return an empty page.
- No authentication (none was specified); `regularized_by` is free text.
