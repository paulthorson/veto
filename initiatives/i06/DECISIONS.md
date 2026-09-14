# Initiative 06 — Decisions

## 2026-09-14 — Observed-gap rule thresholds: provisional CEO decision

**Context.** `ObservedGapRule` (in `longitudinal.py`) parameterizes the
roadmap's Q1 gate definition of an "observed gap": the same disclosed
rubric dimension scoring below `threshold` in at least `repeat_count` of
the last `window` completed sessions, or an explicit user selection.
The standing plan reserves final approval of these numbers to Paul plus
an independent framework reviewer by 2027-01-10
(`RULE_APPROVAL_DEADLINE`); until approval is recorded every rule
payload carries `"pending_approval": true`.

**Decision (provisional, CEO persona).** The shipped defaults stand as
the provisional operating values:

- `window = 3`
- `repeat_count = 2`
- `threshold = 70`

Rationale: they encode the roadmap's Q1 gate definition verbatim
(same dimension below threshold in 2 of 3 sessions); the hard boundary
— recommendations built ONLY from observed gaps or explicit user
selections, never invented weakness labels — holds regardless of the
numbers. Payloads continue to disclose `pending_approval: true` so no
consumer mistakes provisional values for approved ones.

**What changes this.** Paul and/or the independent framework reviewer
approving different numbers on or before 2027-01-10: the engine adopts
them without a code change (parameters, not constants). This file is
updated at that point and `RULE_PENDING_APPROVAL` flips to `False`.

**What this is not.** Not a waiver of review, not a veto override, and
not Paul's approval — it is a documented provisional default so the
initiative does not block on a future-dated human gate. Only Paul can
clear a constitutional veto; none is open on Initiative 06.
