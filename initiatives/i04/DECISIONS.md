# Initiative 04 — decision record (resume decoder, Epic 1)

Constitutional format: every entry carries genuine options (Rule 2,
each phrased "trades X for Y"), a `schedule_driven` flag (Rule 3), and
an `ops_goal` naming a metric and direction (Rule 4).

## Epic 1 — resume_decoder (2026-09-13)

### D1: PII handling — mask on emit, never store values
- **Chosen: emit-time masking (`_redact_pii`) on every emitted
  string, `pii_present` records shapes only.**
  - Trades away the convenience of verbatim quotes to get a hard
    guarantee: no raw email/phone value can ever reach a downstream
    surface (share cards, grill profiles). The summary path was
    missed on first pass and fixed under adversarial review —
    every emitted string is now audited for the same hole.
- **Rejected: strip PII at ingest, keep clean text.**
  - Trades away quote fidelity to get simpler downstream code —
    rejected: mangled ingest text would corrupt every quote and the
    resume hash would no longer match the user's document.
- **Rejected: no masking; downstream surfaces scrub.**
  - Trades away a single enforcement point to get lazier decoder
    code — rejected: one missed downstream consumer leaks PII.
- `schedule_driven`: false. `ops_goal`: "raw contact values in
  decoded output — zero in tests (blob-wide assertions)".

### D2: skill matching — word-boundary tokens, never bare substring
- **Chosen: token matching with boundary lookarounds for every
  keyword (`_keyword_in_text`).**
  - Trades away recall on glued compounds to get zero hallucinated
    "explicit" skills: "Java" no longer fires inside "JavaScript",
    "Rust" inside "trust", "Agile" inside "fragile". The boundary
    class also covers `+#.` so "C++" and "Node.js" still match as
    whole tokens.
- **Rejected: bare substring matching.**
  - Trades away precision to get maximal recall — rejected after
    adversarial review found it fabricating "explicit"-confidence
    skills from unrelated words.
- **Rejected: embedding similarity.**
  - Trades away determinism and the stdlib-only guarantee to get
    fuzzy recall — rejected: non-deterministic matching breaks the
    honesty invariant (every claim needs a verbatim quote).
- `schedule_driven`: false. `ops_goal`: "substring-hallucinated
  skills — zero in regression tests".

### D3: seniority inference — title lines preferred, highest rank wins
- **Chosen: ranked keyword table; title lines first, then bullets,
  then other lines; highest rank in a pass wins.**
  - Trades away the simplicity of first-match-wins to get correct
    readings: "Senior Software Engineer (ex-intern)" resolves to
    senior, and a "director" mentioned in a bullet never outranks an
    actual senior title.
- **Rejected: fixed ladder order, first match wins.**
  - Trades away correctness to get three lines of code — rejected:
    it systematically reported the *lowest* rung ("ex-intern"
    beat "Senior").
- **Rejected: scan all lines flat, no title preference.**
  - Trades away title signal to get uniform code — rejected: body
    prose ("reported to the director") is weaker evidence than the
    candidate's own title line.
- `schedule_driven`: false. `ops_goal`: "seniority mislabels on
  title-vs-bullet fixtures — zero in regression tests".

