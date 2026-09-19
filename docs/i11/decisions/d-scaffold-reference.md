# Decision Record: unit d fixer — scaffold generator + reference extension KICK_BACK rework

Run ID: 2026-09-13-d-scaffold-reference-fixer
Lane: Develop & Deliver
Started: 2026-09-13
Human arbiter: anonymous operator

**This file is append-only.** Nothing above a committed line is edited. A correction is a new
entry that references the entry it corrects.

---

## 1. Request

> You are the rework fixer for Veto Initiative 11, unit d (scaffold generator + reference
> extension). A blind review returned KICK_BACK (critic only — no constitutional veto; ops and
> reliability ALLOW). The ed25519 signature is cryptographically VALID and the reference
> extension is sound — those need no rework. Fix in `initiatives/i11/scaffold/` and
> `tests/test_ext_scaffold.py` only: C1 (snippets import veto core while the docstring says the
> scan rejects that — keep the "policy-clean out of the box" promise, verified by running the
> policy suite on fresh output); C2 (duplicate/unknown action_kinds silently corrupt the
> manifest — loud ValueError + regression tests); C3 (sanitize name/description interpolation
> into generated Python source, web_card.html, wizard_step.py + hostile-input test); minors
> (robust policy-suite test, decision record). Do NOT commit; never invent.

Solution shape assumed by the request: fix in place; no redesign of the scaffold's
one-call-generates-nine-files shape or the reference extension.

## 2. Artifacts produced

| Artifact | Path |
|---|---|
| Scaffold rework: C1 scan-scope evidence + clarified host-integration docstrings, C2 action-kind validation, C3 per-context sanitizers | `initiatives/i11/scaffold/template.py` |
| Fix-proving tests (11 tests; brittle NotImplementedError rewrite removed) | `tests/test_ext_scaffold.py` |
| This decision record | `docs/i11/decisions/d-scaffold-reference.md` |

## 3. Decisions

### D1: C1 — keep the snippets in the extension dir, prove the scan exclusion (fix b)
- **Chose.** Fix (b) from the brief: the `cli_snippet.py`/`mcp_snippet.py`/`wizard_step.py`
  files stay in the generated extension directory, and the contradiction is resolved by
  evidence plus documentation rather than relocation.
- **Evidence (read-only; policy_kit and sandbox untouched).**
  - `policy_kit/checks.py::check_import_scan_clean` delegates to `scan_imports` in
    `sandbox/host.py`, which explicitly skips `*_snippet.py` and `wizard_step.py` with the
    rationale: "Developer tooling is not extension runtime code and is not scanned: ...
    they run as host code, not extension code, and legitimately import host modules."
  - `Host.load_extension` (`sandbox/host.py`, ~line 593) only ever execs `extension.py`;
    the snippets never load as extension code — they are documents the host integrator
    copies into cli.py / wizard.py / the MCP server.
  - Verified directly: pristine fresh scaffold output passes `run_policy_suite` 10/10
    (see D4) — the "policy-clean out of the box" promise holds on default output.
