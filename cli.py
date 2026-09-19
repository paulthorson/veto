#!/usr/bin/env python3
"""Command-line interface for the Veto MCP server.

Same tools as the MCP server (search, details, apply, boards,
applications, profile), usable directly from a terminal with no MCP
client config required.

Examples:
    python cli.py boards
    python cli.py search "software engineer" --location "New York, NY"
    python cli.py search "software engineer" --board greenhouse --limit 5
    python cli.py search "software engineer" --remote --json
    python cli.py show <job_id>
    python cli.py apply <job_id> --resume ~/resume.pdf        # dry run
    python cli.py apply <job_id> --resume ~/resume.pdf --confirm
    python cli.py applications
    python cli.py profile
    python cli.py wizard
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))  # make `import server` work from anywhere

import server  # noqa: E402  (reuses all tool implementations; no logic duplicated)

PROFILE_PATH = BASE_DIR / "profiles" / "profile.json"
WIZARD_PATH = BASE_DIR / "wizard.py"


# ---------------------------------------------------------------------------
# Output helpers (stdlib only)
# ---------------------------------------------------------------------------


def _truncate(text: object, width: int) -> str:
    text = "" if text is None else str(text).replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print aligned columns with a separator row."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*row))


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _fail(message: str, code: int = 1) -> int:
    print(f"Error: {message}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_boards(args: argparse.Namespace) -> int:
    boards = server.list_boards()
    _print_table(
        ["board", "status", "notes"],
        [[b["board"], b["status"], b["notes"]] for b in boards],
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    try:
        jobs = server.search_jobs(
            args.query,
            args.location,
            board=args.board,
            limit=args.limit,
            remote_only=args.remote,
        )
    except ValueError as exc:
        return _fail(str(exc))
    if args.json:
        _print_json(jobs)
        return 0
    if not jobs:
        print("No jobs found. Try a broader query or a different board.")
        return 0
    _print_table(
        ["id", "title", "company", "location", "board"],
        [
            [
                _truncate(j.get("id", ""), 26),
                _truncate(j.get("title", ""), 40),
                _truncate(j.get("company", ""), 28),
                _truncate(j.get("location", ""), 24),
                j.get("board", ""),
            ]
            for j in jobs
        ],
    )
    print(f"\n{len(jobs)} job(s). Use `show <id>` for full details.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    details = server.get_job_details(args.job_id)
    if args.json:
        _print_json(details)
        return 0
    if details.get("error"):
        return _fail(
            f"could not fetch job {args.job_id!r}: {details['error']}"
        )
    print(f"Title:    {details.get('title', 'Unknown')}")
    print(f"Company:  {details.get('company', 'Unknown')}")
    print(f"Board:    {details.get('board', 'Unknown')}")
    print(f"URL:      {details.get('url', '')}")
    print(f"Apply:    {details.get('apply_url', '')}")
    requirements = details.get("requirements") or ""
    if requirements:
        print("\nRequirements / criteria:")
        print(requirements)
    description = details.get("description") or ""
    if description:
        print("\nDescription:")
        if len(description) > 3000:
            print(description[:3000] + "\n… [truncated; open the URL for the full text]")
        else:
            print(description)
    return 0


def _load_profile_file(path: str) -> dict | None:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read profile file {path!r}: {exc}", file=sys.stderr)
        return None


def cmd_apply(args: argparse.Namespace) -> int:
    profile: dict = {}
    if args.profile:
        loaded = _load_profile_file(args.profile)
        if loaded is None:
            return 1
        profile = loaded

    result = server.apply_to_job(
        args.job_id,
        args.resume or "",
        cover_letter=args.cover_letter or "",
        confirm=args.confirm,
        profile=profile,
    )

    status = result.get("status")
    if status == "error":
        return _fail(str(result.get("error", "unknown error")))

    if status == "refused":
        # Legal-hardening commit 8 (spec §6.3): the per-action circuit
        # breaker refused — e.g. no interactive terminal. Nonzero exit.
        print(
            f"Refused: {result.get('instructions') or result.get('error')}",
            file=sys.stderr,
        )
        return 2

    if status == "dry_run":
        preview = result.get("preview", {})
        print("DRY RUN — nothing was submitted or recorded.\n")
        print(f"Job:      {preview.get('title')} @ {preview.get('company')}")
        print(f"Location: {preview.get('location')}")
        print(f"Board:    {preview.get('board')}")
        print(f"Apply:    {preview.get('apply_url')}")
        resume = preview.get("resume_path") or "(none)"
        found = "found" if preview.get("resume_found") else "NOT FOUND"
        print(f"Resume:   {resume} [{found}]")
        if preview.get("warning"):
            print(f"Warning:  {preview['warning']}")
        print(f"Cover letter: {preview.get('cover_letter_chars', 0)} chars")
        fields = preview.get("typical_form_fields") or []
        if fields:
            print(f"Typical form fields: {', '.join(fields)}")
        print(f"\n{result.get('message', '')}")
        print("Re-run with --confirm to record this application.")
        return 2

    # Confirmed (Phase 1 local log or Phase 2 browser result).
    app = result.get("application", {})
    print(f"{result.get('message', '')}\n")
    print(f"Job:      {app.get('title')} @ {app.get('company')}")
    print(f"Status:   {app.get('status')}")
    if app.get("submitted_at"):
        print(f"Logged:   {app['submitted_at']}")
    if app.get("screenshot"):
        print(f"Screenshot: {app['screenshot']}")
    print(f"Recorded in: {server.APPLICATIONS_FILE}")
    return 0


def cmd_applications(args: argparse.Namespace) -> int:
    apps = server.track_applications()
    if args.json:
        _print_json(apps)
        return 0
    if not apps:
        print("No applications recorded yet.")
        return 0
    _print_table(
        ["date", "title", "company", "board", "status"],
        [
            [
                _truncate((a.get("submitted_at") or "")[:10], 10),
                _truncate(a.get("title", ""), 38),
                _truncate(a.get("company", ""), 26),
                a.get("board", ""),
                a.get("status", ""),
            ]
            for a in apps
        ],
    )
    print(f"\n{len(apps)} application(s) in {server.APPLICATIONS_FILE}")
    return 0


def _summarize_profile(profile: dict) -> None:
    print(f"Name:     {profile.get('full_name') or profile.get('name', '(not set)')}")
    print(f"Email:    {profile.get('email', '(not set)')}")
    print(f"Phone:    {profile.get('phone', '(not set)')}")
    print(f"Location: {profile.get('location', '(not set)')}")
    titles = profile.get("target_titles") or profile.get("target_job_titles") or []
    if titles:
        print(f"Targets:  {', '.join(titles)}")
    skills = profile.get("skills") or []
    if skills:
        shown = ", ".join(skills[:12])
        extra = f" (+{len(skills) - 12} more)" if len(skills) > 12 else ""
        print(f"Skills:   {shown}{extra}")
    exp = profile.get("experience") or profile.get("positions") or []
    if exp:
        print(f"Roles:    {len(exp)} position(s) on file")


def cmd_profile(args: argparse.Namespace) -> int:
    get_profile = getattr(server, "get_profile", None)
    if callable(get_profile):
        try:
            profile = get_profile()
        except Exception as exc:  # never crash the CLI on a tool failure
            return _fail(f"get_profile failed: {exc}")
        if isinstance(profile, dict) and profile.get("error"):
            print(profile.get("message") or profile["error"])
            print("Run `python cli.py wizard` to create one.")
            return 1
        if isinstance(profile, dict) and "profile" in profile:
            # Unwrap the get_profile() tool envelope {"profile": {...}}.
            message = profile.get("message")
            inner = profile.get("profile") or {}
            if not inner:
                print(message or "No saved profile found.")
                print("Run `python cli.py wizard` to create one.")
                return 1
            _summarize_profile(inner)
            return 0
        if isinstance(profile, dict):
            _summarize_profile(profile)
            return 0
    if PROFILE_PATH.is_file():
        try:
            _summarize_profile(
                json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            )
            return 0
        except json.JSONDecodeError as exc:
            return _fail(f"profile file is corrupt: {exc}")
    print("No saved profile found.")
    print("Run `python cli.py wizard` to create one (it can ingest your LinkedIn).")
    return 1


def cmd_wizard(args: argparse.Namespace) -> int:
    if not WIZARD_PATH.is_file():
        print("The onboarding wizard isn't built yet — check back soon.")
        return 1
    # Hand off the terminal to the interactive wizard.
    completed = subprocess.run([sys.executable, str(WIZARD_PATH)])
    return completed.returncode


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


# Help grouping mirrors the dashboard's grouped menu:
# FIND / TAILOR / APPLY / TRAIN / WIN / GOVERN / UTILITIES.

_GROUP_ORDER = ("FIND", "TAILOR", "APPLY", "TRAIN", "WIN", "GOVERN", "UTILITIES")

_CORE_COMMAND_GROUPS = {
    "boards": "FIND",
    "search": "FIND",
    "show": "FIND",
    "apply": "APPLY",
    "applications": "APPLY",
    "dashboard": "UTILITIES",
    "serve": "UTILITIES",
    "wizard": "UTILITIES",
    "profile": "UTILITIES",
}

# Enhancement-plugin module -> help group. A command whose module is not
# listed here still gets a home: it falls back to UTILITIES, so every
# registered command always appears somewhere in --help.
_PLUGIN_MODULE_GROUPS = {
    "match": "FIND",
    "radar": "FIND",
    "jd_decoder": "FIND",
    "byol": "FIND",
    "cover_letters": "TAILOR",
    "linkedin_optimizer": "TAILOR",
    "ats_apply": "APPLY",
    "apply_queue": "APPLY",
    "grill": "APPLY",
    "mock_interview": "TRAIN",
    "soft_skills": "TRAIN",
    "ai_proficiency": "TRAIN",
    "mentors": "TRAIN",
    "skill_gaps": "TRAIN",
    "briefs": "WIN",
    "followup": "WIN",
    "referrals": "WIN",
    "network_crm": "WIN",
    "offer_compare": "WIN",
    "rejection_autopsy": "WIN",
    "analytics": "WIN",
    "email_sync": "WIN",
    "crew": "GOVERN",
    "streaks": "GOVERN",
    "doctor": "UTILITIES",
}

# command -> plugin module that registered it; filled by _plugin_cli_handlers.
_COMMAND_MODULES: dict[str, str] = {}


def _command_group(command: str) -> str:
    if command in _CORE_COMMAND_GROUPS:
        return _CORE_COMMAND_GROUPS[command]
    return _PLUGIN_MODULE_GROUPS.get(_COMMAND_MODULES.get(command, ""), "UTILITIES")


class _GroupedHelpFormatter(argparse.HelpFormatter):
    """Render subcommands under FIND / TAILOR / ... group headings."""

    def _format_action(self, action):
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)
        parts: list[str] = []
        for group in _GROUP_ORDER:
            subactions = [
                a
                for a in action._choices_actions
                if _command_group(a.dest) == group
            ]
            if not subactions:
                continue
            parts.append(f"{group}:")
            for subaction in subactions:
                parts.append("  " + super()._format_action(subaction).rstrip("\n"))
        return "\n".join(parts) + "\n"


_PLUGIN_CLI_MODULES = (
    "ats_apply",
    "apply_queue",
    "byol",
    "doctor",
    "analytics",
    "briefs",
    "email_sync",
    "grill",
    "match",
    "radar",
    "followup",
    "referrals",
    "mock_interview",
    "soft_skills",
    "ai_proficiency",
    "mentors",
    "offer_compare",
    "skill_gaps",
    "cover_letters",
    "linkedin_optimizer",
    "crew",
    "jd_decoder",
    "network_crm",
    "rejection_autopsy",
    "streaks",
    "outcomes",
    "notify",
    "provider_health",
    "reply_radar",
    "action_inbox",
    "calibration",
    "model_card",
    "near_miss",
    "funnels",
    "experiment_ledger",
    "outcome_nudges",
)


def _plugin_cli_handlers(sub) -> dict:
    """Register enhancement-plugin CLI commands on the subparsers.

    Returns {command: handler}; existing core commands are never
    overwritten. Plugins that fail to import are skipped silently
    (server.py logs them at startup). A plugin whose ``register_cli``
    raises, or returns something other than a {command: handler} dict,
    is skipped with a clear warning naming the plugin — never silently.
    """
    handlers: dict = {}
    for _mod_name in _PLUGIN_CLI_MODULES:
        try:
            _mod = __import__(_mod_name)
        except ImportError:
            continue
        _register = getattr(_mod, "register_cli", None)
        if _register is None:
            continue
        try:
            _registered = _register(sub)
        except Exception as exc:
            print(
                f"Warning: plugin '{_mod_name}' register_cli failed "
                f"({exc}); skipping its commands.",
                file=sys.stderr,
            )
            continue
        if not isinstance(_registered, dict):
            print(
                f"Warning: plugin '{_mod_name}' register_cli must return a "
                f"{{command: handler}} dict, got "
                f"{type(_registered).__name__}; skipping its commands.",
                file=sys.stderr,
            )
            continue
        for _cmd, _fn in _registered.items():
            if _cmd not in handlers:
                handlers[_cmd] = _fn
                # Record which module registered the command so --help can
                # group it (an existing command is never overwritten).
                _COMMAND_MODULES[_cmd] = _mod_name
    return handlers


def _add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="CLI for the Veto MCP server: search jobs, "
        "read details, and prepare applications from the terminal.",
        formatter_class=_GroupedHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output (search, show, applications).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show routine operational log chatter on stderr.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Errors only on stderr (suppresses warnings too).",
    )
    sub = parser.add_subparsers(dest="command", required=True, title="commands", metavar="command")

    sub.add_parser("boards", help="List supported job boards and their status.")

    p_search = sub.add_parser("search", help="Search job postings.")
    _add_json_flag(p_search)
    p_search.add_argument("query", help='Job title / keywords, e.g. "software engineer".')
    p_search.add_argument("--location", default="", help='e.g. "New York, NY" or "Remote".')
    p_search.add_argument(
        "--board",
        default="all",
        help='Board to search: "all", "greenhouse", "lever", "ashby", "adzuna", "glassdoor", "user" (default: all). "user" searches your saved bring-your-own listings only.',
    )
    p_search.add_argument("--limit", type=int, default=10, help="Max jobs (default: 10).")
    p_search.add_argument(
        "--remote", action="store_true", help="Only remote-friendly postings."
    )

    p_show = sub.add_parser("show", help="Show full details for a job.")
    p_show.add_argument("job_id", help="Job id from the search output.")
    _add_json_flag(p_show)

    p_apply = sub.add_parser(
        "apply", help="Preview (default) or record an application."
    )
    p_apply.add_argument("job_id", help="Job id from the search output.")
    p_apply.add_argument("--resume", default="", help="Path to your resume file.")
    p_apply.add_argument("--cover-letter", default="", help="Cover letter text.")
    p_apply.add_argument(
        "--profile",
        default="",
        help="Path to a profile JSON file (keys: full_name, email, phone, "
        "location, linkedin_url, website, cover_letter).",
    )
    p_apply.add_argument(
        "--confirm",
        action="store_true",
        help="Record the application (Phase 1 local log, or Phase 2 "
        "fill-only browser flow when VETO_BROWSER_APPLY=1). Phase 2 "
        "always requires the per-action circuit breaker: you type the "
        "company name exactly as shown at an interactive terminal — "
        "--confirm alone authorizes nothing. Without --confirm, only a "
        "dry-run preview is printed.",
    )

    p_apps = sub.add_parser("applications", help="List recorded applications.")
    _add_json_flag(p_apps)

    sub.add_parser("profile", help="Show the saved profile summary.")

    sub.add_parser(
        "wizard",
        help="Run the onboarding wizard (builds your profile).",
        description=(
            "Interactive onboarding wizard: walks you through entering "
            "your details (name, email, location, skills, experience), "
            "optionally ingests your LinkedIn profile, and saves "
            "profiles/profile.json so search/match/tailor/apply can use it."
        ),
    )

    sub.add_parser(
        "dashboard",
        help="Interactive terminal dashboard: KPI header + guided workflows for every tool.",
    )

    p_serve = sub.add_parser(
        "serve",
        help="Start the web dashboard (binds 127.0.0.1 by default; --host lan "
             "exposes it on your LAN; the JSON API is token-authenticated).",
    )
    p_serve.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765).")
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Address to bind: 127.0.0.1 (default), 'lan' to auto-detect this "
             "machine's LAN IP, or an explicit IP/hostname. Binding off "
             "loopback serves plain HTTP (no TLS): the bearer token travels "
             "in cleartext, so use only on a trusted LAN.",
    )
    p_serve.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the browser; just print the URL.",
    )
    p_serve.add_argument(
        "--regenerate-token",
        action="store_true",
        help="Replace the web UI API token with a fresh one, print it, and "
             "exit without starting the server. Restart any running server "
             "for the new token to take effect.",
    )
    p_serve.add_argument(
        "--show-token",
        action="store_true",
        help="Print the current web UI API token and exit without starting "
             "the server.",
    )

    parser.set_defaults(
        _plugin_handlers=_plugin_cli_handlers(sub),
    )

    return parser


def _cmd_dashboard(args: argparse.Namespace) -> int:
    import dashboard

    return dashboard.cmd_dashboard(args)


def _cmd_serve(args: argparse.Namespace) -> int:
    import webui

    return webui.cmd_serve(args)


def _configure_stderr_verbosity(args: argparse.Namespace) -> None:
    """Keep stderr for errors and genuine warnings only.

    The repo logs through the stdlib tree rooted at "veto-mcp"
    (server.py configures it at INFO to stderr), so a successful read-only
    command used to spray INFO chatter on stderr. Default the CLI to
    warnings+, so a successful command leaves stderr silent. --verbose opts
    back into the chatter; --quiet drops to errors only.
    """
    level = logging.WARNING
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.ERROR
    logging.getLogger("veto-mcp").setLevel(level)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_stderr_verbosity(args)
    handlers = {
        "boards": cmd_boards,
        "search": cmd_search,
        "show": cmd_show,
        "apply": cmd_apply,
        "applications": cmd_applications,
        "profile": cmd_profile,
        "wizard": cmd_wizard,
        "dashboard": _cmd_dashboard,
        "serve": _cmd_serve,
    }
    handlers.update(getattr(args, "_plugin_handlers", {}))
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
