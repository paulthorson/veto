# WS-A decision record: rubric/modes disclosure-parity fixes

Date: 2026-09-13. Workstream: Initiative 06 WS-A (`initiatives/i06/rubric.py`,
`initiatives/i06/modes.py`, `tests/test_i06_rubric_modes.py`).
Trigger: blind review KICK_BACK — three major disclosure-vs-implementation
mismatches, plus three minors and a nit. This workstream's contract is
exact disclosure ("no hidden factors"), so every fix makes the card and
the code agree exactly; the card is never quietly rewritten to bless a
bug, and the code is never quietly changed under a stale card.

`schedule_driven: false` — no schedule pressure drove these calls. Where a
faster option existed (rewrite the card to bless the existing code), the
slower option (implement what the card promised) was chosen, twice.

## Decision 1 — jargon qualifiers: implement them for real

Options considered:

- **A (chosen): implement the disclosed context qualifiers with documented
  heuristics.** This option trades away true part-of-speech accuracy to get
  the disclosed semantics with stdlib-only, deterministic code: "leverage"
  counts only as a verb (inflected forms always count; the base form is
  skipped after a noun-phrase word like "the"/"financial"/"debt"),
  "bandwidth" counts only for people (skipped after a network word like
  "network"/"internet"/"data"), all matching word-boundary, and the fixed
  list is exactly the 6 disclosed terms.
- **B (rejected): keep plain-substring matching and rewrite the card to
  disclose it.** This option trades away the promised semantics to get a
  one-line fix. Rejected: the card promised context-aware scoring, and
  quietly downgrading the promise after a review caught the mismatch is
  exactly the "disclosure drift" this workstream exists to prevent.
- **C (rejected): real NLP (POS tagging) for the qualifiers.** This option
  trades away the stdlib-only, zero-dependency, deterministic design to get
  linguistically correct classification. Rejected: it would make the rubric
  engine non-deterministic across environments and unreviewable by the
  blind-review loop, which executes the code as ground truth.

Honest limits of option A (disclosed, not hidden): the "verb" test is a
one-token lookbehind, not grammar — "use leverage to grow" is counted as a
verb use, and an unusual noun construction ("brand leverage") may be
missed. The card documents the heuristic, and the decision record (this
file) documents that it is a heuristic.

## Decision 2 — unreachable 100 ceilings: rebalance to reach 100

Options considered:

- **A (chosen): rebalance component weights so a perfect answer scores
  exactly 100.** This option trades away the old point values (evidence
  personal-decision +10 → +20; clarity base 75 → 80, bottom-line +10 → +15)
  to get the disclosed promise that 100 is attainable. The card now shows
  its own arithmetic (e.g. "30 + 20 + 15 + 15 + 20 = 100"), which the
  parity tests verify.
- **B (rejected): keep the weights and disclose the true maxima (90).**
  This option trades away the "capped at 100" promise to get zero code
  change. Rejected: a rubric whose ceiling is unreachable trains candidates
  to chase points that do not exist — worse than either alternative.

## Decision 3 — website-answerable questions: implement the −30 formula

Options considered:

- **A (chosen): implement the disclosed formula** (start 30, add the three
  positive signals, then −30 for website-answerable, floor 0). This option
  trades away the simplicity of the flat-10 short-circuit to get the
  disclosed behavior: a website-answerable question with every positive
  signal scores exactly 70, and positive signals are never silently
  discarded.
- **B (rejected): keep flat-10 and disclose it.** This option trades away
  signal preservation to get simpler code. Rejected: discarding all
  positive signals for a weak question contradicts the rubric's own
  "traceable points" contract.

## Decision 4 — conclusion markers: disclose exactly 4, require content

Options considered:

- **A (chosen): remove the 3 undisclosed markers ("i shipped", "the
  outcome", "ultimately") and require the marker sentence to carry
  content** (a number or ≥ 6 words). This option trades away a little
  recall on terse-but-real conclusions ("We shipped.") to get precision:
  an empty "Ultimately, things happened" can no longer buy +15.
- **B (rejected): keep the markers and disclose that no content check is
  done.** This option trades away scoring integrity to get zero code
  change. Rejected: the reviewer's example proved the +15 was unearned.

## Decision 5 — "because" needs a real clause

Options considered:

- **A (chosen): "because" counts only with a 5+ word clause.**
  ("The sky is blue because I like it." → no reason credit.) This option
  trades away credit for genuinely terse reasons ("because it was faster")
  to get a disclosed, testable bar for what counts as a reason. The choice
  verbs ("chose", "picked", …) still count on plain match, as disclosed.
- **B (rejected): disclose the looseness.** Rejected for the same reason
  as decision 4B: the card promises judgment, and "because I like it" is
  not judgment.

## Decision 6 — STAR markers: remove the loose ones

Options considered:

- **A (chosen): remove "had to", "at the time", "was facing"; match the
  rest word-boundary.** This option trades away a little recall on
  casually-worded real stories to get precision: the reviewer's lunch
  example now scores 40 (action only) instead of 60+.
- **B (rejected): publish the full marker list in the card.** This option
  trades away card readability to get to keep loose markers. Rejected: the
  structure card promises "explicit STAR markers", and a published list
  containing "at the time" would make the looseness official rather than
  fixed.

## Decision 7 — banned-token check: word-boundary everywhere

- **Chosen: word-boundary regex in both the FOLLOWUPS loop and the MODES
  loop** (previously substring in the first, regex in the second). This
  option trades away the (purely theoretical) catch of a banned token glued
  inside another word to get zero false positives ("dumbo" must not trip
  "dumb"). No genuine alternative: a safety check that false-positives on
  ordinary words is worse than useless.

## Revisit triggers

Reopen this record (append-only entry) if any of these fire:

1. A blind review finds another card/code mismatch in WS-A scope.
2. Real practice answers hit 100 on clarity at a rate suggesting the
   markers are too loose (tripwire, not a target — see ops goal).
3. User reports of false jargon flags ("leverage"/"bandwidth" heuristic
   wrong in a way the disclosed rule does not cover).
4. The Q1 threshold gate (the operator + independent framework reviewer) changes
   scoring weights or the 70 default — the parity tests must be re-run
   against the new card.

## Ops goal

- **Metric:** share of scored mock-interview answers hitting exactly 100
  on any rubric dimension, per week.
- **Direction:** keep low and investigate spikes. A healthy rubric makes
  100 genuinely hard; a rising share means a marker or formula got loose.
- **Anti-vanity guardrail:** this metric is a tripwire, not a KPI. Do not
  tune markers to hit a "target" 100-rate, and do not cite a low rate as
  proof of quality — the proof is the blind review plus the parity tests,
  not the distribution. If the rate moves, the response is a new decision
  record entry, not a silent constant tweak.
