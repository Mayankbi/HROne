# Employee Attendance & Analytics API

FastAPI + MongoDB backend for employee attendance tracking, manual corrections, immutable audit logging, and analytical aggregations according to the OpenAPI 2.0.0 specification and business rules R1–R10.

---

## 1. Prerequisites
- Python 3.11+ (Python 3.13 tested)
- MongoDB 6.0+ (Tested against MongoDB 8.2 / Atlas 7.0+)
- Virtual environment (`venv` or `conda`)

---

## 2. Environment Variables & Setup

Configuration is read from environment variables; `.env` is loaded automatically if present, with OS environment variables taking priority:

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection URI | `mongodb://localhost:27017` |
| `MONGO_DB` | MongoDB database name | `attendance_db` |

### Setup Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Seed Sample Data
Load sample seed documents from `sample_data/` into MongoDB:
```bash
python sample_seed.py
```

---

## 4. Run the API
Start the application with Uvicorn:
```bash
uvicorn app.main:app --port 8000 --reload
```
The API is available at `http://localhost:8000` and Swagger documentation is interactive at `http://localhost:8000/docs`.

---

## 5. Run Tests
Execute the comprehensive test suite covering business rules, concurrency race protections, regularization audits, analytics pipelines, and explain plans:
```bash
pytest tests/test_api.py -v
```

---

## 6. Endpoints Overview

| Method | Path | Description | Notes |
|---|---|---|---|
| `GET` | `/health` | Service & DB readiness probe | 200 on DB ping ok; 503 otherwise |
| `POST` | `/employees` | Create employee | 201 created; 409 duplicate emp_code |
| `GET` | `/employees` | List employees | Filter by department; paginated; sorted |
| `POST` | `/attendance/punch-in` | Record punch-in | Race-safe; R1 (IST/overnight), R2 (late) |
| `POST` | `/attendance/punch-out` | Record punch-out | Atomically closes open record; R3-R5 |
| `GET` | `/attendance` | List attendance records | Filter by employee, date range, status; indexed |
| `PATCH`| `/attendance/{emp_code}/{date}` | Regularize attendance | Appends to immutable history; recomputes fields |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary | MongoDB aggregation pipeline; R7 working days |
| `GET` | `/analytics/departments/summary` | Department summary | Cross-collection aggregation; headcount includes 0-log emps |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard | Competition ranking with `$setWindowFields` / `$rank` |
| `GET` | `/analytics/departments/{dept}/trend` | Department trend | Gap-filling with `$densify`, 7-day moving avg |
| `GET` | `/admin/explain/{endpoint}` | Explain query plans | Returns MongoDB `executionStats` query plans |

---

## 7. Business Rules Implemented (R1 – R10)
- **R1 Time Zone & Attendance Date:** All shift calculations and attendance dates are IST (UTC+05:30). Overnight shifts (`shift_end <= shift_start`) attribute punch-ins before `shift_end` to the previous calendar day. Instants are truncated to whole seconds before calculation.
- **R2 Late Calculation:** 10-minute grace period. Punch-ins strictly after grace period calculate late minutes from `shift_start` (floor minutes).
- **R3 Overtime:** Counted only if punch-out is at least 30 minutes after shift end (floor minutes). Overnight shifts correctly adjust shift end to next calendar day.
- **R4 Work Hours:** `(punch_out - punch_in)` in seconds / 3600, rounded half-up to 2 decimals using `Decimal`.
- **R5 Half Day:** `half_day = True` if rounded work hours < 4.50. Counts as 0.5 present day.
- **R6 Presence:** `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.
- **R7 Working Days:** Monday to Friday. Evaluated from `joined_on` for mid-month joiners. Weekend records add to overtime and late totals, but only weekday records contribute to `present_days`.
- **R8 Rounding:** 2 decimals half-up for counts/hours; 4 decimals half-up for rates.
- **R9 Headcount:** Employees with `joined_on <= period_end` whether or not they have logs.
- **R10 Pagination:** Default page size 20, max 100, page 1-indexed. Total reflects filtered query count.

---

## 8. Database Indexes
Created idempotently at application startup:
- `employees.emp_code` (unique)
- `employees.(department, emp_code)`
- `employees.joined_on`
- `attendance_logs.(emp_code, date)` (unique)
- `attendance_logs.(date, emp_code)`
- `attendance_logs.(emp_code, date, status)`
- `attendance_logs.(date, late_minutes)`
