# Review: Known Defects and Regression Coverage

## Purpose

This review records correctness risks identified during development of
the attendance analytics API and the expected behavior regression tests
should protect. Automated tests should be treated as the source of truth
for whether each item is fixed.

## Findings

### 1. Index creation in the explain endpoint

**Problem:** The explain endpoint attempted to create an index on
`(emp_code, date)` without `unique=True`, while an index with the same
generated name already existed as a unique index. MongoDB rejected the
request with `IndexKeySpecsConflict` (code 86), resulting in HTTP 500.

**Expected resolution:** The explain endpoint should inspect a query
plan without recreating indexes on every request. Index creation belongs
in application setup or a deliberate setup/migration step, with index
options kept consistent.

**Regression coverage:** Exercise each supported explain target and
assert that a valid request returns a query plan rather than HTTP 500.
For the employee-monthly query, verify that the winning plan uses the
compound `(emp_code, date)` index (`IXSCAN`) for the employee equality
and date-range predicates.

### 2. Timestamp unit consistency

**Risk:** Mixing Unix seconds and milliseconds can silently produce
dates near 1970 or shift attendance to the wrong day, especially when
the API contract specifies milliseconds.

**Expected resolution:** Convert timestamps according to the API
contract at the input boundary, then use one consistent internal
representation. Keep date-only attendance fields separate from timestamp
values.

**Regression coverage:** Test a known millisecond timestamp, a missing
timestamp where allowed, and invalid or out-of-range values. Assert the
resulting IST date/time and validation response.

### 3. Department trend must be department-scoped

**Risk:** A daily trend can return plausible numbers while accidentally
counting attendance from employees in other departments.

**Expected resolution:** For each date, calculate `present_count` and
`late_count` only from employees belonging to the requested department.
Calculate headcount from that department's employees whose `joined_on`
date is on or before the relevant day.

**Regression coverage:** Seed records for at least two departments on
the same date. Verify that the trend for one department excludes the
other department's presence and lateness. Also test employees who join
during the requested range.

### 4. Daily trend gap filling and moving average

**Expected behavior:** Return exactly one row per calendar day in the
inclusive range, including weekends and dates with no attendance
records. Weekends remain in the output, but their `attendance_rate` is
`null`. On working days with positive headcount, a day with no presence
has a rate of `0`.

The seven-row moving average should use only non-null `attendance_rate`
values in the current row and up to six preceding rows within the
requested range. It should not treat a `null` weekend rate as zero.

**Regression coverage:** Test a range containing weekdays, a weekend, a
no-record day, and a half-day record. Verify row count, date sequence,
rate precision, and moving-average values.

### 5. Monthly attendance calculations

**Expected behavior:** Working days are Monday through Friday, with no
holiday calendar applied. For a mid-month joiner, the working-day
denominator begins on the later of the month start and the employee's
`joined_on` date. Presence statuses (`PRESENT`, `WFH`, `ON_DUTY`)
receive full credit unless marked half-day, which receives `0.5`.
`ABSENT` and `LEAVE` do not count as presence. Weekend logs may
contribute to lateness and overtime totals but must not increase weekday
`present_days`.

**Regression coverage:** Include full-day, half-day, leave, absent,
weekend, and mid-month-joiner cases. Verify rounding at the API
boundary, including half-up rounding where specified.

### 6. Input validation and response contracts

**Risk:** Invalid months, reversed date ranges, overly long ranges,
invalid limits, unknown departments, and unsupported explain targets can
cause confusing errors or inconsistent response shapes.

**Expected resolution:** Reject invalid inputs with deliberate HTTP
status codes and clear error messages. Keep response fields and wrapper
shapes consistent with the API contract.

**Regression coverage:** Test malformed month strings, `to < from`,
trend ranges over 92 days, limits outside the allowed range, unknown
departments, nonexistent employees, and unsupported explain endpoint
names. Assert status codes and response bodies.

## Verification notes

Run the complete test suite after changes and retain focused regression
tests for each finding. Do not mark a finding resolved solely because
one Swagger request returns HTTP 200; verify returned values and the
winning query plan too.
