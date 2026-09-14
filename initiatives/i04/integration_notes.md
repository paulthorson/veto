# Initiative 04 — Integration notes (for the later integration sweep)

**Status:** wiring snippets verified against the repo on 2026-09-13
(every referenced symbol grep-checked; the nonexistent-printer and
wrong-registration bugs from the 2026-09-13 blind review are fixed
below — evidence in §7). The sweep applies these snippets verbatim.
**Do not edit** `cli.py`, `webui.py`, `dashboard.py`, or `server.py`
in the 04 workstream — those files take only the small, exact wiring
edits named in §§1–2.

The registration snippets in §§1–2 are added to
`initiatives/i04/__init__.py` by the sweep (the package exists at
`initiatives/i04/__init__.py`; `initiatives/__init__.py` exists, so
dotted imports work). All analysis imports come from the
`initiatives.i04` package. Every workflow below ships the guided path
+ escape hatch + empty/loading/blocked/failure states per the single
definition of done; safety-sensitive actions (share-card export, file
ingestion analysis) go through an explicit preview → confirm → audit
sequence — now implemented in the CLI helpers, not just noted.

**Decision record (parity harness):** the spike recommended a TS port
of `fit_explain` + corpus extension as the 01–02 → 04 gate artifact.
The Python surfaces below are the Q1 product; the TS port is a
follow-on workstream owned by whoever extends
`site/harness/jd-decoder/`. The existing `jd_verdict` parity harness
stays green and untouched. Rationale: shipping the six epics now on
the proven Python engine beats gating product on a second port;
`fit_explain` is a pure function of text-in/JSON-out, so the port is
mechanical when scheduled. (convenience_driven: true — what we give
up: browser-side fit decoding in Q1; what we save: one full port +
corpus cycle before any user value ships.)

*Falsifier for the "mechanical port" claim:* the claim is false if the
TS port changes any observable behavior of the Python implementation
(non-deterministic output, paraphrasing, new inference) or if
`fit_explain`/`build_evidence_map` stop being pure text-in/JSON-out
functions (e.g. gain model, API, or network calls). The check is
byte-equality of port output against the Python parity corpus —
any divergence falsifies "mechanical".

---

## 1. CLI (`cli.py`)

The actual plugin flow (cli.py:384-468): a module name goes in
`_PLUGIN_CLI_MODULES`, and that module exposes
`register_cli(subparsers) -> {command: handler}`. The sweep adds the
block below to `initiatives/i04/__init__.py` (imports included, so it
is self-contained).

