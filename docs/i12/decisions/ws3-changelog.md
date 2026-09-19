# Decision Record: WS3 fixer — changelog hardening

Run ID: 2026-09-13-ws3-fixer
Lane: Develop & Deliver
Started: 2026-09-13
Human arbiter: anonymous operator

**This file is append-only.** Nothing above a committed line is edited. A correction is a new
entry that references the entry it corrects.

---

## 1. Request

> You are a fixer for Veto Initiative 12, workstreams WS1 (tools+contracts) and WS3 (changelog), in repo ~/workspace/veto-mcp. Both blind reviews returned ALLOW, but each lists ship-blocking must-fix findings. Your job: apply all findings below, add tests proving the fixes, run the i12 test subset, and report changed files. Do NOT commit anything (a later sweep commits).

Solution shape assumed by the request: fix the listed findings in place; implement (don't remove) the advertised `--since-git` CLI.

## 2. Artifacts produced

| Skill | Artifact | Path |
|---|---|---|
| — | Hardened transparent changelog | `initiatives/i12/changelog.py` |
| — | Fix-proving tests (25 tests) | `tests/test_i12_changelog_fixes.py` |
| — | Updated tests for new read/write discipline | `tests/test_i12_changelog.py` |
| — | This decision record | `docs/i12/decisions/ws3-changelog.md` |

## 3. Decisions

### D1: Flat JSON file store (kept), hardened — vs SQLite / git-notes / markdown-only
- **Chose.** Keep the flat `CHANGELOG_ENTRIES.json` file; harden it (explicit seeding, atomic writes, strict validation, `source` provenance field).
- **Over.**
  - SQLite store: trades away zero-dependency transparency (anyone can read the JSON in a text editor; the changelog is itself a transparency artifact) to get query power and transactions — overkill for a curated list read a few times a day.
  - Git-notes / commit-message-derived changelog: trades away the curated rejected/limited record (commits only record what happened, never what was refused) to get zero maintenance — but a changelog with no no's is a defect by this module's own rule.
  - Markdown-only file, no JSON: trades away machine-readable health checks (`health()` counts per category) to get hand-editability — the JSON stays valid for both readers and code.
- **Because.** The review's findings were all fixable inside the flat-file design; none of them indicted the storage choice. Changing stores would trade away the artifact's readability for problems we don't have.
- **ops_goal.** Curated entries lost or half-written by the write path: hold at 0 (direction: zero); entries missing non-empty why/limitation/source: hold at 0% (direction: 100% carry provenance).
- **schedule_driven.** false. No deadline pressure shaped this; the review's correctness bar did. The fast path (leave the write side effect in the read path) did not silently win — it was explicitly removed.
- **Confidence.** high. Atomic-write, validation, dedup, and read-purity tests all pass.
- **Unknown at decision time.** None material — entry volume is tiny and curated.

### D2: `source` provenance field + verifiable-only seed citations
- **Chose.** Add required `source` (provenance pointer) to the entry schema; cite only artifacts verified to exist in the repo (module paths, `docs/i12/education/qualified-applications.md`'s applications-per-day line, `telemetry.py`'s tripwire); render `**Source:**` in markdown; reject empty `source` in validation.
- **Over.**
  - Keep `why`-only entries: trades away nothing and keeps evidence-free claims — rejected by the review.
  - Cite the "annual roadmap DOCX": would trade away honesty to get a weightier-looking citation — rejected. **Flag:** no annual-roadmap DOCX exists in the repo (verified: the only `.docx` files are inside `.venv`); the roadmap artifacts live outside the repo. Seed "Roadmap Q3 scope" claims are cited to the in-repo artifacts that actually carry them (`initiatives/i12/__init__.py` "Q3 2027 scope", `README.md`, `qualified-applications.md`), not laundered through an invented document.
- **Because.** A transparency changelog whose entries can't be checked is marketing copy. Every seed source was verified by reading the cited file before writing the entry.
- **ops_goal.** Seed entries with unverifiable sources: hold at 0 (direction: zero invented references).
- **schedule_driven.** false.
- **Confidence.** high on the five seeds; medium that future contributors will cite as carefully — validation enforces non-empty, not truthful.
- **Unknown at decision time.** Whether the annual roadmap will land in the repo as a DOCX later; if it does, sources can be upgraded then.

### D3: Retitle the "shipped / NOT launched" entry + consistency check
- **Chose.** Retitle to "Public growth machinery built — launch-gated, not yet public (Initiative 12 build)", reword the limitation to drop the "NOT launched" phrasing, and add `consistency_warnings()` (regex for not-launched language in a shipped entry's limitation) surfaced in `health()` and `render_markdown()`.
- **Over.**
  - Recategorize as `limited`: trades away the true fact that the code did ship to the repo to get a cleaner category — the honest statement is "shipped to the tree, launch-gated", which the retitle says plainly.
  - Leave the contradiction: trades away nothing — rejected; it was the review's at-a-glance finding.
- **Because.** Category `shipped` + limitation "NOT launched" reads as a contradiction in a scan; the retitle keeps the shipped fact and the launch gate in one line, and the check prevents regressions.
- **ops_goal.** Shipped entries carrying not-launched language: hold at 0 (direction: zero, enforced by the check).
- **schedule_driven.** false.
- **Confidence.** high. Regex covered by positive and negative tests; seeds pass clean.
- **Unknown at decision time.** None.

### D4: Reads never write — explicit `seed_entries()`
- **Chose.** `load_entries()` returns `[]` on a missing file and never writes; `seed_entries(force=False)` is the explicit init path (also the `seed` CLI command); `add_entry()` seeds first only because it is a write path and must not silently drop the curated defaults.
- **Over.**
  - Keep seed-on-read: trades away read purity to get zero-friction first run — rejected by the review; a read that mutates the tree is a side effect wearing a read's name.
  - Raise on missing file: trades away graceful first-run UX (empty changelog renders with its own defect warnings, which is honest) to get stricter errors — the empty render already says out loud what's missing.
- **Because.** The review was explicit: remove the write side effect from the read path. The existing tests that assumed seed-on-read were updated to seed explicitly.
- **ops_goal.** Filesystem writes performed by the read path: hold at 0 (direction: zero).
- **schedule_driven.** false.
- **Confidence.** high. Read-purity test asserts the file still doesn't exist after `load_entries()`.
- **Unknown at decision time.** None.

### D5: Atomic writes + explicit JSON error + documented backup expectation
- **Chose.** Write-temp-then-`os.replace` (with flush+fsync) in `seed_entries()` and `add_entry()`; `json.JSONDecodeError` caught and re-raised as `ValueError` naming the file and the parse error; module docstring documents the backup story honestly.
- **Over.**
  - Direct `write_text`: trades away crash-safety (a killed process mid-write leaves a truncated file) to get simpler code — rejected; this file is the transparency record.
  - Let `JSONDecodeError` propagate raw: trades away a clear message naming the file to get less code — rejected; the reviewer asked for an explicit clear error.
- **Because.** Backup expectation, verified 2026-09-13 via `git ls-files`: the entries file is currently UNTRACKED (the whole `initiatives/i12` package is new), so the docstring says recovery is re-seeding until the commit sweep lands, after which git history is the backup. No invented "it's in git" claim.
- **ops_goal.** Half-written entries files observable after a crash: hold at 0 (direction: zero; no `.tmp` files left behind, asserted in tests).
- **schedule_driven.** false.
- **Confidence.** high for the write path; medium on the crash-safety claim (fsync+rename is the standard guarantee, not a chaos-tested one).
- **Unknown at decision time.** When the commit sweep lands; the docstring's backup story flips to git history at that point.

### D6: Strict validation — non-empty why/limitation/source, date format, dedup
- **Chose.** `_validate_entry()` rejects empty/whitespace `why`/`limitation`/`source`, enforces `YYYY-MM-DD` plus real-calendar dates, rejects bad categories; `_check_duplicates()` + `add_entry()` guard reject duplicate `(date, title)`.
- **Over.**
  - Lenient validation (warn, don't reject): trades away the changelog's integrity guarantee to get friendlier CLI UX — rejected; silent bad data in a transparency artifact is worse than a loud error.
- **Because.** Every rule maps to a review finding; dedup makes `add-entry` idempotent-safe for scripts.
- **ops_goal.** Invalid entries accepted by the write path: hold at 0 (direction: zero).
- **schedule_driven.** false.
- **Confidence.** high. Each rule has a dedicated test including bad-date edge cases (`2026-02-30`, `2026-13-01`).
- **Unknown at decision time.** None.

### D7: Minimal argparse CLI (implement, don't remove)
- **Chose.** Implement the advertised CLI: `render [--with-git]`, `add-entry` (all six fields required), `seed [--force]`, `health` — as `main()` + `if __name__ == "__main__"` block, ~40 lines.
- **Over.** Remove the `--since-git` docstring claim: trades away a useful convenience to get a smaller module — the review preferred implementing, and the git view is genuinely useful context next to curated entries.
- **Because.** The docstring advertised it; implementing small beats deleting. `git_highlights()` keeps curated entries authoritative (rendered under "context, not decisions").
- **ops_goal.** Advertised CLI surface that doesn't exist: hold at 0 (direction: zero).
- **schedule_driven.** false.
- **Confidence.** high. CLI tested end-to-end (seed → add-entry → render → health, plus `--help` for each).
- **Unknown at decision time.** None.

### D8: Nit fixes (cwd, ref sanitization, encoding, docstring path)
- **Chose.** `_repo_root()` prefers `git rev-parse --show-toplevel`, falls back to the module-file-derived tree root (never the caller's cwd); `_sanitize_ref()` allowlists git refs before argv interpolation (rejects leading-dash/whitespace/metacharacters); explicit `encoding="utf-8"` on all reads/writes; docstring describes `ENTRIES_FILE` instead of a hardcoded path.
- **Because.** Each was a reviewer nit with a concrete failure mode (cwd-dependent git log, ref-as-flag injection, platform-default encoding).
- **ops_goal.** n/a (hygiene).
- **schedule_driven.** false.

## 4. Options presented

| Option | Trades away | To get |
|---|---|---|
| Harden flat JSON (chosen) | Query power and transactions | Zero-dependency transparency anyone can read |
| SQLite store | Readability of the transparency artifact itself | Query power for a list read a few times a day |
| Git-notes-derived changelog | The curated rejected/limited record | Zero-maintenance generation from history |
| Verifiable-only `source` citations (chosen) | Weightier-looking citations | A changelog whose claims can actually be checked |
| Cite the roadmap DOCX | Honesty | A more impressive-looking provenance line |
| Retitle + consistency check (chosen) | A cleaner category label | The shipped fact and the launch gate in one honest line |
| Recategorize as `limited` | The true fact that the code shipped to the tree | Category/limitation agreement |
| Pure reads + explicit seed (chosen) | Zero-friction first run | No side effects wearing a read's name |
| Atomic temp-then-rename writes (chosen) | Simpler write code | A transparency record that survives a killed process |
| Strict validation (chosen) | Friendlier CLI UX on bad input | A transparency artifact that can't silently hold bad data |
| Implement the CLI (chosen) | A smaller module | The advertised surface actually existing |

## 5. Neutral facts issued to the Ops Advocate

Not applicable — fixer loop, no advocate run. The blind reviews that produced these findings already returned ALLOW.

## 6. Verdicts

Not applicable — no critic/advocate/reviewer loop was run for the fixer pass itself. Findings came from the WS3 blind review (ALLOW with must-fix list), applied above.

## 7. Worker response to verdicts

| Finding | Response | Status |
|---|---|---|
| WS3-1 docstring advertises `--since-git`, no CLI | Implemented minimal argparse CLI (`render`/`add-entry`/`seed`/`health`) | revised |
| WS3-2 entries lack evidence | Required `source` field; five seeds cite verified artifacts; validation enforces non-empty | revised |
| WS3-2 open question: unverifiable seed claims | Flagged: no annual-roadmap DOCX exists in repo; roadmap-scope claims cite in-repo artifacts only | revised |
| WS3-3 "shipped" + "NOT launched" contradiction | Retitled launch-gated; `consistency_warnings()` check in `health()` + render | revised |
| WS3-4 write side effect in read path | `load_entries()` pure; explicit `seed_entries()`; write path seeds only in `add_entry()` | revised |
| WS3-5 atomic writes, JSON errors, backup docs | Temp-then-rename + fsync; explicit `JSONDecodeError` handling; docstring documents untracked-until-sweep backup story | revised |
| WS3-6 validation | Non-empty why/limitation/source, YYYY-MM-DD + real dates, `(date,title)` dedup | revised |
| WS3 nit: fragile cwd | `_repo_root()` via `git rev-parse --show-toplevel`, file-derived fallback | revised |
| WS3 nit: since_ref into argv | `_sanitize_ref()` allowlist; rejects flag-like/malformed refs | revised |
| WS3 nit: encoding | `encoding="utf-8"` on all reads/writes | revised |
| WS3 nit: hardcoded docstring path | Docstring describes `ENTRIES_FILE`; path derived from module file | revised |

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
| Committing any changes | Task instruction ("Do NOT commit; a later sweep commits") | 2026-09-13 |