- **Over.**
  - Relocate snippets to a `host-integration/` sibling dir (fix a): trades away the
    single-directory handoff (the scaffold's whole value proposition is "one directory,
    hand it to the integrator") to get an airtight never-scanned guarantee — rejected;
    the scan exclusion is already the designed, documented behavior, and moving files
    would silently diverge the scaffold from the reference extension (which was built
    with the scaffold and ships the same layout).
  - Leave the contradiction undocumented: trades away nothing to get nothing — rejected;
    the review's real point was the lie in the docstring, not the scan.
- **Because.** The scan exclusion is genuine policy-kit behavior, not a loophole: the
  snippets are host-side integration docs. The honest fix is to say that in the files
  themselves — each snippet now carries a "HOST-INTEGRATION CODE — not extension code"
  header, and the action docstrings in `extension.py` qualify the NEVER-import rule as
  applying to extension runtime code.
- **schedule_driven.** false. Nothing about the schedule drove this; correctness of the
  promise (verified by an actual suite run) won over the quicker relocation.
- **ops_goal.** "Policy-clean out of the box": a fresh scaffold must pass the policy
  suite with zero author edits, so integrators can trust the default output.

### D2: C2 — validate action kinds at scaffold time, before touching the filesystem
- **Chose.** `_validate_action_kinds` rejects duplicate kinds and kinds outside
  `ACTION_KINDS` (imported from `manifest/schema.py`, the same source of truth the
  manifest validator uses) with a loud `ValueError`, run before any directory is created
  so a rejected scaffold leaves nothing behind.
- **Over.**
  - Rely on manifest validation at install time: trades away early failure to get a
    smaller scaffold — rejected; the manifest validator already rejects unknown kinds
    (`schema.py`), but `["read", "read"]` produced two actions with the same id and
    duplicate function defs that the validator accepted, so the corruption was real and
    only visible later.
  - Dedupe silently: trades away the author's stated intent to get never-failing output
    — rejected; silently dropping a requested action is exactly the kind of quiet
    divergence the governance framework exists to prevent.
- **Because.** The scaffold is the last place where the author's intent is unambiguous;
  failing there, loudly, is cheaper than debugging a corrupt manifest at install.
- **schedule_driven.** false. A one-line dedupe would have been faster; loud failure won.
- **ops_goal.** "No corrupt manifest ever leaves the scaffold": every generated manifest
  has unique action ids by construction.

### D3: C3 — sanitize per output context (`_py_safe` for Python/markdown, `html.escape` for HTML)
- **Chose.** `_py_safe()` collapses free text to a single line and escapes backslashes
  and double quotes, so interpolated names can never terminate a `"""..."""` string;
  `html.escape()` handles `web_card.html`; `wizard_step.py` was restructured to bake the
  name into a plain (non-f) triple-quoted string so braces in hostile input can't break
  the generated code either. JSON (`manifest.json`) was already safe via `json.dumps`.
- **Over.**
  - Reject hostile input (whitelist names): trades away legitimate names to get a
    smaller code change — rejected; the scaffold should be robust to dictation artifacts
    and odd names, not gatekeep them.
  - A templating engine with context-aware autoescaping: trades away zero-dependency
    stdlib simplicity to get stronger guarantees — considered; rejected for now because
    the current interpolation surface is small and fully covered by the hostile-input
    tests, but flagged as the right move if the template surface grows.
- **Because.** The reviewer's repro (`name='Inj"""\nimport os\n"""Ext'`) silently
  terminated the module docstring and injected top-level code — a code-injection bug in a
  code generator. Sanitizing at the interpolation site keeps the generator total over
  its input domain.
- **schedule_driven.** false. Input rejection would have shipped faster; robustness won.
- **ops_goal.** "Generated code always compiles to the intended structure": hostile
  input produces inert text, never injected code (asserted by AST inspection in tests).

### D4: minor — run the policy suite on pristine output; delete the brittle rewrite
- **Chose.** The old test's double `str.replace('raise NotImplementedError(')` was
  order-dependent and tested rewritten output, not the template's actual output. The new
  test runs `run_policy_suite` directly on pristine scaffold output across three
  action-kind sets (`["read"]`, `["read","draft","confirm-required"]`,
  `["preview","notify"]`) — 10/10 each, verified 2026-09-13.
- **Over.**
  - Keep the rewrite but make it regex-robust: trades away honesty (still not testing
    the real output) to keep the no-op safety net — rejected; the safety net was
    unnecessary, as the pristine runs prove.
- **Because.** A "policy-clean out of the box" test that modifies the box first proves
  nothing; the suite's confirmation checks treat unimplemented actions as refusals, so
  the pristine path is green and the test now guards the actual promise.
- **schedule_driven.** false.
- **ops_goal.** Same as D1: the test is the executable form of the out-of-the-box promise.

## 4. Options presented

| Option | Trades away | To get |
|---|---|---|
| Keep snippets in dir + prove scan exclusion (chosen) | Nothing (evidence already existed) | Single-directory handoff + honest docstrings |
| Relocate snippets to host-integration/ (rejected) | Single-directory handoff; diverges from reference layout | Airtight never-scanned guarantee |
| Scaffold-time kind validation, fail before mkdir (chosen) | Smaller scaffold API | No corrupt manifest ever leaves the scaffold |
| Install-time manifest validation only (rejected) | Early, loud failure | Smaller scaffold |
| Silent dedupe of duplicate kinds (rejected) | Author's stated intent (quiet divergence) | Never-failing output |
| Per-context sanitizers `_py_safe`/`html.escape` (chosen) | A smaller diff | Generator total over hostile input |
| Reject hostile names (rejected) | Legitimate odd names | Smaller code change |
| Templating engine with autoescaping (deferred) | Zero-dependency stdlib simplicity | Stronger escaping guarantees |
| Policy suite on pristine output (chosen) | No-op-stub safety net (unneeded) | Test proves the actual out-of-the-box promise |
| Robust-regex rewrite of stubs (rejected) | Test honesty | Keep the (unnecessary) safety net |

## 5. Neutral facts issued to the Ops Advocate

Not applicable — fixer loop, no advocate run. Findings came from the blind review recorded as
KICK_BACK for case tag init-11-d-scaffold-reference.

## 6. Verdicts

Not applicable — no critic/advocate/reviewer loop was run for the fixer pass itself. The
KICK_BACK findings and their responses:

| Finding | Response | Status |
|---|---|---|
| C1: snippets import veto core while docstring says scan rejects it | Fix (b): scan-scope evidence obtained read-only (policy_kit/checks.py → scan_imports exclusion + load_extension only execs extension.py); HOST-INTEGRATION headers added; verified pristine output passes run_policy_suite 10/10 | revised |
| C2: duplicate action_kinds silently corrupt manifest | `_validate_action_kinds` raises ValueError on duplicates and unknown kinds (against ACTION_KINDS), before any filesystem writes; regression tests | revised |
| C3: name/description injection into generated source | `_py_safe` for Python/markdown contexts, `html.escape` for web_card.html, brace-safe wizard_step.py; hostile-input tests (reviewer's exact repro) with AST assertions | revised |
| MINOR: brittle NotImplementedError string-replace | Deleted; policy suite runs on pristine output across three kind sets | revised |
| MINOR: unit had no decision record | This file | revised |

## 7. Worker response to verdicts

All KICK_BACK findings are revised. The ed25519 signature and the reference extension were
explicitly out of scope (both sound per the review) and were not touched; the reference
extension was also not regenerated, since regenerating it would change shipped artifacts
outside the fixer's brief.

## 8. Gate

Gate: n/a (fixer pass; commit sweep is the later human-gated step).
Routed because: scheduled gate — the later sweep commits, and only the human clears.

### Human decision
- Arbiter: anonymous operator
- Date: pending (commit sweep)
- Decision: pending
- Reason: —
- Vetoes cleared: none

## 9. Skips

| What was skipped | Instructed by | Recorded at |
|---|---|---|
| Committing any changes | Task instruction ("leave changes uncommitted") | 2026-09-13 |
| Touching `policy_kit/` or `sandbox/` (read-only evidence gathering) | Task instruction | 2026-09-13 |
| Regenerating the reference extension with the new scaffold | Out of scope: reference verified sound; regeneration would alter shipped artifacts | 2026-09-13 |
| Templating-engine autoescaping | Deferred: current surface fully covered by hostile-input tests; revisit if templates grow | 2026-09-13 |