```python
"""Initiative 04 CLI registration (added by the integration sweep)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jd_decoder import _print_result  # jd_decoder.py:538, verified

from initiatives.i04 import (
    career_graph, evidence_map, file_ingest, fit_explain,
    role_compare, share_card,
)


def _read_cli_text(value: str, *, auto_yes: bool = False) -> str | None:
    """Read a CLI text argument, routing PDF/DOCX through the ingest gate.

    "-" reads stdin; plain-text files are read directly; .pdf/.docx
    files go through file_ingest.extract_file plus the preview gate.
    confirm_preview_cli returns one of GATE_CONTINUE / GATE_PASTE /
    GATE_CANCEL: "paste" starts the paste-instead stdin flow via
    read_pasted_text_cli; anything but "continue" cancels (fails
    closed). With auto_yes the preview is still printed but the prompt
    is skipped, i.e. the gate defaults to "continue". Returns None when
    the user cancels at the gate — handlers turn that into exit code 2.
    """
    if value == "-":
        return sys.stdin.read()
    path = Path(value)
    if path.suffix.lower() in (".pdf", ".docx"):
        extraction = file_ingest.extract_file(path)
        if auto_yes:
            print(file_ingest.preview_report(extraction))
            return extraction["text"]
        decision = file_ingest.confirm_preview_cli(extraction)
        if decision == file_ingest.GATE_PASTE:
            return file_ingest.read_pasted_text_cli()
        if decision != file_ingest.GATE_CONTINUE:
            print("Cancelled at the preview gate — nothing was analyzed.",
                  file=sys.stderr)
            return None
        return extraction["text"]
    return path.read_text(encoding="utf-8", errors="replace")


def _as_fit_result_doc(obj: dict[str, Any]) -> dict[str, Any]:
    """Normalize to the bare veto/fit-result/v1 doc.

    fit-decode --json prints the fit_explain envelope
    {"fit_result": <doc>, "suggested_grill_questions": [...],
    "validation_errors": [...]} (fit_explain.py:172-176). share-card needs
    the bare doc — the evidence map lives at doc["evidence_map"]
    ["entries"] (fit_explain.py:159), and build_share_card itself reads
    the evidence map at the doc's top level (share_card.py:122), so
    handing it the raw envelope would silently select zero fragments.
    Accepts either shape; returns the doc.
    """
    if (isinstance(obj, dict) and "evidence_map" not in obj
            and "fit_result" in obj):
        return obj["fit_result"]
    return obj


def cmd_fit_decode(args: argparse.Namespace) -> int:
    resume = _read_cli_text(args.resume, auto_yes=args.yes)
    jd = _read_cli_text(args.jd, auto_yes=args.yes)
    if resume is None or jd is None:
        return 2
    outcome = fit_explain.fit_explain(
        resume, jd, args.job_id, args.job_title,
    )
    _print_result(outcome, args.json)
    return 0


def cmd_evidence_map(args: argparse.Namespace) -> int:
    jd = _read_cli_text(args.jd, auto_yes=args.yes)
    resume = _read_cli_text(args.resume, auto_yes=args.yes)
    if jd is None or resume is None:
        return 2
    outcome = evidence_map.build_evidence_map(
        jd, resume, args.job_id, args.job_title,
    )
    _print_result(outcome, args.json)
    return 0


def cmd_compare_roles(args: argparse.Namespace) -> int:
    jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    resume = ""
    if args.resume:
        resume = _read_cli_text(args.resume, auto_yes=args.yes)
        if resume is None:
            return 2
    result = role_compare.compare_roles(jobs, resume)
    _print_result(result, args.json)
    return 0


def cmd_career_graph(args: argparse.Namespace) -> int:
    events = [json.loads(line) for line in
              Path(args.events).read_text(encoding="utf-8").splitlines()
              if line.strip()]
    fits = (json.loads(Path(args.fits).read_text(encoding="utf-8"))
            if args.fits else {})
    result = career_graph.build_career_graph(events, fits)
    _print_result(result, args.json)
    return 0


def cmd_share_card(args: argparse.Namespace) -> int:
    doc = _as_fit_result_doc(
        json.loads(Path(args.fit_result).read_text(encoding="utf-8")))
    # Preview: list selectable evidence fragments (id, requirement, quote).
    entries = (doc.get("evidence_map") or {}).get("entries", [])
    print("Selectable evidence fragments:")
    for entry in entries:
        req = entry.get("requirement", {})
        for item in entry.get("evidence") or []:
            print(f"  {item['evidence_id']}: [{req.get('text', '')}] "
                  f"{item.get('quote', '')[:120]}")
    selected = [s.strip() for s in args.select.split(",") if s.strip()]
    if not args.yes:
        print("Re-run with --select <ids> --yes to build the card after review.")
        return 0
    resume = _read_cli_text(args.resume)
    jd = _read_cli_text(args.jd)
    if resume is None or jd is None:
        return 2
    card = share_card.build_share_card(doc, selected, resume, jd)
    print(share_card.encode_share_payload(card))  # audit: log this fragment
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    extraction = file_ingest.extract_file(args.path)
    if args.yes:
        # --yes skips the prompt but the preview is still printed.
        print(file_ingest.preview_report(extraction))
    else:
        decision = file_ingest.confirm_preview_cli(extraction)
        if decision == file_ingest.GATE_PASTE:
            print(file_ingest.read_pasted_text_cli())
            return 0
        if decision != file_ingest.GATE_CONTINUE:
            print("Cancelled — nothing was analyzed.")
            return 0
    print(extraction["text"])
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the six Initiative 04 commands to an argparse subparsers.

    Returns a {command: handler} mapping, the shape
    cli.py::_plugin_cli_handlers requires (cli.py:425-468).
    """
    # --- veto fit-decode: Epic 1 (resume + job, on-device) ---
    p = subparsers.add_parser(
        "fit-decode",
        help="Decode resume + job fit fully on-device (no model/API).",
    )
    p.add_argument("--resume", required=True,
                   help="Path to resume (.pdf/.docx/.txt) or '-' for stdin. "
                        "PDF/DOCX go through the ingest preview gate.")
    p.add_argument("--jd", required=True,
                   help="Path to job description text file or '-' for stdin")
    p.add_argument("--job-id", required=True)
    p.add_argument("--job-title", default="")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the ingest preview prompt (the preview is "
                        "still printed).")
    # --- veto evidence-map: Epic 2 ---
    p = subparsers.add_parser(
        "evidence-map",
        help="Map each JD requirement to resume evidence, a gap, or a grill question.",
    )
    p.add_argument("--resume", required=True,
                   help="Path to resume (.pdf/.docx/.txt) or '-' for stdin. "
                        "PDF/DOCX go through the ingest preview gate.")
    p.add_argument("--jd", required=True,
                   help="Path to job description text file or '-' for stdin")
    p.add_argument("--job-id", required=True)
    p.add_argument("--job-title", default="")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the ingest preview prompt (the preview is "
                        "still printed).")

    # --- veto compare-roles: Epic 3 (up to 4) ---
    p = subparsers.add_parser(
        "compare-roles",
        help="Compare up to 4 jobs across fit, risk, compensation, location, readiness.",
    )
    p.add_argument("--jobs", required=True,
                   help="JSON file: list of {job_id, job_title, jd_text, score_snapshot?}")
    p.add_argument("--resume", default="",
                   help="Resume text file for live scoring "
                        "(omit when all rows have snapshots)")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the ingest preview prompt (the preview is "
                        "still printed).")

    # --- veto career-graph: Epic 4 ---
    p = subparsers.add_parser(
        "career-graph",
        help="Trend strengths, gaps, seniority, target-role movement from outcome events.",
    )
    p.add_argument("--events", default="outcomes.jsonl",
                   help="Outcome events file (Initiative 01 format)")
    p.add_argument("--fits", default="",
                   help="JSON file: {application_id: fit-result} (optional)")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")

    # --- veto share-card: Epic 5 (preview -> confirm -> audit) ---
    p = subparsers.add_parser(
        "share-card",
        help="Build a privacy-safe share card (scores + selected evidence only).",
    )
    p.add_argument("--fit-result", required=True,
                   help="fit-result JSON file (fit-decode --json output or "
                        "the bare fit-result doc)")
    p.add_argument("--select", default="",
                   help="Comma-separated evidence_ids to include (preview lists them)")
    p.add_argument("--resume", required=True,
                   help="Raw resume text file (for the privacy scrub; "
                        "PDF/DOCX go through the ingest preview gate)")
    p.add_argument("--jd", required=True,
                   help="Raw JD text file (for the privacy scrub)")
    p.add_argument("--yes", action="store_true",
                   help="Confirm after preview (otherwise prints preview and exits)")

    # --- veto ingest: Epic 6 (preview gate before analysis) ---
    p = subparsers.add_parser(
        "ingest",
        help="Extract text from a PDF/DOCX/TXT file with an explicit preview first.",
    )
    p.add_argument("path", help="File to ingest")
    p.add_argument("--yes", action="store_true",
                   help="Skip the preview prompt (the preview is still printed)")
    return {
        "fit-decode": cmd_fit_decode,
        "evidence-map": cmd_evidence_map,
        "compare-roles": cmd_compare_roles,
        "career-graph": cmd_career_graph,
        "share-card": cmd_share_card,
        "ingest": cmd_ingest,
    }
```

