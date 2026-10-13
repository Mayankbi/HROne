# DECISIONS.md

### 1. Indexes
We created the following indexes at startup via `init_indexes()`:
- `employees`:
  - `{"emp_code": 1}` (`unique=True`): Enforces unique employee codes and accelerates single-employee lookups (O(1)).
  - `{"department": 1, "emp_code": 1}`: Covers `GET /employees` queries filtered by department and sorted by `emp_code`.
  - `{"joined_on": 1}`: Optimizes headcount filtering in department summary analytics (`joined_on <= month_end`).
- `attendance_logs`:
  - `{"emp_code": 1, "date": 1}` (`unique=True`): Enforces the core domain rule of at most one attendance record per employee per day and supports natural key lookups for `PATCH /attendance/{emp_code}/{date}`.
  - `{"date": -1, "emp_code": 1}`: Covers `GET /attendance` default sort and date range filters, guaranteeing fast cursor paging on 100k+ records without memory sorting.
  - `{"emp_code": 1, "date": 1, "status": 1}`: Powers `GET /analytics/employees/{emp_code}/monthly` index-only scans.
  - `{"date": 1, "late_minutes": 1}`: Optimizes leaderboard and monthly queries filtering records by date and late minutes.

*Rejected Index*: We considered creating an index on `{"history.at": 1}`, but rejected it because `history` is purely an audit trail nested subdocument array queried only during full record retrieval, never as a filter target.

### 2. Punch-in race
When two identical punch-in requests arrive simultaneously for the same employee and date:
1. Both requests retrieve the employee profile to resolve shift timing.
2. Both compute the deterministic attendance date (IST / R1) and build the candidate attendance document.
3. Both issue an atomic `insert_one` against MongoDB.
4. Because `attendance_logs` has a unique compound index on `(emp_code, date)`, MongoDB's WiredTiger engine accepts exactly one insert and rejects the other with a duplicate key error (code 11000).
5. The winning thread receives the inserted document and returns HTTP 201 Created.
6. The losing thread catches `pymongo.errors.DuplicateKeyError` and cleanly returns HTTP 409 Conflict with an informative error message. Neither request leaves corrupted state.

### 3. Ties
When two employees tie on total late minutes at the cutoff of `limit`:
Our leaderboard aggregation uses MongoDB 6.0+'s `$setWindowFields` with the `$rank` operator:
```json
{
  "$setWindowFields": {
    "sortBy": {"total_late_minutes": -1},
    "output": {"rank": {"$rank": {}}}
  }
}
```
Standard competition ranking assigns the identical rank to tied employees and skips subsequent ranks (e.g., 1, 2, 2, 4). The filtering stage `{"$match": {"rank": {"$lte": limit}}}` applies strictly *after* ranking. Consequently, if two employees tie at rank `limit`, both qualify and are returned in the response. Tied rows are deterministically ordered by `emp_code ASC`.

### 4. Headcount
The `GET /analytics/departments/summary` aggregation pipeline starts from the `employees` collection rather than `attendance_logs`:
1. It matches employees belonging to the department with `joined_on <= month_end` (R9).
2. It performs a `$lookup` into `attendance_logs` matching only logs for that employee and target month.
3. An `$unwind` with `"preserveNullAndEmptyArrays": True` ensures employees with zero attendance records are preserved.
4. The subsequent `$group` stage uses `{"$addToSet": "$emp_code"}` to measure `headcount` from the set of active employees, correctly including employees with zero logs while omitting employees who join in subsequent months.

### 5. One thing you would change for 100x data
At 100x data (~10 million attendance records and 100k+ employees), computing cross-collection aggregations (like department summaries and trends) on-the-fly from raw daily logs creates high CPU overhead. I would introduce daily pre-aggregated rollup collections (e.g. `department_daily_rollups`) maintained asynchronously via MongoDB Change Streams or background cron jobs, allowing analytical endpoints to query pre-computed daily summaries in O(1) time while sharding `attendance_logs` horizontally on `{emp_code: "hashed"}`.
