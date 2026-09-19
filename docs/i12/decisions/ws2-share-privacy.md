# WS2 — share/privacy decisions (Initiative 12, 2026-09-13)

Constitutional format: genuine options (Rule 2, each phrased
"trades X for Y"), a `schedule_driven` flag (Rule 3), and an `ops_goal`
naming a metric and direction (Rule 4).

Honest framing: this is a local pre-launch library. Veto has no public
users yet, no production traffic, and no measured detection statistics —
every "metric" below is a test-enforced target, not an observed number.
Nothing here invents users, incidents, or launch dates.

## D1: scanner design (two-gate fail-closed + frozen artifacts)

- **Chosen: regex/heuristics scanner run twice — at build time AND at
  render time — over deep-frozen (immutable) artifacts.**
  - Trades away caller convenience (artifacts can't be edited after
    build; every render pays a scan) to get a fail-closed guarantee: no
    code path can construct-or-mutate-then-render an artifact without
    passing the scan. The blind review's C1 bypass (mutable dict +
    render-time blind spot) is closed from both ends.
  - F1 correction (second rework): the first rework shipped a depth-6
    recursion cutoff in scan_value that silently returned [] past the
    cutoff — a hand-built dict nested 7+ deep published its content
    through render_markdown, falsifying the "fails closed on any dict"
    claim. The cutoff is gone: the scan now walks containers to full
    depth (iterative walk, no recursion limit to hit) with a visited set
    guarding against cyclic structures. Nothing is silently passed at any
    depth.
- **Surviving option B: name-pattern-only scanner (no value heuristics).**
  - Trades away value-side detection (emails, phones, markers, and names
    smuggled inside whitelisted fields) to get a near-zero false-positive
    rate and a rule set a reviewer can audit in one sitting. Survives as
    the fallback if the heuristics ever false-positive on real labels —
    the name-pattern gate alone still blocks the highest-risk field names.
- **Surviving option C: typed dataclass artifacts with per-field
  validators instead of scanned dicts.**
  - Trades away dict/JSON simplicity (the web UI and terminal currently
    render the same plain dict for free) to get construction-time type
    guarantees that make whole bug classes unrepresentable. Survives as
    the v2 direction if the artifact surface grows beyond three kinds.

- `schedule_driven`: false — this work was correctness-driven (a blind
  review KICK_BACK), not calendar-driven; no launch date was traded.
- `ops_goal`: "content-bearing artifacts reaching render output — down to
  zero at any nesting depth (full-depth iterative scan, no cutoff,
  cycle-guarded — proven by the C1 regression tests including the depth-8
  hostile-dict render test); false-positive detections on the clean
  fixture suite — zero."

## D2: bare-name gap (C2) — person-name detection for name-token fields
(F2 second rework)

The scanner's docstring claimed "names" were covered, but the person-name
value check applied only to the exact field `name`: `{"candidate_name":
"Alex Rivera"}`, `{"hiring_manager": "Alex Rivera"}`, `{"username":
"Alex Rivera"}` and the like all passed clean — the `\bname\b`
forbidden-name pattern never matches underscored compounds, and nothing
checked the values.

