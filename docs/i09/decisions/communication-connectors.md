# Communication connectors decisions (Initiative 09, 2026-09-13)

Constitutional format: genuine options (Rule 2, each phrased "trades X
for Y"), a `schedule_driven` flag (Rule 3), and an `ops_goal` naming a
metric and direction (Rule 4).

Honest framing: this is a local pre-launch library. Veto has no public
users yet, no production traffic, and no measured operational statistics
— every "metric" below is a test-enforced target, not an observed
number. Nothing here invents users, incidents, partner agreements, or
launch dates.

## D1: storage_state (bearer-equivalent session secrets) encryption at rest

Rule 4 requires encryption at rest. `login_session` saves Chromium
`storage_state` (cookies / localStorage — bearer-equivalent: anyone
holding the bytes can impersonate the sessions) protected ONLY by
0o600 file permissions. Real encryption was attempted and assessed:

- Python's stdlib has no AES primitive; hand-rolling one is not
  acceptable.
- The `cryptography` package (Fernet) is not installed and cannot be
  installed in this environment (PEP 668 externally-managed
  environment); adding a new third-party dependency plus
  decrypt-to-tempfile plumbing around Playwright's `storage_state`
  loading is new security-critical surface (temp-file lifecycle,
  guaranteed deletion, key handling) beyond what this rework round can
  implement safely.
- Shelling out to the `openssl` CLI for AES-GCM was considered and
  rejected: IV management, tag handling, and error paths through a
  subprocess are exactly the home-rolled-crypto shape Rule 4 is meant
  to prevent.
- Even with proper encryption, the residual risk would REMAIN: the key
  would live on the same machine (0o600, same threat model as the
  consent key), so root — or any process running as the user — bypasses
  it. Encryption raises the bar against offline theft (stolen backups,
  disk disposal, accidental file shares), not against local privilege.

- **Chosen: harden what can be hardened safely; record the rest as
  explicit residual risk for Paul (human gate).**
  - What ships: 0o600 on the session file (loud failure, not silent);
    on hardening failure the captured file is DELETED (m4 — never left
    at umask permissions with a "delete it manually" note); disclosure
    warnings on every save; `sessions/` gitignored.
  - Trades away real at-rest encryption (ciphertext on disk) to get no
    new crypto code paths that this unit cannot review to the standard
    Rule 4 demands — a rushed Fernet/openssl-CLI implementation with a
    temp-file decrypt window could easily be WORSE than honest 0o600
    (e.g. plaintext temp files left behind after crashes). The residual
    risk is not silently downgraded: it is recorded below and routed to
    Paul, the only one who can accept it.
- **Surviving option B: Fernet-encrypt storage_state with a
  locally-generated 0o600 key (the consent-key pattern).**
  - Trades away dependency minimalism and implementation simplicity —
    adds `cryptography` to requirements, plus decrypt-to-0o600-tempfile
    plumbing with guaranteed deletion around every
    `browser.new_context(storage_state=...)` call — to get genuine
    at-rest encryption: stolen ciphertext without the key file is
    useless. Survives as the recommended direction if Paul accepts the
    residual risk below only conditionally, or when a dependency review
    can bless the new crypto surface properly.
- **Surviving option C: OS keyring / platform credential store for the
  session secrets.**
  - Trades away portability and headless operability (keyring access
    needs a desktop session or extra daemons in containers; CI/dev-VM
    runs break) to get the strongest local protection (OS-enforced
    access control, often hardware-backed). Survives for a future
    headed-desktop-only distribution; rejected now because Veto runs
    headless in containers/VMs where keyrings are unavailable.

- `schedule_driven`: false — this was correctness-driven (blind-review
  KICK_BACK rework), not calendar-driven; no launch date was traded.
- `ops_goal`: "saved session files readable by anyone other than the
  owner — zero (test-pinned: chmod failure deletes the file; success
  asserts 0o600)."

## D2: PII screenshot encryption at rest

`_take_screenshot` captures filled application forms (name, email,
phone, address). Rule 4 requires encryption at rest; screenshots are
not encrypted.

- **Chosen: 0o600 on capture + retention caps, no encryption, residual
  risk recorded for Paul.**
  - What ships: every screenshot is chmodded 0o600 immediately after
    capture; on hardening failure the file is DELETED (fallback chmod
    0), loudly reported as an error — never left at umask permissions
    with a warning (per "Fail loud on hardening failures" below; the
    round-3 warning-only path was a violation, fixed in round 4); the
    retention policy (at most 50, at most 30 days old, enforced at the
    start of every run) bounds the exposure window; the policy is
    disclosed in the module docstring.
  - Trades away at-rest encryption to get a human-usable review surface:
    screenshots exist so the user can OPEN them in their file manager
    and visually verify what the browser filled. Encrypting them would
    require building a decrypt-and-view workflow that does not exist —
    and an encrypted screenshot the user cannot open is a screenshot
    that never gets reviewed, which weakens the human-in-the-loop the
    whole approval mechanism depends on. Documented here rather than
    silently downgraded.
- **Surviving option B: encrypt screenshots with the session key and
  add a `veto screenshots view` decrypt command.**
  - Trades away implementation effort and review friction (a new
    decrypt path that must itself protect the plaintext window) to get
    encrypted screenshots that remain viewable through a first-party
    command. Survives if Paul judges the residual risk below
    unacceptable — it is the natural companion to option B in D1
    (same key, same threat model).
- **Surviving option C: no screenshots at all (text-only form dump).**
  - Trades away the visual verification surface — the user can no
    longer SEE what the browser filled, which is the strongest
    pre-approval check available — to get zero PII image files on disk.
    Rejected: it removes a safety feature to satisfy a storage rule.

- `schedule_driven`: false.
- `ops_goal`: "screenshot files on disk with permissions broader than
  0o600, or older than the retention cap — zero (test-pinned)."

## Residual risks — routed to Paul (human risk-acceptance gate)

The blind reviewer was explicit: only a human can accept these. They
are recorded here, not silently downgraded:

1. **storage_state is not encrypted at rest** (D1). Anyone who can read
   the file (same user, root, stolen disk/backup without the key —
   there is no key) can impersonate the saved board sessions.
   Mitigations that DO exist: 0o600 (loud failure, file deleted on
   hardening failure), disclosure warnings, `sessions/` gitignored.
2. **Screenshots are not encrypted at rest** (D2). Anyone who can read
   the files (same user, root, stolen disk/backup) sees filled
   application PII. Mitigations that DO exist: 0o600 on capture (the
   file is deleted if hardening fails — never left at weaker
   permissions), retention caps (50 / 30 days, enforced every run),
   disclosure.
3. **Cross-process approval-replay evidence depends on the
   `approval_consumed` log entry persisting** (B1 honest gap,
   integration-surfaces unit's scope): if that single log write fails
   (disk full, I/O error), a fresh process within the approval TTL
   could reuse the approval — the registry logs this loudly but the
   reuse would succeed. Mitigations that DO exist: the in-process
   single-use set still blocks same-process replay; the failure is
   ERROR-logged, never silent. Full B1 proof awaits the registry
   rework.

Paul: accepting (1) and (2) as-is, or directing option B in D1/D2, is
your call. This record stands as the disclosure either way.

4. **A hostile page can label a button "Reject all" while its click
   handler actually opts into tracking** (F3, round 8 — consent-banner
   dismissal is heuristic). No code fix is offered: the tool cannot
   verify a banner button's handler semantics from the DOM, only its
   label. Mitigations that DO exist: clicks are confined to
   reject/dismiss-labeled buttons (never "Accept"); any readback
   doubt means NO dismissal and the run continues safely; the consent
   click itself transmits no applicant PII. Your options, as
   tradeoffs:
   - **(a) Accept as-is.** Banners usually get dismissed and runs
     proceed unattended; residual risk is a mislabeled-button
     misclick that opts into tracking on a board site (no PII
     transmitted by the click itself).
   - **(b) Never auto-dismiss.** Eliminates the misclick entirely,
     but banners may occlude the form and every affected run needs
     manual dismissal.
   - **(c) Known-ID allowlist only.** Dismiss only buttons matched by
     known consent-framework element IDs, never text-matched ones:
     near-zero misclick risk, but most custom banners stop being
     dismissed and fall back to manual handling like (b).

## Round-4 rework (2026-09-13)

Fresh blind security review of the round-3 rework KICK_BACKed on six
findings. B1 (cross-process replay), M1 (final-URL binding), M2 (rescue
before consume), and all round-1 items were verified FIXED and were not
regressed. M4 (Rule-4 encryption-at-rest residual) stays routed to Paul
— untouched here.

- **R4-N1: M3 binds the RAW bytes, not the sanitized display text.**
  The docstring claimed the re-verification compared the raw field
  bytes; it actually hashed the sanitized display body. Two raw values
  with identical sanitization ("Ada\x00Lovelace" vs "Ada\u200bLovelace"
  → both "Ada�Lovelace") passed the check, so the click could transmit
  bytes the user never approved. Fixed: the click-time re-verification
  compares the raw click-time DOM values against the raw fill-time
  values by exact dict equality — no hash, no sanitization in the
  comparison. URL drift is checked separately with its own error.
  Docstrings corrected to describe the actual binding.
- **R4-N2: `assert` is not a security gate (`python -O`).** The three
  approval gates (`browser_apply._attempt_authorized_submit`,
  `email_sync.send_followup`, `email_sync._apply_eligible_updates`)
  relied on `assert approval is not None`; under `python -O` those
  vanish, and a contract-violating `(None, None)` return would have
  proceeded to click/send/write. All three are now explicit
  `if ... is None` checks that fail closed with a named refusal and a
  loud log. Full audit of the three files found no other
  load-bearing asserts on security invariants.
- **R4-N3: screenshot chmod failure now fails loud.** See the D2
  update above (delete on hardening failure, error not warning —
  matches the login_session m4 path).
- **R4-N4: consent-banner click re-verifies text at click time.** The
  generic-button path enumerated by positional index and clicked the
  index later — racing DOM mutation could shift it onto an
  accept-like button. The click JS now re-reads the button's text and
  clicks only if it still matches the reject/dismiss-classified text;
  a mismatch is left alone (fail closed) and logged.
- **R4-N5: `screenshots/` is gitignored.** `.gitignore` already lists
  `screenshots/` (and `sessions/`); the module now notes it next to
  `SCREENSHOTS_DIR`, matching the existing note on `SESSIONS_DIR`.
- **R4-N6: `result["m3_readback"]` initialized on all paths.** It was
  documented but only set on the submit path. Now `"n/a"` unless the
  submit path overwrites it with `"verified"` / `"unavailable"`.

- `schedule_driven`: false — correctness-driven blind-review rework.
- `ops_goal`: "blind-review MAJOR findings open — zero."

## Round-5 rework (2026-09-13)

Fresh blind security review of the round-4 rework KICK_BACKed on five
findings. N1–N4 and N6 were verified FIXED and were not regressed. M4
(Rule-4 encryption-at-rest residual) stays routed to Paul — untouched
here.

- **R5-S1 (MAJOR): M3 select re-verification is exact on the transmitted
  bytes.** The round-4 fix compared raw values exactly — except on the
  `<select>` path, which still substituted the approved profile value
  whenever the selected option's label *contained* it, and whose
  readback reported only the option's visible text. Hostile page JS
  could swap the option's `value` attribute (the actual transmitted
  bytes) to attacker-controlled bytes while keeping a matching label;
  the check passed and the click transmitted bytes the user never
  approved — the same N1 pattern surviving in the select path. Fixed:
  the readback JS now returns BOTH the selected option's visible text
  and its `value` attribute; fill time captures the matched option's
  actual (label, value-attribute) pair in the same evaluate that
  matched it; the click-time comparison is exact equality on the pair —
  no containment substitution. A swapped value attribute OR a changed
  label aborts, fail closed. The approval prompt renders selects as
  `<label> [transmits: <value>]` so the user sees the transmitted bytes,
  not just the matched profile value.
- **R5-S2: screenshot retention failures are loud and surfaced.**
  `apply_via_browser` discarded the `cleanup_screenshots()` report, so
  per-file deletion failures were invisible — the D2 ops_goal and the
  "fail loud" standing constraint unenforced when deletion failed.
  Fixed: deletion errors are ERROR-logged (explicitly: the files are
  PII-bearing) and surfaced in `result["screenshot_cleanup"]`; a
  cleanup crash is reported the same way. The run itself still proceeds
  (cleanup is best-effort), but the failure is visible in logs and in
  the result dict.
- **R5-S3: M3 binds fields by fill-time identity, not page-controlled
  names.** Readback keys derived from `el.name || el.id ||
  el.placeholder` — all page-controlled — so page JS could rename a
  filled field away from its key and rename a different field onto the
  filled key with the approved value, and the name-keyed comparison
  passed. Fixed: `fill_application` records each filled field's
  fill-time positional index (the same `querySelectorAll` order used to
  fill and to re-read) plus the total field count; the click-time
  re-verification binds by index — the element at the fill-time index
  must still carry the filled key (rename/replace aborts) — and aborts
  when the field count changed (fields added/removed). The count check
  also defeats the insert-shift decoy and any brand-new field smuggled
  into the form after approval (the click would transmit it; the
  approval never bound it). Residuals, documented in code: the
  name-keyed fallback (plain-dict test doubles / legacy callers only —
  never the production path) does not detect rename/insert spoofs; two
  fields sharing one derived name collapse to a single binding (the
  `filled` dict was already ambiguous there).
- **R5-S4: no interactive approval for bodies the prompt truncates.**
  The approval prompt shows the first 4000 body characters while the
  approval binds the FULL body — the user typed "send" for bytes they
  never saw (previously disclosed in the UI as "[…body truncated…]").
  Fixed at every in-scope approval boundary (`email_sync.send_followup`,
  `email_sync._apply_eligible_updates`, the browser submit path):
  bodies over the 4000-char display cap are refused BEFORE any prompt
  is shown (no approval minted or burned), with instructions and the
  full body saved to a named 0o600 preview file — "preview another way"
  — after which the user shortens the message and approves again. The
  prompt renderer itself (`registry._present_draft`) is out of this
  unit's scope and unchanged.
- **R5-S5: `--disable-blink-features=AutomationControlled` disclosed.**
  The module docstring disclosed `--no-sandbox` but not this equally
  bot-detection-evasion Chromium launch arg. It is now documented next
  to `--no-sandbox`: what it does (hides automation signals from page
  JS), the trade-off (bot-detection evasion, stated plainly), and why
  it is accepted (human in the loop on every submit; no CAPTCHA/auth
  bypass — those pause the run).

## Round-6 rework (2026-09-13)

Round-6 review (R6) raised two MAJORs, three MINORs, and two NITs. All
are addressed below. The Round-5 S1–S5 fixes were re-verified as part of
this round's regression run (no regressions).

- **R6-MAJOR-1: the approval binds EVERY successful form control, not
  just the filled subset.** The M3 re-verification recorded only the
  fields the tool filled — hidden inputs, untouched checkboxes, and
  unfilled text inputs were never part of the approval binding, so
  hostile page JS could rewrite a hidden input or check an opt-in
  checkbox after approval and the click would have transmitted bytes
  the approval never bound. Fixed: `fill_application` now captures a
  full control snapshot at fill time — every successful form control
  (hidden inputs; all text/input/textarea values including unmatched
  and unfilled controls; checkbox/radio checked state and value; all
  selected options with label AND transmitted value; structural
  identity/index; name, disabled state, form association, and
  successful-control status; the submit destination) — and the
  click-time re-verification compares the full snapshot EXACTLY: value
  changes, checked-state changes, added/removed/moved controls, and
  key renames (a renamed control is reported as renamed, not silently
  "added"+"removed"). The approval copy now states explicitly that
  EVERY successful form control is bound. Abort is fail closed: any
  divergence aborts the submit, and the error NAMES the changed
  controls ("these controls changed after your approval") instead of
  reporting only that "a value" changed.
- **R6-MAJOR-2: security readback runs in a CDP isolated world, never
  the page's main world — and fails closed with NO main-world
  fallback.** The old readback fell back to `page.evaluate` when CDP
  was unavailable — the exact trust the finding required eliminated.
  Fixed: all security readback (fill snapshots, click-time snapshots,
  submit-destination reads, option matching) goes through a CDP
  isolated world (`Page.getFrameTree` → `Page.createIsolatedWorld` →
  guarded `Runtime.callFunctionOn`); `page.evaluate` is NEVER consulted
  for a production security decision. The three page kinds are
  distinguished explicitly: (1) no `page.context` at all — an
  incomplete test double (real Playwright pages always expose
  `.context`; page JS cannot remove the Python-side attribute) —
  keeps the legacy loud `_READBACK_UNAVAILABLE` path; (2) a real
  non-Chromium page (context present, no CDP channel — CDP is
  Chromium-only) raises RuntimeError and refuses to submit; (3) a
  Chromium page whose isolated world cannot be established raises
  RuntimeError the same way. The error propagates to
  `apply_via_browser`, which records it and never submits. Proven by
  a forged-main-world test: the fake's `evaluate()` is forged to
  report approved values while the live DOM is tampered, and the
  re-verification — via the isolated world only — aborts, with
  `evaluate()` provably never called.
- **R6-MINOR-3: approval-preview files are retention-capped.** Previews
  (`_save_approval_preview`) accumulated indefinitely in the temp dir.
  Fixed: `_APPROVAL_PREVIEW_KEEP = 10`; every successful save prunes
  the oldest `veto-approval-preview-*.txt` files past the newest ten
  (the just-saved preview always survives — it is the newest).
- **R6-MINOR-4: live stage is re-read immediately before each stage
  write.** `_apply_eligible_updates` approved a stage transition, then
  wrote it — if the store moved (or the entry vanished) between
  approval and write, the write landed under a stale approval. Fixed:
  `_read_live_stage()` re-reads the stage immediately before each
  `lifecycle.update_stage` call. Changed stage →
  `action="refused_stage_changed"` (no write, named note, ERROR log);
  missing/unreadable stage →
  `action="refused_stage_unverifiable"` (no write, named note, ERROR
  log); unchanged stage → the write proceeds as before.
- **R6-MINOR-5: the form action is bound as the submit destination.**
  The approval said "this will be submitted at <page URL>" even when
  the form posts elsewhere — the click actually transmits to the
  form's action. Fixed: the fill-time snapshot captures the form's
  action (relative actions resolved via page.url with a Python
  `urljoin` fallback); the approval's `to` binds the resolved action
  when known, and the page URL remains disclosed as separate
  navigation context. A form with NO action says so explicitly —
  "form has no action — SPA submit path" — instead of implying a
  normal form post.
- **R6-NIT-6: one shared approval-body cap.** The 4000-char cap was
  defined separately in `email_sync` and `browser_apply`. Fixed:
  `email_sync._APPROVAL_PROMPT_BODY_CAP` is the single definition;
  `browser_apply` imports it (and reuses `_save_approval_preview`
  instead of its own mkstemp copy). Documented residual:
  `registry._present_draft` keeps an out-of-scope hardcoded copy that
  the owning unit must align — flagged, not silently duplicated.
- **R6-NIT-7: PII/secret files are owner-only FROM CREATION.** Screenshot
  and session files were written at umask permissions and chmodded to
  0o600 afterwards — the bytes briefly existed world-readable. Fixed:
  `_precreate_owner_only()` creates the target with
  `O_CREAT|O_TRUNC|O_WRONLY` at 0o600 plus an explicit `fchmod`
  (tightens pre-existing wider files) BEFORE Playwright writes a byte;
  `login_session` refuses to save at all when the target cannot be
  secured. The post-write chmods remain as defense-in-depth.

- **M4 (encryption-at-rest) residual: untouched.** As routed: file
  permissions (now owner-only from creation) remain the only
  protection for screenshots, session files, and approval previews —
  none are encrypted. Unimplemented by this unit; routed to Paul.

- `schedule_driven`: false — correctness-driven blind-review rework.
- `ops_goal`: "blind-review MAJOR findings open — zero; no main-world
  trust in the submit path."

## Round-7 rework (2026-09-14)

Round-7 review (R7) raised three MAJORs (A, B, C), one MINOR (D), and
one NIT (E). All are addressed below. The Round-6 MAJORs 1–2 and all
Round-6 MINORs were re-verified as part of this round's regression run
(no regressions).

- **R7-A (MAJOR): the approval binds the ACTUAL submitter and its
  submission overrides.** The bound submitter is the concrete control
  the tool will click — discovered in the isolated world and
  identity-matched against the main-world locator pattern (a mismatch
  refuses) — and the binding records its `formaction`, resolved
  `formaction`, `formmethod`, `formtarget`, `value`, and `type`.
  `formaction` overrides the form's action (the click POSTs there);
  `formmethod` overrides the HTTP method — so a post-approval swap of
  either is a bound-state change that aborts the click, fail closed,
  naming the submitter and the destination change. The approval
  display names the actual submitter explicitly instead of implying
  "the form will be submitted".
- **R7-B (MAJOR): the approval-time snapshot is the security
  baseline — the fill-time snapshot is superseded on the real submit
  path.** The submit flow locates the submitter first, then captures a
  fresh full-control isolated-world snapshot IMMEDIATELY before
  approval is minted; the approval's "will transmit" display is
  rendered from that snapshot (normalized) — never from the claimed
  filled dict — including hidden inputs, unfilled controls,
  checkbox state, select label/value pairs, and the actual submitter.
  The click-time re-read is diffed against the approval-time baseline,
  not the fill-time snapshot. Baseline or display-capture failures
  refuse BEFORE any approval is minted or consumed. Consequence: a DOM
  mutation during the pre-approval steps is displayed honestly and
  approved as-is (it IS the approved state), never misreported as
  post-approval tamper; a forged filled dict cannot smuggle text into
  the prompt — the user sees DOM truth.
- **R7-C (MAJOR): consent handling trusts the isolated world only.**
  Consent-banner enumeration, known-ID text probes, generic
  click-time verification, and known-ID verify+click all run through
  `_evaluate_unforged`. If the isolated channel is unavailable, NO
  consent button is clicked — the run continues without dismissal
  rather than trusting main-world readback for a security-relevant
  click. Proven by a hostile-main-world regression: the main world
  forges a "Reject all" banner while the isolated world sees only
  "Accept all" — nothing is clicked and the forged readback is never
  consulted.
- **R7-D (MINOR): the destination binds the submitter's OWN form,
  never the first form.** Effective destination precedence: (1) the
  submitter's `formaction`; (2) the submitter's associated form's
  `action`; (3) the SPA/no-action path. The first-form fallback
  survives ONLY when no submitter was located and is explicitly
  labeled as degraded ("no submit button located — first-form
  fallback"). The approval wording says honestly where the
  destination came from — submitter formaction override,
  submitter-form action, or fallback — instead of implying "the
  form's action" when the binding actually came from a button
  override.
- **R7-E (NIT): binding keys are JSON-encoded, not
  separator-joined.** The old `\x1f`-joined key had a collision: a
  control whose name/tag contained the separator could alias onto a
  different control's binding (two distinct controls, one key —
  silently merged). The key is now the JSON encoding of the component
  array — self-delimiting, so distinct component tuples always encode
  to distinct keys; `_split_bkey` decodes it (never `str.split`).

- **M4 (encryption-at-rest) residual: untouched.** As routed: file
  permissions remain the only protection for screenshots, session
  files, and approval previews — none are encrypted. Unimplemented by
  this unit; routed to Paul.

### Corrections to prior round notes (2026-09-14)

- The R6-MAJOR-1 note says the click-time re-verification compares
  the fill-time snapshot. Superseded by R7-B: on the real submit path
  the approval-time baseline supersedes the fill-time snapshot; the
  fill-time snapshot survives only for legacy/lookup callers. The
  user approves what the approval-time baseline saw.
- The R6-MAJOR-2 note's "all security readback goes through a CDP
  isolated world" was false for consent-banner handling — enumeration
  and click-time verification did not use `_evaluate_unforged`.
  Corrected by R7-C. R6-MAJOR-2 also did not bind the actual
  submitter's overrides; corrected by R7-A.

- `schedule_driven`: false — correctness-driven blind-review rework.
- `ops_goal`: "blind-review MAJOR findings open — zero; no
  main-world trust in the submit or consent-dismissal path; the
  approval display is derived from the approval-time baseline."

## Round-8 rework (2026-09-14)

Round-8 review raised two findings against the round-7 rework (F1, F2)
and one accepted residual (F3). Reviewer-verified findings B–E were
re-checked and not regressed; the M4 encryption-at-rest residual
remains routed to Paul, untouched.

- **F1: the click targets the isolated-world-resolved node itself —
  the main-world locator is discovery-only.** The round-7 note
  overclaimed: R7-A "identity-matched" the isolated-world submitter
  against the main-world locator *pattern*, and pattern agreement is
  not element identity — two different buttons can satisfy one
  pattern (the D1/D2 exploit: an attacker `formaction` button whose
  visible text is `"Apply\nnow"` from `<br>`, no `type` attribute,
  sorts before the benign `"Apply now"` button; the old raw-`indexOf`
  text matcher disagreed with Playwright's `:has-text()` about which
  button matched, so the approval bound one node while the click hit
  the other). Round 8 removes the pattern-agreement check and replaces
  it with construction: `_click_bound_submitter` re-resolves the
  baseline-bound submitter identity in the isolated world, verifies
  the re-resolved node's structural `nodePath` equals the bound path,
  re-reads the click-time destination and requires it to equal the
  approved destination, then clicks THAT node in the same isolated
  evaluation — bound == clicked by construction. The text matcher is
  shared verbatim across the locate, snapshot, destination, and click
  probes and converges with Playwright (`\s+` collapse, trim,
  lowercase, substring), so the two worlds can no longer bind
  different buttons for one pattern. Any re-resolution failure
  (no-match, identity-changed, destination-changed) refuses, fail
  closed, before any click. The production click path no longer calls
  `locator.click()` at all; the only surviving locator click is the
  explicitly marked `main-world-legacy` path for incomplete test
  doubles with no JS bridge.
- **F2: a failed destination read fails closed.** `_normalize_snapshot`
  returned a fabricated empty-destination binding when the destination
  probe yielded `None`. It now returns `None`, and the submit flow
  refuses before any approval is presented — no prompt, no click, no
  approval minted.
- **F3 (ACCEPTED RESIDUAL, routed to Paul): hostile consent-banner
  labeling.** A hostile page can label a button "Reject all" while its
  click handler actually opts into tracking. No code fix is offered —
  handler semantics are not verifiable from the DOM, so this is
  inherent to heuristic banner dismissal. Existing mitigations: clicks
  are confined to reject/dismiss-labeled buttons; readback doubt
  causes no dismissal and the run continues safely; the consent click
  transmits no applicant PII. Recorded as residual item 4 above with
  genuine options (accept as-is / never auto-dismiss / known-ID
  allowlist only) for Paul's human acceptance decision.

### Corrections to prior round notes (2026-09-14)

- The R7-A note's "identity-matched against the main-world locator
  pattern" did not bind the concrete control: agreement that the same
  *pattern* matched in both worlds is not proof that the same
  *element* matched. Corrected by F1: the click targets the
  isolated-world-resolved node itself, and element identity is the
  structural `nodePath` — never the pattern.

- `schedule_driven`: false — correctness-driven blind-review rework.
- `ops_goal`: "blind-review MAJOR findings open — zero; the clicked
  node is the bound node by construction; destination read failures
  refuse before approval."

## Standing constraints (non-negotiable)

- **No silent downgrades.** A Rule-4 requirement that cannot be met
  safely is recorded here and routed to the human gate — never quietly
  redefined as satisfied.
- **No home-rolled crypto.** AES via subprocess/CLI plumbing or
  hand-written ciphers is not an acceptable "attempt" at encryption.
- **Fail loud on hardening failures.** A permission/protection step
  that did not land is an error, never a warning — and bearer secrets
  are never left on disk at weaker permissions pending manual cleanup.

## Round 9 — event-free bound-form submit (2026-09-14)

Veto-grade blocker closed: the round-8 submit path re-verified the
destination *before* `el.click()`, so a hostile page's click/submit
handler could fire synchronously during dispatch and mutate the
submission (swap `formaction`, flip `formmethod`, rewrite hidden
controls) after the probe's checks passed. The round-9 submit path
dispatches no event at all:

- `_SUBMIT_BOUND_FORM_JS` runs in the CDP isolated world and submits
  via pristine `HTMLFormElement.prototype.submit.call(form)` — per the
  HTML spec, `form.submit()` fires no `submit` event, and no `click`
  is dispatched, so no page-registered handler can run between the
  final verification and the request build. The TOCTOU window the old
  `el.click()` left open is structurally closed, not narrowed.
- Pristine `Element.prototype.setAttribute.call(form, ...)` sets
  `action`/`method`/`target`/`enctype` explicitly to the approved
  values (a named control such as `<input name="action">` shadows the
  IDL setters on the instance, which would silently swallow a plain
  assignment); pristine `HTMLFormElement.prototype.submit` for the
  same reason (`<input name="submit">` shadows `form.submit`).
- Because `form.submit()` builds the entry list with no submitter,
  the submitter's `formaction`/`formmethod` overrides would otherwise
  be silently dropped — the explicit attribute setup makes the
  approved values determinative.

Same-evaluation full-control verification: the earlier round-9 draft
verified the destination in the final evaluation but diffed the
control rows in Python from an earlier snapshot — leaving a gap where
a hostile handler could rewrite a hidden control between the
Python-side diff and the submit. The final evaluation now
re-captures the FULL control set with the shared `veto_capture_rows`
walk (used by both the snapshot and the submit probes), normalizes
it with JS ports of `_row_binding_key`/`_row_transmitted`
(`_VETO_NORMALIZE_ROWS_JS`, kept in sync — byte-for-byte parity is a
unit test), and requires EXACT equality with the approved normalized
binding (`{"rows": ..., "submit": ...}`) in the SAME synchronous
evaluation that submits. On any divergence the probe refuses with
`rows-changed` naming the changed/added/removed binding keys.

Effective method/target/enctype binding: the approval binds the
submitter's `formmethod`/`formtarget`/`formenctype` (overriding the
form's `method`/`target`/`enctype`; method defaults to GET per the
HTML spec). The final evaluation re-verifies all four against the
approved `submit` block and refuses on `method-changed`,
`target-changed`, or `enctype-changed`. Approval copy now says
"native form submission using the approved method" (not "plain form
POST" — the method may be GET) and "form has no action — submits to
the current page URL" (not "SPA submit path" — an empty action is a
native submit to the document URL).

