#!/usr/bin/env python3
"""Job watches for veto-mcp.

A *watch* is a saved search (query + location + board + filters). Each
``check_watch`` run diffs fresh search results against the ids seen last
time and reports only the new postings. Watches persist in
``watches.json`` (gitignored — it contains your search history).

The first check of a watch only records a baseline and reports no new
jobs; this is deliberate so you don't get spammed with everything that
already existed when the watch was created.

``search_fn`` is injected (``server.search_jobs`` in production) so this
module never imports ``server.py`` — no circular imports, and tests can
pass a fake.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("veto-mcp.watch")

SearchFn = Callable[..., list[dict[str, Any]]]


def load_watches(path: Path) -> dict[str, dict[str, Any]]:
    """Read the watches store (empty dict if missing/corrupt)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.debug("Could not read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_watches(path: Path, watches: dict[str, dict[str, Any]]) -> None:
    """Persist the watches store."""
    Path(path).write_text(
        json.dumps(watches, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def add_watch(
    watches: dict[str, dict[str, Any]],
    name: str,
    query: str,
    location: str = "",
    board: str = "all",
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create (or replace) a named watch. Not yet checked -> baseline pending."""
    name = str(name).strip()
    if not name:
        raise ValueError("Watch name must not be empty")
    if not str(query).strip():
        raise ValueError("Watch query must not be empty")
    watch = {
        "name": name,
        "query": query,
        "location": location or "",
        "board": board or "all",
        "filters": dict(filters or {}),
        "last_seen_ids": [],
        "initialized": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_checked_at": None,
    }
    watches[name] = watch
    return watch


def remove_watch(watches: dict[str, dict[str, Any]], name: str) -> bool:
    """Delete a watch. Returns True if it existed."""
    return watches.pop(str(name), None) is not None


def _search_kwargs(watch: dict[str, Any]) -> dict[str, Any]:
    """Map a watch's filters onto search_jobs kwargs."""
    filters = watch.get("filters") or {}
    kwargs: dict[str, Any] = {
        "query": watch["query"],
        "location": watch.get("location", ""),
        "board": watch.get("board", "all"),
    }
    if "limit" in filters:
        kwargs["limit"] = int(filters["limit"])
    if "remote_only" in filters:
        kwargs["remote_only"] = bool(filters["remote_only"])
    return kwargs


def check_watch(
    watch: dict[str, Any],
    search_fn: SearchFn,
    record_health: bool = False,
) -> dict[str, Any]:
    """Run one watch's search and diff against previously seen ids.

    First run: records the baseline and returns ``new_jobs == []``.
    Later runs: returns only postings whose ids were not seen before.
    The watch dict is updated in place (``last_seen_ids``,
    ``initialized``, ``last_checked_at``).

    With ``record_health=True``, the attempt is also recorded on the
    provider health board (see :mod:`provider_health`) under the watch's
    ``board`` name — the only coupling between watches and the board.
    """
    try:
        jobs = search_fn(**_search_kwargs(watch))
    except Exception as exc:  # a failing provider must not kill the sweep
        log.warning("Watch %r search failed: %s", watch.get("name"), exc)
        if record_health:
            _record_watch_health(watch, ok=False, note=str(exc)[:140])
        return {"new_jobs": [], "total_seen": 0, "error": str(exc)}
    if record_health:
        _record_watch_health(watch, ok=True)
    jobs = jobs or []
    seen = set(watch.get("last_seen_ids") or [])
    current_ids = [str(j.get("id")) for j in jobs if j.get("id")]

    if not watch.get("initialized"):
        # Baseline run: remember everything, alert on nothing.
        new_jobs: list[dict[str, Any]] = []
    else:
        new_jobs = [j for j in jobs if str(j.get("id")) not in seen]

    watch["last_seen_ids"] = current_ids
    watch["initialized"] = True
    watch["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    log.info(
        "Watch %r: %d total, %d new",
        watch.get("name"),
        len(current_ids),
        len(new_jobs),
    )
    return {"new_jobs": new_jobs, "total_seen": len(current_ids)}


def _record_watch_health(watch: dict[str, Any], ok: bool, note: str = "") -> None:
    """Best-effort health recording for a watch run. Never raises."""
    try:
        import provider_health

        board_name = str(watch.get("board") or "all")
        provider_health.record_fetch(board_name, ok, note=note)
    except Exception:  # noqa: BLE001 - health must never break a watch run
        log.debug("health recording failed for watch %r", watch.get("name"))


def check_all(
    watches: dict[str, dict[str, Any]],
    search_fn: SearchFn,
    record_health: bool = False,
) -> dict[str, dict[str, Any]]:
    """Check every watch; returns ``{name: {new_jobs, total_seen}}``."""
    return {
        name: check_watch(watch, search_fn, record_health=record_health)
        for name, watch in watches.items()
    }


if __name__ == "__main__":
    # Cron-friendly entry point: `python watch.py` checks all saved
    # watches, prints JSON, and exits 0. Schedule it, e.g.:
    #   0 9 * * * cd ~/workspace/veto && .venv/bin/python watch.py
    base = Path(__file__).resolve().parent
    store = base / "watches.json"
    all_watches = load_watches(store)
    try:
        from server import search_jobs as _search_jobs
    except ImportError as exc:  # pragma: no cover - defensive
        print(json.dumps({"error": f"could not import server: {exc}"}))
        sys.exit(1)
    results = check_all(all_watches, _search_jobs)
    save_watches(store, all_watches)
    summary = {
        name: {
            "new_jobs": len(r.get("new_jobs", [])),
            "total_seen": r.get("total_seen", 0),
            **({"error": r["error"]} if r.get("error") else {}),
            "jobs": r.get("new_jobs", []),
        }
        for name, r in results.items()
    }
    print(json.dumps(summary, indent=2))
    sys.exit(0)
