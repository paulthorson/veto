#!/usr/bin/env python3
"""Progressive web app bundle for Veto (Epic 5).

The installable phone shell lives in ``initiatives/i10/pwa/``:

* ``manifest.webmanifest`` — installable metadata (name, icons, theme).
* ``service-worker.js`` — offline-safe read views (GET /api/: network-first
  with TTL-bounded cache fallback; app shell: cache-first) and mutation
  handoff: on network failure a non-navigation mutation gets the synthetic
  202 {queued:true} (namespaced with the ``x-veto-offline-queue`` header)
  WITHOUT the worker enqueueing — the page (deferred-queue.js) is the
  single owner of the deferred-action queue (Initiative 10, WS5 review
  B1). Navigation-mode form POSTs get offline.html instead: a page
  mid-navigation cannot run submit() to enqueue, so a 202 would claim a
  queueing that never happens.
* ``deferred-queue.js`` — IndexedDB-backed FIFO queue, single enqueue choke
  point with idempotency-key dedup (unique index, cross-tab safe), replay
  with Idempotency-Key headers, user confirmation before replay, bounded
  retries with backoff; 4xx (non-429) go to a dead-letter list.
* ``offline.html`` — offline navigation fallback.
* ``sw-version.js`` — GENERATED bundle version stamp (content hash); the
  service worker derives CACHE_NAME from it so a deploy can never silently
  serve a stale shell. Regenerate with ``python -m initiatives.i10.pwa
  stamp`` after any change under ``pwa/``.

``validate_bundle`` gates shippability: bundle-internal coherence checks
plus a ``webui-wiring`` check that fails HONESTLY while the webui.py
serving/registration wiring (integration_notes.md §2) has not landed —
the bundle is not installable as-shipped without it, so CI must be red,
not green.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PWA_DIR = Path(__file__).resolve().parent / "pwa"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEBUI_PATH = REPO_ROOT / "webui.py"

REQUIRED_MANIFEST_KEYS = {
    "name",
    "short_name",
    "start_url",
    "display",
    "background_color",
    "theme_color",
    "icons",
}

REQUIRED_QUEUE_API = (
    "submit",
    "enqueueRequest",
    "replay",
    "confirmReplay",
    "pending",
    "deadLetter",
    "retryDead",
    "discardDead",
)

# Markers the webui.py wiring must contain for the bundle to be
# installable as-shipped (integration_notes.md §2). The wiring sweep owns
# landing them; until then validate_bundle() fails honestly.
#
# Icon-serving markers are required: the install prompt needs the 192/512
# icons, so the gate must not report shippable while installability can't
# fire (F5 review: the wiring gate previously ignored icons entirely).
WEBUI_WIRING_MARKERS = (
    "/manifest.webmanifest",
    "/service-worker.js",
    "/deferred-queue.js",
    "/offline.html",
    "app.css",
    "app.js",
    "serviceWorker",
    "icons/",
    "icon-192",
    "icon-512",
)

SW_VERSION_FILE = "sw-version.js"
STAMP_VERSION_LEN = 16


@dataclass
class PwaCheck:
    name: str
    ok: bool
    detail: str


def _cache_url_list(sw: str, const_name: str) -> set[str] | None:
    """Extract the quoted URLs from ``const <const_name> = [...]`` in the worker.

    Returns None when the constant is not declared — the caller fails the
    sw-cache-list check with a clear message instead of raising IndexError
    (F6: an unguarded ``sw.split(const)[1]`` crashes validate_bundle() on a
    worker missing those constants).
    """
    match = re.search(const_name + r"\s*=\s*\[(.*?)\];", sw, flags=re.DOTALL)
    if not match:
        return None
    return set(re.findall(r'"/([^"]+)"', match.group(1)))


def bundle_fingerprint(pwa_dir: Path = PWA_DIR) -> str:
    """Content hash of the bundle (excluding the generated version stamp).

    Any byte change under pwa/ changes the fingerprint, which becomes the
    service worker's CACHE_NAME — old shells are purged on activate.
    """
    pwa_dir = Path(pwa_dir)
    digest = hashlib.sha256()
    files = sorted(
        (p for p in pwa_dir.rglob("*") if p.is_file() and p.name != SW_VERSION_FILE),
        key=lambda p: p.relative_to(pwa_dir).as_posix(),
    )
    for path in files:
        rel = path.relative_to(pwa_dir).as_posix()
        digest.update(rel.encode("utf-8") + b"\x00")
        digest.update(path.read_bytes() + b"\x00")
    return digest.hexdigest()


def read_stamp(pwa_dir: Path = PWA_DIR) -> str | None:
    """Return the stamped version from sw-version.js, or None if absent."""
    stamp = Path(pwa_dir) / SW_VERSION_FILE
    if not stamp.exists():
        return None
    match = re.search(r'VETO_SW_VERSION\s*=\s*"([0-9a-f]+)"', stamp.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def write_version_stamp(pwa_dir: Path = PWA_DIR) -> str:
    """Stamp sw-version.js with the current bundle fingerprint. Returns it."""
    pwa_dir = Path(pwa_dir)
    fingerprint = bundle_fingerprint(pwa_dir)
    version = fingerprint[:STAMP_VERSION_LEN]
    (pwa_dir / SW_VERSION_FILE).write_text(
        "/* GENERATED by `python -m initiatives.i10.pwa stamp` — do not edit.\n"
        "   Bundle content hash; the service worker derives CACHE_NAME from it.\n"
        "   Re-stamp after ANY change under pwa/ or CI fails the sw-version-fresh check. */\n"
        f'self.VETO_SW_VERSION="{version}";\n',
        encoding="utf-8",
    )
    return version


def _strip_js_comments(src: str) -> str:
    """Remove // and /* */ comments so static checks inspect code, not prose."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def check_webui_wiring(webui_path: Path = WEBUI_PATH) -> PwaCheck:
    """Honest shippability gate: the bundle cannot install as-shipped until
    webui.py serves it and registers the worker (integration_notes.md §2).

    Fails with the exact missing markers while the wiring sweep has not
    landed — CI red, not green.
    """
    webui_path = Path(webui_path)
    if not webui_path.exists():
        return PwaCheck("webui-wiring", False, f"{webui_path} not found — bundle not served")
    text = webui_path.read_text(encoding="utf-8")
    missing = [m for m in WEBUI_WIRING_MARKERS if m not in text]
    if missing:
        return PwaCheck(
            "webui-wiring",
            False,
            "webui.py wiring NOT landed — bundle is not installable as-shipped; "
            f"missing markers: {missing}. See integration_notes.md §2 and §6 "
            "(bundle + wiring must land atomically).",
        )
    return PwaCheck("webui-wiring", True, "all wiring markers present in webui.py")


