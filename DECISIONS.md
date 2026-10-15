# Engineering Decisions

## 1. Keep the assignment implementation easy to inspect

The application is kept in a single `main.py` file for the scope of this
take-home assignment. This avoids unnecessary package structure while
keeping API routes, shared helpers, and MongoDB access easy to locate
during review.

Within the file, related code should remain grouped in a predictable
order:

1.  Imports, configuration, and database connection
2.  Input/output models and validation
3.  Shared date, time, and rounding helpers
4.  Employee and attendance routes
5.  Analytics routes
6.  Admin explain route

Shared calculations belong in named helpers rather than being copied
between endpoints. If the implementation grows beyond the assignment's
scope, the natural next step would be to separate models, services, and
routes into modules.

## 2. Use MongoDB for data-intensive analytics

Analytics should use MongoDB aggregation pipelines for filtering,
grouping, joins, date expansion, and window calculations rather than
loading an entire collection into Python. This keeps work close to the
data and matters for large-data evaluation.

Indexes should support predicates and ordering used by the actual
queries. The explain endpoint should inspect a representative version of
the real query or aggregation pipeline. It should not create indexes on
every request, and its sample filters should stay aligned with the
endpoint being explained.

## 3. Treat date and time handling as an API-boundary concern

Attendance timestamps are interpreted in IST, while Unix timestamps must
be converted using the unit defined by the API contract. Date-only
values use a consistent `YYYY-MM-DD` representation for comparisons and
grouping.

The working-day rule is Monday through Friday, with no holiday calendar.
Monthly working days begin at the later of the first day of the month
and the employee's joining date. These rules are explicit so analytics
remain deterministic and testable.

## 4. Make attendance status and rounding rules explicit

`PRESENT`, `WFH`, and `ON_DUTY` count as presence. A half-day presence
contributes `0.5`; a full-day presence contributes `1.0`. `ABSENT` and
`LEAVE` do not contribute to presence credit.

Use decimal half-up rounding where the contract specifies it, and apply
rounding at the defined output boundary. Keep percentage/rate precision
separate from the precision used for work hours or minute totals.
Weekend records can contribute to late and overtime totals, but do not
add to weekday `present_days`.

## 5. Validate inputs close to the endpoint

Validate inputs before building a database query. Examples include:

-   Month values must follow `YYYY-MM`.
-   Date ranges must be ordered and no longer than the specified
    maximum.
-   Leaderboard limits must stay within the allowed range.
-   Unknown employees or departments should return the documented
    not-found response.
-   Unsupported explain endpoint names should be rejected explicitly.

Use consistent HTTP status codes and actionable error messages. Avoid
catching every exception and converting it to a generic success-shaped
response; unexpected database errors should remain visible to logs and
tests.

## 6. Prefer explicit response shapes

Keep endpoint response structures aligned with their contracts. Do not
mix a bare list with a wrapper object such as
`{ "month": ..., "items": [...] }` unless that difference is specified
by the API. Return numeric values as numbers and use `null` only where
the contract allows it.

## 7. Use tests to define and preserve behavior

Automated tests should cover normal cases and boundaries: empty result
sets, no-record days, weekends, half-days, mid-month joiners, invalid
inputs, and multiple departments with overlapping dates. Query-plan
tests should inspect the winning plan, not merely search the full
explain output for any occurrence of `IXSCAN`, because a rejected plan
may also contain that stage.

These decisions prioritize correctness, straightforward review, and the
ability to explain the implementation during a live code walkthrough.