### D4: section segmentation — full-line header match only
- **Chosen: `fullmatch` on the stripped, colon-trimmed line.**
  - Trades away tolerance for decorated headers ("Experience —
    2019–2024") to get structural honesty: a body line like
    "Experience building APIs for clients" can never hijack section
    routing again.
- **Rejected: unanchored `search` for the header word.**
  - Trades away precision to get header tolerance — rejected: it
    silently re-homed body text into the wrong section.
- `schedule_driven`: false. `ops_goal`: "body lines promoted to
  section headers — zero in regression tests".

### D5: years-claim attribution — longest leading known phrase
- **Chosen: multi-word capture after "N years", attributed to the
  longest leading phrase that is a known skill.**
  - Trades away single-token simplicity to get real attribution:
    "4 years Machine Learning experience" attributes Machine
    Learning; "6 years Python building data pipelines" still
    attributes Python. The attributable set is exactly the names
    `skills_found` can emit, so attribution and emission stay
    consistent (alias-only names like "Infrastructure as Code" are
    never attributed).
- **Rejected: single-token capture.**
  - Trades away multi-word skills to get a simpler regex —
    rejected: "Machine Learning" could never be attributed.
- **Rejected: attribute the whole captured phrase verbatim.**
  - Trades away the known-vocabulary guard to get maximal
    attribution — rejected: "8 years of experience building" would
    become a bogus skill claim.
- `schedule_driven`: false. `ops_goal`: "misattributed or
  unattributable years claims on fixtures — zero in regression
  tests".

### D6: decoder architecture — deterministic stdlib-only, no model
- **Chosen: regex + counting, pure function, quote on every claim.**
  - Trades away language understanding (synonyms beyond the curated
    alias table, paraphrase) to get determinism, zero network, and
    the same honesty invariant as `jd_decoder`: the decoder reports
    what the text states and labels the rest.
- **Rejected: model-backed extraction.**
  - Trades away determinism, offline operation, and quote
    grounding to get recall — rejected: a model can invent
    employers and metrics, which this initiative exists to prevent.
- `schedule_driven`: false. `ops_goal`: "non-deterministic output
  across runs — zero (hash-stable in tests)".

## WS1 — schemas.py frozen contracts (2026-09-13)

### D7: tri-state evidence links — no "maybe" status
- **Chosen: tri-state `supported` / `gap` / `grill_question`, with the
  extension-key policy making no-free-text-assertion contractual.**
  `supported` needs ≥1 verbatim quote; `gap` carries no evidence and is
  never upgraded by the system; `grill_question` must link a
  `grill_question_id` and carries no evidence. Undeclared non-`x_` keys
  on entries are rejected, so a free-text `assertion` smuggled onto an
  entry fails validation instead of flowing through.
  - Trades away expressive nuance — there is no way to say "probably
    supported" — to get a contract in which the decoder can never emit
    an unsupported claim.
- **Rejected: confidence score 0–1 per link.**
  - Trades away machine-checkable honesty to get finer-grained
    ranking — rejected: a 0.62 "supported-ish" is an assertion without
    a quote, and no validator can tell whether the number was earned.
- **Rejected: binary supported/gap, ambiguity forced into gap.**
  - Trades away the grill's question pipeline to get a simpler
    contract — rejected: alias/quantification ambiguity is real and the
    grill exists to resolve it.
- `schedule_driven`: false. `convenience_driven`: false (driven by the
  honesty requirement, not implementation ease). `ops_goal`:
  free-text assertions emitted on `gap`/`grill_question` entries —
  metric: occurrences per 1,000 validated maps; direction: decrease to 0.

### D8: share-card scrub — every 24-char window, step 1
- **Chosen: test *every* 24-char window (step 1) of every payload
  string — dict keys included — against the raw inputs, plus a
  whitespace-collapsed corpus so re-flowing a line break into a space
  does not hide a leak.**
  - Trades away scan speed — O(n) windows per string instead of
    O(n/12) — to get alignment-independent soundness: any verbatim run
    of ≥24 chars is caught regardless of where it sits. Payloads are
    tiny, so the cost is negligible.
- **Rejected: 24-char windows with step 12.**
  - Trades away soundness to get ~12× fewer comparisons — rejected:
    adversarial review demonstrated that any 24–35-char run misaligned
    to the step grid evades the check entirely (caught at offsets 0
    and 12 only).
- **Rejected: full-line matching only.**
  - Trades away fragment detection to get simplicity — rejected:
    leaks rarely align to line boundaries; a 24-char run inside a
    longer field is the realistic attack.
- `schedule_driven`: false. `convenience_driven`: true — the original
  step-12 stride was chosen for fewer comparisons and the adversarial
  review caught the unsoundness; this rework corrects it.
  `ops_goal`: verbatim ≥24-char raw-input runs reaching users — metric:
  occurrences per 1,000 share-card validations; direction: decrease to
  0 (the gate fails closed: no raw inputs, no certification).

### D9: hash anchoring — verify the binding, don't just record it
- **Chosen: `evidence_source_hash = "sha256:" + first-16-hex of
  SHA-256 over the exact source text`, with `verify_evidence_binding()`
  recomputing the hash against supplied source text and requiring every
  evidence quote to be a verbatim substring of that text.**
  - Trades away flexibility — any edit, even whitespace, invalidates
    the map, and verifiers must hold the raw text — to get
    tamper-evident staleness detection with no silent divergence.
- **Rejected: hash recorded but never verified (presence-only).**
  - Trades away the entire anti-staleness guarantee to get zero
    verifier complexity — rejected: a hash nobody checks is a
    silent-divergence vector, worse than no hash because it looks like
    a guarantee.
- **Rejected: hash over the canonical JSON of the decoded profile.**
  - Trades away sensitivity to text edits that don't change the decode
    to get stability across re-decodes — rejected: the binding must
    anchor to what was actually analyzed (the raw text), not a derived
    view of it.
- `schedule_driven`: false. `convenience_driven`: false (driven by the
  staleness hazard, not ease). `ops_goal`: maps applied with a
  mismatched source hash — metric: occurrences per 1,000 binding
  verifications; direction: decrease to 0.

### D9b: fit-result envelope — embedded evidence map, not a pointer
- **Chosen: the envelope embeds the whole `veto/evidence-map/v1`
  document; `validate_fit_result` validates it in place (violations
  prefixed `evidence_map:`).** Docstring, validator, producer
  (`fit_explain`), and the contract doc agree on embedding — the blind
  review found a three-way split (docstring said "pointer", validator
  said nothing, producer embedded the whole map) and this records the
  resolution.
  - Trades away payload compactness and the ability to evolve the map
    independently of the envelope to get a single source of truth:
    consumers such as the share-card builder read entries straight from
    the envelope, and a map can never dangle or go stale relative to
    the result it explains.
- **Rejected: pointer (`evidence_map_ref`) to a stored map.**
  - Trades away the staleness/dangling-reference hazard to get smaller
    envelopes — rejected: a pointer needs a store, a store needs a
    lookup, and a lookup can return a different map than the one the
    score was computed from; the envelope must be self-contained.
- **Rejected: envelope carries the map but the validator ignores it.**
  - Trades away validation cost to get a thinner validator — rejected:
    an unvalidated embedded document is a second, unchecked schema
    living inside the first.
- `schedule_driven`: false. `convenience_driven`: false (driven by the
  review finding, not ease). `ops_goal`: fit results whose embedded map
  fails in-place validation — metric: occurrences per 1,000
  validations; direction: decrease to 0.

## Epic 2 — evidence_map (2026-09-13, blind-review rework)

### D10: tri-state classification with "gap never upgraded"
- **Chosen: three statuses — supported / gap / grill_question — and a
  gap is never upgraded by the system.** A gap means no evidence was
  found; only the human (via the grill) can resolve ambiguity into
  support. The frozen `veto/evidence-map/v1` schema enforces it
  mechanically: gap entries must not carry evidence items.
  - Trades away recall (fuzzy/partial matches that "probably" count)
    to get a hard no-fabrication guarantee — a supported entry always
    means a verbatim quote was found.
- **Rejected: binary supported/unsupported.**
  - Trades away the ambiguity channel to get a simpler schema —
    rejected: ambiguity resolved by assertion is exactly the
    fabrication path the honesty invariant forbids.
- **Rejected: system auto-upgrade of gaps via fuzzy or alias-only
  matches.**
  - Trades away the no-fabrication guarantee to get fewer gaps —
    rejected: the map must report absence as absence.
- `schedule_driven`: false. `ops_goal`: "entries asserting support
  without a verbatim quote — zero in tests".

### D11: grill-question ids namespaced by job
- **Chosen: `grill_question_id = gq_<sha256(job_id + requirement)[:8]>`.**
  The same requirement against two jobs yields two distinct ids, so a
  grill session for job A can never resolve (and silently consume) a
  question that belonged to job B.
  - Trades away id readability (opaque hashes instead of
    `gq_python`) to get collision-freedom across jobs.
- **Rejected: keyword-only ids (`gq_python`).**
  - Trades away cross-job isolation to get human-readable ids —
    rejected: two applications would share one question identity and
    answers would leak across jobs.
- `schedule_driven`: false. `ops_goal`: "grill-question id collisions
  across jobs — zero in tests".

### D12: verbatim-quote enforcement strategy
- **Chosen: trace every candidate quote back to its exact source line;
  quote only lines that survive the trace, and assert
  `quote in resume_text` at emission.** Matching runs over the
  decoder's emitted section lines; a line the decoder altered
  (PII-masked contact values) or constructed (joined multi-line
  summary, joined canonical skill list) is skipped for quoting —
  skills resolve to their own verbatim source lines via the decoder's
  per-skill records instead. A second presence pass over the decoded
  lines catches keyword mentions that exist only on unquotable lines;
  those degrade to `grill_question` (the mention is real, so not a
  gap), never to a fabricated `supported` quote. The raw contact
  value is never substituted in — the decoder's PII guarantee (Epic
  1 D1) holds for every emitted string.
  - Trades away implementation simplicity and some quotable evidence
    (a redacted contact line holding the only keyword mention cannot
    be quoted) to get the invariant the frozen schema promises —
    every `supported` quote is a verbatim substring, never a
    decoder-constructed string — without punching a PII hole through
    the decoder into share cards.
- **Rejected: quoting decoder-normalized profile strings as evidence.**
  - Trades away the verbatim guarantee to get clean canonical quotes
    — rejected: a canonical "Kubernetes" quote for a resume that only
    said "k8s" is a fabricated quote, and the blind review caught it
    doing exactly that.
- **Rejected: quoting raw source lines, PII included.**
  - Trades away the decoder's PII guarantee to get trivially verbatim
    quotes — rejected: raw contact values would flow through evidence
    quotes into share cards and grill surfaces (Epic 1 D1 forbids
    this); an unquotable mention degrades to `grill_question` instead.
- **Rejected: trusting the scan and skipping the emit-time check.**
  - Trades away one cheap assertion to get simpler code — rejected:
    the invariant is the product's core promise and must be enforced,
    not assumed.
- `schedule_driven`: false. `ops_goal`: "supported quotes failing
  `quote in resume_text`, or carrying raw contact values — zero in
  tests".
