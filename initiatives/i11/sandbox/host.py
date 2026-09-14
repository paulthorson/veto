#!/usr/bin/env python3
"""Extension host: capability broker, confirmation gate, and runtime.

This is the enforcement point for the hard rule: **an extension can prove
what it accesses and CANNOT bypass the core confirmation or governance
gates.**

How the guarantees hold:

1. **Frozen capabilities.** The manifest is validated at load; the runtime
   snapshots the capability set as ``types.MappingProxyType`` (a true
   read-only view — ``dict`` base-class mutators cannot bypass it).
   Later manifest edits cannot widen a loaded extension.
2. **Brokered powers.** Extension code never receives references to Veto
   core modules. It receives an ``ExtensionContext`` exposing only the
   capabilities its manifest declares (data scopes, fs roots, network
   destinations). Filesystem, network, and data access all run through
   host checks first. The context carries an *opaque weakref* dispatch
   binding (see ``_dispatch.py``) — never the Host, never a token into a
   reachable registry — and every error crossing back into extension code
   is re-raised severed from host frames.
3. **Confirmation cannot be self-minted.** ``request_confirmation()``
   creates a *pending* request. Only host UI code (CLI/web/terminal —
   never extension code) may call ``host.confirmations.mint()`` after
   the human approves. Tokens are HMAC-bound to (extension, action,
   payload hash, expiry) and single-use; a forged or replayed token is
   rejected.
4. **Static import scan + stripped builtins.** Install/verify rejects
   extension code that imports anything, calls ``__import__`` / ``eval``
   / ``exec`` / ``compile`` / ``open``, or touches private/dunder
   attributes — and the module is then executed with a builtins subset
   from which those names are absent entirely, so the scan is not the
   only layer.
5. **Governance veto.** ``confirm-required`` actions additionally pass
   the user's agentic-governance veto screen; a veto blocks even a
   confirmed action, and a screen *error* fails closed (blocks).
   Extensions cannot weaken this — the screen runs in the host.
6. **Host-only audit.** Every brokered action is appended to the
   hash-chained audit log. Extension code gets a read-only view of its
   own entries, never a write handle.

Trust labels are *derived*, never caller-supplied: ``load_extension``
verifies the packaging signature (``signing.verify_package``) and
labels the load ``signed-verified`` (pinned publisher key),
``signed-integrity`` (self-asserted key: untampered since signing),
or ``local-unsigned``. A present-but-invalid sidecar fails the load.

Residual honesty: in-process Python is not a security boundary against
a sophisticated adversarial author (see SECURITY_RESIDUAL.md). The
layers above close every demonstrated bypass class, but
``load_extension`` still requires the explicit ``unsafe_local_only``
acknowledgment flag — there is no silent in-process path.
"""

from __future__ import annotations

import ast
import base64
import builtins as _builtins_mod
import hashlib
import hmac
import json
import os
import secrets
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..manifest.schema import ManifestError, load_manifest, manifest_summary
from .audit import ExtensionAudit
from .broker import Broker, ExtensionContext, _safe_repr, _safe_str
from ._dispatch import register as _register_binding
from ._dispatch import revoke as _revoke_binding
from .fs import (QuotaExceeded, SandboxFS,
                 SandboxViolation as FSSandboxViolation)
from .net import SandboxHTTP, SandboxViolation as NetSandboxViolation

SandboxViolation = (FSSandboxViolation, NetSandboxViolation)

# Runtime state defaults to the user's veto dir, never the repo tree.
HOST_KEY_FILE = Path.home() / ".veto" / "extension_host.key"
DEFAULT_AUDIT_PATH = Path.home() / ".veto" / "extensions_audit.jsonl"

PII_FIELD_HINTS = ("email", "e-mail", "phone", "tel", "address", "ssn")

#: Quotas: bounded blast radius per loaded extension.
MAX_DRAFTS_PER_EXTENSION = 100
MAX_NOTIFICATIONS_PER_EXTENSION = 100


class InProcessExecRefused(RuntimeError):
    """In-process extension execution was requested without the explicit
    ``unsafe_local_only`` acknowledgment (see SECURITY_RESIDUAL.md)."""


class ExtensionAlreadyLoadedError(ValueError):
    """A second load was attempted for an already-loaded extension id.

    Raised loudly — the platform never silently displaces a loaded
    extension (extension ID squatting / confused deputy).
    """


