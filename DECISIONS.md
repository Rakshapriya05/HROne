# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicates, lookups, sort); `(department, joined_on)` and `joined_on` (headcount). `attendance_logs`: unique `(emp_code, date)` (one record per day, race guard, monthly query); `(date desc, emp_code)` (list order, date ranges); `(status, date desc, emp_code)` (status filter); `(emp_code, punch_in desc)` (punch-out lookup). Rejected: an index on `late_minutes`; the date index already narrows the leaderboard scan.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which becomes 409. There is no read-before-write.

3. **Ties.** `$rank` gives tied employees the same rank and skips the next. `limit` filters on rank, so a tie at the cutoff returns every tied employee, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end), joins that month's logs, and keeps employees whose join is empty. Zero-log people still count once.

5. **100x data.** Store monthly per-employee totals, updated on punch and correction, so analytics read small summaries instead of aggregating millions of logs.
