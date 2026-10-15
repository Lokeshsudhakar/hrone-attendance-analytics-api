# DECISIONS.md

1. **Indexes.** Idempotent at startup: unique `employees.emp_code` (duplicate creates collide -> one 201, rest 409); unique `attendance_logs (emp_code, date)` (the record's natural key, which makes punch-in race-safe and serves per-employee lookups); `(date desc, emp_code asc)` for GET /attendance's filter + sort and every pipeline's month `$match`; `(department, joined_on)` and `(joined_on)` for the headcount `$match`. I rejected `(emp_code, punch_out)`: records per employee are few, and `(emp_code, date)` already orders them identically since `date` derives from `punch_in`.

2. **Punch-in race.** Both requests pass the friendly duplicate check, then both `insert_one`; the unique index rejects the second with `DuplicateKeyError`, which maps to 409. The database decides the winner - no check-then-insert window.

3. **Ties.** Sort by `total_late_minutes desc, emp_code asc`, `$setWindowFields` pushes totals seen so far, rank = 1 + count strictly greater. Ties share a rank, next rank skipped (1, 2, 2, 4). `limit` filters `rank <= limit` **after** ranking, so a tie at the cutoff can return more rows than `limit` - that is the contract.

4. **Headcount.** The summary starts from `employees` (`joined_on <= month end`, R9) and groups by department **before** the `$lookup`, with `preserveNullAndEmptyArrays`: zero-log people keep their department and count toward `headcount` while contributing zero metrics.

5. **One change for 100x data.** Per-day `$lookup` rows in the trend become the bottleneck; I would precompute daily department rollups into a materialised collection fed by change streams, keeping the trend a single indexed read.
