# WS5 decision record — onboarding experiment design (Initiative 12)

Date: 2026-09-13. Workstream: WS5 (onboarding). Status: decided; fixes from
the blind adversarial review applied in `initiatives/i12/onboarding.py`.

## Rule 2 — genuine options, in "this option trades away X to get Y" form

### A. Experiment shape: 3x3 grid vs collapsed axes vs single-path control

- **3x3 grid (role maturity x technical comfort):** trades away statistical
  power per cell (each cell gets ~1/9th of traffic, so real differences take
  longer to detect) to get guidance matched to the visitor's actual
  situation — the whole point of the experiment is learning *which guidance
  activates whom*, not whether onboarding beats no onboarding.
- **Collapsed axes (3 paths by role maturity only):** trades away the
  technical-comfort signal (seniors who are low-comfort get the same terse
  path as high-comfort ones, risking drop-off) to get 3x the sample per arm
  and faster reads.
- **Single-path control (one canonical onboarding for everyone):** trades
  away all personalization learning to get a clean, fast baseline and the
  simplest possible copy to keep dark-pattern-free.

**Decision: 3x3 grid.** Chosen because the product's thesis is that
mismatched guidance is why people bounce; power per cell is bought back by
restricting comparisons to within-cell and to the common qualified-activation
outcome (see analysis plan in `experiment_summary`).

### B. Outcome definition: per-cell goals vs common outcome

- **Per-cell goals (each cell measured against its own goal_workflow):**
  trades away cross-cell comparability (a jd_decode completion is not a
  risk_check completion — comparing rates across cells is invalid) to get
  outcomes that match what each path was actually built to activate toward.
- **Common outcome (one qualified-activation definition for all cells):**
  trades away fidelity to each path's intent (a cell whose goal is the rare
  role_compare looks "worse" through a common lens) to get clean cross-cell
  comparisons and a single dashboard number.

**Decision: common outcome definition, reported per goal_workflow, never
pooled.** `detect_qualified_activation` is a pure function over the fixed
`QUALIFIED_ACTIVATION_SCHEMA`; cross-cell raw-rate comparisons are documented
as invalid in the analysis plan. This trades away per-cell fidelity to get
honest comparability — and keeps the confound explicit instead of hidden.

### C. Assignment identity: hash-bucket on session token vs stable user id

- **Hash-bucket on the anonymous session token:** trades away stable
  per-user identity (a user who returns on a new token is a new bucket) to
  get zero PII handling — there is no user id to store, leak, or get wrong.
- **Stable user id:** trades away anonymity simplicity (now there is an
  identity store, with all its privacy surface) to get true per-user
  stickiness and no re-randomization on return visits.

**Decision: session token, logged as sha256 prefix only.** Chosen because
assignment is currently deterministic by grid cell (the token does not route),
so the stickiness question is moot today; the token's only job is making
misjoins diagnosable via `ASSIGNMENT_LOG`. The "never re-randomized"
guarantee is documented with its explicit assumption: callers must keep
tokens stable per user across the experiment window *if* variant arms are
ever reintroduced. This trades away per-user stickiness guarantees to get a
PII-free design.

## schedule_driven

`schedule_driven: false` — this decision is event-driven (shipped with the WS5
fixes on 2026-09-13), not calendar-driven. Revisit triggers: (1) first real
variant arms are proposed — the phantom-arm removal must be replaced with
real variant copies and the token-stability assumption re-examined; (2) a
dark-pattern override is registered — the copy rules get a review pass;
(3) any cell's qualified-activation rate is compared across different
goal_workflows in a dashboard — the analysis plan forbids it.

## ops_goal

`ops_goal: {metric: qualified_activation_rate_per_goal_workflow, direction: increase, guardrail: never optimize application volume or submission counts}` —
the experiment exists to raise the rate at which visitors complete the core
workflow their path points at, measured by `detect_qualified_activation`
over the common schema; application/submission volume is a banned north star
(per telemetry `BANNED_METRICS`) and must not become the target.

## Review findings closed here

1. Dark-pattern rules now cover resetting countdowns, nagging loops,
   confirm-shaming decline variants, delete-via-email roach motels, and
   non-"free" paywalls; pre-checked consent moved to the UI-definition
   layer (`register_consent_toggle`) because copy strings cannot express
   checkbox state.
2. Phantom `-v{bucket}` variant arms removed; `assign_path` returns the
   canonical path id; `experiment_summary()["paths"]` enumerates exactly the
   ids `assign_path` can return.
3. Session-token lifecycle requirement documented in the `assign_path`
   docstring (stable tokens required; assumption named, not assumed).
4. Experiment de-confounded via the documented analysis plan
   (within-cell or common-outcome comparisons only); `role_compare` defined
   in `GOAL_WORKFLOWS`.
5. Qualified-activation event schema specified
   (`QUALIFIED_ACTIVATION_SCHEMA`); detection is the pure function
   `detect_qualified_activation`. Telemetry wiring is a follow-up for the
   integration sweep (telemetry.py untouched); see
   `initiatives/i12/integration_notes.md`.
6. Fail-closed override mechanism (`register_dark_pattern_override` +
   `OVERRIDE_AUDIT`) so one future false positive cannot brick all 9 paths.
7. Assignment logging (`ASSIGNMENT_LOG`, sha256 token prefixes) makes the
   18-vs-9 misjoin class diagnosable.