def validate_bundle(
    pwa_dir: Path = PWA_DIR,
    check_wiring: bool = True,
    webui_path: Path | None = None,
) -> list[PwaCheck]:
    """Validate the PWA bundle. Every check must pass for the PWA to be
    considered shippable."""
    pwa_dir = Path(pwa_dir)
    checks: list[PwaCheck] = []

    manifest_path = pwa_dir / "manifest.webmanifest"
    if not manifest_path.exists():
        return [PwaCheck("manifest", False, "manifest.webmanifest missing")]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [PwaCheck("manifest", False, f"invalid JSON: {exc}")]

    missing = REQUIRED_MANIFEST_KEYS - set(manifest)
    checks.append(
        PwaCheck(
            "manifest-keys",
            not missing,
            "all required keys present" if not missing else f"missing: {sorted(missing)}",
        )
    )
    icons = manifest.get("icons", [])
    icons_ok = bool(icons) and all("src" in i and "sizes" in i for i in icons)
    maskable = any("maskable" in str(i.get("purpose", "")) for i in icons)
    checks.append(
        PwaCheck(
            "manifest-icons",
            icons_ok and maskable,
            f"{len(icons)} icon(s), maskable={'yes' if maskable else 'NO'}"
            if icons_ok
            else "icons missing or malformed",
        )
    )
    if not manifest.get("id"):
        checks.append(PwaCheck("manifest-id", False, "manifest 'id' missing (required for stable identity)"))
    else:
        checks.append(PwaCheck("manifest-id", True, f"id={manifest['id']!r}"))
    if manifest.get("display") not in ("standalone", "fullscreen", "minimal-ui"):
        checks.append(PwaCheck("manifest-display", False, f"display={manifest.get('display')!r} is not installable"))
    else:
        checks.append(PwaCheck("manifest-display", True, f"display={manifest['display']}"))

    sw_path = pwa_dir / "service-worker.js"
    if not sw_path.exists():
        checks.append(PwaCheck("service-worker", False, "service-worker.js missing"))
    else:
        sw = sw_path.read_text(encoding="utf-8")
        has_cache = "caches.open" in sw and "CACHE_NAME" in sw
        has_offline = "offline.html" in sw
        has_version_purge = "caches.delete" in sw
        has_tol_install = "allSettled" in sw and "CORE_ASSETS" in sw
        checks.append(
            PwaCheck(
                "service-worker",
                all((has_cache, has_offline, has_version_purge, has_tol_install)),
                f"cache={has_cache} offline-fallback={has_offline} "
                f"version-purge={has_version_purge} tolerant-install={has_tol_install}",
            )
        )
        # B1 single-owner invariant: the worker must NEVER enqueue or
        # fan-out queue messages — the page owns the queue. (Comments are
        # stripped first so the historical note in the header can't trip it.)
        code = _strip_js_comments(sw)
        fanout = "veto:queue-mutation" in code or re.search(r"\.postMessage\s*\(", code)
        checks.append(
            PwaCheck(
                "sw-single-owner",
                not fanout,
                "worker never enqueues / postMessages (page owns the queue)"
                if not fanout
                else "worker still fans out queue messages — B1 duplication risk",
            )
        )
        # F4/F10: only 2xx cached; read-API branch is GET-only.
        checks.append(
            PwaCheck(
                "sw-cache-hygiene",
                "response.ok" in sw and 'request.method === "GET"' in sw,
                "caches only ok responses; read-API branch GET-only",
            )
        )
        # The SW cache list must reference files the bundle ships or that
        # webui.py serves (app shell + icons). Actual serving is enforced
        # by the webui-wiring check below — a bundle whose install would
        # fail as-shipped cannot pass validate_bundle().
        # F6: the constants are parsed defensively — a worker missing one
        # fails this check cleanly with a clear message instead of
        # crashing validate_bundle() with IndexError.
        core_assets = _cache_url_list(sw, "CORE_ASSETS")
        optional_assets = _cache_url_list(sw, "OPTIONAL_ASSETS")
        missing_consts = [
            name
            for name, listed in (("CORE_ASSETS", core_assets), ("OPTIONAL_ASSETS", optional_assets))
            if listed is None
        ]
        if missing_consts:
            checks.append(
                PwaCheck(
                    "sw-cache-list",
                    False,
                    "service worker missing cache-list constant(s): "
                    f"{missing_consts} — the worker must declare "
                    "CORE_ASSETS/OPTIONAL_ASSETS for its install",
                )
            )
        else:
            cached = core_assets | optional_assets
            bundle_files = {p.name for p in pwa_dir.iterdir() if p.is_file()}
            webroot_served = {"app.css", "app.js"}
            missing_files = {
                c
                for c in cached
                if c.split("/")[-1] not in bundle_files
                and c not in webroot_served
                and not c.startswith("icons/")
                and c != ""
            }
            checks.append(
                PwaCheck(
                    "sw-cache-list",
                    not missing_files,
                    "all cached files accounted for"
                    if not missing_files
                    else f"missing: {sorted(missing_files)}",
                )
            )
        # F5/F6: versioned cache name tied to the bundle content stamp.
        checks.append(
            PwaCheck(
                "sw-versioned-cache",
                "sw-version.js" in sw and "VETO_SW_VERSION" in sw,
                "CACHE_NAME derived from stamped bundle content hash",
            )
        )

    # The stamp must be fresh: a stale sw-version.js means the deployed
    # shell's CACHE_NAME no longer matches the bundle content.
    expected = bundle_fingerprint(pwa_dir)[:STAMP_VERSION_LEN]
    stamped = read_stamp(pwa_dir)
    checks.append(
        PwaCheck(
            "sw-version-fresh",
            stamped == expected,
            f"stamp={stamped} matches bundle"
            if stamped == expected
            else f"STALE/MISSING stamp (have {stamped!r}, want {expected!r}) — "
            "run `python -m initiatives.i10.pwa stamp` after any pwa/ change",
        )
    )

    q_path = pwa_dir / "deferred-queue.js"
    if not q_path.exists():
        checks.append(PwaCheck("deferred-queue", False, "deferred-queue.js missing"))
    else:
        q = q_path.read_text(encoding="utf-8")
        q_code = _strip_js_comments(q)
        absent = [api for api in REQUIRED_QUEUE_API if api not in q_code]
        has_idb = "indexedDB" in q_code
        has_key = "idempotencyKey" in q_code and "Idempotency-Key" in q_code
        has_dedup = "by-key" in q_code and "unique" in q_code
        has_confirm = "confirmReplay" in q_code
        no_fanout = "veto:queue-mutation" not in q_code
        checks.append(
            PwaCheck(
                "deferred-queue",
                not absent and has_idb and has_key and has_dedup and has_confirm and no_fanout,
                f"indexeddb={has_idb} idempotency-key={has_key} dedup={has_dedup} "
                f"confirm={has_confirm} no-fanout={no_fanout}"
                + ("" if not absent else f" missing api: {absent}"),
            )
        )

    off_path = pwa_dir / "offline.html"
    checks.append(
        PwaCheck(
            "offline-fallback",
            off_path.exists(),
            "offline.html present" if off_path.exists() else "offline.html missing",
        )
    )

    if check_wiring:
        checks.append(check_webui_wiring(webui_path or WEBUI_PATH))
    return checks


def bundle_ok(checks: list[PwaCheck]) -> bool:
    return all(c.ok for c in checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pwa", description="Veto PWA bundle tooling")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stamp", help="Write sw-version.js from the current bundle content hash")
    sub.add_parser("validate", help="Run validate_bundle() and report")
    args = parser.parse_args(argv)
    if args.cmd == "stamp":
        version = write_version_stamp()
        print(f"stamped sw-version.js: VETO_SW_VERSION={version!r}")
        return 0
    results = validate_bundle()
    for c in results:
        print(f"  [{'✓' if c.ok else '✗'}] {c.name}: {c.detail}")
    return 0 if bundle_ok(results) else 1


if __name__ == "__main__":
    sys.exit(main())
