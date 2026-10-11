# Technical decisions

1. **Indexes.** `emp_code` is unique for employees; `(emp_code, date)` is unique for attendance and makes duplicate punch-ins impossible. Date/employee, employee/date, status/date, department/join date, and join-date indexes support the list and report filters. I avoided a large index containing every attendance field because it would increase write cost without serving a clear query.
2. **Punch-in race.** Both requests can reach the insert, but MongoDB's unique `(emp_code, date)` index allows only one insert. The winner receives 201; the duplicate-key loser receives 409. An application-level existence check would not be sufficient.
3. **Ties.** `$rank` assigns the same rank to equal late-minute totals and skips the next rank. The pipeline sorts ties by `emp_code`; the API applies `limit` after ranking, so all employees tied at a rank within the limit are returned.
4. **Headcount.** Department summary starts from employees who joined by month end, then looks up attendance. Employees with no logs therefore remain in the aggregation and count toward headcount.
5. **100x data.** I would measure query plans and workload first, then consider pre-aggregated daily summaries, a background rollup job and pagination strategies that avoid large offsets. I would preserve the raw attendance history for auditability.
