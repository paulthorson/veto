#!/usr/bin/env python3
"""Initiative 12 package CLI: ``python -m initiatives.i12 <command> ...``.

Terminal surface for the public tools, shareable artifacts, changelog,
onboarding experiments, and telemetry controls. Local-only; nothing here
submits, sends, or publishes anything.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _lazy_i12_module(name: str):
    """Import an i12 submodule, degrading honestly on missing/broken modules.

    Returns ``(module, None)`` on success or ``(None, reason)`` when the
    module cannot be imported — the caller prints the reason instead of
    letting a raw ModuleNotFoundError traceback reach the user.
    """
    try:
        return importlib.import_module(f"initiatives.i12.{name}"), None
    except Exception as exc:  # missing OR broken module: degrade honestly
        return None, f"the {name} module is not available: {exc}"


def _module_unavailable(reason: str) -> int:
    print(f"not available: {reason}", file=sys.stderr)
    return 1


def _emit(obj: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
        return
    from initiatives.i12 import tools as t
    if isinstance(obj, dict):
        print(t.render_text(obj))
    else:
        print(json.dumps(obj, indent=2, default=str))


def _load_json_file(path: str, flag: str) -> tuple[Any, int]:
    """Load a JSON file for a CLI flag; (None, 2) + stderr on any failure."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), 0
    except FileNotFoundError:
        print(f"{flag}: file not found: {path}", file=sys.stderr)
    except OSError as exc:
        print(f"{flag}: cannot read {path}: {exc}", file=sys.stderr)
    except json.JSONDecodeError as exc:
        print(f"{flag}: invalid JSON in {path}: {exc}", file=sys.stderr)
    return None, 2


def cmd_tools(args: argparse.Namespace) -> int:
    from initiatives.i12 import tools as t
    profile = t.MiniProfile(
        skills=[s.strip() for s in (args.skills or "").split(",") if s.strip()])
    if args.demo == "jd":
        _emit(t.jd_demo(args.text or ""), args.json)
    elif args.demo in ("fit", "risk"):
        job: Any = {}
        if args.job_json:
            job, code = _load_json_file(args.job_json, "--job-json")
            if code:
                return code
        if not isinstance(job, dict):
            print("--job-json must contain a JSON object describing one job, "
                  f"got {type(job).__name__}", file=sys.stderr)
            return 2
        _emit(t.fit_explainer(job, profile) if args.demo == "fit"
              else t.risk_check(job, profile), args.json)
    elif args.demo == "compare":
        job: Any = {}
        if args.job_json:
            job, code = _load_json_file(args.job_json, "--job-json")
            if code:
                return code
        if not isinstance(job, dict):
            print("--job-json must contain a JSON object describing one job, "
                  f"got {type(job).__name__}", file=sys.stderr)
            return 2
        jobs: Any = [job]
        if args.jobs_json:
            jobs, code = _load_json_file(args.jobs_json, "--jobs-json")
            if code:
                return code
        if not isinstance(jobs, list):
            print("--jobs-json must contain a JSON array of job objects, "
                  f"got {type(jobs).__name__}", file=sys.stderr)
            return 2
        _emit(t.role_compare(jobs, profile), args.json)
    elif args.demo == "install-status":
        from initiatives.i12 import contracts
        _emit(contracts.install_path_status(), args.json)
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    s, reason = _lazy_i12_module("share")
    if s is None:
        return _module_unavailable(reason)
    if args.kind == "score-card":
        art = s.score_card(args.role, args.company, args.score,
                           json.loads(args.factors_json or "[]"),
                           args.verdict, (args.reasons or "").split("|"))
    elif args.kind == "interview-plan":
        art = s.interview_plan(args.role,
                               json.loads(args.factors_json or "[]"),
                               args.sessions)
    elif args.kind == "progress":
        art = s.progress_snapshot(args.weeks,
                                  json.loads(args.factors_json or "{}"),
                                  args.outcomes)
    else:
        raise SystemExit(f"unknown share kind {args.kind}")
    print(s.render_markdown(art) if args.markdown else json.dumps(art, indent=2))
    return 0


