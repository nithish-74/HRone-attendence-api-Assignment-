# DECISIONS.md

1. **Indexes.** On `employees`, unique `emp_code` guarantees customer code uniqueness, while `(department, joined_on)` and `joined_on` power fast headcount filtering. On `attendance_logs`, compound unique `(emp_code, date)` guarantees one record per employee-day. `(date, -1, emp_code, 1)` supports attendance listing and date range scans. `(date, 1, late_minutes, 1)` powers the leaderboard aggregation, and `(emp_code, 1, punch_in, -1)` accelerates punch-out lookups. I considered and rejected a standalone index on `date`, because the compound index on `(date, -1, emp_code, 1)` already covers date prefix queries.

2. **Punch-in race.** Both requests execute input validation and attempt to insert `{emp_code, date, ...}`. MongoDB's compound unique index on `(emp_code, date)` ensures that the database engine atomically accepts only the first write. The concurrent second insert raises a `DuplicateKeyError`, which our application catches to cleanly return HTTP 409 Conflict without check-then-insert race conditions.

3. **Ties.** The leaderboard pipeline ranks records via `$setWindowFields` using MongoDB's `$rank` (standard competition ranking: 1, 2, 2, 4) ordered by `total_late_minutes` descending. The `limit` filter (`rank <= limit`) applies strictly after ranking. If two employees tie at the cutoff rank, both share that rank and are both returned, sorted deterministically by `emp_code` ascending.

4. **Headcount.** The department summary aggregation starts from `employees` rather than `attendance_logs`, filtering by `joined_on <= end_date`. It joins logs using `$lookup` and unwinds with `preserveNullAndEmptyArrays: true`. Consequently, active employees with zero logs still contribute 1 to department headcount and 0 to attendance sums.

5. **One thing you would change for 100x data.** At 100x scale (~10 million records), I would introduce daily incremental pre-aggregated rollups (or materialized summary collections updated via change streams / `$merge`) to avoid scanning cross-collection lookups on every request.