- **Chosen: person-name value check on any field whose name contains a
  `name` token.** The field name is split on underscores and camelCase
  boundaries and checked for a "name" token; a token merely *containing*
  "name" counts, so `candidate_name`, `full_name`, `display_name`,
  `contact_name`, `userName`, and `username` are all covered. `name`
  itself keeps the check (C2: factor entries legitimately use it for
  factor names like "skills", but a multi-word capitalized value there is
  a person's name). Fields in `SAFE_FIELD_NAMES` other than `name` are
  exempt from the *value* check: `product_name` was added there, so
  `product_name="Cloud Architecture"` does not fire — its values are
  still scanned for emails/phones/markers/quotations/long text like
  everything else, and it matches no forbidden *name* pattern, so the
  forbidden-name check is unaffected.
  - Trades away some precision to get the docstring's claim actually
    enforced. Two documented residuals: (1) compounds like "filename"
    also match the token rule, so a multi-word capitalized value there
    would fire — accepted residual false-positive risk; (2) fields with
    no "name" token at all (e.g. "hiring_manager") are NOT covered —
    accepted residual false-negative risk. Rationale for the
    SAFE_FIELD_NAMES scoping over accepting the product_name false
    positive: pinned schema fields are known metadata labels by
    construction, so exempting them costs no true-positive coverage.
    Rationale for keeping the C2 `name` exception instead of scoping the
    whole check to non-SAFE fields: the `{"name": "Alex Rivera"}` case is
    the highest-risk shape (it is a real builder field) and must keep
    firing.
- `schedule_driven`: false. `ops_goal`: "person-name values passing the
  scanner in name-token fields — down to zero for multi-word names
  (test-pinned for name, candidate_name, full_name, display_name,
  contact_name, username); product_name='Cloud Architecture' pinned as
  non-firing; single-token names, non-name-token fields (hiring_manager),
  and the filename-style false-positive trade-off remain documented
  residual risk — no broader claim."

## D3: short-prose blind spot (C5) — quotation heuristic + stated boundary

Sub-280-char resume/JD quotations in `evidence_summary`/`top_reasons`/`why`
passed the scanner when they contained no marker words.

- **Chosen: add a verbatim-quotation heuristic (quoted spans ≥40 chars
  fire), and document the remaining boundary honestly.** Short, unquoted,
  marker-free prose under the long-text threshold is *indistinguishable
  from a legitimate paraphrase* — no honest detector can catch it without
  drowning clean labels in false positives, so it is accepted residual
  risk, not a claimed capability. The test suite pins both sides: a long
  quoted span fires; short unquoted prose passes.
  - Trades away perfect recall (some pasted prose will always pass) to
    get a scanner whose every claimed detection is test-proven — no
    invented detection, no marketing copy in the docstring.
- `schedule_driven`: false. `ops_goal`: "quoted-span quotations passing
  the scanner — down to zero for spans ≥40 chars (test-pinned)."

## D4: methodology link (C6) — honest TBD placeholder

- **Chosen: `METHODOLOGY_URL = "TBD — public methodology URL set by the operator
  at launch"`, rendered verbatim into shared markdown.**
  - Trades away a clickable link in pre-launch shares to get honesty: a
    relative repo path ("docs/i12/methodology.md") is meaningless once the
    markdown leaves this machine, and inventing a real-looking URL would
    be fabrication. The operator sets the real URL at launch; the placeholder
    cannot be mistaken for a live link. (Note: `initiatives/i12/tools.py`
    has its own `METHODOLOGY_LINK` with the same relative path — out of
    WS2 scope, flagged for its owner.)
- `schedule_driven`: false. `ops_goal`: "shared artifacts containing a
  relative or invented methodology URL — zero (test-pinned)."

## D5: observability on detection (O3)

- **Chosen: module-level `detection_count` + optional `set_detection_hook`
  in privacy.py, fired from `assert_clean` on every ContentDetected.**
  - Trades away zero-overhead purity (one counter increment and a log
    line per detection) to get a production-visible signal that the
    control is live. Triage expectation: a share-path detection means
    caller code let user data reach a builder — fix the caller; a
    telemetry-path detection belongs to the tripwire protocol in
    telemetry.py. The hook can never break fail-closed (its exceptions
    are swallowed) and findings never carry offending values.
- `schedule_driven`: false. `ops_goal`: "detections observable without
  reading code — counter increments and hook fires on every raise
  (test-pinned)."

## D6: threshold rationale (C9) — labeled estimates, no depth cutoff

`LONG_TEXT_CHARS` (280) and `QUOTED_SPAN_CHARS` (40) are documented
estimates, not measured optima: 280 sits well below real resume/JD prose
and well above any legitimate metadata label; 40 is long enough that clean
short labels don't quote at that length. If production ever shows false
positives/negatives at these values, they are tuning knobs with tests
pinned to the boundary behavior — not guarantees. There is deliberately
no depth cutoff (F1): the scan walks containers to full depth with an
iterative, cycle-guarded walk, so no nesting depth can silently pass —
the old depth-6 cutoff was removed in the second rework for exactly this
reason.

## D7: schema-evolution story (C7)

`SHARE_VERSION` bumps on any artifact-schema change; unknown versions are
malformed to consumers. `SAFE_FIELD_NAMES` is a cross-module contract —
adding a name exempts it from the forbidden-name check for share AND
telemetry, so additions need reviewer sign-off plus a `SHARE_VERSION`
bump. Chosen over a separate per-module allowlist: one definition of
"content field" was the original design intent, and splitting it would
let the two scanners drift apart silently.

## Accepted residual risks (F5 and others)

Marker-phrase evasion by hyphenation or punctuation ("responsible-for",
"responsible.for") passes the CONTENT_MARKERS heuristics: the markers are
literal regexes over prose, and any fixed phrase list can be defeated by
trivial obfuscation. This is inherent to marker heuristics, not a bug in
the list — closing it would take either a semantic model (out of scope
for a local pre-launch library) or a marker list so aggressive it drowns
legitimate labels in false positives. It is accepted residual risk; the
two-gate scan still catches the same content through the email, phone,
person-name, quotation, and long-text checks whenever those signals are
present, and this record claims nothing more.

Other accepted residuals, each test-pinned where stated: short, unquoted,
marker-free prose under LONG_TEXT_CHARS (D3); single-token person names
like "Plato" (D2); person names in fields with no "name" token, e.g.
"hiring_manager" (D2); the filename-style false-positive trade-off of the
name-token rule (D2); thresholds as estimates, not guarantees (D6).
