#!/usr/bin/env python3
"""Near-miss review queue for Veto (Initiative 02).

Postings that scored just under the fit veto bar (default 50–60) land
here for human review: the veto is heuristic, and a near-miss can be
worth a second look (hidden skill match, mis-parsed seniority, stale
posting data). The queue is advisory — resolving an item never changes a
score or an application; it only records the human's verdict.

* :func:`scan` — score candidate postings, queue near-misses.
* :func:`list_items` — open items, highest score first.
* :func:`resolve` — record the human verdict (``worth_a_look`` /
  ``correct_veto``) with an optional note.

Stdlib only. Queue persists in ``near_miss.json``.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.near_miss")

BASE_DIR = Path(__file__).resolve().parent
QUEUE_FILE = BASE_DIR / "near_miss.json"

#: Score band that counts as a near-miss (vetoed, but close).
NEAR_MISS_LOW = 50
NEAR_MISS_HIGH = 60

_STATUSES = ("open", "worth_a_look", "correct_veto")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_items(path: str | Path | None = None) -> list[dict[str, Any]]:
    try:
        raw = json.loads(Path(path or QUEUE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.debug("Could not read near-miss queue: %s", exc)
        return []
    return raw if isinstance(raw, list) else []


def save_items(items: list[dict[str, Any]],
               path: str | Path | None = None) -> None:
    Path(path or QUEUE_FILE).write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def scan(
    jobs: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
    preferences: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Score postings; queue vetoed near-misses (advisory, read-only
    w.r.t. scores and applications)."""
    import match

    items = load_items(path)
    seen = {(i.get("job_id"), i.get("status")) for i in items}
    queued: list[dict[str, Any]] = []
    for job in jobs or []:
        scored = match.score_job(job, profile or {}, preferences or {})
        score = int(scored.get("score", 0))
        if not (NEAR_MISS_LOW <= score < NEAR_MISS_HIGH):
            continue
        job_id = job.get("job_id") or job.get("id")
        if (job_id, "open") in seen:
            continue
        item = {
            "item_id": uuid.uuid4().hex[:12],
            "job_id": job_id,
            "title": job.get("title"),
            "company": job.get("company"),
            "score": score,
            "veto_reason": scored.get("veto_reason"),
            "missing_skills": list(scored.get("missing", [])),
            "status": "open",
            "created_at": _utcnow_iso(),
            "resolved_at": None,
            "verdict": None,
        }
        items.append(item)
        queued.append(item)
        seen.add((job_id, "open"))
    save_items(items, path)
    return {
        "scanned": len(jobs or []),
        "queued": len(queued),
        "open": sum(1 for i in items if i.get("status") == "open"),
        "items": queued,
    }


def list_items(
    status: str | None = None, path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Queue items, highest score first; optional status filter."""
    items = load_items(path)
    if status is not None:
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}")
        items = [i for i in items if i.get("status") == status]
    return sorted(items, key=lambda i: int(i.get("score") or 0), reverse=True)


def resolve(
    item_id: str, verdict: str, note: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Record the human's verdict on a near-miss.

    ``verdict`` is ``worth_a_look`` (human will pursue it) or
    ``correct_veto`` (the veto stands). Advisory only: resolving changes
    no score and no application.
    """
    if verdict not in ("worth_a_look", "correct_veto"):
        raise ValueError("verdict must be 'worth_a_look' or 'correct_veto'")
    items = load_items(path)
    for item in items:
        if item.get("item_id") == item_id:
            if item.get("status") != "open":
                raise ValueError(f"item {item_id} already resolved")
            item["status"] = verdict
            item["verdict"] = verdict
            item["resolved_at"] = _utcnow_iso()
            item["note"] = str(note)[:280]
            save_items(items, path)
            return {"item_id": item_id, "verdict": verdict}
    raise KeyError(f"No near-miss item {item_id!r}")


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register near-miss tools on the MCP server."""

    @mcp.tool()
    def near_miss_scan(jobs: list) -> dict:
        """Score postings and queue near-misses (50-60, vetoed but
        close) for human review. Advisory; changes nothing."""
        return scan(jobs)

    @mcp.tool()
    def near_miss_list(status: str | None = None) -> dict:
        """List near-miss queue items, highest score first."""
        return {"items": list_items(status=status)}

    @mcp.tool()
    def near_miss_resolve(item_id: str, verdict: str,
                          note: str = "") -> dict:
        """Record the human verdict on a near-miss: 'worth_a_look' or
        'correct_veto'."""
        return resolve(item_id, verdict, note)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_items(items: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"items": items}, indent=2, ensure_ascii=False))
        return
    if not items:
        print("No near-miss items.")
        return
    for i in items:
        print(f"[{i['item_id']}] {i.get('title')} @ {i.get('company')} "
              f"— score {i.get('score')} ({i.get('status')})")
        if i.get("veto_reason"):
            print(f"  veto: {i['veto_reason']}")


def _cli_scan(args: Any) -> int:
    try:
        jobs = json.loads(Path(args.jobs_file).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: cannot read jobs file: {exc}")
        return 2
    result = scan(jobs if isinstance(jobs, list) else [])
    _print_items(result["items"], args.json)
    return 0


def _cli_list(args: Any) -> int:
    _print_items(list_items(status=args.status), args.json)
    return 0


def _cli_resolve(args: Any) -> int:
    try:
        result = resolve(args.item_id, args.verdict, args.note or "")
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(result, indent=2))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "near-miss", help="Review queue for vetoed-but-close postings."
    )
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    sub = p.add_subparsers(dest="nm_cmd", required=True)

    ps = sub.add_parser("scan", help="Score a jobs file; queue near-misses.")
    ps.add_argument("jobs_file")
    ps.set_defaults(func=_cli_scan)

    pl = sub.add_parser("list", help="List queue items.")
    pl.add_argument("--status", default=None, choices=list(_STATUSES))
    pl.set_defaults(func=_cli_list)

    pr = sub.add_parser("resolve", help="Record a verdict on an item.")
    pr.add_argument("item_id")
    pr.add_argument("verdict", choices=["worth_a_look", "correct_veto"])
    pr.add_argument("--note", default="")
    pr.set_defaults(func=_cli_resolve)

    return {"near-miss": lambda args: args.func(args)}
