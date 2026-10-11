# Starter code review

The following defects were identified in the supplied `app/main.py`. Tests should be run against MongoDB 6.0+ and include the edge cases noted below.

| # | Where | What is wrong | How to notice it | Fix applied |
|---|---|---|---|---|
| 1 | `health` | Always reports ready without contacting MongoDB. | Stop MongoDB and call `GET /health`. | Ping the database; return 503 on a database error. |
| 2 | `create_employee` | Duplicate check followed by insert is race-prone and does not validate all contract fields. | Send simultaneous creates for the same `emp_code`. | Unique database index; catch duplicate-key errors; validate request fields and shift times. |
| 3 | `create_employee` | `created_at` is a Python datetime in the response rather than epoch milliseconds. | Inspect the response schema. | Serialize the server timestamp to integer epoch milliseconds. |
| 4 | `list_employees` | Pagination uses `page * page_size`, skipping the first page. | Request page 1 with page size 2. | Use `(page - 1) * page_size`. |
| 5 | `list_employees` | `total` counts all employees rather than the filtered department. | Filter to a department and compare total with filtered records. | Count using the same query filter. |
| 6 | `list_employees` | Results are not sorted by `emp_code`; pagination values are not constrained. | Seed out-of-order codes and use invalid page sizes. | Sort ascending and enforce page/page-size bounds. |
| 7 | `punch_in` | Unknown employee is dereferenced and can produce a 500. | Punch in using an unknown code. | Return 404 before accessing shift fields. |
| 8 | `punch_in` | Epoch seconds/strings/floats and invalid timestamp ranges are not rejected; local machine time is used. | Submit seconds, a float, a string, or run on a non-IST machine. | Strict integer epoch-millisecond validation, UTC storage, and second truncation. |
| 9 | `punch_in` | Attendance date ignores the overnight-shift rule. | Punch in after midnight for a 22:00–06:00 shift. | Derive attendance date in IST and assign early-morning punches to the prior shift date. |
| 10 | `punch_in` | Late calculation incorrectly measures lateness from the grace-period boundary and does not use whole elapsed minutes as specified. | For a 09:30 shift, test 09:40:01 and 10:15:59. | Apply the strict 10-minute threshold, then count whole minutes since shift start. |
| 11 | `punch_in` | Check-then-insert can create duplicate attendance records under concurrent requests. | Fire parallel punch-ins for one employee/date. | Enforce a unique compound index on `(emp_code, date)` and translate duplicate-key errors to 409. |
| 12 | `list_attendance` | Loads the entire matching result set into Python and sorts/slices it there. | Run against 100,000 records; memory and response time grow with result size. | Count in MongoDB and use indexed sort, skip and limit. |
| 13 | `list_attendance` | Does not validate date formats or `date_from <= date_to`. | Supply malformed dates or reverse the range. | Validate both dates and return 422 for an invalid range. |
| 14 | `list_attendance` | Does not consistently normalize timestamps/history and relies on default MongoDB output. | Inspect legacy documents and response types. | Normalize attendance fields and nested audit timestamps at the API boundary. |
| 15 | `compute_work_hours` | Python `round` uses ties-to-even behaviour, not the required half-up rounding. | Test durations at a half-centesimal boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 16 | `compute_overtime` | Treats every positive minute as overtime and does not correctly handle an overnight shift end. | Test 29 and 30 minutes after shift end, plus an overnight shift. | Use the actual shift-end instant and apply the 30-minute threshold. |
| 17 | `punch_out` (missing) | No implementation for closing an open record, validating the 24-hour bound, or handling concurrent punch-outs. | Call the endpoint or send two simultaneous requests. | Implemented with conditional atomic update and 404/409/422 outcomes. |
| 18 | `regularize_attendance` (missing) | No validation/recalculation/audit append or concurrency protection. | Correct a time twice or submit concurrent corrections. | Implemented validation, derived-field recomputation, changed-field audit and compare-and-swap update. |
| 19 | Analytics endpoints (missing) | Required monthly, department, leaderboard and trend reports are absent. | Call the documented analytics paths. | Added MongoDB aggregation pipeline implementations. |
| 20 | Explain endpoint (missing) | No explain route to return execution statistics. | Call `/admin/explain/{endpoint}`. | Added explain command for the supported endpoint pipelines/queries. |

## Reviewed and intentionally retained

- `emp_code` remains the public business identifier; MongoDB `_id` is never returned.
- Attendance dates remain ISO date strings, while instants are stored as BSON datetimes and exposed as epoch milliseconds.
- Analytics read stored derived fields instead of recalculating them from punch timestamps, as the contract requires.
- `ABSENT` and `LEAVE` are not created by punch-in; they are only supported as existing data or a regularization status.