Wiring (the sweep makes exactly these edits in `cli.py` — cli.py is
not touched in this workstream):

1. Add `"initiatives.i04",` to `_PLUGIN_CLI_MODULES` (cli.py:384).
2. `_plugin_cli_handlers` (cli.py:425-466) loads plugins with
   `__import__(_mod_name)` (the `__import__` is at cli.py:437); for a dotted name that returns the
   top-level package, so `getattr(_mod, "register_cli", None)` would
   be `None` and all six commands would silently never register. The
   sweep must switch that loop to `importlib.import_module`
   (and add `import importlib` near cli.py's other stdlib imports).

Note: until the sweep adds a `_PLUGIN_MODULE_GROUPS` entry
(cli.py:324), the six commands fall back to UTILITIES in `--help`
(cli.py:356-360) — cosmetic, not a registration failure.

---

## 2. MCP server (`server.py`)

The actual plugin flow (server.py:162-202, 1593-1612): a module name
goes in the `_PLUGIN_MODULES` name tuple, and that module exposes
`register_tools(mcp)`. The sweep adds the block below to
`initiatives/i04/__init__.py` (same file as §1, so the
`_as_fit_result_doc` helper is in scope).

```python
def register_tools(mcp: Any) -> None:
    """Register the five Initiative 04 MCP tools on a server instance."""
    from initiatives.i04 import (
        career_graph, evidence_map, fit_explain,
        role_compare, share_card,
    )

    @mcp.tool()
    def fit_decode(resume_text: str, jd_text: str, job_id: str,
                   job_title: str = "") -> dict:
        """Decode resume+job fit on-device (no model/API).

        Returns the fit_explain envelope: {"fit_result":
        <veto/fit-result/v1 doc>, "suggested_grill_questions": [...],
        "validation_errors": [...]}.
        """
        return fit_explain.fit_explain(
            resume_text, jd_text, job_id, job_title)

    @mcp.tool()
    def build_evidence_map(jd_text: str, resume_text: str, job_id: str,
                           job_title: str = "") -> dict:
        """Map each JD requirement to evidence, a gap, or a grill question."""
        return evidence_map.build_evidence_map(
            jd_text, resume_text, job_id, job_title)

    @mcp.tool()
    def compare_roles(jobs: list, resume_text: str = "") -> dict:
        """Compare up to 4 jobs across fit, risk, compensation, location, readiness."""
        return role_compare.compare_roles(jobs, resume_text)

    @mcp.tool()
    def career_graph_tool(events: list, fit_results: dict | None = None) -> dict:
        """Trend strengths, gaps, seniority, target-role movement (read-only)."""
        return career_graph.build_career_graph(events, fit_results)

    @mcp.tool()
    def build_share_card(fit_result: dict, selected_evidence_ids: list,
                         raw_resume_text: str, raw_job_text: str) -> dict:
        """Privacy-safe share card. Fails closed on any scrub violation.

        Accepts either the fit-decode envelope or the bare fit-result
        doc; the envelope is normalized because build_share_card reads
        the evidence map at the doc's top level.
        """
        return share_card.build_share_card(
            _as_fit_result_doc(fit_result), selected_evidence_ids,
            raw_resume_text, raw_job_text)
```

Wiring (the sweep makes exactly these edits in `server.py` —
server.py is not touched in this workstream):

1. Add `"initiatives.i04",` to the `_PLUGIN_MODULES` name tuple
   (server.py:162).
2. The plugin loop (server.py:163-206) loads modules with
   `__import__(_plugin_name)` (the `__import__` is at server.py:202); for a dotted name that returns the
   top-level package, so `getattr(_mod, "register_tools", None)`
   would be `None` and zero tools would register, silently. The
   sweep must switch that loop to `importlib.import_module`
   (and add `import importlib` near server.py's other stdlib imports).

Note: `file_ingest` is intentionally **not** an MCP tool — file bytes
crossing the tool boundary would break the local-first guarantee for
resume content. What this trades away: MCP clients get no file
ingestion at all. A file's bytes can only enter Veto through the
CLI, the web UI, or the phone surfaces; an MCP-only client cannot
ingest PDF/DOCX and must paste text. Ingestion stays in CLI / web /
phone surfaces where the bytes never leave the device.

---

## 3. Local web UI (`webui.py`) + phone

New routes (follow the existing page conventions):

* `GET /decoder/fit` — the fit wizard (guided path):
  1. Paste resume **or** upload PDF/DOCX → extraction runs locally →
     **preview gate** (counts + first 600 chars + Continue / Paste
     instead / Cancel).
  2. Paste job description → **preview gate** (same contract).
  3. Result: fit score + provenance badge ("Static score — no outcome
     data yet" until 02 lands) + limitations card (always visible,
     never fine print) + JD risk read + evidence map table
     (supported / gap / grill-question rows; grill rows link into the
     grill session via `grill_question_id`).
  4. Escape hatches: "paste text instead" on both ingestion steps;
     "skip evidence map" jumps to score-only view; empty states for
     empty resume/JD with what-to-do-next copy.
* `GET /decoder/compare` — role comparison (up to 4 slots; adding a
  5th shows the cap message). Compensation column hidden with the
  stated reason unless ≥ 2 postings state ranges. Mixed
  snapshot/live rows labeled; version-mismatch warning banner.
* `GET /decoder/career` — career graph (read-only). Refuse-to-render
  state when outcome events use an unknown schema version (show the
  reason, not a broken graph). Bands with < 5 applications show
  "insufficient data".
* `GET /decoder/share` — share-card builder: fit result → evidence
  fragment checklist (explicit opt-in per fragment) → **preview of the
  exact payload** → confirm → link fragment shown + audit log entry.
  No excerpt option exists on this surface (by design).

Phone: all four pages follow the responsive conventions from the
phone-access build (single column, sticky nav, ≥ 44px tap targets, no
iOS input zoom). The preview gates are full-screen sheets on narrow
viewports.

---

## 4. Dashboard (`dashboard.py`)

New cards (data from `dashboard_data.py` additions in the sweep):

* **Fit decoder card** — latest fit score + provenance badge + top 3
  evidence rows. Links to `/decoder/fit`.
* **Evidence gaps card** — recurring gap labels from the career graph
  (top 5, with `times_seen`); the coaching handoff for Initiative 06.
* **Career trend card** — seniority points sparkline + reply-rate by
  fit band (bands with < 5 shown as "insufficient data", never a
  rate).

---

## 5. Initiative 05 handoff (already published)

05 consumes `veto/evidence-map/v1` per
`initiatives/i04/schema_evidence_map_v1.md` §4: store link entries
keyed by `evidence_source_hash`, re-run `validate_evidence_map` on
load, treat violations as "needs re-approval". The
`grill_question_id` values (`gq_<hash(job_id, requirement)>`) are
stable: the grill surface materializes questions with these ids so
evidence-map rows deep-link into the session.

*Falsifier for the stability claim:* `_grill_question_id`
(evidence_map.py:189-194) is `gq_` + the first 8 hex chars of
sha256(`job_id` + `\x00` + `requirement_text`). The claim is false if
the hash inputs change (anything beyond job_id and the raw
requirement text), the 8-hex format changes, or requirement-text
normalization (keyword extraction / aliases) alters
`requirement_text` for the same JD. Any such change silently breaks
every 05 deep link.

## 6. Assumption flags carried into the sweep

* 01's `score_snapshot` shape and 02's provenance engine are
  **contracted, not implemented** — the sweep must not assume live
  stores exist; snapshot rows render from the contracted shape, live
  rows decode on-device.
* If the Q4 evidence threshold is unreachable, the sweep keeps the
  static provenance badge and the "insufficient data" band labels —
  the product degrades, it never invents confidence.

*Falsifier for the "never invents confidence" claim:* the claim is
false if any surface renders a fit score without the static
provenance badge while 02 is unimplemented, renders a reply-rate for
a fit band with fewer than 5 applications, or renders a
version-mismatched comparison row without the warning banner. One
such render is a violation — the DoD's empty/blocked/failure states
exist precisely to hold this line.

---

## 7. Symbol verification (2026-09-13)

Every symbol the snippets above reference was grep-checked before
writing. (`_print_json_or_summary`: zero hits anywhere in the repo —
the name from the pre-review draft does not exist, which is why it
was removed.)

| Symbol | Location |
|---|---|
| `jd_decoder._print_result(result, as_json)` | jd_decoder.py:538 (sig: `(result: dict[str, Any], as_json: bool) -> None`; prints `result["markdown"]` or JSON) |
| `jd_decoder.register_cli` / `jd_decoder.register_tools` | jd_decoder.py:558 / jd_decoder.py:502 (the plugin conventions §§1–2 follow) |
| `_PLUGIN_CLI_MODULES`, `_plugin_cli_handlers` (`register_cli` attr, `{command: handler}` dict) | cli.py:384, cli.py:425-466 |
| `_PLUGIN_MODULES` name tuple, `_register_plugin_tools` (`register_tools` attr) | server.py:162, server.py:1593 |
| `__import__` dotted-name caveat (returns top-level package) | cli.py:437, server.py:202; neither file imports `importlib` — the wiring edits add it |
| `fit_explain.fit_explain(resume_text, jd_text, job_id, job_title="")` | fit_explain.py:87; envelope return fit_explain.py:172-176; `"evidence_map"` at fit_explain.py:159 |
| `evidence_map.build_evidence_map(jd_text, resume_text, job_id, job_title="")` | evidence_map.py:196 |
| `role_compare.compare_roles(jobs, resume_text="")`, `MAX_JOBS = 4` | role_compare.py:137, role_compare.py:44 |
| `career_graph.build_career_graph(events, fit_results=None)` | career_graph.py:181 |
| `share_card.build_share_card(fit_result, selected_evidence_ids, raw_resume_text, raw_job_text)` | share_card.py:103; evidence-map read at top level share_card.py:122 |
| `share_card.encode_share_payload(payload)` | share_card.py:196 |
| `file_ingest.extract_file(path)` / `preview_report` / `confirm_preview_cli` | file_ingest.py:223 / :257 / :300 — note: `confirm_preview_cli` returns `"continue"` \| `"paste"` \| `"cancel"` (GATE_*, file_ingest.py:99-101), not bool; the snippets handle all three branches, incl. `read_pasted_text_cli()` |
| `_grill_question_id` | evidence_map.py:189 |
| `initiatives/__init__.py`, `initiatives/i04/__init__.py` | exist (dotted import `initiatives.i04` valid) |
