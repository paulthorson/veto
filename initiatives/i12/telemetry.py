#!/usr/bin/env python3
"""Initiative 12 / Epic 6 — Consent-based product analytics + Q3 exit-gate tripwire.

Measures activation and workflow completion WITHOUT collecting resume
content or private job-search content. This is the mechanism behind the
Q3 exit gate, not a promise:

* **Consent-gated.** Analytics default OFF. ``set_consent(True)`` records an
  explicit opt-in with source and timestamp; consent changes are audited.
* **Schema-allowlisted.** Every event type has a fixed field schema; every
  schema field is required. Unknown fields, missing fields, non-scalar
  values, values outside the pinned ``TOOLS`` / ``SURFACES`` /
  ``ARTIFACT_KINDS`` domains, and token values shaped like smuggled content
  are rejected before storage. ``session_id`` is restricted to compact
  alphanumerics so structured PII (dotted emails, dashed phones,
  underscore-joined names) cannot fit in the one free-form-ish field.
* **Content-scanned.** Every event passes through
  :mod:`initiatives.i12.privacy`. Any content field (resume text, JD text,
  names, contact details, long free text) triggers the tripwire.
* **The tripwire.** On detection: analytics shut off immediately and stay
  off; the triggering payload and the live events present at trip time are
  written to access-controlled quarantine (directory ``0700``, files
  ``0600`` — deliberately *not* called "sealed": there is no encryption,
  only OS file permissions); an incident record is filed; the configured
  notifier fires. The trip is fail-safe: even if the quarantine write
  fails, analytics are still shut off and the incident is still recorded —
  the wire never fails open.
* **Clearing protocol.** Re-enable requires, in order: (1) a clearing entry
  by the contracted security/privacy specialist *named in the roles
  registry*, stating the reason; (2) verification by the independent
  reviewer *named in the roles registry*, a different person; (3) no operator
  veto. Every step is appended to a hash-chained, append-only JSONL audit
  log that is verified on every load. The registry ships empty — operator/ops
  populate it via :meth:`TelemetryStore.set_roles`; clearing is blocked
  until both roles are named.
* **Operator veto.** ``paul_veto(reason)`` halts *recording* immediately — not
  just re-enable — and stays in force until the operator lifts it with
  ``lift_paul_veto(confirmation)``, typing an explicit confirmation that is
  recorded in the audit log.
* **No vanity metrics.** The reporting layer refuses to compute
  applications-per-day or any volume north star (roadmap guardrail); it
  reports qualified activation and workflow completion only.

TRUST BOUNDARY — what this module does and does not guarantee:

* It DOES guarantee: no recording without opt-in consent; no recording
  while the tripwire is tripped or the operator's veto stands; schema-allowlisted
  events only; any scanner finding shuts analytics off, quarantines the
  payloads, and files an incident; clearing requires the registry-named
  specialist AND a registry-named, different reviewer; every
  clearing/veto/purge action is appended to a hash-chained log verified on
  load; a veto lifts only on an explicit typed confirmation.
* It does NOT guarantee: that the human typing a registry name IS that
  person. Local code cannot verify human identity or true independence of
  two people — one actor with Python access could populate the registry
  with two names and play both parts. The registry + distinct-identity
  rule + tamper-evident log raise the cost (every step is attributable and
  immutable, and tampering the log breaks the chain on next load), but
  ultimate enforcement of human separation of duties is a human process
  owned by operator/ops: they populate the registry, they run the clearing,
  they answer for it.

MIDNIGHT-PAGER RUNBOOK — what happens on a trip, and what the operator does:

1. ``record()`` raises :class:`TripwireTripped` to the caller AND the store
   invokes the configured notifier. The default notifier writes a loud
   block to stderr and drops a ``PENDING-NOTIFICATION-<incident_id>.json``
   marker file next to the state file — the event is never silently
   swallowed.
2. In a daemon/service context the operator MUST configure a real pager
   hook at store creation: ``TelemetryStore(path, notifier=my_pager_fn)``
   where the callable receives the incident dict, or
   ``notifier=["/usr/local/bin/page", ...]`` (argv form, no shell: the
   incident id and quarantine path are appended as arguments and the
   incident JSON goes on stdin).
3. What the responder finds: the incident record in the state JSON
   (``incidents``), the offending payload plus the live events present at
   trip time in ``quarantine/INC-*.json`` (directory ``0700``, files
   ``0600`` — access-controlled, not encrypted), and the human directory:
   ``i12_telemetry_roles.json`` names the contracted security/privacy
   specialist and the independent reviewer — that registry IS the contact
   directory the midnight-pager protocol needs.
4. Clearing order: registry-named specialist writes ``clear_incident``
   (name + reason) → registry-named, different reviewer runs
   ``verify_clearance`` (evidence ref) → ``reenable()``, unless the operator's veto
   is in force. Every step lands in the hash-chained
   ``i12_telemetry_clearings.jsonl`` audit log.
5. Retention: quarantine payloads are never auto-deleted.
   :meth:`TelemetryStore.purge_quarantine` lists (and, only with explicit
   ``confirm=True``, deletes) payloads older than N days, and evidence for
   open or unverified incidents is excluded entirely.

All state lives in one JSON file (default under the user's local data
dir); tests inject a temp path. The roles registry
(``i12_telemetry_roles.json``), the audit log
(``i12_telemetry_clearings.jsonl``), the quarantine directory, and any
pending-notification markers live next to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

# NOTE: _EMAIL_RE / _PHONE_RE are package-internal names in privacy.py;
# telemetry reuses them (same package) for the token content screen so the
# "what counts as smuggled content" definition stays in one place. If the
# privacy hardening promotes them to public names, update this import.
from .privacy import (
    CONTENT_MARKERS,
    FORBIDDEN_FIELD_PATTERNS,
    ContentDetected,
    _EMAIL_RE,
    _PHONE_RE,
    scan_payload,
)

#: Event schemas: event type -> {field: kind}. Kinds: token (short enum-ish
#: string), flag (bool), seconds (float). No free-text kinds exist on
#: purpose — there is no legitimate content field in telemetry. Every field
#: in the schema is REQUIRED: record() rejects events with missing fields.
EVENT_SCHEMAS: dict[str, dict[str, str]] = {
    "tool_opened": {"tool": "token", "surface": "token", "session_id": "token"},
    "tool_completed": {
        "tool": "token", "surface": "token", "session_id": "token",
        "duration_s": "seconds", "completed": "flag",
    },
    "artifact_shared": {"kind": "token", "surface": "token", "session_id": "token"},
    "onboarding_path_assigned": {
        "path_id": "token", "role_maturity": "token",
        "tech_comfort": "token", "session_id": "token",
    },
    "guide_viewed": {"guide_id": "token", "surface": "token", "session_id": "token"},
    "workflow_completed": {
        "workflow": "token", "surface": "token", "session_id": "token",
        "duration_s": "seconds",
    },
}

TOOLS = ("jd_decoder", "fit_explainer", "risk_check", "role_compare")
SURFACES = ("terminal", "web", "phone")
ARTIFACT_KINDS = ("score_card", "interview_plan", "progress_snapshot")

#: Field name -> pinned value domain. _validate() rejects any token value
#: outside its field's domain (e.g. tool="resume_uploader" is rejected).
_FIELD_DOMAINS: dict[str, tuple[str, ...]] = {
    "tool": TOOLS,
    "surface": SURFACES,
    "kind": ARTIFACT_KINDS,
}

#: General token grammar: 1-32 chars. 64 chars was ample room for
#: "john_smith_5551234567"; 32 chars plus the content screen below is not.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$")

#: session_id grammar: compact alphanumerics only. No separators means no
#: room for structured PII (dotted emails, dashed phones,
#: underscore-joined names) in the one free-form-ish field. Bare
#: digit-strings still pass the grammar on purpose — the privacy scanner is
#: the backstop that catches phone-shaped values (see the tripwire proof in
#: tests/test_i12_telemetry.py).
_SESSION_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")

#: Metrics the reporting layer refuses to compute (roadmap guardrail:
#: never optimize applications-per-day as the north star).
BANNED_METRICS = ("applications_per_day", "application_volume",
                  "submissions_per_day", "apply_count")

#: Top-level keys a state file must carry. A valid-JSON file missing any of
#: these is treated as corrupt (fail closed) rather than merged with
#: defaults — silent merging could resurrect a "clean" state over a veto.
_REQUIRED_STATE_KEYS = (
    "consent", "enabled", "events", "incidents", "clearings",
    "verifications", "paul_veto", "schema_version",
)

_GENESIS_HASH = "GENESIS"


class TelemetryError(RuntimeError):
    """Base telemetry error."""


class ConsentRequired(TelemetryError):
    """Analytics are off: no opt-in consent on record."""


class SchemaViolation(TelemetryError):
    """Event failed schema validation (unknown/missing field, bad type,
    bad token, or value outside the pinned domain)."""


class TripwireTripped(TelemetryError):
    """Content field detected: analytics shut off, data quarantined.

    Re-enable requires the clearing protocol:
    clear_incident(specialist_name, reason) -> verify_clearance(reviewer,
    evidence_ref) -> reenable(), with both actors named in the roles
    registry and no operator veto in force.
    """


class VetoInForce(TelemetryError):
    """the operator's veto is in force: recording is halted immediately."""


