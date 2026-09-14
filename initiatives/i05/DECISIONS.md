# Initiative 05 — decision record

Constitutional format: every entry carries genuine options (Rule 2,
each phrased "trades X for Y"), a `schedule_driven` flag (Rule 3), and
an `ops_goal` naming a metric and direction (Rule 4).

## WS1 — versioned resume variants (2026-09-13)

### D1: variant storage format
- **Chosen: one JSON file per variant** (`resume_variants/<id>.json`,
  atomic tmp+rename writes).
  - Trades away concurrent-writer safety and query power to get zero
    new dependencies and human-inspectable files.
- **Rejected: SQLite.**
  - Trades away dependency-free simplicity to get transactions and
    indexed queries — overkill for a single-user local tool at v1.
- **Rejected: one JSON per version.**
  - Trades away single-file branch readability to get smaller writes —
    versions are small; file-count explosion is the worse cost.
- `schedule_driven`: false. `ops_goal`: "variant restore correctness —
  100% of restores byte-identical in tests".

### D2: restore semantics
- **Chosen: checkout moves the head pointer; history stays append-only**
  with an audit entry.
  - Trades away a pristine linear history to get non-destructive
    restore (the old head is never lost).
- **Rejected: copy-on-restore (duplicate the old version as new head).**
  - Trades away history compactness to get a strictly linear log —
    rejected: it hides that a restore happened.
- `schedule_driven`: false. `ops_goal`: "restore time after mistaken
  tailoring — down (one checkout call, no re-render)".

### D3: statement trace matching
- **Chosen: exact normalized match only.**
  - Trades away tolerance for pasted/edited statements to get a
    mechanically honest proof-of-value (fuzzy matching could claim a
    trace that isn't real).
- **Rejected: fuzzy substring matching.**
  - Trades away trace soundness to get convenience — rejected after
    adversarial review found it could false-positive.
- `schedule_driven`: false. `ops_goal`: "untraced-statement false
  negatives — zero in tests".

### D4: cross-initiative contracts before owners land
- **Chosen: contract-first with assumption flags; validate on read.**
  - Trades away live integration to get unblocked parallel builds
    (Initiatives 01/02/04 land independently).
- `schedule_driven`: true — what we give up: end-to-end trace against
  the real decoder/evidence store today; what we save: the studio does
  not idle waiting on three other teams.
- `ops_goal`: "integration rework when contracts land — down (adapters
  already validate; only the source of records changes)".

## WS2 — evidence library (2026-09-13)

### D5: library storage
- **Chosen: single JSON file** (`evidence_library/library.json`,
  atomic tmp+rename).
  - Trades away per-item history to get one-file inspectability and
    the same write pattern as the variant store.
- **Rejected: one file per item.**
  - Trades away file-count simplicity to get granular history —
    rejected: git history on the store is out of scope (store is
    gitignored user data).
- `schedule_driven`: false. `ops_goal`: "evidence-item write
  correctness — 100% round-trip in tests".

### D6: derivation method
- **Chosen: verbatim copy from profile fields.**
  - Trades away extraction smarts to get a hard no-invention
    guarantee (text is copied, never generated).
- **Rejected: LLM/similarity-based extraction.**
  - Trades away the no-invention guarantee to get normalization —
    rejected: any paraphrase is a fabrication risk.
- `schedule_driven`: false. `ops_goal`: "invented-fact incidents from
  derivation — zero".

### D6b: evidence trust model + corrupt-store handling (2026-09-13, blind-review rework)
- **Chosen: two-origin tagging (`derived` vs `asserted`) + profile-ref
  validation when a profile is supplied + loud refusal on a corrupt store.**
  - Trades away add_item signature simplicity to get a machine-checked
    honesty anchor: `add_item(..., profile=...)` rejects sources that
    don't resolve and text that isn't verbatim; without a profile the
    item is labeled `asserted` and can never look identical to a
    profile-derived one; retrieval surfaces `origin` and ranks derived
    first on ties. A corrupt `library.json` raises `CorruptStoreError`
    on reads AND writes, leaving the bytes untouched for manual
    recovery. `build_from_profile` dedups on `(kind, text, source)`;
    writes fsync before the atomic rename; `_load` enforces the
    contracts.py schema-version promise.