Handler immunity vs SPA compatibility (genuine X-for-Y trade-off,
stated honestly in the approval): sites whose apply flow depends on
JS submit handlers (SPA validation, analytics beacons) will not run
them — the submission is a native form submission to the approved
destination using the approved method, and the submitter's own
name=value contribution is not transmitted. Handler-immunity for the
approved destination is bought with SPA-handler incompatibility. The
refused alternative is dispatching a click, which re-introduces the
round-8 TOCTOU — so no click path is retained anywhere: the
incomplete-test-double fallback that clicked the discovery-hint
locator is removed and now fails closed (`submit_channel=
"main-world-unavailable"`, named error, nothing submitted).

Formless submit buttons (minor): a located button with no associated
`<form>` (common in SPA apply flows) is refused BEFORE any approval
is minted — no approval is burned on the doomed run. The tool
deliberately does not execute the page's JS submit path.

Hostile-ID label lookup (nit): the snapshot's `label[for=...]` lookup
now uses `CSS.escape(el.id)` inside a guarded try/catch — a hostile
id containing a quote or newline previously threw a SyntaxError that
failed the whole snapshot (fail-closed DoS: every submit refused).

Result contract: `submitted_form` (nodePath + verified
destination/method/source) is the canonical record. `submit_clicked`
no longer means "the button was clicked" — it means the bound form
was submitted via the event-free path (compatibility key);
`submitted` stays a DEPRECATED alias for the out-of-scope server.py
consumer. Honesty note (m2) preserved: the record proves the submit
fired and the page settled — no board confirms receipt.

