# Decision Record: WS1 fixer — tools/contracts/__main__ hardening

Run ID: 2026-09-13-ws1-fixer
Lane: Develop & Deliver
Started: 2026-09-13
Human arbiter: Paul Thorson

**This file is append-only.** Nothing above a committed line is edited. A correction is a new
entry that references the entry it corrects.

---

## 1. Request

> You are a fixer for Veto Initiative 12, workstreams WS1 (tools+contracts) and WS3 (changelog), in repo ~/workspace/veto-mcp. Both blind reviews returned ALLOW, but each lists ship-blocking must-fix findings. Your job: apply all findings below, add tests proving the fixes, run the i12 test subset, and report changed files. Do NOT commit anything (a later sweep commits).

Solution shape assumed by the request: fix the listed findings in place; no redesign.

## 2. Artifacts produced

| Skill | Artifact | Path |
|---|---|---|
| — | Hardened public tools | `initiatives/i12/tools.py` |
| — | Consistent contract degradation | `initiatives/i12/contracts.py` |
| — | Hardened package CLI | `initiatives/i12/__main__.py` |
| — | Fix-proving tests (24 tests) | `tests/test_i12_tools_fixes.py` |
| — | Updated changelog tests for new behavior | `tests/test_i12_changelog.py` |
| — | This decision record | `docs/i12/decisions/ws1-tools-contracts.md` |

## 3. Decisions

### D1: Defensive `.get()` access in `jd_demo` (no contract return-schema pin)
- **Chose.** Defensive `.get()` chains plus `_decode_section`/`_decode_list` helpers and a non-dict shape guard in `tools.py`; the contract keeps pinning signatures only.
- **Over.**
  - Pin the decoder return schema in `contracts.py` alongside the signature check: trades away a side-effect-free contract layer (it would have to invoke the decoder to smoke-check shape) to get fail-loud drift detection at the contract boundary.
  - Leave the hard indexing: trades away nothing and keeps a `KeyError` crash path on any upstream shape change — rejected outright by the review.
- **Because.** The review's bar was "degrade honestly, never KeyError", and the other three demos already use `.get()` — one idiom across the module beats two. A contract-level shape check would move runtime invocation into capability resolution, which is the wrong layer for it.
- **ops_goal.** Unhandled exceptions from the four public demos on missing/drifted upstreams: hold at 0 (direction: zero, verified by tests feeding shape-drifted decoder output).
- **schedule_driven.** false. No deadline pressure shaped this; the honest-degradation requirement did. The fast path (leave hard indexing) did not silently win — it was explicitly rejected.
- **Confidence.** high. Covered by unit tests that feed `{}`, mistyped sections, and a non-dict decoder result.
- **Unknown at decision time.** Whether Initiative 04's richer interface will keep these top-level keys; the defensive reads hold regardless.

### D2: `_import_match()` degradation envelope for fit_explainer / risk_check / role_compare
- **Chose.** `try/except ImportError` → `None` → unavailable envelope, mirroring the `decoder_capabilities()` → unavailable-envelope pattern `jd_demo` already uses.
- **Over.**
  - Fail loudly on a missing `match`: trades away public-tool robustness to get an earlier developer signal — the wrong trade for a no-account public surface where a traceback is the worst outcome.
  - Stub or vendor `match` so demos always "work": trades away honesty to get always-on demos — rejected; never fake the scorer.
- **Because.** The review required the same contract-resolution/degradation pattern for all public demos. One helper (`_import_match`) plus one envelope builder (`_match_unavailable`) keeps the pattern identical in all three call sites.
- **ops_goal.** Tracebacks from public demos on a missing upstream scorer: hold at 0 (direction: zero).
- **schedule_driven.** false.
- **Confidence.** high. Tests null out `sys.modules["match"]` and assert the envelope.
- **Unknown at decision time.** None material — `match` is importable in this repo today.

### D3: Single source of truth for profile/prefs
- **Chose.** `_mini_profile_dict()` stays the canonical profile dict; new `_profile_prefs(prof)` derives the prefs view from it; precedence documented in the docstring (when `match.score_job(job, prof, prefs)` sees a key in both, the prefs view wins for location/salary preferences).
- **Over.**
  - Merge into one dict passed as both arguments: trades away the scorer's profile-vs-preferences distinction (`score_job` honors `salary_min`/`locations` from preferences) to get a single literal — risks changing scoring semantics silently.
  - Keep the duplicated literals: trades away nothing — rejected; the ambiguity was the finding.
- **Because.** Derivation (not duplication) removes the ambiguity without touching scorer semantics; the documented precedence settles the "which wins" question instead of leaving it to reader inference.
- **ops_goal.** Prefs-construction call sites outside `_profile_prefs`: hold at 0 (direction: zero divergence).
- **schedule_driven.** false.
- **Confidence.** high. Test asserts the derived prefs dict exactly, including `salary_min` present/absent cases.
- **Unknown at decision time.** Whether `match.score_job`'s prefs precedence will ever change; the docstring pins today's contract.