- **Rejected: convention-only source strings (any non-empty string).**
  - Trades away the honesty guarantee to get a one-arg-fewer API —
    rejected: the blind review demonstrated a stored fabrication with
    fake `experience[0].bullets[0]` provenance that looked identical
    to a derived item.
- **Rejected: backup-then-proceed on a corrupt store.**
  - Trades away the fail-loud guarantee to get uninterrupted operation
    — rejected: silently continuing normalizes data loss; the corrupt
    bytes stay exactly where they are until a human recovers them.
- `schedule_driven`: false. `ops_goal`: "fabricated-source incidents
  reaching the tailor path — zero".

## WS3 — ATS readiness check (2026-09-13)

### D7: copy safety mechanism
- **Chosen: closed message templates + runtime contract sweep.**
  - Trades away free-form wording to get a mechanically verifiable QA
    contract (every emitted message checked against the banned-phrase
    list at creation and at report assembly).
- **Rejected: convention-only ("be careful in code review").**
  - Trades away verifiability to get writing freedom — rejected: the
    QA contract is a release blocker and must be machine-checked.
- `schedule_driven`: false. `ops_goal`: "vendor-ranking-claim incidents
  in shipped copy — zero".
- `resume_variants/` and the evidence-library store are gitignored:
  they hold the user's personal resume data and must never be
  committed.

## WS4 — diff explanations + application packet (2026-09-13)

### D8: change classification
- **Chosen: mechanical diff classification** (delete+insert line
  matching for moves; section-aware added/removed).
  - Trades away semantic understanding of edits to get deterministic,
    explainable output with no invention risk.
- **Rejected: LLM-generated change summaries.**
  - Trades away determinism to get fluent prose — rejected: a summary
    could describe a change that didn't happen.
- `schedule_driven`: false. `ops_goal`: "unexplained changes reaching
  the checklist — zero (every diff opcode becomes a change)".

### D9: packet approval model
- **Chosen: checklist-gated approval with content hash; any review-state
  edit reverts approval to draft.**
  - Trades away approval convenience to get a hard guarantee that
    "approved" always means "the exact reviewed content passed every
    check".
- **Rejected: approval as a sticky flag.**
  - Trades away the guarantee to get fewer re-approvals — rejected: a
    sticky flag could bless content that changed after review.
- `schedule_driven`: false. `ops_goal`: "packets approved with pending
  checks — zero".

## WS6 — earned-success feedback (2026-09-13)

### D10: Initiative 00 gate adapter
- **Chosen: caller-supplied `gate_open` boolean.**
  - Trades away a live contract to get a shippable module today:
    `success_moment()` takes `context["gate_open"]`, which the webui
    owner will source from Initiative 00's outcome events per
    integration_notes.md.
- **Rejected: blocking WS6 on Initiative 00 landing.**
  - Trades away schedule to get a live contract — rejected: the gate
    logic itself (state matrix, dismissal, reduced motion) is fully
    testable against the adapter, and the swap is a one-line wiring
    change later.
- `schedule_driven`: false. `ops_goal`: "delight shown in a prohibited
  state — zero (full state-matrix test)".

### Known accepted limitations
- Concurrent writers can lose an update (last-write-wins; tmp+rename
  guarantees no torn writes). Single-user local tool at v1.
- Packet `ats-ready` checklist copy overclaims (2026-09-14 final-mile
  close-out; post-review docs-only addition, no behavior change). The
  label reads "ATS readiness check passes (no failures)" and the pending
  detail reports "N failing check(s)", but since the Epic 3 honest-verdict
  fix, blocking *warnings* (e.g. `length_warn`) also hold `ready=False` —
  so a resume can show "0 failing check(s)" while still pending. Accepted
  at close; reword tracked as follow-up F2 (UX copy review + blind
  re-review).
