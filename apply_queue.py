#!/usr/bin/env python3
"""Drip-apply scheduler: a persistent, human-paced application queue.

Jobs are added to ``apply_queue.json`` (gitignored, lives next to this
project) and processed by ``run_queue`` — cron-friendly: it pops due
items one at a time, enforces the compliance gate before every item,
and sleeps a random ``APPLY_PACE_SECONDS`` between applications so
applying always moves at a human pace.

Compliance contract (see compliance.py):

* ``compliance.check_apply_allowed()`` is consulted before each item;
  when the daily cap is hit the run stops cleanly (remaining items stay
  queued) and the report says so.
* On every successful application ``compliance.record_application()``
  is called to spend one unit of the daily budget.

The actual apply work is injected as ``apply_fn(job_id) -> dict``. In
production this is the server's apply path (``server.apply_to_job``
with ``confirm=True``); in tests it is a stub. An apply_fn result is
treated as success when it is a dict without an ``"error"`` key.
Failures (error result or exception) are left in the queue with an
error note and an incremented attempt counter, so the next run retries
them.

``if __name__ == "__main__"`` usage::

    python3 apply_queue.py queue-add <job_id> [--scheduled-for <iso>]
    python3 apply_queue.py queue-list
    python3 apply_queue.py queue-run [--max-items N] [--resume R] [--notify]

Wiring into the main CLI (cli.py) without editing it — in the wiring
code, after ``sub = parser.add_subparsers(...)``::

    import apply_queue
    handlers = apply_queue.register_cli(sub)  # {command: handler} mapping
    dispatch.update(handlers)

The returned mapping merges into the caller's dispatch table; each
parser also carries ``func`` so a caller can dispatch
``args.func(args)`` when the parsed args carry it.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent
QUEUE_FILE = BASE_DIR / "apply_queue.json"

import compliance  # noqa: E402  (stdlib-style local module)

# apply_fn contract: (job_id: str) -> dict. Success == dict without "error".
ApplyFn = Callable[[str], dict]
# notify_fn contract: (title: str, body: str) -> dict. May be None.
NotifyFn = Callable[[str, str], dict]


# ---------------------------------------------------------------------------
# Queue store
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue(path: Path | None = None) -> dict:
    """Load the queue store; return {"items": []} when missing/corrupt."""
    path = Path(path) if path else QUEUE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def save_queue(queue: dict, path: Path | None = None) -> None:
    path = Path(path) if path else QUEUE_FILE
    path.write_text(
        json.dumps(queue, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def add_to_queue(
    job_id: str,
    scheduled_for: str | None = None,
    path: Path | None = None,
) -> dict:
    """Add a job to the queue (idempotent: re-adding refreshes the entry).

    ``scheduled_for`` is an ISO-8601 timestamp; the item is not picked
    up by ``run_queue`` before then. ``None`` means due immediately.
    """
    job_id = str(job_id).strip()
    if not job_id:
        raise ValueError("job_id must not be empty")
    if scheduled_for is not None:
        # Validate early so a typo doesn't silently queue a never-due item.
        datetime.fromisoformat(str(scheduled_for))
    queue = load_queue(path)
    for item in queue["items"]:
        if item.get("job_id") == job_id:
            item["scheduled_for"] = scheduled_for
            item["status"] = "queued"
            save_queue(queue, path)
            return item
    item = {
        "job_id": job_id,
        "added_at": _utcnow_iso(),
        "scheduled_for": scheduled_for,
        "status": "queued",
        "attempts": 0,
        "last_error": None,
    }
    queue["items"].append(item)
    save_queue(queue, path)
    return item


def list_queue(path: Path | None = None) -> list[dict]:
    """Return all queue items in insertion order."""
    return list(load_queue(path).get("items", []))


def remove_from_queue(job_id: str, path: Path | None = None) -> bool:
    """Remove a job from the queue. Returns True when something was removed."""
    queue = load_queue(path)
    before = len(queue["items"])
    queue["items"] = [
        i for i in queue["items"] if i.get("job_id") != str(job_id)
    ]
    if len(queue["items"]) != before:
        save_queue(queue, path)
        return True
    return False


def _is_due(item: dict, now: datetime) -> bool:
    if item.get("status") != "queued":
        return False
    scheduled_for = item.get("scheduled_for")
    if not scheduled_for:
        return True
    try:
        return datetime.fromisoformat(scheduled_for) <= now
    except ValueError:
        return True  # unparseable timestamp: don't strand the item


# ---------------------------------------------------------------------------
# Recording (lazy server import — keeps queue-add/queue-list light)
# ---------------------------------------------------------------------------


def _default_record_fn(job_id: str, result: dict) -> dict:
    """Record a successful queue application in applications.json.

    Lazy-imports server so this module stays importable without the MCP
    stack. Dedupes on job_id: the production apply_fn (server.apply_to_job
    with confirm=True) already records the application itself, so the
    queue must not double-log it.
    """
    from server import (  # noqa: E402  (lazy: heavy MCP import)
        _load_applications,
        _save_applications,
    )

    app = result.get("application") if isinstance(result, dict) else None
    preview = result.get("preview") if isinstance(result, dict) else None
    app = app if isinstance(app, dict) else {}
    preview = preview if isinstance(preview, dict) else {}
    applications = _load_applications()
    if any(e.get("job_id") == job_id for e in applications):
        existing = next(e for e in applications if e.get("job_id") == job_id)
        # Already recorded: still ensure the outcome event exists (the
        # original recording may predate capture). Duplicate-safe.
        try:
            import outcomes as _outcomes

            _outcomes.record_application_event(existing, source="apply_queue")
        except Exception:  # noqa: BLE001 - capture must never break recording
            import logging

            logging.getLogger("job-apply-mcp.apply_queue").exception(
                "outcome-min-v0 capture failed for %s", job_id
            )
        return existing
    submitted_at = _utcnow_iso()
    entry = {
        "job_id": job_id,
        "board": preview.get("board") or app.get("board") or "",
        "title": preview.get("title") or app.get("title") or "",
        "company": preview.get("company") or app.get("company") or "",
        "location": preview.get("location") or "",
        "apply_url": preview.get("apply_url") or "",
        "status": "confirmed",
        "submitted_at": submitted_at,
        "stage": "applied",
        "stage_history": [
            {"stage": "applied", "at": submitted_at, "note": "via apply_queue"}
        ],
        "follow_up_due": None,
        "note": "Recorded by the drip-apply scheduler (apply_queue).",
    }
    applications.append(entry)
    _save_applications(applications)
    # outcome-min-v0 capture: append-only `applied` event. Guarded so
    # capture can never break a confirmed queue submission.
    try:
        import outcomes as _outcomes

        _outcomes.record_application_event(entry, source="apply_queue")
    except Exception:  # noqa: BLE001 - capture must never break recording
        import logging

        logging.getLogger("job-apply-mcp.apply_queue").exception(
            "outcome-min-v0 capture failed for %s", job_id
        )
    return entry


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_queue(
    apply_fn: ApplyFn,
    notify_fn: NotifyFn | None = None,
    max_items: int | None = None,
    path: Path | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    pace_fn: Callable[[], float] | None = None,
    record_fn: Callable[[str, dict], dict] | None = None,
) -> dict:
    """Process due queue items one at a time. Never raises.

    ``sleep_fn``/``pace_fn``/``record_fn`` are injectable seams for
    tests; production uses ``time.sleep``, ``random.uniform`` over
    ``compliance.APPLY_PACE_SECONDS``, and the lazy server recorder.

    Returns a JSON-serializable run report.
    """
    sleep_fn = sleep_fn or time.sleep
    pace_fn = pace_fn or (lambda: random.uniform(*compliance.APPLY_PACE_SECONDS))
    record_fn = record_fn or _default_record_fn

    queue = load_queue(path)
    now = datetime.now(timezone.utc)
    due = [i for i in queue["items"] if _is_due(i, now)]
    if max_items is not None:
        due = due[: max(0, int(max_items))]

    # Risk gate (additive): the whole automated run is adjudicated before
    # any application is attempted. Fail-closed on veto or check errors.
    try:
        from governance import risk_policy as _risk_policy
    except ImportError:
        _risk_policy = None

    report: dict[str, Any] = {
        "processed": 0,
        "applied": [],
        "failed": [],
        "stopped": None,  # "cap_reached" / "risk_blocked" stop the run
        "cap_reason": None,
        "remaining": 0,
    }

    if _risk_policy is not None:
        _auto_verdict = _risk_policy.adjudicate_automation(
            "queue-run", f"{len(due)} due application(s)"
        )
        if not _auto_verdict.get("allowed", False):
            report["stopped"] = "risk_blocked"
            report["cap_reason"] = _auto_verdict.get(
                "reason", "blocked by risk policy")
            return report

    for idx, item in enumerate(due):
        job_id = item["job_id"]
        allowed, reason = compliance.check_apply_allowed()
        if not allowed:
            report["stopped"] = "cap_reached"
            report["cap_reason"] = reason
            break
        if idx > 0:
            sleep_fn(pace_fn())  # human pace between applications
        try:
            result = apply_fn(job_id)
        except Exception as exc:  # apply_fn must not kill the run
            result = {"error": f"{type(exc).__name__}: {exc}"}
        report["processed"] += 1
        if isinstance(result, dict) and not result.get("error"):
            try:
                record_fn(job_id, result)
            except Exception as exc:
                # Recording failed: keep the item queued with a note so
                # the application is not silently lost.
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["last_error"] = f"record_failed: {exc}"
                report["failed"].append(
                    {"job_id": job_id, "error": item["last_error"]}
                )
                save_queue(queue, path)
                continue
            compliance.record_application()
            item["status"] = "applied"
            report["applied"].append(job_id)
        else:
            error = (
                result.get("error")
                if isinstance(result, dict)
                else f"unexpected result: {result!r}"
            )
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["last_error"] = str(error)
            report["failed"].append({"job_id": job_id, "error": str(error)})
        save_queue(queue, path)

    # Drop applied items from the queue — they now live in applications.json.
    queue["items"] = [i for i in queue["items"] if i.get("status") != "applied"]
    save_queue(queue, path)
    report["remaining"] = sum(
        1 for i in queue["items"] if i.get("status") == "queued"
    )

    if notify_fn is not None:
        try:
            notify_fn(
                "apply_queue run finished",
                f"processed={report['processed']} applied={len(report['applied'])} "
                f"failed={len(report['failed'])} remaining={report['remaining']} "
                f"stopped={report['stopped']}",
            )
        except Exception:
            pass  # notification failure must not fail the run report
    return report


# ---------------------------------------------------------------------------
# CLI (plugin: register_cli; do NOT edit cli.py — wire from outside)
# ---------------------------------------------------------------------------


def _cmd_queue_add(args: argparse.Namespace) -> int:
    try:
        item = add_to_queue(args.job_id, scheduled_for=args.scheduled_for)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(item, indent=2, ensure_ascii=False))
    return 0


def _cmd_queue_list(args: argparse.Namespace) -> int:
    items = list_queue()
    if args.json:
        print(json.dumps({"queue": items}, indent=2, ensure_ascii=False))
        return 0
    if not items:
        print("Queue is empty.")
        return 0
    for item in items:
        due = item.get("scheduled_for") or "now"
        print(
            f"- {item['job_id']}  status={item.get('status')}  "
            f"due={due}  attempts={item.get('attempts', 0)}"
            + (f"  last_error={item['last_error']}" if item.get("last_error") else "")
        )
    return 0


def _cmd_queue_run(args: argparse.Namespace) -> int:
    from server import apply_to_job  # noqa: E402  (lazy: heavy MCP import)

    profile: dict = {}
    if args.profile:
        try:
            profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: cannot read profile file: {exc}", file=sys.stderr)
            return 1

    def _apply(job_id: str) -> dict:
        return apply_to_job(
            job_id,
            args.resume or "",
            cover_letter=args.cover_letter or "",
            confirm=True,  # queue-run is explicit consent to record
            profile=profile,
        )

    notify_fn = None
    if args.notify:
        from notify import send as _send  # noqa: E402

        notify_fn = _send
    report = run_queue(
        _apply, notify_fn=notify_fn, max_items=args.max_items
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add ``queue-add`` / ``queue-list`` / ``queue-run`` to a CLI.

    Returns a {command: handler} mapping the caller merges into its
    own dispatch table (cli.py-style). Each parser also gets ``func``
    set so the owner can dispatch ``args.func(args)``.
    """
    p_add = subparsers.add_parser(
        "queue-add", help="Add a job to the drip-apply queue."
    )
    p_add.add_argument("job_id", help="Job id from the search output.")
    p_add.add_argument(
        "--scheduled-for",
        default=None,
        help="ISO-8601 timestamp; the item is not processed before then.",
    )
    p_add.set_defaults(func=_cmd_queue_add)

    p_list = subparsers.add_parser("queue-list", help="List queued applications.")
    p_list.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    p_list.set_defaults(func=_cmd_queue_list)

    p_run = subparsers.add_parser(
        "queue-run",
        help="Process due queue items (cron-friendly; prints a JSON report).",
    )
    p_run.add_argument(
        "--max-items", type=int, default=None, help="Max items to process."
    )
    p_run.add_argument("--resume", default="", help="Path to your resume file.")
    p_run.add_argument("--cover-letter", default="", help="Cover letter text.")
    p_run.add_argument(
        "--profile",
        default="",
        help="Path to a profile JSON file (defaults to the saved profile).",
    )
    p_run.add_argument(
        "--notify",
        action="store_true",
        help="Send a summary notification via notify.send when done.",
    )
    p_run.set_defaults(func=_cmd_queue_run)
    return {
        "queue-add": _cmd_queue_add,
        "queue-list": _cmd_queue_list,
        "queue-run": _cmd_queue_run,
    }


def register_tools(mcp: Any) -> None:
    """Register queue tools on an MCP server instance."""

    @mcp.tool()
    def queue_add(job_id: str) -> dict:
        """Add a job to the drip-apply queue (processed later by queue-run).

        Args:
            job_id: The job id returned by search_jobs.
        """
        return {"item": add_to_queue(job_id)}

    @mcp.tool()
    def queue_list() -> dict:
        """List the jobs currently waiting in the drip-apply queue."""
        items = list_queue()
        return {"queue": items, "count": len(items)}


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(
        prog="apply_queue.py",
        description="Drip-apply scheduler: queue jobs, process them at a human pace.",
    )
    _sub = _parser.add_subparsers(dest="command", required=True)
    register_cli(_sub)
    _args = _parser.parse_args()
    sys.exit(_args.func(_args))