### D4: Lazy-import hardening in `__main__.py`
- **Chose.** `_lazy_i12_module()` catching `Exception` at the import boundary → `not available: <reason>` on stderr, exit 1.
- **Over.**
  - Direct imports (status quo): trades away graceful degradation to get simpler code — rejected by the review.
  - Catch only `ImportError`: trades away coverage of broken-module import errors to get a narrower except clause — the review said "missing/broken", so the boundary catches `Exception` but only around the import call itself.
- **Because.** Four of the five subcommands delegate to modules outside the review bundle; a missing or broken one must read as an honest message, never a raw `ModuleNotFoundError` traceback.
- **ops_goal.** Subcommands that print a raw traceback on a missing/broken module: hold at 0 (direction: zero); all five subcommands import and `--help` cleanly.
- **schedule_driven.** false.
- **Confidence.** high. Tests cover a missing module, a broken module (patched import raising), and `--help` for all five subcommands; also verified live via subprocess.
- **Unknown at decision time.** None — verified against the real tree.

### D5: Implement `render_text` (minor)
- **Chose.** Implement the plain-text view the module docstring promised, and use it in the tools CLI's non-JSON path.
- **Over.** Remove the claim: trades away a human-readable terminal view to get a slightly smaller module — implementing was cheaper than deleting, since the CLI already needed a text path.
- **Because.** The docstring promised it; keeping the promise with a 15-line function beats editing the promise away.
- **ops_goal.** Non-JSON CLI output renders as labeled text lines, not a raw JSON dump (direction: hold).
- **schedule_driven.** false.
- **Confidence.** high. Direct unit test.
- **Unknown at decision time.** None.

## 4. Options presented

| Option | Trades away | To get |
|---|---|---|
| Defensive `.get()` in tools.py (chosen) | Fail-loud drift detection at the contract layer | Honest degradation with no side effects in capability resolution |
| Pin return schema in contracts.py | A side-effect-free contract layer (must invoke the decoder to check shape) | Loud failure at the contract boundary on shape drift |
| `_import_match()` envelope (chosen) | Early loud failure for developers on a missing scorer | Public demos that degrade honestly instead of tracebacks |
| Fail loudly on missing `match` | Public robustness on broken installs | Earlier developer signal during integration |
| Derive prefs from canonical profile dict (chosen) | A single literal passed as both args | Zero ambiguity without changing scorer semantics |
| Merge profile+prefs into one dict | The scorer's profile-vs-preferences distinction | One fewer object to reason about |
| Catch `Exception` at lazy-import boundary (chosen) | A narrowly-scoped except clause | Honest degradation for missing AND broken modules |
| Catch only `ImportError` | Coverage of broken-module import errors | Tighter exception hygiene |
| Implement `render_text` (chosen) | A few lines of module surface | The docstring's promise kept for terminal users |

## 5. Neutral facts issued to the Ops Advocate

Not applicable — fixer loop, no advocate run. The blind reviews that produced these findings already returned ALLOW.

## 6. Verdicts

Not applicable — no critic/advocate/reviewer loop was run for the fixer pass itself. Findings came from the WS1 blind review (ALLOW with must-fix list), applied above.

## 7. Worker response to verdicts

| Finding | Response | Status |
|---|---|---|
| WS1-1 jd_demo hard-indexes decoder keys | Defensive `.get()` via `_decode_section`/`_decode_list` + non-dict guard | revised |
| WS1-2 bare `import match` in three demos | `_import_match()` + `_match_unavailable()` envelope | revised |
| WS1-3 duplicated prof/prefs namespaces | `_profile_prefs()` derives from canonical dict; precedence documented | revised |
| WS1-4 `__main__` direct imports of share/changelog/onboarding/telemetry | `_lazy_i12_module()` → honest message, exit 1; five `--help`s verified | revised |
| WS1 minor: render_text promise | Implemented and wired into CLI text path | revised |
| WS1 minor: truncated flag | True only when input exceeded MAX_COMPARE_JOBS | revised |
| WS1 minor: ImportError shape in decoder_capabilities() | Returns both records, unavailable, consistent shape | revised |
| WS1 minor: open() without context manager | `_load_json_file` with `with open(..., encoding="utf-8")` | revised |
| WS1 minor: "args" first-param acceptance | Documented with a comment at the drift check | revised |

## 8. Gate

Gate: n/a (fixer pass; commit sweep is the later human-gated step).
Routed because: scheduled gate — the later sweep commits, and only Paul clears.

### Human decision
- Arbiter: Paul Thorson
- Date: pending (commit sweep)
- Decision: pending
- Reason: —
- Vetoes cleared: none

## 9. Skips

| What was skipped | Instructed by | Recorded at |
|---|---|---|
| Committing any changes | Task instruction ("Do NOT commit; a later sweep commits") | 2026-09-13 |