F1 and F2 remain fixed (D1/D2 divergent-button regressions rewritten
for the submit path: no click event and no submit event are ever
dispatched; failed destination reads still refuse before approval).
New regressions: hostile click+submit handlers never fire; method /
target / enctype flips refuse; in-probe `rows-changed` aborts;
formless buttons refuse pre-approval; JS/Python normalization parity
is automated under node.

### Corrections to prior round notes (2026-09-14, round 9)

- "The clicked node is the bound node by construction" is superseded:
  no node is ever clicked. The submitted form IS the bound form by
  construction — the isolated-world-resolved node, verified in the
  same evaluation that submits.
- The round-8 `_CLICK_SUBMITTER_JS` / click-probe mechanism is
  deleted, not kept as a fallback. Any note referencing it describes
  history, not the current path.

### Unchanged routing

- M4 (Rule-4 encryption-at-rest residual) remains routed to Paul and
  is untouched by this round.
- D1/D2 encryption decisions are unchanged.

## Round 10 — display-wording micro-fix (2026-09-14)

Round-9 review raised one MINOR: the R7-A-era approval display still
rendered the bound submitter row as `submit_btn [submit button the
approval binds]: submit_btn=Submit application, type=submit` under the
heading "Will transmit (every successful form control):". That wording
predates the round-9 mechanism change (R7-A clicked the button, which
transmitted its name=value); the round-9 event-free `form.submit()`
builds the entry list with NO submitter, so the name=value is never
transmitted. Security direction is benign (the performed action is a
subset of the approved one), but the approval prompt is the user's
only view of what will be sent — it must not imply a pair is sent when
it is not.

The row now renders as `submit_btn [submit button the approval binds —
overrides honored; name=value NOT transmitted by the event-free
submit]`, keeping the destination-override bits (`formaction` /
`formmethod` / `formtarget`) — the security-relevant part. No test
pinned the old wording; display logic only, no security logic
touched (M4 untouched).