class CorruptStateError(TelemetryError):
    """State file or audit log failed to load or verify: fail closed.

    The corrupt file is stashed aside (``.corrupt-<timestamp>`` suffix) and
    the store refuses to start rather than silently resetting — a silent
    reset would erase vetoes, incidents, and consent history.
    """


def default_state_path() -> Path:
    d = Path.home() / ".local" / "share" / "veto"
    d.mkdir(parents=True, exist_ok=True)
    return d / "i12_telemetry.json"


def _quarantine_dir(state_path: Path) -> Path:
    """Quarantine directory, access-controlled (0700).

    Deliberately NOT called "sealed": there is no encryption, only OS file
    permissions. Do not weaken the chmod without updating the docstring,
    the manifest note, and the decision record.
    """
    q = state_path.parent / "quarantine"
    q.mkdir(parents=True, exist_ok=True)
    os.chmod(q, 0o700)
    return q


def _fresh_state() -> dict[str, Any]:
    return {
        "consent": {"opted_in": False, "history": []},
        "enabled": True,  # tripwire-controlled; consent is separate
        "events": [],
        "incidents": [],
        "clearings": [],
        "verifications": [],
        "paul_veto": None,
        "schema_version": 1,
    }


def _entry_hash(entry: dict[str, Any]) -> str:
    """Hash of a clearing-log entry (canonical JSON, excluding "hash")."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _token_looks_like_content(value: str) -> str | None:
    """Return a reason if a schema token value looks like smuggled content.

    Defense in depth behind the token grammar: token fields are enum-ish
    identifiers, so anything shaped like an email, a phone number, a
    forbidden content marker ("resume", "ssn", ...), or resume/JD prose is
    rejected here at validation time. The privacy scanner remains the
    backstop for what this screen cannot see (e.g. a bare name with no
    markers) — see the honest scanner-behavior test in
    tests/test_i12_telemetry_hardening.py.
    """
    if _EMAIL_RE.search(value):
        return "email-shaped value"
    if _PHONE_RE.search(value):
        return "phone-shaped value"
    for pat in FORBIDDEN_FIELD_PATTERNS:
        if pat.search(value):
            return f"matches forbidden content pattern {pat.pattern!r}"
    for pat in CONTENT_MARKERS:
        if pat.search(value):
            return f"matches resume/JD marker {pat.pattern!r}"
    return None


def _default_notify(state_path: Path, incident: dict[str, Any]) -> None:
    """Default out-of-band notifier: loud stderr block + a
    PENDING-NOTIFICATION marker file next to the state file.

    The default never silently swallows a trip. In a daemon context the
    operator must configure a real pager hook at store creation (see the
    module docstring runbook).
    """
    incident_id = incident["incident_id"]
    msg = (
        f"[i12-telemetry] TRIPWIRE TRIPPED ({incident_id}): "
        f"{incident['trigger']}. Analytics shut off. "
        f"Quarantine: {incident.get('quarantine_file') or 'WRITE FAILED: ' + str(incident.get('quarantine_error'))}. "
        f"Roles registry (who to page): {state_path.parent / 'i12_telemetry_roles.json'}. "
        f"Clearing protocol: clear_incident -> verify_clearance -> reenable. "
        f"See module docstring MIDNIGHT-PAGER RUNBOOK."
    )
    print(msg, file=sys.stderr)
    marker = state_path.parent / f"PENDING-NOTIFICATION-{incident_id}.json"
    marker.write_text(json.dumps({
        "incident_id": incident_id,
        "at": incident["at"],
        "trigger": incident["trigger"],
        "finding_kinds": incident["finding_kinds"],
        "quarantine_file": incident.get("quarantine_file"),
        "quarantine_error": incident.get("quarantine_error"),
        "roles_registry": str(state_path.parent / "i12_telemetry_roles.json"),
        "runbook": ("Page the contracted security/privacy specialist named in "
                    "the roles registry, then follow the clearing protocol: "
                    "clear_incident -> verify_clearance -> reenable. "
                    "The operator retains veto at every step."),
    }, indent=2))
    os.chmod(marker, 0o600)


_ROLES_TEMPLATE_NOTE = (
    "Empty registry: the operator (or ops on his explicit instruction) must populate "
    "it via set_roles(specialist, reviewer, populated_by), naming the "
    "contracted security/privacy specialist and the independent reviewer. "
    "Clearing is blocked until both roles are named with two different people. "
    "This module checks registry membership and distinctness; it cannot "
    "verify the human behind the keyboard (see TRUST BOUNDARY in the module "
    "docstring)."
)


class TelemetryStore:
    """Consent-gated analytics store with the content tripwire."""

    def __init__(self, path: Path | None = None,
                 notifier: Callable[[dict[str, Any]], None]
                          | Sequence[str] | None = None):
        self.path = Path(path) if path else default_state_path()
        if notifier is not None and not callable(notifier):
            if (isinstance(notifier, str)
                    or not isinstance(notifier, Sequence)
                    or not notifier
                    or not all(isinstance(a, str) for a in notifier)):
                raise TelemetryError(
                    "notifier must be a callable taking the incident dict, "
                    "or a non-empty argv sequence of strings (no shell)."
                )
        self._notifier = notifier
        self._state = self._load()
        self._log_seq, self._log_last_hash = self._verify_log()

    # -- persistence -----------------------------------------------------
    def _loud(self, msg: str) -> None:
        print(f"[i12-telemetry] {msg}", file=sys.stderr)

    def _stash_corrupt(self, why: str) -> Path:
        """Rename the corrupt state file aside; return the backup path."""
        backup = self.path.with_name(
            self.path.name + ".corrupt-"
            + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        )
        try:
            self.path.rename(backup)
        except OSError as exc:
            raise CorruptStateError(
                f"telemetry state is corrupt ({why}) and could not be "
                f"stashed aside: {exc}; refusing to start"
            ) from exc
        return backup

    def _load(self) -> dict[str, Any]:
        """Load state. Fail closed: a corrupt/unreadable/misshapen state
        file is stashed aside and loading RAISES — never silently reset,
        which would erase vetoes, incidents, and consent history."""
        if not self.path.exists():
            return _fresh_state()
        try:
            raw_bytes = self.path.read_bytes()
        except OSError as exc:
            backup = self._stash_corrupt("unreadable")
            self._loud(f"state file unreadable ({exc}); stashed at {backup}; "
                       f"refusing to reset silently")
            raise CorruptStateError(
                f"telemetry state unreadable; stashed at {backup}") from exc
        try:
            state = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            backup = self._stash_corrupt("unparsable")
            self._loud(f"state file is not valid JSON/UTF-8; stashed at "
                       f"{backup}; refusing to reset silently")
            raise CorruptStateError(
                f"telemetry state is not valid JSON; stashed at {backup}; "
                f"refusing to reset silently") from exc
        if not isinstance(state, dict) or any(
                k not in state for k in _REQUIRED_STATE_KEYS):
            backup = self._stash_corrupt("bad-shape")
            self._loud(f"state file has wrong shape; stashed at {backup}; "
                       f"refusing to reset silently")
            raise CorruptStateError(
                f"telemetry state has wrong shape; stashed at {backup}; "
                f"refusing to reset silently")
        return state

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    # -- tamper-evident clearing log --------------------------------------
    def _log_path(self) -> Path:
        return self.path.parent / "i12_telemetry_clearings.jsonl"

    def _verify_log(self) -> tuple[int, str]:
        """Verify the hash chain; return (next_seq, last_hash).

        Raises CorruptStateError on any tamper, gap, or malformed line —
        fail closed, like the state file.
        """
        p = self._log_path()
        last = _GENESIS_HASH
        seq = 0
        if p.exists():
            for lineno, line in enumerate(p.read_text().splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError as exc:
                    raise CorruptStateError(
                        f"clearing log line {lineno} is not JSON; refusing "
                        f"to trust the audit trail") from exc
                if not isinstance(entry, dict) or entry.get("seq") != seq \
                        or entry.get("prev") != last:
                    raise CorruptStateError(
                        f"clearing log chain broken at line {lineno} "
                        f"(seq/prev mismatch — possible tampering or "
                        f"concurrent writers); refusing to trust the audit "
                        f"trail")
                if entry.get("hash") != _entry_hash(entry):
                    raise CorruptStateError(
                        f"clearing log entry hash mismatch at line {lineno} "
                        f"(tampered); refusing to trust the audit trail")
                last = entry["hash"]
                seq += 1
        return seq, last

    def _append_log(self, entry_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """Append one entry to the hash-chained audit log.

        Permissive actions (clearing, verification, veto lift, roles,
        purge) call this BEFORE mutating state: if the audit entry cannot
        be written, the action does not happen. Restrictive actions
        (veto, trip) log best-effort after the fact — the restriction
        itself must never depend on the log being writable.
        """
        entry: dict[str, Any] = {
            "seq": self._log_seq,
            "type": entry_type,
            "at": time.time(),
            "prev": self._log_last_hash,
            "data": data,
        }
        entry["hash"] = _entry_hash(entry)
        p = self._log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        os.chmod(p, 0o600)
        self._log_seq += 1
        self._log_last_hash = entry["hash"]
        return entry

    # -- roles registry (separation of duties) ------------------------------
    def _roles_path(self) -> Path:
        return self.path.parent / "i12_telemetry_roles.json"

    def get_roles(self) -> dict[str, Any]:
        """Return the roles registry content, or the empty template.

        Never invents names: an empty/missing registry reports None for
        both roles and clearing stays blocked until a human populates it.
        """
        p = self._roles_path()
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                return data
        return {
            "security_privacy_specialist": None,
            "independent_reviewer": None,
            "populated_by": None,
            "populated_at": None,
            "note": _ROLES_TEMPLATE_NOTE,
        }

    def set_roles(self, specialist: str, reviewer: str,
                  populated_by: str) -> dict[str, Any]:
        """Populate the roles registry. A human action: the operator (or ops on his
        explicit instruction) names the contracted security/privacy
        specialist and the independent reviewer. Both must be non-empty and
        different people; the populator is recorded. Audited in the
        tamper-evident log before the registry is written."""
        s = (specialist or "").strip()
        r = (reviewer or "").strip()
        pb = (populated_by or "").strip()
        if not s:
            raise TelemetryError(
                "roles registry requires a named security/privacy specialist")
        if not r:
            raise TelemetryError(
                "roles registry requires a named independent reviewer")
        if s.casefold() == r.casefold():
            raise TelemetryError(
                "specialist and reviewer must be different people")
        if not pb:
            raise TelemetryError(
                "roles registry must record who populated it")
        self._append_log("roles_populated",
                         {"specialist": s, "reviewer": r,
                          "populated_by": pb})
        entry = {
            "security_privacy_specialist": s,
            "independent_reviewer": r,
            "populated_by": pb,
            "populated_at": time.time(),
            "note": ("Populated by a human (operator/ops). This module checks "
                     "registry membership and distinctness; it cannot verify "
                     "the human behind the keyboard."),
        }
        p = self._roles_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entry, indent=2))
        os.chmod(p, 0o600)
        return entry

    def _require_roles(self) -> tuple[str, str]:
        """Return (specialist, reviewer) or raise: clearing is blocked until
        a human populates the registry with two different named people."""
        roles = self.get_roles()
        s = roles.get("security_privacy_specialist")
        r = roles.get("independent_reviewer")
        s = str(s).strip() if s else ""
        r = str(r).strip() if r else ""
        if not s or not r:
            raise TelemetryError(
                "clearing blocked: roles registry is empty or missing — "
                "operator/ops must populate it via set_roles(specialist, "
                "reviewer, populated_by) naming the contracted "
                "security/privacy specialist and the independent reviewer")
        if s.casefold() == r.casefold():
            raise TelemetryError(
                "clearing blocked: registry names the same person as "
                "specialist and reviewer")
        return s, r

    # -- consent ----------------------------------------------------------
    def set_consent(self, opted_in: bool, source: str = "user") -> dict[str, Any]:
        rec = {"opted_in": bool(opted_in), "at": time.time(), "source": source}
        self._state["consent"]["opted_in"] = bool(opted_in)
        self._state["consent"]["history"].append(rec)
        self._save()
        return rec

    @property
    def consented(self) -> bool:
        return bool(self._state["consent"]["opted_in"])

    @property
    def enabled(self) -> bool:
        """Recording is possible only with consent, the tripwire un-tripped,
        AND no operator veto. A veto filed mid-run flips this to False
        immediately, halting collection."""
        return (bool(self._state["enabled"]) and self.consented
                and not self._state["paul_veto"])

    # -- event recording ---------------------------------------------------
    @staticmethod
    def _check_token(name: str, value: Any) -> None:
        if not isinstance(value, str):
            raise SchemaViolation(f"field {name!r} must be a string token")
        if name == "session_id":
            # Persistent pseudonymous correlator — see _SESSION_RE.
            if not _SESSION_RE.match(value):
                raise SchemaViolation(
                    "field 'session_id' must be 1-32 ASCII alphanumerics, "
                    f"got {value!r}")
            return
        if not _TOKEN_RE.match(value):
            raise SchemaViolation(
                f"field {name!r} must be a short token "
                f"(1-32 chars, [A-Za-z0-9_.:-]), got {value!r}")
        domain = _FIELD_DOMAINS.get(name)
        if domain is not None and value not in domain:
            raise SchemaViolation(
                f"field {name!r} must be one of {sorted(domain)}, "
                f"got {value!r}")
        reason = _token_looks_like_content(value)
        if reason is not None:
            raise SchemaViolation(
                f"field {name!r} rejected: {reason} (got {value!r})")

    def _validate(self, event_type: str, fields: dict[str, Any]) -> dict[str, Any]:
        schema = EVENT_SCHEMAS.get(event_type)
        if schema is None:
            raise SchemaViolation(f"unknown event type {event_type!r}")
        missing = [name for name in schema if name not in fields]
        if missing:
            raise SchemaViolation(
                f"event {event_type!r} missing required field(s): "
                + ", ".join(missing))
        clean: dict[str, Any] = {"event": event_type, "ts": time.time()}
        for name, value in fields.items():
            if name not in schema:
                raise SchemaViolation(
                    f"field {name!r} not in schema for {event_type!r}"
                )
            kind = schema[name]
            if kind == "token":
                self._check_token(name, value)
            elif kind == "flag":
                if not isinstance(value, bool):
                    raise SchemaViolation(f"field {name!r} must be bool")
            elif kind == "seconds":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise SchemaViolation(f"field {name!r} must be numeric")
                if value < 0 or value > 86400:
                    raise SchemaViolation(f"field {name!r} out of range")
            else:  # pragma: no cover - schemas are static; unknown kinds are bugs
                raise SchemaViolation(f"unknown schema kind {kind!r}")
            clean[name] = value
        return clean

    def record(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """Record one analytics event.

        Raises ConsentRequired / VetoInForce / SchemaViolation /
        TripwireTripped. A TripwireTripped leaves analytics disabled. A
        veto filed mid-run raises VetoInForce here immediately — collection
        halts, it does not drain.
        """
        if not self.consented:
            raise ConsentRequired("analytics opt-in required before recording")
        if self._state["paul_veto"]:
            raise VetoInForce(
                "the operator's veto is in force: recording halted immediately. "
                "Only the human can lift it (lift_paul_veto with his explicit "
                "confirmation).")
        if not self._state["enabled"]:
            raise TripwireTripped(
                "analytics are shut off (tripwire or incident). "
                "Complete the clearing protocol before recording."
            )
        event = self._validate(event_type, fields)
        findings = scan_payload(event)
        if findings:
            self._trip(event_type, event, findings)  # always raises
        self._state["events"].append(event)
        self._save()
        return event

    # -- the tripwire -------------------------------------------------------
    def _quarantine_write(self, incident_id: str, at: float,
                          event_type: str, event: dict[str, Any],
                          findings: list[str]) -> Path:
        """Persist the offending payload(s) for the specialist's forensic
        investigation. Access-controlled (0600), never "sealed" (no
        encryption — see module docstring)."""
        qdir = _quarantine_dir(self.path)
        qfile = qdir / f"{incident_id}.json"
        qfile.write_text(json.dumps({
            "incident_id": incident_id,
            "at": at,
            "trigger_event_type": event_type,
            # Finding *descriptions* only — never the offending values, so
            # the finding list itself cannot leak content...
            "finding_kinds": findings,
            # ...but the full payloads ARE stored below on purpose: the
            # specialist cannot investigate what was not kept. This file is
            # access-controlled (0600) and lives in a 0700 directory.
            "offending_event": event,
            "prior_live_events": list(self._state["events"]),
            "note": ("Access-controlled quarantine copy (file 0600, "
                     "directory 0700): the full triggering event payload "
                     "plus the live events present at trip time, written "
                     "for the contracted security/privacy specialist's "
                     "investigation. Do not copy elsewhere."),
        }, indent=2))
        os.chmod(qfile, 0o600)
        return qfile

    def _notify(self, incident: dict[str, Any]) -> None:
        """Invoke the out-of-band notifier. Best-effort: a notifier failure
        is loud on stderr but never un-trips the wire."""
        try:
            if self._notifier is None:
                _default_notify(self.path, incident)
            elif callable(self._notifier):
                self._notifier(incident)
            else:
                argv = [str(a) for a in self._notifier]
                subprocess.run(
                    argv + [incident["incident_id"],
                            incident.get("quarantine_file") or ""],
                    input=json.dumps(incident).encode(),
                    timeout=30, capture_output=True, check=False)
        except Exception as exc:  # noqa: BLE001 - notifier must not break _trip
            self._loud(f"out-of-band notifier failed ({exc}); the "
                       f"TripwireTripped exception to the caller and the "
                       f"incident record remain the durable signals")

    def _trip(self, event_type: str, event: dict[str, Any],
              findings: list[str]) -> None:
        """Q3 exit-gate tripwire: quarantine, shut off, file the incident,
        notify. Fail-safe: the quarantine write is attempted first, but the
        shut-off and the incident record happen even if it raises — the wire
        never fails open, and the incident is never left unrecorded because
        a write failed. Always raises TripwireTripped."""
        incident_id = f"INC-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        at = time.time()
        quarantine_file: str | None = None
        quarantine_error: str | None = None
        try:
            quarantine_file = str(self._quarantine_write(
                incident_id, at, event_type, event, findings))
        except Exception as exc:  # noqa: BLE001 - fail closed, see below
            quarantine_error = f"{type(exc).__name__}: {exc}"
            self._loud(f"QUARANTINE WRITE FAILED for {incident_id} "
                       f"({quarantine_error}); analytics are still shut off "
                       f"and the incident is still recorded")
        incident = {
            "incident_id": incident_id,
            "at": at,
            "trigger": f"content field in telemetry event {event_type!r}",
            "finding_kinds": findings,
            "quarantine_file": quarantine_file,
            "quarantine_error": quarantine_error,
            "status": "open",
        }
        self._state["incidents"].append(incident)
        # Shut off and clear the live store. In-memory first so that even a
        # failed _save() below cannot leave this process recording.
        self._state["enabled"] = False
        self._state["events"] = []
        try:
            self._save()
        except OSError as exc:
            self._loud(f"STATE SAVE FAILED during trip {incident_id} ({exc}); "
                       f"in-memory state is shut off and TripwireTripped "
                       f"still fires")
        try:
            self._append_log("incident_tripped", {
                "incident_id": incident_id,
                "trigger": incident["trigger"],
                "quarantine_file": quarantine_file,
                "quarantine_error": quarantine_error,
            })
        except Exception as exc:  # noqa: BLE001 - audit is best-effort here
            self._loud(f"audit log append failed during trip {incident_id} "
                       f"({exc}); incident is recorded in state regardless")
        self._notify(incident)
        try:
            # Remove any rotated copies next to the state file.
            for extra in self.path.parent.glob("i12_telemetry.*.bak"):
                extra.unlink()
        except OSError:
            pass
        raise TripwireTripped(
            f"Content field detected in telemetry ({incident_id}). Analytics "
            f"shut off; offending payload quarantined to "
            f"{quarantine_file or 'QUARANTINE WRITE FAILED — see incident record'} "
            f"and live store cleared. A registry-named contracted "
            f"security/privacy specialist must write a clearing entry and "
            f"the registry-named independent reviewer must verify before "
            f"re-enable. The operator retains veto."
        )

    # -- quarantine retention --------------------------------------------------
    def purge_quarantine(self, older_than_days: float = 90,
                         confirm: bool = False) -> dict[str, Any]:
        """Retention: list quarantine payloads older than N days; delete them
        only with explicit ``confirm=True``.

        Evidence for open or not-yet-verified incidents is NEVER deleted —
        it is excluded from the candidates entirely, not merely skipped at
        delete time. There is no automatic deletion: retention is always a
        deliberate human act, audited in the tamper-evident log.
        """
        if older_than_days <= 0:
            raise TelemetryError("older_than_days must be positive")
        qdir = self.path.parent / "quarantine"
        protected: set[str] = set()
        for inc in self._state["incidents"]:
            if inc.get("status") in ("open", "cleared-pending-verification"):
                qf = inc.get("quarantine_file")
                if qf:
                    protected.add(str(Path(qf).resolve()))
        cutoff = time.time() - older_than_days * 86400
        candidates: list[str] = []
        skipped_protected: list[str] = []
        skipped_fresh: list[str] = []
        if qdir.exists():
            for qf in sorted(qdir.glob("INC-*.json")):
                if str(qf.resolve()) in protected:
                    skipped_protected.append(str(qf))
                    continue
                if qf.stat().st_mtime >= cutoff:
                    skipped_fresh.append(str(qf))
                    continue
                candidates.append(str(qf))
        deleted: list[str] = []
        if confirm and candidates:
            self._append_log("quarantine_purge", {
                "deleted": candidates, "older_than_days": older_than_days})
            for c in candidates:
                try:
                    Path(c).unlink()
                    deleted.append(c)
                except OSError as exc:
                    self._loud(f"purge could not delete {c}: {exc}")
        return {
            "candidates": candidates,
            "deleted": deleted,
            "skipped_protected": skipped_protected,
            "skipped_fresh": skipped_fresh,
            "confirm": bool(confirm),
        }

    # -- clearing protocol ---------------------------------------------------
    def clear_incident(self, incident_id: str, specialist_name: str,
                       reason: str) -> dict[str, Any]:
        """Clearing entry. The specialist must be the contracted
        security/privacy specialist named in the roles registry (populated
        by operator/ops — never invented here); the reason must be substantive.
        The audit entry is written before the state mutation: if the log
        cannot record it, the clearing does not happen."""
        if not specialist_name or not specialist_name.strip():
            raise TelemetryError("clearing requires a named specialist")
        if not reason or len(reason.strip()) < 20:
            raise TelemetryError(
                "clearing requires a reason of at least 20 characters")
        specialist_reg, _reviewer_reg = self._require_roles()
        if specialist_name.strip().casefold() != specialist_reg.casefold():
            raise TelemetryError(
                "clearing specialist is not the contracted security/privacy "
                "specialist named in the roles registry")
        inc = next((i for i in self._state["incidents"]
                    if i["incident_id"] == incident_id), None)
        if inc is None:
            raise TelemetryError(f"unknown incident {incident_id!r}")
        if inc["status"] != "open":
            raise TelemetryError(f"incident {incident_id!r} is not open")
        entry = {
            "incident_id": incident_id,
            "specialist_name": specialist_name.strip(),
            "reason": reason.strip(),
            "at": time.time(),
        }
        self._append_log("clearing", entry)
        self._state["clearings"].append(entry)
        inc["status"] = "cleared-pending-verification"
        self._save()
        return entry

    def verify_clearance(self, incident_id: str, reviewer_name: str,
                         evidence_ref: str) -> dict[str, Any]:
        """Independent reviewer verifies the clearing evidence.

        The reviewer must be the independent reviewer named in the roles
        registry and a different person from the clearing specialist; the
        evidence reference must be non-empty; an incident is verified at
        most once. The audit entry is written before the state mutation.
        """
        if not reviewer_name or not reviewer_name.strip():
            raise TelemetryError("verification requires a named reviewer")
        if not evidence_ref or not str(evidence_ref).strip():
            raise TelemetryError(
                "verification requires a non-empty evidence_ref")
        _specialist_reg, reviewer_reg = self._require_roles()
        if reviewer_name.strip().casefold() != reviewer_reg.casefold():
            raise TelemetryError(
                "verifier is not the independent reviewer named in the "
                "roles registry")
        if any(v.get("incident_id") == incident_id
               for v in self._state["verifications"]):
            raise TelemetryError(
                f"incident {incident_id!r} has already been verified")
        clearing = next((c for c in self._state["clearings"]
                         if c["incident_id"] == incident_id), None)
        if clearing is None:
            raise TelemetryError(
                f"no clearing entry for incident {incident_id!r}")
        if clearing["specialist_name"].strip().casefold() \
                == reviewer_name.strip().casefold():
            raise TelemetryError(
                "reviewer must be independent of the clearing specialist")
        rec = {
            "incident_id": incident_id,
            "reviewer_name": reviewer_name.strip(),
            "evidence_ref": str(evidence_ref).strip(),
            "at": time.time(),
        }
        self._append_log("verification", rec)
        self._state["verifications"].append(rec)
        inc = next(i for i in self._state["incidents"]
                   if i["incident_id"] == incident_id)
        inc["status"] = "verified"
        self._save()
        return rec

    def paul_veto(self, reason: str) -> None:
        """The operator retains veto authority: halts recording immediately and
        blocks re-enable. A reason is required — a veto without one is not
        recorded. The veto itself is never blocked by a failing audit log:
        the restriction is applied first, the log entry best-effort after.
        """
        if not reason or not reason.strip():
            raise TelemetryError("a veto requires a reason")
        self._state["paul_veto"] = {"reason": reason.strip(),
                                    "at": time.time()}
        self._save()
        try:
            self._append_log("veto", {"reason": reason.strip()})
        except Exception as exc:  # noqa: BLE001 - veto stands regardless
            self._loud(f"audit log append failed for veto ({exc}); "
                       f"the veto is recorded in state regardless")

    def lift_paul_veto(self, confirmation: str | None = None) -> None:
        """Lift the operator's veto. Only the human performs this, in a human session —
        it is not a bare call: he must type an explicit confirmation (at
        least 12 characters), which is recorded verbatim in the
        tamper-evident audit log alongside the lifted veto's reason.

        This is a speed bump plus an immutable audit trail, not identity
        proof: the module cannot verify that the typist is the operator (see TRUST
        BOUNDARY in the module docstring).
        """
        veto = self._state["paul_veto"]
        if not veto:
            raise TelemetryError("no operator veto is in force")
        if not isinstance(confirmation, str) or len(confirmation.strip()) < 12:
            raise TelemetryError(
                "lifting a veto requires an explicit human confirmation "
                "(at least 12 characters), typed by the operator — a bare "
                "lift_paul_veto() call is refused")
        self._append_log("veto_lifted", {
            "veto_reason": veto["reason"],
            "veto_at": veto["at"],
            "confirmation": confirmation.strip(),
        })
        self._state["paul_veto"] = None
        self._save()

    def reenable(self) -> None:
        """Re-enable analytics after the full clearing protocol."""
        if self._state["paul_veto"]:
            raise TelemetryError(
                "the operator's veto is in force; analytics cannot be re-enabled.")
        open_incs = [i for i in self._state["incidents"]
                     if i["status"] == "open"]
        if open_incs:
            raise TelemetryError(
                f"{len(open_incs)} incident(s) still open; clear them first.")
        pending = [i for i in self._state["incidents"]
                   if i["status"] == "cleared-pending-verification"]
        if pending:
            raise TelemetryError(
                f"{len(pending)} incident(s) cleared but not verified.")
        if not self.consented:
            raise ConsentRequired("opt-in consent required before re-enable")
        self._state["enabled"] = True
        self._save()

    # -- reporting (qualified activation, never vanity) -----------------------
    def report(self, metric: str) -> dict[str, Any]:
        """Aggregate reporting. Banned vanity metrics raise TelemetryError."""
        if metric in BANNED_METRICS:
            raise TelemetryError(
                f"metric {metric!r} is banned by the roadmap guardrail: never "
                "optimize applications-per-day as the north star."
            )
        events = self._state["events"]
        if metric == "tool_opens_by_tool":
            out: dict[str, int] = {t: 0 for t in TOOLS}
            for e in events:
                if e["event"] == "tool_opened" and e.get("tool") in out:
                    out[e["tool"]] += 1
            return {"metric": metric, "counts": out}
        if metric == "workflow_completion_rate":
            opened = sum(1 for e in events if e["event"] == "tool_opened")
            done = sum(1 for e in events
                       if e["event"] == "tool_completed" and e.get("completed"))
            return {"metric": metric, "opened": opened, "completed": done,
                    "rate": (done / opened) if opened else None}
        if metric == "qualified_activation":
            # Sessions with >=1 completed workflow AND >=1 shared artifact:
            # the roadmap's "qualified activation", not raw traffic.
            by_session: dict[str, set[str]] = {}
            for e in events:
                by_session.setdefault(e.get("session_id", "?"), set()).add(e["event"])
            qualified = sum(
                1 for evts in by_session.values()
                if "workflow_completed" in evts and "artifact_shared" in evts
            )
            return {"metric": metric,
                    "sessions": len(by_session),
                    "qualified_sessions": qualified}
        if metric == "guide_views":
            out = {}
            for e in events:
                if e["event"] == "guide_viewed":
                    out[e.get("guide_id", "?")] = out.get(e.get("guide_id", "?"), 0) + 1
            return {"metric": metric, "counts": out}
        raise TelemetryError(f"unknown metric {metric!r}")

    def public_payload(self) -> dict[str, Any]:
        """Public-safe aggregate payload. Contains counts only — provably no
        content fields (scan_payload-clean by construction)."""
        payload = {
            "schema": "i12-public-analytics-v1",
            "tool_opens": self.report("tool_opens_by_tool")["counts"],
            "workflow_completion": self.report("workflow_completion_rate"),
            "qualified_activation": self.report("qualified_activation"),
            "guide_views": self.report("guide_views")["counts"],
        }
        findings = scan_payload(payload)
        if findings:  # pragma: no cover - defensive; construction prevents this
            raise ContentDetected("public analytics payload", findings)
        return payload

    def status(self) -> dict[str, Any]:
        return {
            "consented": self.consented,
            "enabled": self.enabled,
            "events_stored": len(self._state["events"]),
            "open_incidents": sum(1 for i in self._state["incidents"]
                                  if i["status"] == "open"),
            "paul_veto": self._state["paul_veto"],
            # The wire is armed exactly when recording is possible: there
            # is something to trip. Never hardcoded.
            "tripwire_armed": self.enabled,
        }
