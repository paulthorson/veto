#!/usr/bin/env python3
"""Nightly limen engine updater.

Keeps the limen engine (github.com/overment/limen) at the newest version:

  - If the limen checkout is missing: clones it, runs `npm install` and
    `npm link` (first-time install).
  - If present: `git fetch`es and fast-forward pulls when origin is ahead;
    re-runs `npm install` + `npm link` only when package.json changed.

Location: ~/limen by default, LIMEN_DIR env var overrides.
Upstream: LIMEN_REPO env var (default https://github.com/overment/limen.git).

Designed for cron: never raises, always exits 0, prints a single JSON
report line to stdout. A non-"current" status means something changed or
failed and is worth surfacing.

Report statuses:
  installed  - first-time install completed
  updated    - pulled new commits (old_rev -> new_rev)
  current    - already up to date
  failed     - something went wrong (see error)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

LIMEN_DIR = Path(os.environ.get("LIMEN_DIR", Path.home() / "limen")).expanduser()
LIMEN_REPO = os.environ.get("LIMEN_REPO", "https://github.com/overment/limen.git")
NPM_TIMEOUT = 600


def _run(cmd: list[str], cwd: Path | None = None,
         timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()[-2000:]
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"


def _rev(directory: Path, ref: str = "HEAD") -> str | None:
    rc, out = _run(["git", "rev-parse", "--short", ref], cwd=directory)
    return out.strip().split("\n")[0] if rc == 0 else None


def _npm_install_and_link(directory: Path) -> tuple[bool, str]:
    if shutil.which("npm") is None:
        return False, "npm not on PATH; skipping install/link"
    rc, out = _run(["npm", "install", "--no-audit", "--no-fund"],
                   cwd=directory, timeout=NPM_TIMEOUT)
    if rc != 0:
        return False, f"npm install failed: {out[-500:]}"
    rc, out = _run(["npm", "link"], cwd=directory, timeout=NPM_TIMEOUT)
    if rc != 0:
        return False, f"npm link failed: {out[-500:]}"
    return True, "npm install + npm link ok"


def main() -> int:
    report: dict = {
        "status": "failed", "dir": str(LIMEN_DIR), "repo": LIMEN_REPO,
        "old_rev": None, "new_rev": None, "error": None, "detail": "",
    }
    if shutil.which("git") is None:
        report["error"] = "git not on PATH"
        print(json.dumps(report))
        return 0

    # -- first-time install ------------------------------------------------
    if not (LIMEN_DIR / ".git").is_dir():
        rc, out = _run(
            ["git", "clone", LIMEN_REPO, str(LIMEN_DIR)], timeout=300
        )
        if rc != 0:
            report["error"] = f"git clone failed: {out[-500:]}"
            print(json.dumps(report))
            return 0
        ok, detail = _npm_install_and_link(LIMEN_DIR)
        report["status"] = "installed" if ok else "failed"
        report["new_rev"] = _rev(LIMEN_DIR)
        report["detail"] = detail
        if not ok:
            report["error"] = detail
        print(json.dumps(report))
        return 0

    # -- update check -------------------------------------------------------
    old_rev = _rev(LIMEN_DIR)
    report["old_rev"] = old_rev
    rc, out = _run(["git", "fetch", "origin"], cwd=LIMEN_DIR, timeout=180)
    if rc != 0:
        report["error"] = f"git fetch failed: {out[-500:]}"
        print(json.dumps(report))
        return 0

    rc, out = _run(["git", "status", "-sb", "--porcelain=v1", "-b"],
                   cwd=LIMEN_DIR)
    behind = "[behind" in out if rc == 0 else False
    # Fallback: compare refs directly.
    if not behind:
        local = _rev(LIMEN_DIR, "HEAD")
        remote = _rev(LIMEN_DIR, "origin/HEAD") or _rev(LIMEN_DIR, "origin/main")
        behind = bool(local and remote and local != remote)

    if not behind:
        report["status"] = "current"
        report["new_rev"] = old_rev
        report["detail"] = f"already at newest ({old_rev})"
        print(json.dumps(report))
        return 0

    # -- pull ---------------------------------------------------------------
    rc, out = _run(["git", "pull", "--ff-only"], cwd=LIMEN_DIR, timeout=300)
    if rc != 0:
        report["error"] = (
            "git pull --ff-only failed (diverged? manual merge needed): "
            + out[-500:]
        )
        print(json.dumps(report))
        return 0
    new_rev = _rev(LIMEN_DIR)
    report["new_rev"] = new_rev

    # Reinstall only if dependencies changed.
    rc, out = _run(["git", "diff", "--name-only",
                    f"{old_rev}..{new_rev}", "--", "package.json",
                    "package-lock.json"], cwd=LIMEN_DIR)
    if rc == 0 and out.strip():
        ok, detail = _npm_install_and_link(LIMEN_DIR)
        report["detail"] = (
            f"pulled {old_rev}..{new_rev}; {detail}"
        )
        if not ok:
            report["status"] = "failed"
            report["error"] = detail
            print(json.dumps(report))
            return 0
    else:
        report["detail"] = f"pulled {old_rev}..{new_rev}; no dep changes"
    report["status"] = "updated"
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