# ---------------------------------------------------------------------------
# Host secret + confirmation tokens
# ---------------------------------------------------------------------------

def _host_secret() -> bytes:
    """Per-install host secret (0600). Extension code never sees this."""
    key_file = Path(os.environ.get("VETO_HOST_KEY_FILE", str(HOST_KEY_FILE)))
    try:
        if key_file.is_file():
            return key_file.read_bytes()
    except OSError:
        pass
    secret = secrets.token_bytes(32)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secret)
    except OSError:
        pass
    return secret


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class PendingConfirmation:
    pending_id: str
    ext_id: str
    action_id: str
    payload: dict[str, Any]
    payload_sha: str
    created_at: float
    expires_at: float


class ConfirmationBroker:
    """Issues and verifies single-use, HMAC-bound confirmation tokens.

    The extension may *request*; only the host (human-facing UI) may
    *mint*. ``mint`` is never exposed through ``ExtensionContext``.
    """

    TOKEN_TTL_S = 15 * 60

    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret if secret is not None else _host_secret()
        self._pending: dict[str, PendingConfirmation] = {}
        self._used_nonces: dict[str, float] = {}  # nonce -> exp (pruned)

    # -- extension-visible -------------------------------------------------
    def request(self, ext_id: str, action_id: str,
                payload: dict[str, Any]) -> PendingConfirmation:
        payload_sha = hashlib.sha256(
            json.dumps(payload, sort_keys=True,
                       default=_safe_str).encode()
        ).hexdigest()
        pending = PendingConfirmation(
            pending_id="pc_" + secrets.token_hex(8),
            ext_id=ext_id,
            action_id=action_id,
            payload=payload,
            payload_sha=payload_sha,
            created_at=time.time(),
            expires_at=time.time() + self.TOKEN_TTL_S,
        )
        self._pending[pending.pending_id] = pending
        return pending

    # -- host-only ----------------------------------------------------------
    def mint(self, pending_id: str) -> str:
        """Mint a confirmation token. Host UI only — call after the human
        explicitly approves the pending request."""
        pending = self._pending.get(pending_id)
        if pending is None:
            raise ValueError(f"unknown pending confirmation {pending_id!r}")
        if time.time() > pending.expires_at:
            del self._pending[pending_id]
            raise ValueError("pending confirmation expired")
        body = {
            "ext_id": pending.ext_id,
            "action_id": pending.action_id,
            "payload_sha": pending.payload_sha,
            "exp": int(pending.expires_at),
            "nonce": secrets.token_hex(16),
        }
        raw = json.dumps(body, sort_keys=True).encode()
        sig = hmac.new(self._secret, raw, hashlib.sha256).hexdigest()
        del self._pending[pending_id]
        return _b64url(raw) + "." + sig

    def verify(self, token: str, *, ext_id: str, action_id: str,
               payload: dict[str, Any]) -> bool:
        """Verify a token is genuine, unexpired, single-use, and bound to
        this exact (extension, action, payload)."""
        try:
            raw_b64, sig = token.rsplit(".", 1)
            raw = _unb64url(raw_b64)
        except (ValueError, base64.binascii.Error):
            return False
        expected = hmac.new(self._secret, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False  # forged or wrong host secret
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(body, dict):
            return False  # structurally invalid token body
        now = time.time()
        # Opportunistically prune expired nonces so the replay set
        # cannot grow without bound.
        if len(self._used_nonces) > 1024:
            self._used_nonces = {n: e for n, e in self._used_nonces.items()
                                 if e > now}
        if body.get("nonce") in self._used_nonces:
            return False  # replay
        if now > float(body.get("exp", 0)):
            return False  # expired
        if body.get("ext_id") != ext_id or body.get("action_id") != action_id:
            return False  # bound to a different extension/action
        payload_sha = hashlib.sha256(
            json.dumps(payload, sort_keys=True,
                       default=_safe_str).encode()
        ).hexdigest()
        if not hmac.compare_digest(str(body.get("payload_sha", "")), payload_sha):
            return False  # payload was swapped after confirmation
        self._used_nonces[body["nonce"]] = float(body.get("exp", 0))
        return True

    def pending_requests(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {"pending_id": p.pending_id, "ext_id": p.ext_id,
             "action_id": p.action_id, "payload": p.payload,
             "expires_in_s": max(0, int(p.expires_at - now))}
            for p in self._pending.values() if p.expires_at > now
        ]


# ---------------------------------------------------------------------------
# Static import scan (install-time security gate)
# ---------------------------------------------------------------------------
# The extension sandbox is deny-by-default at BOTH layers:
#   1. this static scan rejects hostile constructs at install/verify time;
#   2. the module is executed with a stripped builtins subset
#      (_SAFE_BUILTIN_NAMES) in which __import__/eval/exec/compile/open
#      (and getattr/setattr) do not exist at all.
# Either layer alone fails closed; an attacker must beat both.

#: Calls that are never legitimate in extension code. Flagged by the
#: scan AND absent from the exec builtins.
_DANGEROUS_CALL_NAMES = frozenset(
    {"__import__", "eval", "exec", "compile", "open"})

#: ``from __future__ import ...`` is a compile-time directive, not a
#: runtime import (it works with stripped builtins); allow it.
_FUTURE_MODULE = "__future__"


def _scan_source(source: str, filename: str) -> list[dict[str, Any]]:
    """Scan one source string. Returns a list of violation dicts."""
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [{"file": filename, "lineno": 0,
                 "module": "<unparseable>",
                 "detail": f"unparseable source: {exc}"}]
    violations: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.append({
                "file": filename, "lineno": node.lineno,
                "module": "<import>",
                "detail": "imports are unavailable in the extension "
                          "sandbox; use the ctx API "
                          "(ctx.data / ctx.fs / ctx.http)"})
        elif isinstance(node, ast.ImportFrom):
            if node.module == _FUTURE_MODULE and node.level == 0:
                continue
            violations.append({
                "file": filename, "lineno": node.lineno,
                "module": "<import>",
                "detail": "imports are unavailable in the extension "
                          "sandbox; use the ctx API "
                          "(ctx.data / ctx.fs / ctx.http)"})
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _DANGEROUS_CALL_NAMES:
                violations.append({
                    "file": filename, "lineno": node.lineno,
                    "module": f"{name}()",
                    "detail": f"{name}() is forbidden in the extension "
                              "sandbox and absent from its builtins"})
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                violations.append({
                    "file": filename, "lineno": node.lineno,
                    "module": f"attribute {node.attr!r}",
                    "detail": "private/dunder attribute access is forbidden "
                              "in the extension sandbox (the host API is "
                              "public-only)"})
    return violations


def scan_imports(ext_dir: str | Path) -> dict[str, Any]:
    """AST-scan extension code for sandbox violations.

    Returns {ok, violations:[{file, lineno, module, detail}], warnings}.
    The extension entrypoint is the only executed file, but every
    runtime ``*.py`` is scanned (defense in depth). Developer tooling is
    not extension runtime code and is not scanned:

    - tests/ and docs/ (author's own tests and docs)
    - *_snippet.py / wizard_step.py (generated for the HOST integrator
      to copy into cli.py / wizard.py / MCP server — they run as host
      code, not extension code)
    """
    ext_dir = Path(ext_dir)
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    skip_names = {"wizard_step.py"}
    for py_file in sorted(ext_dir.rglob("*.py")):
        if ".data" in py_file.parts:
            continue
        parts = py_file.relative_to(ext_dir).parts
        if parts[0] in ("tests", "docs"):
            continue
        if py_file.name in skip_names or py_file.name.endswith("_snippet.py"):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            violations.append({"file": str(py_file), "lineno": 0,
                               "module": "<unreadable>",
                               "detail": f"cannot read source: {exc}"})
            continue
        rel = str(py_file.relative_to(ext_dir))
        violations.extend(_scan_source(source, rel))
    return {"ok": not violations, "violations": violations,
            "warnings": warnings}


# ---------------------------------------------------------------------------
# Restricted execution builtins
# ---------------------------------------------------------------------------
# Extension modules are exec'd with ONLY these builtins. Missing names
# raise NameError at runtime — fail closed. Notably absent:
# __import__ (no imports, so no module smuggling), eval/exec/compile
# (no runtime code generation), open/input (no raw IO), getattr/setattr/
# delattr (no dynamic dunder access around the static attribute ban),
# vars/dir/globals/locals (no namespace introspection), type (no
# metaclass/Broker reconstruction games), breakpoint/help/exit/quit.

_SAFE_BUILTIN_NAMES = frozenset({
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "callable", "chr", "complex", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "hash", "hex", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    # class machinery + exception taxonomy (raise/catch only)
    "__build_class__", "staticmethod", "classmethod", "property", "super",
    "hasattr",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "AttributeError", "StopIteration",
    "NotImplementedError",
})


def _safe_builtins() -> dict[str, Any]:
    """Build the restricted builtins dict for extension exec globals."""
    return {name: getattr(_builtins_mod, name)
            for name in _SAFE_BUILTIN_NAMES
            if hasattr(_builtins_mod, name)}


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

def redact_pii(value: Any) -> Any:
    """Recursively mask PII-hint fields in data handed to extensions."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in PII_FIELD_HINTS):
                out[key] = "***redacted***"
            else:
                out[key] = redact_pii(val)
        return out
    if isinstance(value, list):
        return [redact_pii(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Data providers (contract-first seam)
# ---------------------------------------------------------------------------
# Core (or Initiative 09 connectors) register callables here:
#   register_data_provider("jobs:read", lambda: [...])
# The platform defines the interface; the integration sweep wires the
# real providers (see integration_notes.md).

_DATA_PROVIDERS: dict[str, Callable[[], Any]] = {}


def register_data_provider(scope: str, fn: Callable[[], Any]) -> None:
    _DATA_PROVIDERS[scope] = fn


def _default_providers() -> dict[str, Callable[[], Any]]:
    # Synthetic, clearly-labeled fixtures so the platform and its tests
    # run without core wiring. Real providers replace these.
    return {
        "jobs:read": lambda: [{"id": "syn-job-1", "title": "Synthetic fixture role",
                               "company": "Fixture Corp",
                               "_synthetic": True}],
        "applications:read": lambda: [],
        "profile:read": lambda: {"name": "Synthetic User",
                                 "email": "user@example.com",
                                 "_synthetic": True},
        "outcomes:read": lambda: [],
        "lifecycle:read": lambda: [],
        "watches:read": lambda: [],
    }


# ---------------------------------------------------------------------------
# Frozen manifests
# ---------------------------------------------------------------------------
# The manifest is the ONLY authority for what an extension may do. It is
# deep-frozen (immutable) at load so extension code can never widen its
# own capabilities at runtime. types.MappingProxyType is a TRUE
# read-only view: unlike a dict subclass with overridden dunders, the
# dict base-class mutators (dict.__setitem__/update/pop/...) cannot
# bypass it. Every host enforcement check reads the frozen manifest,
# never extension-reachable mutable state.

def _freeze(value: Any) -> Any:
    """Recursively convert a manifest to immutable structures."""
    if isinstance(value, dict):
        return types.MappingProxyType(
            {k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# Trust derivation (from packaging signatures — never caller-supplied)
# ---------------------------------------------------------------------------

def _derive_trust(ext_dir: Path,
                  publisher_key: str | None = None) -> str:
    """Derive the trust label from the packaging signature.

    - no sidecar                    -> ``local-unsigned``
    - sidecar verifies (embedded key) -> ``signed-integrity``: the bytes
      are untampered since signing, but the publisher is self-asserted
    - sidecar verifies against the PINNED ``publisher_key``
                                    -> ``signed-verified``: real
      provenance. The registry install path passes its pinned key here
      (wiring point: registry/registry.py).
    - sidecar present but invalid, or pinned verification fails
                                    -> ManifestError (fail closed: a
      broken claim is worse than no claim)
    - signing stack unavailable     -> ``local-unsigned`` (honest label)

    Read-only use of the signing unit: this module never writes
    signatures or key material.
    """
    try:
        from ..signing.sign import SIGNATURE_FILE, verify_package
    except ImportError:
        # Signing stack unavailable in this environment: label honestly.
        return "local-unsigned"
    sidecar = ext_dir / SIGNATURE_FILE
    try:
        if publisher_key is not None:
            result = verify_package(ext_dir, public_key=publisher_key)
        else:
            result = verify_package(ext_dir)
    except Exception as exc:
        if sidecar.is_file() or publisher_key is not None:
            raise ManifestError(
                f"extension signature verification failed: {exc}") from exc
        return "local-unsigned"
    if not result.get("ok"):
        if sidecar.is_file() or publisher_key is not None:
            raise ManifestError(
                "extension signature invalid "
                f"({result.get('error')}); refusing to load a package "
                "whose signature claim does not verify")
        return "local-unsigned"
    if result.get("pinned"):
        return "signed-verified"
    return "signed-integrity"


# ---------------------------------------------------------------------------

@dataclass
class LoadedExtension:
    ext_id: str
    dir: Path
    manifest: Any  # deep-frozen (see _freeze); mutation raises TypeError
    module: types.ModuleType
    ctx: ExtensionContext
    fs: SandboxFS
    http: SandboxHTTP
    binding: Any = None  # opaque weakref dispatch binding (host-side)
    trust: str = "local-unsigned"
    draft_count: int = 0
    notification_count: int = 0


class Host:
    """Loads extensions, freezes capabilities, brokers every sensitive op."""

    def __init__(self, audit_path: str | Path | None = None,
                 secret: bytes | None = None) -> None:
        default_audit = Path(os.environ.get("VETO_EXTENSIONS_AUDIT",
                                            str(DEFAULT_AUDIT_PATH)))
        self.audit = ExtensionAudit(audit_path or default_audit)
        self.confirmations = ConfirmationBroker(secret=secret)
        self._loaded: dict[str, LoadedExtension] = {}
        self._providers: dict[str, Callable[[], Any]] = _default_providers()
        self._providers.update(_DATA_PROVIDERS)

    # -- data providers -------------------------------------------------------
    def register_provider(self, scope: str, fn: Callable[[], Any]) -> None:
        self._providers[scope] = fn

    def _data_for(self, ext_id: str, scope: str) -> Any:
        loaded = self._loaded[ext_id]
        # Read the frozen manifest — never extension-reachable mutable state.
        declared = set(loaded.manifest["permissions"]["data"])
        if scope not in declared:
            self.audit.append(ext_id, "data.denied",
                              {"scope": scope, "reason": "not declared"})
            raise PermissionError(
                f"extension {ext_id!r} did not declare data scope "
                f"{_safe_repr(scope)}")
        fn = self._providers.get(scope)
        if fn is None:
            raise RuntimeError(
                f"no provider registered for scope {_safe_repr(scope)}")
        try:
            value = fn()
        except Exception as exc:
            raise RuntimeError(
                f"data provider {_safe_repr(scope)} failed: "
                f"{_safe_str(exc)}") from exc
        if loaded.manifest["permissions"]["pii"] == "redact":
            value = redact_pii(value)
        self.audit.append(ext_id, "data.read",
                          {"scope": scope,
                           "pii": loaded.manifest["permissions"]["pii"]})
        return value

    def _fs_for(self, ext_id: str) -> SandboxFS:
        return self._loaded[ext_id].fs

    def _http_for(self, ext_id: str) -> SandboxHTTP:
        return self._loaded[ext_id].http

    # -- drafts / notifications --------------------------------------------------
    def _draft_artifact(self, ext_id: str, name: str, content: str) -> str:
        loaded = self._loaded[ext_id]
        kinds = {a["kind"] for a in loaded.manifest["permissions"]["actions"]}
        if "draft" not in kinds and "preview" not in kinds:
            raise PermissionError(
                f"extension {ext_id!r} declares no draft/preview action")
        if loaded.draft_count >= MAX_DRAFTS_PER_EXTENSION:
            raise QuotaExceeded(
                f"extension {ext_id!r} exceeded its draft quota "
                f"({MAX_DRAFTS_PER_EXTENSION})")
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_"
                       for c in name).strip("._") or "draft"
        # fs.write_text enforces the per-file / total byte quotas.
        loaded.fs.write_text(f"drafts/{safe}.md", content)
        loaded.draft_count += 1
        self.audit.append(ext_id, "draft.created", {"name": safe})
        return f"drafts/{safe}.md"

    def _queue_notification(self, ext_id: str, title: str, body: str) -> str:
        loaded = self._loaded[ext_id]
        kinds = {a["id"] for a in loaded.manifest["permissions"]["actions"]
                 if a["kind"] == "notify"}
        if not kinds:
            raise PermissionError(
                f"extension {ext_id!r} declares no notify action")
        if loaded.notification_count >= MAX_NOTIFICATIONS_PER_EXTENSION:
            raise QuotaExceeded(
                f"extension {ext_id!r} exceeded its notification quota "
                f"({MAX_NOTIFICATIONS_PER_EXTENSION})")
        nid = "nq_" + secrets.token_hex(8)
        # Notifications are proposals: the host notification center shows
        # them; nothing is sent by the extension.
        loaded.fs.write_text(f"notifications/{nid}.json",
                             json.dumps({"title": title, "body": body,
                                         "queued_at": time.time()}))
        loaded.notification_count += 1
        self.audit.append(ext_id, "notify.queued",
                          {"id": nid, "title": title,
                           "note": "proposal only; user confirms in host UI"})
        return nid

    # -- confirmation -------------------------------------------------------------
    def _request_confirmation(self, ext_id: str, action_id: str,
                              payload: dict[str, Any]) -> PendingConfirmation:
        loaded = self._loaded[ext_id]
        # Action lookup reads the FROZEN manifest — never the context's
        # mutable surface — so kind-downgrade attempts are inert.
        action = next((a for a in loaded.manifest["permissions"]["actions"]
                       if a["id"] == action_id), None)
        if action is None:
            raise ValueError(f"unknown action {_safe_repr(action_id)}")
        if action["kind"] != "confirm-required":
            raise ValueError(
                f"action {_safe_repr(action_id)} is kind "
                f"{_safe_repr(action['kind'])}; only "
                "confirm-required actions use the confirmation broker")
        pending = self.confirmations.request(ext_id, action_id, payload)
        self.audit.append(ext_id, "confirm.requested",
                          {"action": action_id, "pending_id": pending.pending_id,
                           "payload_sha": pending.payload_sha})
        return pending

    def _governance_veto_screen(self, ext_id: str, action_id: str,
                                payload: dict[str, Any]) -> list[str]:
        """Defense in depth: run the user's governance veto screen on a
        confirm-required action description. A veto blocks even a confirmed
        action. A screen *error* fails closed (blocks) — per the
        framework's own fail-closed policy for the veto gate. Only a
        missing framework degrades to advisory (the deterministic
        HMAC-token gates below still hold)."""
        try:
            from governance import adapter as _gov
        except ImportError:
            self.audit.append(ext_id, "governance.unavailable",
                              {"note": "framework not installed; "
                                       "deterministic gates only"})
            return ["governance unavailable; deterministic gates only"]
        try:
            if not _gov.governance_enabled():
                return ["governance unavailable; deterministic gates only"]
            framework = _gov.load_framework()
            result = framework.check_veto(
                domain=getattr(_gov, "GOVERNANCE_DOMAIN", "universal"),
                text=(f"Extension {ext_id} requests confirm-required action "
                      f"{action_id} with payload {json.dumps(payload)[:500]}"),
            )
        except Exception as exc:
            # FAIL CLOSED: an unevaluated gate must not read as passed.
            self.audit.append(ext_id, "governance.screen_error",
                              {"error": str(exc)[:200]})
            return ["vetoed"]
        vetoed = bool(result.get("veto_triggered") or result.get("vetoed"))
        hits = result.get("veto_hits") or result.get("hits") or []
        if vetoed:
            return ["vetoed", *(str(h) for h in hits)]
        return [str(h) for h in hits]

    # -- loading ---------------------------------------------------------------------
    def _verify_loadable(self, ext_dir: str | Path
                         ) -> tuple[Path, dict[str, Any], str]:
        """Single-read verification: manifest + entrypoint + static scan.

        Returns (resolved ext_dir, manifest dict, extension.py source).
        Raises ManifestError — fail closed. The exact source bytes that
        will be exec'd are scanned, closing the verify->exec TOCTOU.
        """
        ext_dir = Path(ext_dir).resolve()
        manifest = load_manifest(ext_dir)  # stage: manifest
        entry = ext_dir / "extension.py"
        if not entry.is_file():
            raise ManifestError("missing extension.py entrypoint")
        try:
            source = entry.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestError(f"cannot read extension.py: {exc}") from exc
        scan = scan_imports(ext_dir)
        if not scan["ok"]:
            raise ManifestError(
                "extension failed verification at stage import-scan: "
                f"{scan['violations']}")
        exact = _scan_source(source, "extension.py")
        if exact:
            raise ManifestError(
                "extension failed verification at stage import-scan: "
                f"{exact}")
        return ext_dir, manifest, source

    def verify_extension(self, ext_dir: str | Path) -> dict[str, Any]:
        """Full pre-load verification: manifest + import scan. Fail closed.

        No code is executed; no acknowledgment flag needed.
        """
        try:
            _, manifest, _ = self._verify_loadable(ext_dir)
        except ManifestError as exc:
            msg = str(exc)
            if "import-scan" in msg:
                stage = "import-scan"
            elif "entrypoint" in msg:
                stage = "entrypoint"
            else:
                stage = "manifest"
            return {"ok": False, "stage": stage, "error": msg}
        return {"ok": True, "manifest": manifest_summary(manifest),
                "scan": {"ok": True, "violations": [], "warnings": []}}

    def load_extension(self, ext_dir: str | Path, *,
                       publisher_key: str | None = None,
                       unsafe_local_only: bool = False) -> LoadedExtension:
        """Verify, freeze capabilities, and load an extension module.

        The module is executed with a globals dict containing ONLY
        ``ctx`` (plus a stripped builtins subset — no ``__import__``,
        ``eval``, ``exec``, ``compile``, ``open``, ``getattr``): static
        bypass routes are closed by the import scan at verify time AND
        by the missing builtins at runtime. Capabilities are
        deep-frozen (``types.MappingProxyType``) from the manifest;
        later manifest edits are inert.

        ``publisher_key``: pinned Ed25519 public key (hex) for the
        ``signed-verified`` trust label — the registry install path
        passes its pinned key here. ``trust`` is DERIVED from the
        packaging signature; there is no caller-supplied trust label.

        ``unsafe_local_only``: in-process Python is not a security
        boundary against adversarial code (see SECURITY_RESIDUAL.md).
        The in-process path is gated: pass
        ``unsafe_local_only=True`` to explicitly acknowledge the
        residual risk. Only for human-reviewed, signed extensions.
        """
        if not unsafe_local_only:
            raise InProcessExecRefused(
                "refusing in-process extension execution: in-process "
                "Python is not a security boundary against adversarial "
                "extension code "
                "(see initiatives/i11/sandbox/SECURITY_RESIDUAL.md). "
                "Pass unsafe_local_only=True to acknowledge the residual "
                "risk and proceed — only for human-reviewed, signed "
                "extensions.")
        ext_dir, manifest, source = self._verify_loadable(ext_dir)
        ext_id = manifest["id"]
        if ext_id in self._loaded:
            # B5: fail loudly — never silently displace a loaded extension.
            raise ExtensionAlreadyLoadedError(
                f"extension {ext_id!r} is already loaded; refusing to "
                "displace it — unload it first")
        trust = _derive_trust(ext_dir, publisher_key)
        frozen = _freeze(manifest)  # deep-immutable: later edits inert,
        # and extension code cannot widen its own capabilities at runtime.

        perms = frozen["permissions"]
        fs = SandboxFS(ext_dir, perms["sandbox"]["fs_roots"])
        http = SandboxHTTP(perms["network"]["destinations"],
                           calls_per_minute=perms["sandbox"]["rate_limit"]["calls_per_minute"])

        # The extension entrypoint receives exactly one host object: an
        # ExtensionContext holding an opaque integer dispatch handle —
        # never the Host itself, never a token into a reachable
        # registry, never a dereferenceable weakref. The handle
        # resolves to (Host, ext_id) host-side only.
        binding = _register_binding(self, ext_id)
        broker = Broker(binding, ext_id)
        ctx = ExtensionContext(broker, ext_id, frozen)

        module = types.ModuleType(f"veto_ext_{ext_id}")
        mdict = module.__dict__
        mdict["__veto_extension__"] = True
        mdict["__builtins__"] = _safe_builtins()
        mdict["ctx"] = ctx
        # Scrub the dunder machinery a bare ModuleType() sets: bare-name
        # access to these must not resolve inside extension code.
        for _scrub in ("__loader__", "__spec__", "__package__", "__doc__"):
            mdict.pop(_scrub, None)

        loaded = LoadedExtension(
            ext_id=ext_id, dir=ext_dir, manifest=frozen, module=module,
            ctx=ctx, fs=fs, http=http, binding=binding, trust=trust)
        # Register BEFORE exec so re-entrant ctx calls during import
        # resolve; capabilities are already frozen from the manifest.
        self._loaded[ext_id] = loaded
        try:
            code = compile(source, str(ext_dir / "extension.py"), "exec")
            exec(code, mdict)
        except Exception:
            del self._loaded[ext_id]
            _revoke_binding(binding)
            raise
        self.audit.append(ext_id, "extension.loaded",
                          {"version": frozen["version"], "trust": trust,
                           "capabilities": manifest_summary(frozen)})
        return loaded

    def unload_extension(self, ext_id: str) -> None:
        loaded = self._loaded.pop(ext_id, None)
        if loaded is not None:
            _revoke_binding(loaded.binding)
            self.audit.append(ext_id, "extension.unloaded", {})

    # -- action execution ---------------------------------------------------------------
    def run_extension_action(self, ext_id: str, action_id: str,
                             confirmation_token: str | None = None,
                             **kwargs: Any) -> dict[str, Any]:
        """Execute a declared action through the broker.

        - read/draft/preview/notify: run directly (side-effect-free or
          host-mediated).
        - confirm-required: requires a valid, single-use, HMAC-bound
          confirmation token minted by the host AFTER human approval, plus
          a governance veto screen. No token → no execution. Period.
        """
        loaded = self._loaded.get(ext_id)
        if loaded is None:
            raise ValueError(f"extension {ext_id!r} is not loaded")
        action = next((a for a in loaded.manifest["permissions"]["actions"]
                       if a["id"] == action_id), None)
        if action is None:
            self.audit.append(ext_id, "action.denied",
                              {"action": action_id, "reason": "undeclared"})
            raise ValueError(f"action {_safe_repr(action_id)} is not declared")
        fn = getattr(loaded.module, "ACTIONS", {}).get(action_id)
        if not callable(fn):
            raise ValueError(
                f"extension {ext_id!r} declares action "
                f"{_safe_repr(action_id)} "
                "but extension.py has no matching ACTIONS entry")

        if action["kind"] == "confirm-required":
            if not confirmation_token:
                self.audit.append(ext_id, "action.denied",
                                  {"action": action_id,
                                   "reason": "missing confirmation token"})
                raise PermissionError(
                    f"action {_safe_repr(action_id)} requires a host-minted "
                    "confirmation token; the extension cannot self-confirm")
            payload = {"action": action_id, "params": kwargs}
            if not self.confirmations.verify(
                    confirmation_token, ext_id=ext_id,
                    action_id=action_id, payload=payload):
                self.audit.append(ext_id, "action.denied",
                                  {"action": action_id,
                                   "reason": "invalid/forged/expired/replayed "
                                             "confirmation token"})
                raise PermissionError(
                    f"confirmation token for {_safe_repr(action_id)} is "
                    "invalid, expired, replayed, or bound to a different "
                    "payload")
            veto_hits = self._governance_veto_screen(ext_id, action_id, payload)
            hard_veto = any(h == "vetoed" for h in veto_hits)
            if hard_veto:
                self.audit.append(ext_id, "action.vetoed",
                                  {"action": action_id, "hits": veto_hits})
                raise PermissionError(
                    f"action {_safe_repr(action_id)} blocked by governance "
                    "veto; only a human clears a veto")
            self.audit.append(ext_id, "action.confirmed",
                              {"action": action_id,
                               "governance_notes": veto_hits})
        else:
            self.audit.append(ext_id, "action.run",
                              {"action": action_id, "kind": action["kind"]})
        try:
            result = fn(loaded.ctx, **kwargs)
        except Exception as exc:
            self.audit.append(ext_id, "action.error",
                              {"action": action_id,
                               # _safe_str: exc is the extension's own
                               # raised exception; a hostile __str__
                               # must not escape raw from this Host
                               # frame (same class as the broker
                               # sanitizer bypass).
                               "error": _safe_str(exc)[:300]})
            raise
        self.audit.append(ext_id, "action.ok", {"action": action_id})
        return {"ok": True, "action": action_id, "result": result}

    # -- inventory ----------------------------------------------------------------------------
    def list_loaded(self) -> list[dict[str, Any]]:
        return [
            {"id": le.ext_id, "version": le.manifest["version"],
             "name": le.manifest["name"], "trust": le.trust,
             "capabilities": manifest_summary(le.manifest)}
            for le in self._loaded.values()
        ]