def cmd_changelog(args: argparse.Namespace) -> int:
    c, reason = _lazy_i12_module("changelog")
    if c is None:
        return _module_unavailable(reason)
    if args.add:
        date, category, title, why, limitation, source = args.add
        c.add_entry(date, category, title, why, limitation, source)
        print("entry added")
    elif args.health:
        _emit(c.health(), True)
    else:
        print(c.render_markdown(include_git=args.with_git))
    return 0


def cmd_onboarding(args: argparse.Namespace) -> int:
    o, reason = _lazy_i12_module("onboarding")
    if o is None:
        return _module_unavailable(reason)
    if args.check_copy:
        findings = o.check_dark_patterns(Path(args.check_copy).read_text())
        _emit({"findings": findings, "clean": not findings}, True)
    else:
        path = o.assign_path(args.session_token or "demo",
                             args.role_maturity, args.tech_comfort)
        _emit({"path_id": path.id, "goal_workflow": path.goal_workflow,
               "steps": [{"id": st.id, "title": st.title, "cta": st.cta,
                          "skippable": st.skippable} for st in path.steps]},
              True)
    return 0


def cmd_telemetry(args: argparse.Namespace) -> int:
    tm, reason = _lazy_i12_module("telemetry")
    if tm is None:
        return _module_unavailable(reason)
    store = tm.TelemetryStore(Path(args.state) if args.state else None)
    if args.consent in ("on", "off"):
        rec = store.set_consent(args.consent == "on", source="cli")
        _emit({"consent": rec}, True)
    elif args.status:
        _emit(store.status(), True)
    elif args.report:
        _emit(store.report(args.report), True)
    elif args.public_payload:
        _emit(store.public_payload(), True)
    elif args.record:
        etype, kv = args.record
        fields = dict(p.split("=", 1) for p in kv.split(",") if "=" in p)
        # coerce simple scalars
        for k, v in list(fields.items()):
            if v.lower() in ("true", "false"):
                fields[k] = v.lower() == "true"
            elif v.replace(".", "", 1).isdigit():
                fields[k] = float(v) if "." in v else int(v)
        _emit(store.record(etype, **fields), True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m initiatives.i12",
        description="Initiative 12 public-growth machinery (local-only demos; "
                    "nothing is published or submitted).")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tools", help="public tool demos")
    t.add_argument("demo", choices=["jd", "fit", "risk", "compare", "install-status"])
    t.add_argument("--text", default="")
    t.add_argument("--job-json", default="")
    t.add_argument("--jobs-json", default="")
    t.add_argument("--skills", default="")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_tools)

    s = sub.add_parser("share", help="build a privacy-safe shareable artifact")
    s.add_argument("kind", choices=["score-card", "interview-plan", "progress"])
    s.add_argument("--role", default=""); s.add_argument("--company", default="")
    s.add_argument("--score", type=float, default=0.0)
    s.add_argument("--verdict", default="")
    s.add_argument("--reasons", default="")
    s.add_argument("--factors-json", default="")
    s.add_argument("--sessions", type=int, default=0)
    s.add_argument("--weeks", type=int, default=0)
    s.add_argument("--outcomes", type=int, default=0)
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(fn=cmd_share)

    c = sub.add_parser("changelog", help="transparent changelog")
    c.add_argument("--with-git", action="store_true")
    c.add_argument("--health", action="store_true")
    c.add_argument("--add", nargs=6,
                   metavar=("DATE", "CATEGORY", "TITLE", "WHY", "LIMITATION", "SOURCE"))
    c.set_defaults(fn=cmd_changelog)

    o = sub.add_parser("onboarding", help="onboarding experiments")
    o.add_argument("--role-maturity", default="new")
    o.add_argument("--tech-comfort", default="medium")
    o.add_argument("--session-token", default="demo")
    o.add_argument("--check-copy", default="")
    o.set_defaults(fn=cmd_onboarding)

    m = sub.add_parser("telemetry", help="consent-based analytics controls")
    m.add_argument("--state", default="")
    m.add_argument("--consent", choices=["on", "off"], default="")
    m.add_argument("--status", action="store_true")
    m.add_argument("--report", default="")
    m.add_argument("--public-payload", action="store_true")
    m.add_argument("--record", nargs=2, metavar=("EVENT", "k=v,k=v"))
    m.set_defaults(fn=cmd_telemetry)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
