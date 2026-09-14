#!/usr/bin/env python3
"""Extension manifest: declared permissions, data access, network destinations,
actions, and confirmation requirements.

An extension is a directory containing ``manifest.yaml`` (or
``manifest.json``) plus its code. The manifest is the *only* authority the
host uses to decide what an extension may do — runtime capabilities are
frozen from the validated manifest at load time; later mutation of the
manifest file has no effect on a loaded extension.

Contract-first note (for Initiative 09 / Initiative 10 teams): the JSON
schema lives in ``MANIFEST_SCHEMA.json`` next to this module and is the
published interface contract. Provider contracts (09) plug in as declared
``network.destinations`` entries; packaging (10) consumes
``packaging.*`` fields and the signing envelope from ``signing/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
    _HAVE_YAML = True
except Exception:  # pragma: no cover - PyYAML may be absent
    _HAVE_YAML = False

MANIFEST_VERSION = 1
MANIFEST_FILENAMES = ("manifest.yaml", "manifest.yml", "manifest.json")

#: Closed vocabulary of data scopes an extension may request.
DATA_SCOPES = (
    "jobs:read",
    "applications:read",
    "profile:read",
    "outcomes:read",
    "lifecycle:read",
    "watches:read",
)

#: Action kinds. Only ``confirm-required`` may perform a consequential
#: action, and even then only through the host's confirmation broker —
#: the extension itself can never mint a confirmation.
ACTION_KINDS = ("read", "draft", "preview", "notify", "confirm-required")

#: PII handling modes. Default is redact; ``read`` requires a registry
#: UX-gate justification (registry/registry.py enforces this).
PII_MODES = ("redact", "read")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


class ManifestError(ValueError):
    """Raised when a manifest is missing, malformed, or declares
    capabilities outside the closed vocabulary."""


def load_manifest(ext_dir: str | Path) -> dict[str, Any]:
    """Load and validate the manifest for an extension directory.

    Returns the normalized manifest dict with capabilities frozen
    (deep-copied). Raises ManifestError on any problem — fail closed.
    """
    ext_dir = Path(ext_dir)
    path = None
    for name in MANIFEST_FILENAMES:
        candidate = ext_dir / name
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        raise ManifestError(
            f"no manifest found in {ext_dir} "
            f"(looked for {', '.join(MANIFEST_FILENAMES)})"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if path.suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
    else:
        if not _HAVE_YAML:
            raise ManifestError(
                f"manifest {path} is YAML but PyYAML is not installed; "
                "use manifest.json instead"
            )
        try:
            data = yaml.safe_load(raw)
        except Exception as exc:
            raise ManifestError(f"manifest {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"manifest {path} must be a mapping")
    return validate_manifest(data, source=str(path))


def validate_manifest(data: dict[str, Any], source: str = "<manifest>") -> dict[str, Any]:
    """Validate a manifest mapping against the closed capability vocabulary.

    Returns a normalized, deep-copied manifest. Raises ManifestError —
    unknown capabilities are rejected, never silently dropped.
    """
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: manifest must be a mapping")

    def need(key: str, typ: type) -> Any:
        if key not in data:
            raise ManifestError(f"{source}: missing required field {key!r}")
        val = data[key]
        if not isinstance(val, typ):
            raise ManifestError(
                f"{source}: field {key!r} must be {typ.__name__}, "
                f"got {type(val).__name__}"
            )
        return val

    version = need("manifest_version", int)
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"{source}: unsupported manifest_version {version} "
            f"(this host supports {MANIFEST_VERSION})"
        )
    ext_id = need("id", str)
    if not _ID_RE.match(ext_id):
        raise ManifestError(
            f"{source}: id {ext_id!r} must match {_ID_RE.pattern}"
        )
    semver = need("version", str)
    if not _SEMVER_RE.match(semver):
        raise ManifestError(
            f"{source}: version {semver!r} must be semver (x.y.z)"
        )
    need("name", str)
    need("description", str)

    perms = need("permissions", dict)

    # -- data scopes (closed vocabulary) ----------------------------------
    data_scopes = perms.get("data", [])
    if not isinstance(data_scopes, list) or not all(
        isinstance(s, str) for s in data_scopes
    ):
        raise ManifestError(f"{source}: permissions.data must be a list of strings")
    unknown = [s for s in data_scopes if s not in DATA_SCOPES]
    if unknown:
        raise ManifestError(
            f"{source}: unknown data scopes {unknown}; "
            f"allowed: {list(DATA_SCOPES)}"
        )

    # -- network destinations (explicit allowlist) -------------------------
    network = perms.get("network", {})
    if not isinstance(network, dict):
        raise ManifestError(f"{source}: permissions.network must be a mapping")
    destinations = network.get("destinations", [])
    if not isinstance(destinations, list) or not all(
        isinstance(d, str) for d in destinations
    ):
        raise ManifestError(
            f"{source}: permissions.network.destinations must be a list of strings"
        )
    for dest in destinations:
        if "://" in dest or "/" in dest or " " in dest or not dest:
            raise ManifestError(
                f"{source}: network destination {dest!r} must be a bare "
                "hostname (no scheme, path, or whitespace)"
            )

    # -- actions -----------------------------------------------------------
    raw_actions = perms.get("actions", [])
    if not isinstance(raw_actions, list):
        raise ManifestError(f"{source}: permissions.actions must be a list")
    actions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, act in enumerate(raw_actions):
        where = f"{source}: permissions.actions[{i}]"
        if not isinstance(act, dict):
            raise ManifestError(f"{where} must be a mapping")
        aid = act.get("id")
        if not isinstance(aid, str) or not aid:
            raise ManifestError(f"{where} missing string 'id'")
        if aid in seen_ids:
            raise ManifestError(f"{where}: duplicate action id {aid!r}")
        seen_ids.add(aid)
        kind = act.get("kind", "read")
        if kind not in ACTION_KINDS:
            raise ManifestError(
                f"{where}: unknown kind {kind!r}; allowed {list(ACTION_KINDS)}"
            )
        confirm_required = bool(act.get("confirm_required", False))
        # confirm_required=True is only meaningful for confirm-required
        # actions; normalize everything else to False so a manifest cannot
        # *look* stricter than it is.
        if kind != "confirm-required":
            confirm_required = False
        actions.append({
            "id": aid,
            "kind": kind,
            "confirm_required": confirm_required,
            "description": str(act.get("description", "")),
        })

    # -- PII mode ----------------------------------------------------------
    pii = perms.get("pii", "redact")
    if pii not in PII_MODES:
        raise ManifestError(
            f"{source}: permissions.pii must be one of {list(PII_MODES)}"
        )

    # -- sandbox tuning ----------------------------------------------------
    sandbox = perms.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ManifestError(f"{source}: permissions.sandbox must be a mapping")
    fs_roots = sandbox.get("fs_roots", [])
    if not isinstance(fs_roots, list) or not all(
        isinstance(r, str) for r in fs_roots
    ):
        raise ManifestError(
            f"{source}: permissions.sandbox.fs_roots must be a list of strings"
        )
    for root in fs_roots:
        if Path(root).is_absolute() or ".." in Path(root).parts:
            raise ManifestError(
                f"{source}: fs_roots entries must be relative paths without "
                f"'..' (got {root!r})"
            )
    rate = sandbox.get("rate_limit", {})
    if not isinstance(rate, dict):
        raise ManifestError(
            f"{source}: permissions.sandbox.rate_limit must be a mapping"
        )
    calls_per_minute = int(rate.get("calls_per_minute", 60))
    if calls_per_minute <= 0 or calls_per_minute > 6000:
        raise ManifestError(
            f"{source}: calls_per_minute must be in 1..6000"
        )

    normalized = {
        "manifest_version": MANIFEST_VERSION,
        "id": ext_id,
        "version": semver,
        "name": data["name"],
        "description": data["description"],
        "author": str(data.get("author", "")),
        "permissions": {
            "data": sorted(set(data_scopes)),
            "network": {"destinations": sorted(set(destinations))},
            "actions": actions,
            "pii": pii,
            "sandbox": {
                "fs_roots": sorted(set(fs_roots)),
                "rate_limit": {"calls_per_minute": calls_per_minute},
            },
        },
        "packaging": dict(data.get("packaging", {})),
    }
    # Deep-copy through JSON so the frozen manifest cannot alias caller data.
    return json.loads(json.dumps(normalized))


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Human/comprehension-check friendly summary of declared capabilities."""
    perms = manifest["permissions"]
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "name": manifest["name"],
        "data_scopes": perms["data"],
        "network_destinations": perms["network"]["destinations"],
        "actions": [
            {"id": a["id"], "kind": a["kind"],
             "confirm_required": a["confirm_required"]}
            for a in perms["actions"]
        ],
        "pii": perms["pii"],
        "can_submit_anything": any(
            a["kind"] == "confirm-required" for a in perms["actions"]
        ),
        "note": (
            "confirm-required actions ALWAYS re-prompt the user at execution "
            "time through the host broker. The extension can never self-confirm."
        ),
    }
