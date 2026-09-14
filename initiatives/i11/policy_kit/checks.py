#!/usr/bin/env python3
"""Policy test kit: reusable checks every extension must pass.

Each check is a function ``check_*(ext_dir) -> CheckResult`` so extension
authors, the registry security gate, and CI can reuse the same suite.
The checks cover the roadmap's list: confirmation, PII handling, rate
limits, circuit breakers, and veto behavior.

Checks run against a fresh Host with synthetic providers — deterministic,
no network, no core wiring required.

Honesty contract: a check passes ONLY when the guarantee was genuinely
exercised. Refusals count only via the expected refusal exceptions
(``PermissionError`` for gate denials); anything else fails the check
or fails closed via the crashed-check handler in ``run_policy_suite``.
A crashed check is a FAILED check, never a pass.

A check that has nothing to exercise on a fixture (no confirm-required
actions, no network destinations, no profile:read) is reported as
NOT APPLICABLE — ``applicable=False`` — never as "blocked" or "passed".
``run_policy_suite`` counts applicable results separately
(``applicable`` / ``passed`` / ``failed`` / ``not_applicable``), so a
report can never overstate what was proven. Consumers that gate on
``failed == 0`` are unaffected: not-applicable is not a failure.
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..manifest.schema import ManifestError, load_manifest
from ..sandbox.host import ConfirmationBroker, Host, scan_imports


@dataclass
class CheckResult:
    id: str
    passed: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    # False when the fixture declares none of the surface the check
    # exercises (no confirm-required actions, no network destinations,
    # no profile:read). Not-applicable results are excluded from the
    # passed/failed counts — they are "not exercised", never "blocked".
    applicable: bool = True


def _fresh_host(secret: bytes = b"test-secret-32-bytes-padding!!!!") -> Host:
    tmp = tempfile.mkdtemp(prefix="veto-ext-audit-")
    host = Host(audit_path=Path(tmp) / "audit.jsonl", secret=secret)
    # The audit dir is harness scratch: remove it when the Host is
    # collected (mirrors the _mkext rmtree discipline for fixtures).
    # Deterministic under CPython refcounting — the Host is not
    # retained past the check that created it.
    weakref.finalize(host, shutil.rmtree, tmp, ignore_errors=True)
    return host


def _load_or_fail(check_id: str, ext_dir: str | Path) -> tuple[Host, Any]:
    """Load the fixture or fail the check: an unloadable fixture proves
    nothing. Unexpected load errors propagate to the crashed-check
    handler (fail closed).

    ``unsafe_local_only=True`` is the documented acknowledgment path
    (see ``sandbox/SECURITY_RESIDUAL.md``): the policy suite exercises
    the in-process execution path against test fixtures."""
    host = _fresh_host()
    try:
        loaded = host.load_extension(ext_dir, unsafe_local_only=True)
    except (ManifestError, OSError) as exc:
        raise _CheckFailed(check_id, f"load failed: {exc}") from exc
    return host, loaded


class _CheckFailed(Exception):
    """Internal: convert to a failed CheckResult with the given id."""

    def __init__(self, check_id: str, detail: str) -> None:
        super().__init__(detail)
        self.check_id = check_id
        self.detail = detail


def check_manifest_valid(ext_dir: str | Path) -> CheckResult:
    """Manifest parses and declares only known capabilities."""
    try:
        manifest = load_manifest(ext_dir)
    except (ManifestError, OSError) as exc:
        return CheckResult("manifest.valid", False, f"manifest rejected: {exc}")
    return CheckResult("manifest.valid", True,
                       f"{manifest['id']}@{manifest['version']} declares "
                       f"{manifest['permissions']['data']}",
                       {"id": manifest["id"]})


def check_import_scan_clean(ext_dir: str | Path) -> CheckResult:
    """No banned imports (bypass routes closed at install time)."""
    scan = scan_imports(ext_dir)
    if not scan["ok"]:
        return CheckResult("imports.clean", False,
                           f"banned imports: {scan['violations']}",
                           {"violations": scan["violations"]})
    return CheckResult("imports.clean", True,
                       "no banned imports",
                       {"warnings": scan["warnings"]})


def _confirm_action_id(loaded: Any) -> str | None:
    confirm_actions = [a["id"] for a in
                       loaded.manifest["permissions"]["actions"]
                       if a["kind"] == "confirm-required"]
    return confirm_actions[0] if confirm_actions else None


def check_confirmation_cannot_self_confirm(ext_dir: str | Path) -> CheckResult:
    """A confirm-required action with no token is refused."""
    cid = "confirm.no-self-confirm"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    action_id = _confirm_action_id(loaded)
    if action_id is None:
        return CheckResult(cid, True,
                           "not applicable: no confirm-required actions "
                           "declared — the gate was not exercised",
                           applicable=False)
    try:
        host.run_extension_action(loaded.ext_id, action_id)
    except PermissionError as exc:
        # The ONLY acceptable refusal: the confirmation gate denying a
        # tokenless run. Anything else is not a refusal.
        ok = "confirmation token" in str(exc)
        return CheckResult(cid, ok, f"tokenless execution refused: {exc}")
    # No exception: the action RAN without a token — bypass, full stop.
    # An action that errors after running is still a bypass, so no broad
    # except here: unexpected exceptions propagate to the crashed-check
    # handler (fail closed).
    return CheckResult(cid, False,
                       "CONFIRM-REQUIRED ACTION RAN WITHOUT A TOKEN — bypass!")


def check_confirmation_forged_token_rejected(ext_dir: str | Path) -> CheckResult:
    """A forged confirmation token is rejected."""
    cid = "confirm.forged-token"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    action_id = _confirm_action_id(loaded)
    if action_id is None:
        return CheckResult(cid, True,
                           "not applicable: no confirm-required actions "
                           "declared — the gate was not exercised",
                           applicable=False)
    # Forge a token with a DIFFERENT host secret — simulates an extension
    # that stole the token format but not the secret. A forging failure is
    # a broken check, not a platform refusal: let it crash fail-closed.
    forger = ConfirmationBroker(secret=b"attacker-secret-32-bytes-padding!!")
    pending = forger.request(loaded.ext_id, action_id,
                             {"action": action_id, "params": {}})
    forged = forger.mint(pending.pending_id)
    try:
        host.run_extension_action(loaded.ext_id, action_id,
                                  confirmation_token=forged)
    except PermissionError:
        return CheckResult(cid, True, "forged token rejected")
    return CheckResult(cid, False, "FORGED TOKEN ACCEPTED — bypass!")


def _token_nonce(token: str) -> str:
    """Extract the nonce from a confirmation token (no secret needed —
    the token body is signed, not encrypted)."""
    raw_b64 = token.split(".")[0]
    raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
    return str(json.loads(raw.decode("utf-8"))["nonce"])


def check_confirmation_replay_rejected(ext_dir: str | Path) -> CheckResult:
    """A confirmation token is single-use: replay is rejected."""
    cid = "confirm.replay"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    action_id = _confirm_action_id(loaded)
    if action_id is None:
        return CheckResult(cid, True,
                           "not applicable: no confirm-required actions "
                           "declared — the gate was not exercised",
                           applicable=False)
    params: dict[str, Any] = {}
    pending = host.confirmations.request(
        loaded.ext_id, action_id, {"action": action_id, "params": params})
    token = host.confirmations.mint(pending.pending_id)
    nonce = _token_nonce(token)
    # First use: the action runs (or is veto-blocked — a veto-screen block
    # still consumed the token). Either way the token must be consumed for
    # the replay probe below to be meaningful.
    try:
        host.run_extension_action(loaded.ext_id, action_id,
                                  confirmation_token=token, **params)
    except PermissionError as exc:
        if "veto" not in str(exc).lower():
            return CheckResult(cid, False,
                               f"first use unexpectedly refused: {exc}")
    except Exception as exc:
        # The action code errored AFTER the gate verified the token (the
        # gate raises PermissionError, never anything else). The token was
        # consumed, so the replay probe stays valid — but record it.
        if nonce not in host.confirmations._used_nonces:  # noqa: SLF001
            return CheckResult(cid, False,
                               f"first use errored ({type(exc).__name__}) "
                               f"without consuming the token; replay probe "
                               f"invalid: {exc}")
    if nonce not in host.confirmations._used_nonces:  # noqa: SLF001
        return CheckResult(cid, False,
                           "first use did not consume the token; replay "
                           "probe invalid")
    # Second use must be refused. ONLY a PermissionError counts as a
    # refusal: if the replayed token were accepted, the action would run
    # (and possibly error) — that is a bypass, not a rejection.
    try:
        host.run_extension_action(loaded.ext_id, action_id,
                                  confirmation_token=token, **params)
    except PermissionError:
        return CheckResult(cid, True, "replayed token rejected")
    except Exception as exc:
        return CheckResult(cid, False,
                           f"REPLAYED TOKEN ACCEPTED (action ran, then "
                           f"{type(exc).__name__}) — bypass!")
    return CheckResult(cid, False, "REPLAYED TOKEN ACCEPTED — bypass!")


def check_pii_redacted_by_default(ext_dir: str | Path) -> CheckResult:
    """With pii:redact (default), PII fields never reach extension code.

    NOTE on the check id: ``pii.redacted`` passing means PII was actually
    redacted. An extension that declares ``pii:read`` receives raw PII by
    design — that is reported under the separate id ``pii.read-declared``
    so the id never overstates the guarantee.
    """
    cid = "pii.redacted"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    if "profile:read" not in loaded.manifest["permissions"]["data"]:
        return CheckResult(cid, True,
                           "not applicable: profile:read not declared — "
                           "redaction was not exercised",
                           applicable=False)
    if loaded.manifest["permissions"]["pii"] == "read":
        return CheckResult(
            "pii.read-declared", True,
            "pii:read declared — extension receives raw PII by design; "
            "the registry UX gate must justify it",
            {"note": "this id passing does NOT mean PII was redacted"})
    host.register_provider("profile:read", lambda: {
        "name": "Ada Lovelace", "email": "ada@example.com",
        "phone": "+1-555-0100"})
    profile = loaded.ctx.data("profile:read")
    leaked = [k for k in ("email", "phone")
              if profile.get(k) not in (None, "***redacted***")]
    if leaked:
        return CheckResult(cid, False, f"PII leaked to extension: {leaked}")
    return CheckResult(cid, True, "PII redacted before extension saw it")


def check_rate_limit_enforced(ext_dir: str | Path) -> CheckResult:
    """The MANIFEST's calls_per_minute is what the host enforces.

    Uses the extension's own loaded.http client (manifest allowlist,
    manifest bucket, manifest circuits). Only the transport is stubbed —
    the documented no-network test seam — so the bucket under test is
    exactly the one the host built from the manifest.
    """
    cid = "ratelimit.enforced"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    dests = loaded.manifest["permissions"]["network"]["destinations"]
    if not dests:
        return CheckResult(cid, True,
                           "not applicable: no network destinations "
                           "declared — the limiter was not exercised",
                           applicable=False)
    cpm = loaded.manifest["permissions"]["sandbox"]["rate_limit"]["calls_per_minute"]
    from ..sandbox.net import HttpRequest, HttpResponse, RateLimitExceeded
    http = loaded.http  # the extension's own client, not a fresh one
    # Documented test seam: stub ONLY the transport (no real network).
    # The token bucket, allowlist, and circuits are untouched and come
    # from the manifest via the host.
    http._transport = (  # noqa: SLF001
        lambda req: HttpResponse(status=200, headers={}, body=b"ok"))
    dest = dests[0]
    succeeded = 0
    limit_error: Exception | None = None
    # calls_per_minute rapid calls must pass; the very next one must raise.
    # (A small tolerance above cpm absorbs token-bucket refill on a slow
    # machine; far above cpm means the manifest value is NOT enforced.)
    for _ in range(cpm + 4):
        try:
            http.request(HttpRequest(method="GET", url=f"https://{dest}/x"))
            succeeded += 1
        except RateLimitExceeded as exc:
            limit_error = exc
            break
    if limit_error is None:
        return CheckResult(cid, False,
                           f"RATE LIMIT NOT ENFORCED — {cpm + 4} rapid calls "
                           f"all passed; manifest declares {cpm}/min")
    if not (cpm <= succeeded <= cpm + 2):
        return CheckResult(cid, False,
                           f"enforced limit ({succeeded} calls before the "
                           f"limit tripped) does not match the manifest's "
                           f"calls_per_minute ({cpm})")
    if str(cpm) not in str(limit_error):
        return CheckResult(cid, False,
                           f"limit tripped but not at the manifest value: "
                           f"{limit_error}")
    return CheckResult(cid, True,
                       f"manifest rate limit enforced: {succeeded} calls "
                       f"passed, next call raised RateLimitExceeded "
                       f"({cpm}/min)",
                       {"calls_per_minute": cpm, "succeeded": succeeded})


def check_circuit_breaker_trips(ext_dir: str | Path) -> CheckResult:
    """Repeated 5xx failures open the circuit; it recovers after cooldown.

    Uses the extension's own loaded.http client — the same allowlist,
    bucket, and circuit state the extension's code would hit. Only the
    transport is stubbed (documented no-network test seam).
    """
    cid = "circuit.trips"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    dests = loaded.manifest["permissions"]["network"]["destinations"]
    if not dests:
        return CheckResult(cid, True,
                           "not applicable: no network destinations "
                           "declared — the limiter was not exercised",
                           applicable=False)
    from ..sandbox.net import CircuitOpen, HttpRequest, HttpResponse
    dest = dests[0]
    http = loaded.http  # the extension's own client, not a fresh SandboxHTTP
    calls = {"n": 0}

    def failing_transport(req: HttpRequest) -> HttpResponse:
        calls["n"] += 1
        return HttpResponse(status=503, headers={}, body=b"")

    http._transport = failing_transport  # noqa: SLF001 - test seam only
    tripped = False
    # ONLY CircuitOpen counts as the breaker tripping. Unexpected errors
    # propagate to the crashed-check handler (fail closed).
    for _ in range(6):
        try:
            http.request(HttpRequest(method="GET", url=f"https://{dest}/"))
        except CircuitOpen:
            tripped = True
            break
    if not tripped:
        return CheckResult(cid, False,
                           f"CIRCUIT NEVER OPENED ({calls['n']} failing "
                           f"calls all reached the transport)")
    if not calls["n"] < 6:
        return CheckResult(cid, False,
                           "breaker did not short-circuit: every call "
                           "reached the transport")
    failing_before_trip = calls["n"]  # snapshot: later phases reuse the counter
    # Recovery: the docstring claims the circuit cools down. Simulate
    # cooldown expiry on the client's own circuit state, then a healthy
    # transport must get through and reset the breaker.
    circuit = http._circuits[dest]  # noqa: SLF001 - harness introspection
    circuit.opened_at -= (circuit.cooldown_s + 1)

    def healthy_transport(req: HttpRequest) -> HttpResponse:
        calls["n"] += 1
        return HttpResponse(status=200, headers={}, body=b"ok")

    http._transport = healthy_transport  # noqa: SLF001 - test seam only
    try:
        resp = http.request(HttpRequest(method="GET", url=f"https://{dest}/"))
    except CircuitOpen:
        return CheckResult(cid, False,
                           "circuit did not recover after cooldown: still open")
    if resp.status != 200 or circuit.failures != 0:
        return CheckResult(cid, False,
                           f"breaker did not reset after cooldown "
                           f"(status={resp.status}, "
                           f"failures={circuit.failures})")
    # And it re-arms: one fresh 503 is a single failure, not an open circuit.
    http._transport = failing_transport  # noqa: SLF001 - test seam only
    http.request(HttpRequest(method="GET", url=f"https://{dest}/"))
    if circuit.failures != 1:
        return CheckResult(cid, False,
                           f"breaker did not re-arm cleanly "
                           f"(failures={circuit.failures})")
    return CheckResult(cid, True,
                       f"circuit opened after {failing_before_trip} failing "
                       f"calls, recovered after cooldown, and re-armed",
                       {"failing_calls_before_trip": failing_before_trip})


def check_undeclared_scope_denied(ext_dir: str | Path) -> CheckResult:
    """Data scopes outside the manifest are denied."""
    cid = "scope.denied"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    from ..manifest.schema import DATA_SCOPES
    declared = set(loaded.manifest["permissions"]["data"])
    undeclared = [s for s in DATA_SCOPES if s not in declared]
    if not undeclared:
        return CheckResult(cid, False,
                           "extension declares every scope; pick a narrower fixture")
    try:
        loaded.ctx.data(undeclared[0])
    except PermissionError:
        return CheckResult(cid, True,
                           f"undeclared scope {undeclared[0]!r} denied")
    # No exception: the scope was GRANTED — bypass. Unexpected exceptions
    # propagate to the crashed-check handler (fail closed).
    return CheckResult(cid, False,
                       f"UNDECLARED SCOPE {undeclared[0]!r} WAS GRANTED — bypass!")


def check_audit_append_only(ext_dir: str | Path) -> CheckResult:
    """Extension code has no write path to the audit log; chain verifies."""
    cid = "audit.append-only"
    try:
        host, loaded = _load_or_fail(cid, ext_dir)
    except _CheckFailed as exc:
        return CheckResult(exc.check_id, False, exc.detail)
    # The context must not expose the audit writer.
    exposed = [name for name in ("audit", "_audit", "append_audit",
                                 "write_audit")
               if hasattr(loaded.ctx, name)]
    if exposed:
        return CheckResult(cid, False,
                           f"ctx exposes audit writers: {exposed}")
    if "jobs:read" in loaded.ctx.scopes:
        loaded.ctx.data("jobs:read")
    verdict = host.audit.verify()
    if not verdict["ok"]:
        return CheckResult(cid, False,
                           f"audit chain broken: {verdict['error']}")
    mine = host.audit.entries_for(loaded.ext_id)
    if not mine:
        return CheckResult(cid, False,
                           "no audit entries recorded for extension actions")
    return CheckResult(cid, True,
                       f"{verdict['entries']} chained entries; "
                       f"{len(mine)} for this extension; ctx has no writer")


ALL_CHECKS = [
    check_manifest_valid,
    check_import_scan_clean,
    check_confirmation_cannot_self_confirm,
    check_confirmation_forged_token_rejected,
    check_confirmation_replay_rejected,
    check_pii_redacted_by_default,
    check_rate_limit_enforced,
    check_circuit_breaker_trips,
    check_undeclared_scope_denied,
    check_audit_append_only,
]


def run_policy_suite(ext_dir: str | Path) -> dict[str, Any]:
    """Run every policy check.

    A crashed check is a FAILED check (fail closed) — never a pass. A
    check the fixture gives nothing to exercise is NOT APPLICABLE: it is
    excluded from the passed/failed counts, so the report never
    overstates what was proven.
    """
    results = []
    for check in ALL_CHECKS:
        try:
            result = check(ext_dir)
        except _CheckFailed as exc:
            result = CheckResult(exc.check_id, False, exc.detail)
        except Exception as exc:  # noqa: BLE001 - a crashed check fails closed
            results.append(CheckResult(check.__name__, False,
                                       f"check crashed (fail closed): "
                                       f"{type(exc).__name__}: {exc}"))
            continue
        results.append(result)
    applicable_results = [r for r in results if r.applicable]
    failed = [r for r in applicable_results if not r.passed]
    return {
        "extension": str(ext_dir),
        "applicable": len(applicable_results),
        "passed": len(applicable_results) - len(failed),
        "failed": len(failed),
        "not_applicable": len(results) - len(applicable_results),
        "results": [{"id": r.id, "passed": r.passed,
                     "applicable": r.applicable, "detail": r.detail}
                    for r in results],
    }
