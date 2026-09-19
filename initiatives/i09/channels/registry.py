"""Epic 3 — explicit opt-in communication channels.

Gmail plus user-selected WhatsApp, Discord, Messenger, or another approved
channel through explicit skills. User-selected and explicit ONLY:

* No channel is ever enabled by default, by upgrade, or by implication.
* ``enable_channel`` requires an explicit ``confirm=True`` from the user
  (the CLI wizard / web UI passes the user's own confirmation through).
* Every enable/disable is appended to a local-first audit log (JSONL),
  written 0o600 with fsync, HMAC-signed with a machine-local key, and read
  back with integrity verification. Tampered or malformed entries are
  loudly logged and never trusted (fail closed).
* Every channel carries a full ConnectorManifest: what it can do, why it
  may be blocked, what data it sends, which actions require confirmation.
* WhatsApp / Discord / Messenger are declared as explicit-skill channels:
  they do nothing until the user both selects the channel AND connects the
  corresponding skill. Veto stores no credentials for them.

The Gmail adapter delegates to the existing ``email_sync`` module; this
registry adds the consent layer and the interactive send-approval boundary
around it.

Interactive send approval (the send boundary)
---------------------------------------------
A caller-asserted boolean can never authorize a send. Every send — through
``GmailChannel.send_followup`` OR through ``email_sync.send_followup``
directly — must pass ``authorize_send`` here first:

1. APPROVE (interactive): ``request_send_approval(draft, expected_value=...)``
   presents the ACTUAL recipient, subject, and body to the user on the
   terminal and requires them to type ``expected_value`` — a value that
   VARIES per send (e.g. the recipient address as shown). A fixed string
   such as "send" FAILS: the typed answer is compared only against the
   per-send expected value (legal exposure reduction, spec §6.2). The display is SANITIZED — ANSI
   escape sequences are stripped and every other control character
   (including newlines) is replaced with a visible placeholder — so a
   hostile draft cannot rewrite the terminal (e.g. overwriting the
   displayed To: line with cursor-movement escapes). Sanitization is
   display-only: the approval record binds the TRUE content bytes.
   Only a genuine interactive approval writes an approval record to the
   HMAC-signed consent log. The record binds ``nonce +
   content_hash(to/subject/body) + timestamp`` — approval evidence is
   bound to a human approval EVENT, not to content alone.
2. SEND: ``authorize_send`` accepts ONLY (a) an ``approval_id`` naming a
   valid, fresh, unconsumed approval record for the EXACT draft being sent,
   or (b) a fresh unconsumed approval record already on file for that exact
   content. Otherwise — including when the caller passes ``confirm=True``
   or a self-minted token — it falls back to the interactive prompt when a
   real terminal is attached (stdin AND stdout both TTYs: stdin carries the
   typed answer, stdout carries the draft display — stdin-TTY with stdout
   piped away is NOT interactive, fail closed), and REFUSES when there is
   none (agents, scripts, MCP servers: fail closed, no user to ask).
3. CLAIM (atomic): ``authorize_send`` validates AND consumes the approval
   as one atomic step under the cross-process consent-store lock — two
   processes cannot both validate the same approval before either
   consumption lands (the loser fails closed with
   ``approval_already_used``). The atomicity holds EXACTLY when the
   ``approval_consumed`` marker write persists: if the marker cannot be
   written (disk full, read-only file), the claim FAILS —
   ``authorize_send`` refuses with ``approval_consume_failed`` and never
   reports success, and the in-process consumption mark is rolled back so
   a retry can claim the approval again. A failed claim is never a spent
   approval: no double-spend. ``consume_approval`` remains for the
   transport layer and is idempotent. Replays, content-substituted
   drafts, and expired approvals (TTL 15 minutes) are refused.

``draft_approval_token`` remains as a pure CONTENT DIGEST (deterministic
SHA-256 of the canonicalized draft) for display and reconciliation. It is
NOT proof of approval and the send path never accepts it as such — anyone
can compute it, which is exactly why it authorizes nothing.

``confirm=True`` (registry and ``email_sync``) is DEPRECATED as an
authorization signal: it is now a no-op that still routes through the
interactive approval above. It is kept in signatures only so existing
callers fail closed with instructions instead of a TypeError.

Recovery: if the machine-local integrity key is lost, every stored
consent/approval entry becomes unverifiable and the registry fails closed
(channels read as disabled, sends refused). The re-keying procedure is
documented in ``RECOVERY.md`` next to this module; the key-loss ERROR log
points at it.

VETO STATUS: this module was reworked under Rule 1 review case
``init-09-comms-rereview`` (KICK_BACK re-raised: self-mintable token +
caller-asserted confirm + direct-call bypass). The rework implements the
interactive send boundary described above. The veto still needs the operator's
explicit clearance after re-review — disclosure does not clear it.
See ``DECISIONS.md`` for the mechanism choice record.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from providers._contract import ConnectorManifest

log = logging.getLogger("veto-mcp.i09.channels")


def _resolve_base_dir() -> Path:
    """Repo root for this registry — fails loudly on an unexpected layout.

    The consent store must never be silently written "somewhere else" if
    this module is relocated; if the on-disk layout does not match the
    expected ``<root>/initiatives/i09/channels/registry.py`` shape, raise
    at import time rather than guessing.
    """
    here = Path(__file__).resolve()
    base = here.parent.parent.parent.parent  # channels/i09/initiatives/<root>
    expected = (base / "initiatives" / "i09" / "channels" / "registry.py").resolve()
    if expected != here:
        raise RuntimeError(
            "channels.registry: unexpected module layout — expected "
            f"{expected}, this file is {here}. Refusing to resolve the "
            "consent-store location rather than writing it elsewhere."
        )
    return base


BASE_DIR = _resolve_base_dir()
CONSENT_LOG = BASE_DIR / "channel_consent.jsonl"

# The opt-in source must be enumerable so the audit log distinguishes a
# genuine wizard/UI opt-in from a hardcoded string.
VALID_VIA_SOURCES = frozenset({"cli-wizard", "web-ui", "api"})

# Interactive approvals expire this long after the user typed the
# per-action varying value. Short enough that a stale approval cannot
# be weaponized later; long enough for a wizard preview → approve →
# send round-trip.
APPROVAL_TTL_SECONDS = 15 * 60

# The approval prompt abandons after this long without an answer: a user
# who walks away mid-prompt must not hang the process forever.
APPROVAL_PROMPT_TIMEOUT_SECONDS = 5 * 60

RECOVERY_DOC = "initiatives/i09/channels/RECOVERY.md"


def _consent_log() -> Path:
    return CONSENT_LOG


def _consent_key_path() -> Path:
    # Sibling of the consent log so tests that redirect CONSENT_LOG into a
    # temp dir get an isolated key automatically.
    return _consent_log().with_name("channel_consent.key")


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _normalize_channel(channel: str | None) -> str:
    """Canonical channel name: the store records lowercase.

    Every entry point that binds, validates, or consumes a channel name
    normalizes through here so ``authorize_send(channel="Gmail")`` matches
    an approval recorded for ``"gmail"``.
    """
    return (channel or "").strip().lower()


# ---------------------------------------------------------------------------
# Cross-process consent-store lock
# ---------------------------------------------------------------------------

_lock_state = threading.local()


def _consent_lock_path() -> Path:
    return _consent_log().with_name("channel_consent.lock")


@contextlib.contextmanager
def _consent_store_lock():
    """Exclusive cross-process lock for consent-store read-modify-write.

    Locking contract (read-modify-write MUST hold this; plain reads need
    not — ``os.replace`` is atomic and a torn concurrent read fails
    closed as a malformed line):

    * ``_append_consent`` holds it around every write, so a concurrent
      append can never slip between ``quarantine_consent_log``'s read and
      its atomic replace and be silently lost.
    * ``quarantine_consent_log`` holds it from the initial read through
      the replace (plus the audit append and re-verification).
    * ``authorize_send`` holds it across validate-then-consume, so two
      processes cannot both claim one approval.

    The lock is an ``fcntl.flock`` (LOCK_EX) on a sibling lock file, so
    it works across processes. It is NOT reentrant: nesting it would
    deadlock on the second flock, so reentry raises loudly — internal
    ``*_locked`` helpers assume the caller already holds it.
    """
    if getattr(_lock_state, "held", False):
        raise RuntimeError(
            "consent-store lock is not reentrant: use the *_locked "
            "helpers while holding _consent_store_lock()"
        )
    lock_path = _consent_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise OSError(f"cannot open consent-store lock: {exc}") from exc
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        _lock_state.held = True
        try:
            yield
        finally:
            _lock_state.held = False
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Terminal display sanitization (approval prompt)
# ---------------------------------------------------------------------------

# ANSI escape sequences: OSC (title etc.), CSI (cursor movement, erase,
# color), and charset/keypad selections. Stripped BEFORE the generic
# control-character replacement below — replacing the ESC byte alone
# would leave visible "[2A" residue that still misleads.
_ANSI_SEQUENCE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL / ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b[()][0-9A-B]"  # charset selection
    r"|\x1b[=>]"  # keypad application/numeric
)


def _sanitize_display(value: str) -> str:
    """Make a string safe to print on the terminal for user approval.

    Strips ANSI escape sequences outright, then replaces every remaining
    control character — newlines included — with a visible placeholder,
    so a hostile draft cannot rewrite the approval display (cursor-up
    overwriting the To: line, or a raw newline injecting a fake second
    To: line). DISPLAY ONLY: the underlying draft bytes are never
    modified — the approval record and the sent payload keep the true
    content.
    """
    text = _ANSI_SEQUENCE_RE.sub("", str(value))
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif unicodedata.category(ch).startswith("C") or ord(ch) == 0x7F:
            out.append("\ufffd")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Machine-local integrity key (generated once, 0o600, never hardcoded)
# ---------------------------------------------------------------------------


def _load_or_create_key() -> bytes:
    """Return the machine-local HMAC key, generating it once if needed.

    The key is random per machine, stored 0o600 next to the consent log,
    and never appears in code. Losing the key invalidates old entries on
    read (fail closed), which is the safe direction — see RECOVERY.md for
    the re-keying procedure.
    """
    key_path = _consent_key_path()
    try:
        raw = key_path.read_bytes()
        if len(raw) < 32:
            raise ValueError(f"consent key too short ({len(raw)} bytes)")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.error("Consent key unreadable (%s); refusing to write.", exc)
        raise OSError(f"consent key unreadable: {exc}") from exc
    else:
        # The key file already exists. This function creates it 0o600, but
        # a key that arrived by other means (backup restore, manual copy)
        # may be world/group-readable — anyone on the machine could then
        # forge consent entries. Refuse to operate rather than use it.
        mode = stat.S_IMODE(key_path.stat().st_mode)
        if mode & 0o077:
            msg = (
                f"consent key {key_path} is accessible beyond its owner "
                f"(mode 0o{mode:o}); refusing to operate. Fix: "
                f"chmod 600 {key_path}"
            )
            log.error("CONSENT INTEGRITY: %s", msg)
            raise OSError(msg)
        return raw

    key = secrets.token_bytes(32)
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create consent dir: {exc}") from exc
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lost the race with another process; read the winner's key.
        return _load_or_create_key()
    except OSError as exc:
        raise OSError(f"cannot create consent key: {exc}") from exc
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key


def rotate_consent_key() -> dict[str, Any]:
    """Rotate the machine-local integrity key (recovery operation).

    Archives the current key next to the consent log and forces a fresh
    key to be generated on next use. THIS INVALIDATES EVERYTHING SIGNED
    WITH THE OLD KEY: all channel consents read as disabled and all
    pending send approvals are void until the user re-enables channels
    and re-approves sends. That is the documented, fail-closed recovery
    path — see RECOVERY.md.

    The rotation itself is recorded as a signed ``consent_key_rotated``
    audit entry (under the NEW key), so the audit trail shows when the
    key changed and where the backup went.
    """
    key_path = _consent_key_path()
    backup: str | None = None
    try:
        with _consent_store_lock():
            if key_path.exists():
                backup = str(
                    key_path.with_name(
                        f"channel_consent.key.{int(time.time())}.bak"
                    )
                )
                os.rename(key_path, backup)
                os.chmod(backup, 0o600)
    except OSError as exc:
        log.error("Key rotation failed: %s", exc)
        return {"ok": False, "error": f"key_rotation_failed: {exc}"}
    log.warning(
        "Consent integrity key rotated (backup: %s). All prior consent and "
        "approval entries are now unverifiable by design; channels must be "
        "re-enabled by the user. See %s.",
        backup, RECOVERY_DOC,
    )
    result: dict[str, Any] = {
        "ok": True,
        "backup": backup,
        "warning": (
            "All existing consent and send-approval entries are now "
            "unverifiable and treated as absent (fail closed). Re-enable "
            f"channels explicitly. Full procedure: {RECOVERY_DOC}."
        ),
    }
    try:
        with _consent_store_lock():
            _load_or_create_key()  # force the fresh key into existence now
            _append_consent_locked(
                {"action": "consent_key_rotated", "key_backup": backup}
            )
    except OSError as exc:
        # The rotation already happened (the old key is archived); only
        # the audit entry failed. Report it loudly rather than failing
        # the rotation result dishonestly.
        log.error("Key rotation audit entry NOT recorded: %s", exc)
        result["audit_warning"] = (
            f"rotation_audit_write_failed: {exc} — the rotation happened, "
            "but no signed audit entry was recorded."
        )
    return result


# ---------------------------------------------------------------------------
# Channel declarations
# ---------------------------------------------------------------------------


@dataclass
class ChannelSpec:
    """Static declaration for one channel."""

    name: str
    skill: str  # connected skill that powers it, "" if built-in
    manifest: ConnectorManifest
    enabled_by_default: bool = False  # invariant: always False


def _gmail_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        name="gmail",
        connector_type="channel",
        what_it_can_do=[
            "Scan the connected Gmail account for recruiter replies and "
            "classify them (delegates to email_sync).",
            "Draft follow-up emails from application records (draft only).",
            "Send a reviewed follow-up draft — only after a genuine "
            "interactive user approval bound to the exact draft content "
            "(see request_send_approval).",
        ],
        why_it_may_be_blocked=[
            "Gmail is not connected (no OAuth grant / CLI auth).",
            "Read-only triage needs the gmail skill's search scope; sending "
            "needs its send scope.",
            "No interactive user approval for this exact draft (refused "
            "without a TTY to ask on).",
            "The approval record is missing, expired, already used, or bound "
            "to different content.",
        ],
        what_data_it_sends=[
            "Gmail search queries (recruiter-reply triage) to Google.",
            "Message IDs of opened messages to fetch their bodies.",
            "On interactively-approved send only: the approved draft's "
            "to/subject/body to Gmail's send endpoint.",
        ],
        what_requires_confirmation=[
            "Enabling the channel at all (explicit user opt-in).",
            "Every send: an interactive user approval (typed at the "
            "terminal) bound to the exact draft content. Caller-asserted "
            "confirm flags and self-computed tokens are NOT sufficient.",
            "Stage updates proposed from classified replies are "
            "proposal-only until the user confirms.",
        ],
        official_interface="Gmail API via the connected gmail skill",
        interface_basis="User's own OAuth grant; Veto stores no password.",
    )


def _skill_channel_manifest(
    name: str, skill: str, description: str
) -> ConnectorManifest:
    return ConnectorManifest(
        name=name,
        connector_type="channel",
        what_it_can_do=[
            f"{description} — once the user selects this channel AND "
            f"connects the '{skill}' skill.",
            "Today: declaration only. No messages are read or sent until "
            "the skill is connected and the channel is explicitly enabled.",
        ],
        why_it_may_be_blocked=[
            f"The '{skill}' skill is not connected.",
            "The channel was never explicitly enabled (it never auto-"
            "enables).",
            "No approved message template exists for job-search outreach "
            "on this channel yet.",
        ],
        what_data_it_sends=[
            "Nothing today: without a connected skill and explicit "
            "enablement, this channel performs no network calls.",
        ],
        what_requires_confirmation=[
            f"Connecting the '{skill}' skill.",
            "Enabling the channel (explicit opt-in).",
            "Every outbound message, individually, at send time — via an "
            "interactive user approval.",
        ],
        official_interface=f"{skill} skill (explicit)",
        interface_basis="Explicit user selection + explicit skill only.",
    )


CHANNELS: dict[str, ChannelSpec] = {}


def _register(spec: ChannelSpec) -> None:
    # Explicit raise (not assert): asserts vanish under `python -O`, and
    # this invariant is load-bearing.
    if spec.enabled_by_default is not False:
        raise ValueError(
            f"channel {spec.name!r} must never be enabled by default"
        )
    CHANNELS[spec.name] = spec


_register(ChannelSpec(name="gmail", skill="gmail", manifest=_gmail_manifest()))
_register(
    ChannelSpec(
        name="whatsapp",
        skill="whatsapp",
        manifest=_skill_channel_manifest(
            "whatsapp", "whatsapp", "WhatsApp messaging"
        ),
    )
)
_register(
    ChannelSpec(
        name="discord",
        skill="discord",
        manifest=_skill_channel_manifest("discord", "discord", "Discord messaging"),
    )
)
_register(
    ChannelSpec(
        name="messenger",
        skill="messenger",
        manifest=_skill_channel_manifest(
            "messenger", "messenger", "Messenger messaging"
        ),
    )
)


# ---------------------------------------------------------------------------
# Consent store (append-only, local-first, integrity-protected)
# ---------------------------------------------------------------------------


def _append_consent(entry: dict[str, Any]) -> None:
    """Append one consent entry. Raises OSError on ANY write failure.

    Entries are HMAC-signed with the machine-local key, written 0o600,
    fsync'd before close. The write holds the cross-process
    consent-store lock (see ``_consent_store_lock``) so it can never slip
    between another process's quarantine read and rewrite and be silently
    lost. Callers translate failures into failed operations — a consent
    write that did not land must never be reported as success.
    """
    with _consent_store_lock():
        _append_consent_locked(entry)


def _append_consent_locked(entry: dict[str, Any]) -> None:
    """Append one consent entry; the caller must hold ``_consent_store_lock``."""
    body = {"ts": time.time(), **entry}
    key = _load_or_create_key()
    body["mac"] = hmac.new(
        key, _canonical_bytes(body), hashlib.sha256
    ).hexdigest()
    line = (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")

    path = _consent_log()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create consent dir: {exc}") from exc
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as exc:
        raise OSError(f"cannot open consent log: {exc}") from exc
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, line)
        os.fsync(fd)
    except OSError as exc:
        raise OSError(f"cannot write consent log: {exc}") from exc
    finally:
        os.close(fd)


def _verify_entry(raw: Any, key: bytes | None) -> tuple[dict[str, Any] | None, str]:
    """Return (entry, problem). problem == "" means verified OK."""
    if not isinstance(raw, dict):
        return None, "not an object"
    mac = raw.get("mac")
    if not isinstance(mac, str):
        return None, "missing signature"
    if key is None:
        return None, "no key available to verify signature"
    body = {k: v for k, v in raw.items() if k != "mac"}
    expected = hmac.new(key, _canonical_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return None, "signature mismatch (tampered or foreign key)"
    return raw, ""


def _read_consent() -> tuple[list[dict[str, Any]], bool]:
    """Read and verify the consent log.

    Returns (verified_entries, integrity_ok). Malformed lines and
    tampered entries are LOUDLY logged (ERROR) and never trusted;
    integrity_ok is False when anything at all failed verification, which
    callers treat as fail-closed.
    """
    path = _consent_log()
    if not path.exists():
        return [], True
    entries: list[dict[str, Any]] = []
    integrity_ok = True
    try:
        key: bytes | None = _load_or_create_key()
    except OSError as exc:
        log.error(
            "CONSENT INTEGRITY: cannot load machine key (%s); "
            "treating existing consent data as unverifiable — all channels "
            "read as disabled and pending send approvals are void. "
            "Recovery procedure: %s.",
            exc, RECOVERY_DOC,
        )
        return [], False
    try:
        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    integrity_ok = False
                    log.error(
                        "CONSENT INTEGRITY: malformed JSON on line %d of %s; "
                        "line ignored, store treated as untrusted.",
                        lineno, path,
                    )
                    continue
                entry, problem = _verify_entry(raw, key)
                if entry is None:
                    integrity_ok = False
                    log.error(
                        "CONSENT INTEGRITY: line %d of %s rejected (%s); "
                        "entry ignored, store treated as untrusted.",
                        lineno, path, problem,
                    )
                    continue
                entries.append(entry)
    except OSError as exc:
        log.error("CONSENT INTEGRITY: cannot read consent log: %s", exc)
        return [], False
    return entries, integrity_ok


def _sign_quarantine_record(
    record: dict[str, Any], key: bytes
) -> dict[str, Any]:
    """HMAC-sign one quarantine record with the machine-local key.

    Same scheme as consent entries (``mac`` over the canonical bytes of
    the body): tampering with the quarantine file is detectable with
    ``_verify_entry``. The quarantine file is the audit trail — it must
    be as tamper-evident as the log it archives.
    """
    record = dict(record)
    record["mac"] = hmac.new(
        key, _canonical_bytes(record), hashlib.sha256
    ).hexdigest()
    return record


def _quarantine_seen_keys(qpath: Path) -> set[tuple[Any, Any]]:
    """``(line_no, original_line)`` pairs already in the quarantine file.

    Dedup key for the repair: if a previous quarantine run moved the
    lines but then failed to rewrite the log, the re-run must not append
    the same records a second time.
    """
    seen: set[tuple[Any, Any]] = set()
    if not qpath.exists():
        return seen
    try:
        with qpath.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seen.add((rec.get("line_no"), rec.get("original_line")))
    except OSError as exc:
        log.warning(
            "QUARANTINE: cannot read %s for dedup (%s); duplicates possible.",
            qpath, exc,
        )
    return seen


def quarantine_consent_log(*, force: bool = False) -> dict[str, Any]:
    """Audit-preserving repair for a consent store that failed verification.

    A single malformed/torn line (or a tampered entry) sets the whole
    store's ``integrity_ok`` False and bricks every channel (fail closed)
    — this is the repair path that restores operation WITHOUT deleting
    anything:

    1. Every line is re-verified against the machine-local key.
    2. Unverifiable lines are MOVED to ``channel_consent.quarantine.jsonl``
       (0o600, next to the log) as quarantine records carrying the line's
       ORIGINAL BYTES verbatim plus reason, line number, and timestamp —
       nothing is silently deleted; the audit trail is the feature. Each
       record is HMAC-signed with the machine key (tamper-evident), and
       records already present from a previous run are NOT duplicated.
    3. The consent log is rewritten with only the verified entries
       (atomic replace, 0o600, fsync).
    4. A signed ``consent_quarantine`` audit entry is appended to the log
       naming the quarantine file and the quarantined count, so the
       repair itself is visible in ``consent_history``. If THAT append
       fails, the repair is complete but unaudited: the result is
       ``ok: False`` with ``error: quarantine_audit_write_failed`` and
       instructions to re-audit via ``record_quarantine_audit`` — never
       re-run the full repair, which would report "nothing to
       quarantine".
    5. The repaired log is re-verified; the result reports whether the
       store reads clean again.

    Concurrency: the cross-process consent-store lock
    (``_consent_store_lock``) is held from the initial read through the
    atomic replace, and every ``_append_consent`` takes the same lock —
    so an entry appended by another process can never slip between the
    read and the rewrite and be silently lost.

    Fresh-key safeguard: when EVERY line fails verification (``good`` is
    empty), the log looks exactly like a lost/replaced key rather than a
    torn line — quarantining would archive all entries permanently and
    they could never be re-trusted. Without ``force=True`` the repair is
    REFUSED loudly, advising the operator to restore the key backup
    first. Pass ``force=True`` (CLI: ``veto channel repair --force``)
    only when the key is certainly gone for good (e.g. after a deliberate
    rotation).

    NOTE: after a key rotation / re-key (RECOVERY.md), every old entry
    fails verification — quarantine is the intended cleanup for that
    case too: it archives the cryptographically-dead entries and leaves
    the post-rotation store clean. Any channel whose own ``enable``
    entry was quarantined must be explicitly re-enabled by the user.
    """
    path = _consent_log()
    key_path = _consent_key_path()
    if not path.exists():
        return {
            "ok": True,
            "quarantined": 0,
            "quarantine_file": None,
            "integrity_ok": True,
            "reasons": {},
            "note": "No consent log exists; nothing to quarantine.",
        }
    try:
        key = _load_or_create_key()
    except OSError as exc:
        log.error(
            "QUARANTINE: consent key unavailable (%s); cannot verify "
            "entries to separate them. Recovery: %s.", exc, RECOVERY_DOC,
        )
        return {
            "ok": False,
            "quarantined": 0,
            "quarantine_file": None,
            "integrity_ok": False,
            "reasons": {},
            "error": f"consent_key_unavailable: {exc}",
            "instructions": (
                "The consent key is unreadable, so entries cannot be "
                f"verified. Follow the key-loss procedure in {RECOVERY_DOC} "
                "(re-key first), then run quarantine_consent_log() again "
                "to clean up the now-dead entries."
            ),
        }
    try:
        lock = _consent_store_lock()
    except OSError as exc:
        log.error("QUARANTINE: consent-store lock unavailable: %s", exc)
        return {
            "ok": False,
            "quarantined": 0,
            "quarantine_file": None,
            "integrity_ok": False,
            "reasons": {},
            "error": f"quarantine_lock_failed: {exc}",
            "instructions": (
                "The consent-store lock could not be acquired — another "
                "process may be mid-repair or mid-write. No lines were "
                "moved and the log was not modified. Wait briefly and "
                "re-run quarantine_consent_log()."
            ),
        }
    # NOTE: _consent_store_lock is a @contextmanager, so a real
    # lock-acquisition failure raises at with-entry, not at construction.
    # The construction-time try/except above covers the mocked failure
    # mode; this with-entry try/except covers the real one. Either way
    # the documented quarantine_lock_failed result is returned — a lock
    # failure never propagates as an exception.
    try:
        with lock:
            try:
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError as exc:
                return {
                    "ok": False,
                    "quarantined": 0,
                    "quarantine_file": None,
                    "integrity_ok": False,
                    "reasons": {},
                    "error": f"consent_log_unreadable: {exc}",
                    "instructions": (
                        "The consent log could not be read, so no lines were "
                        "classified or moved. Resolve the filesystem error and "
                        "re-run quarantine_consent_log()."
                    ),
                }
            good: list[str] = []
            bad: list[tuple[int, str, str]] = []  # (line_no, reason, orig bytes)
            for lineno, line in enumerate(lines, 1):
                raw = line.rstrip("\n")
                if not raw.strip():
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    bad.append((lineno, "malformed_json", raw))
                    continue
                entry, problem = _verify_entry(parsed, key)
                if entry is None:
                    bad.append((lineno, problem, raw))
                    continue
                good.append(raw + "\n")
            reasons: dict[str, int] = {}
            for _lineno, reason, _raw in bad:
                reasons[reason] = reasons.get(reason, 0) + 1
            if not bad:
                return {
                    "ok": True,
                    "quarantined": 0,
                    "quarantine_file": None,
                    "integrity_ok": True,
                    "reasons": reasons,
                    "note": "Every line verified; nothing to quarantine.",
                }
            if not good and not force:
                # Wholesale-unverifiable log: this is what a lost/replaced
                # key looks like, not a torn line. Archiving everything now
                # would permanently destroy the chance to re-trust the log
                # by restoring the key backup — refuse without explicit
                # confirmation.
                backups = sorted(
                    key_path.parent.glob("channel_consent.key.*.bak")
                )
                log.error(
                    "QUARANTINE REFUSED: every line in %s failed verification "
                    "(%d line(s)) — this looks like a lost or replaced "
                    "integrity key, NOT a torn line. Restoring the key backup "
                    "re-trusts the whole log with no data movement; "
                    "quarantining now would archive ALL entries permanently "
                    "(they can never be re-trusted). See %s.",
                    path, len(bad), RECOVERY_DOC,
                )
                backup_advice = (
                    "Key backups found next to the log: "
                    + ", ".join(str(b) for b in backups)
                    + ". Restore the right one over channel_consent.key "
                    "instead of quarantining."
                    if backups
                    else "No key backups were found next to the log — if the "
                    "key was never backed up, the old entries cannot be "
                    "re-trusted by anyone."
                )
                return {
                    "ok": False,
                    "quarantined": 0,
                    "quarantine_file": None,
                    "integrity_ok": False,
                    "reasons": reasons,
                    "error": "quarantine_requires_confirmation",
                    "key_backups_found": [str(b) for b in backups],
                    "instructions": (
                        f"Refused: all {len(bad)} line(s) in the consent log "
                        "failed verification, which looks like a lost or "
                        "replaced integrity key rather than a torn line. "
                        + backup_advice + " Quarantining now would archive "
                        "every entry permanently — quarantined entries can "
                        "never be re-trusted. If you are certain the key is "
                        "gone for good (e.g. after a deliberate rotation with "
                        "no backup to restore), re-run with force=True (CLI: "
                        f"`veto channel repair --force`). Procedure: {RECOVERY_DOC}."
                    ),
                }

            qpath = path.with_name("channel_consent.quarantine.jsonl")
            ts = time.time()
            records = []
            for lineno, reason, raw in bad:
                records.append(
                    _sign_quarantine_record(
                        {
                            "quarantined_ts": ts,
                            "log_file": str(path),
                            "line_no": lineno,
                            "reason": reason,
                            # The original bytes, verbatim. This is the audit
                            # trail: quarantined lines are never deleted, only
                            # moved.
                            "original_line": raw,
                        },
                        key,
                    )
                )
            # Idempotency: a previous run may have moved the lines but failed
            # to rewrite the log — never append the same record twice.
            seen = _quarantine_seen_keys(qpath)
            new_records = [
                r
                for r in records
                if (r["line_no"], r["original_line"]) not in seen
            ]
            if new_records:
                try:
                    qpath.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(
                        qpath, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                    )
                    try:
                        os.fchmod(fd, 0o600)
                        for record in new_records:
                            os.write(
                                fd,
                                (json.dumps(record, sort_keys=True) + "\n").encode(
                                    "utf-8"
                                ),
                            )
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    log.error("QUARANTINE: cannot write quarantine file: %s", exc)
                    return {
                        "ok": False,
                        "quarantined": 0,
                        "quarantine_file": None,
                        "integrity_ok": False,
                        "reasons": reasons,
                        "error": f"quarantine_write_failed: {exc}",
                        "instructions": (
                            "The quarantine file could not be written: no lines "
                            "were moved and the consent log was NOT modified. "
                            "Resolve the filesystem error and re-run "
                            "quarantine_consent_log()."
                        ),
                    }

            # Rewrite the log with only verified entries (atomic replace),
            # then append the audit entry — so the repair itself is in the
            # signed log.
            try:
                tmp = path.with_name("channel_consent.jsonl.quarantine.tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    for line in good:
                        os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, path)
            except OSError as exc:
                log.error("QUARANTINE: cannot rewrite consent log: %s", exc)
                return {
                    "ok": False,
                    "quarantined": len(bad),
                    "quarantine_file": str(qpath),
                    "integrity_ok": False,
                    "reasons": reasons,
                    "error": f"consent_log_rewrite_failed: {exc}",
                    "instructions": (
                        "The quarantine file holds the moved lines; the "
                        "consent log was NOT modified. Resolve the filesystem "
                        "error and re-run quarantine_consent_log() — records "
                        "already moved will not be duplicated."
                    ),
                }
            try:
                _append_consent_locked(
                    _quarantine_audit_payload(qpath, len(bad), reasons)
                )
            except OSError as exc:
                log.error("QUARANTINE: audit entry write failed: %s", exc)
                # The repair itself landed (lines moved, log rewritten); only
                # the audit entry is missing. Re-verify so the result reports
                # the true store state, and point at the dedicated re-audit
                # entry point — re-running the full repair would report
                # "nothing to quarantine".
                _, integrity_ok = _read_consent()
                return {
                    "ok": False,
                    "quarantined": len(bad),
                    "quarantine_file": str(qpath),
                    "integrity_ok": integrity_ok,
                    "reasons": reasons,
                    "error": f"quarantine_audit_write_failed: {exc}",
                    "instructions": (
                        "The repair itself succeeded — the unverifiable lines "
                        "are in the quarantine file and the consent log was "
                        "rewritten — but the signed 'consent_quarantine' audit "
                        "entry could not be appended, so the repair is not yet "
                        "audited. Do NOT re-run quarantine_consent_log() (it "
                        "would report 'nothing to quarantine'). Instead, once "
                        "the filesystem error is resolved, re-audit with: "
                        "registry.record_quarantine_audit(quarantine_file="
                        f"{str(qpath)!r}, quarantined={len(bad)}, "
                        f"reasons={reasons!r})."
                    ),
                }

            _, integrity_ok = _read_consent()
            log.warning(
                "QUARANTINE: moved %d unverifiable line(s) from %s to %s; "
                "store integrity now %s.",
                len(bad), path, qpath, "OK" if integrity_ok else "STILL FAILED",
            )
            return {
                "ok": integrity_ok,
                "quarantined": len(bad),
                "quarantine_file": str(qpath),
                "integrity_ok": integrity_ok,
                "reasons": reasons,
                "instructions": (
                    "Quarantined lines are preserved verbatim in the quarantine "
                    "file (never deleted). The repair is recorded as a "
                    "'consent_quarantine' entry in the consent log. If a "
                    "channel's own 'enable' entry was quarantined, re-enable it "
                    "explicitly with enable_channel(..., confirm=True)."
                ),
            }
    except OSError as exc:
        # Lock acquisition failed at with-entry (the real @contextmanager
        # failure mode — e.g. the lock file cannot be created). No lines
        # were moved and the log was not modified.
        log.error("QUARANTINE: consent-store lock unavailable: %s", exc)
        return {
            "ok": False,
            "quarantined": 0,
            "quarantine_file": None,
            "integrity_ok": False,
            "reasons": {},
            "error": f"quarantine_lock_failed: {exc}",
            "instructions": (
                "The consent-store lock could not be acquired — another "
                "process may be mid-repair or mid-write. No lines were "
                "moved and the log was not modified. Wait briefly and "
                "re-run quarantine_consent_log()."
            ),
        }


def _quarantine_audit_payload(
    quarantine_file: str | Path, quarantined: int, reasons: dict[str, int]
) -> dict[str, Any]:
    """The signed ``consent_quarantine`` audit entry for a completed repair."""
    return {
        "action": "consent_quarantine",
        "quarantined": quarantined,
        "quarantine_file": str(quarantine_file),
        "reasons": dict(reasons),
    }


def record_quarantine_audit(
    quarantine_file: str | Path,
    quarantined: int,
    reasons: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Re-audit a quarantine repair whose audit entry failed to land.

    Dedicated re-audit entry point for the ``quarantine_audit_write_failed``
    path: the bad lines are already moved and the log already rewritten, so
    re-running ``quarantine_consent_log()`` would report "nothing to
    quarantine" and the repair would stay permanently unaudited. This
    appends the same signed ``consent_quarantine`` entry the repair would
    have written (quarantine file, count, reasons), making the "the repair
    itself is a signed consent_quarantine entry" promise true even on that
    path. Returns ``{"ok": True}`` or ``{"ok": False, "error": ...}`` — an
    audit write that does not land is a failed re-audit, never reported as
    success.
    """
    try:
        _append_consent(
            _quarantine_audit_payload(
                quarantine_file, quarantined, reasons or {}
            )
        )
    except OSError as exc:
        log.error("QUARANTINE: re-audit entry write failed: %s", exc)
        return {"ok": False, "error": f"quarantine_audit_write_failed: {exc}"}
    return {"ok": True}


def consent_history(
    channel: str | None = None, *, with_integrity: bool = False
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], bool]:
    """Read the consent audit log (newest last).

    Only integrity-verified entries are returned. Verification failures
    are logged loudly at ERROR by the reader.

    With ``with_integrity=True`` returns ``(entries, integrity_ok)`` so
    callers can distinguish "no consents on file" from "the store failed
    verification" — the plain list form cannot tell those apart.
    """
    entries, integrity_ok = _read_consent()
    if channel is not None:
        channel = (channel or "").strip().lower()
        entries = [e for e in entries if e.get("channel") == channel]
    if with_integrity:
        return entries, integrity_ok
    return entries


def is_enabled(channel: str) -> bool:
    """True only if the verified latest consent entry is an enable.

    Fail closed: if the consent store cannot be integrity-verified, the
    channel is treated as NOT enabled regardless of what the log claims.
    The channel name is case-normalized (the store records lowercase).
    """
    name = (channel or "").strip().lower()
    entries, integrity_ok = _read_consent()
    if not integrity_ok:
        log.error(
            "CONSENT INTEGRITY: store failed verification; treating "
            "channel %r as disabled (fail closed). Recovery: %s.",
            name, RECOVERY_DOC,
        )
        return False
    history = [
        e
        for e in entries
        if e.get("channel") == name
        and e.get("action") in ("enable", "disable")
    ]
    if not history:
        return False
    return history[-1].get("action") == "enable"


def enabled_channels() -> list[str]:
    """Channels the user has explicitly enabled and not since disabled."""
    return [name for name in CHANNELS if is_enabled(name)]


# ---------------------------------------------------------------------------
# Explicit opt-in / opt-out
# ---------------------------------------------------------------------------


def _validate_via(via: str) -> str | None:
    """Return an error message when ``via`` is not an enumerated source."""
    if via not in VALID_VIA_SOURCES:
        return (
            f"invalid_via: {via!r} is not a recognized opt-in source; "
            f"expected one of {sorted(VALID_VIA_SOURCES)}"
        )
    return None


def enable_channel(
    channel: str, *, confirm: bool = False, via: str = ""
) -> dict[str, Any]:
    """Enable a channel — ONLY with the user's explicit confirmation.

    ``confirm=True`` must carry the user's own opt-in (the wizard / UI
    passes it through; it is never defaulted). Without it, nothing changes
    and the caller gets instructions.

    A consent write that does not land is an enable FAILURE: the result
    reports ok False and the channel is not enabled.

    NOTE (residual product question, for the operator): channel *enablement* is
    still a caller-asserted boolean + enumerated ``via`` source. The
    interactive send boundary below means a rogue enable alone can never
    cause a send, but whether enablement itself should be interactive is
    undecided — see DECISIONS.md.
    """
    name = (channel or "").strip().lower()
    spec = CHANNELS.get(name)
    if spec is None:
        return {
            "ok": False,
            "enabled": False,
            "error": f"unknown_channel: {channel!r}",
            "known_channels": sorted(CHANNELS),
        }
    via_error = _validate_via(via)
    if via_error:
        return {
            "ok": False,
            "enabled": is_enabled(name),
            "channel": name,
            "error": via_error,
        }
    if not confirm:
        return {
            "ok": False,
            "enabled": is_enabled(name),
            "channel": name,
            "instructions": (
                f"Enabling {name!r} is an explicit opt-in. Review its "
                "declaration (manifest) first, then call enable_channel "
                "with confirm=True — or use the guided wizard, which asks "
                "you directly. Nothing was enabled."
            ),
            "manifest": spec.manifest.to_dict(),
        }
    try:
        _append_consent({"action": "enable", "channel": name, "via": via})
    except OSError as exc:
        log.error("Channel %r NOT enabled: consent write failed: %s", name, exc)
        return {
            "ok": False,
            "enabled": is_enabled(name),
            "channel": name,
            "error": f"consent_write_failed: {exc}",
        }
    log.info("Channel %r enabled via %r", name, via)
    # Honest state: the write landed, but if some OTHER line in the store
    # failed verification, is_enabled() still reads disabled (fail
    # closed). Report that divergence LOUDLY — never return ok:true /
    # enabled:true while the channel reads disabled.
    if not is_enabled(name):
        return {
            "ok": True,
            "enabled": False,
            "channel": name,
            "integrity_warning": (
                "consent_store_unverifiable: the enable was recorded, but "
                "the consent store failed integrity verification (a "
                "malformed or tampered line elsewhere in the log), so the "
                "channel still reads as disabled (fail closed). This is "
                "NOT silent: repair the store with "
                "registry.quarantine_consent_log() to move the unverifiable "
                "lines to the audit-preserving quarantine file, then "
                "re-enable (the planned `veto channel repair` CLI will be "
                "the future shortcut for the same repair). Full procedure: "
                f"{RECOVERY_DOC}."
            ),
            "instructions": (
                "Repair the store first: call "
                "registry.quarantine_consent_log() (the planned "
                "`veto channel repair` CLI is the future shortcut; see "
                "RECOVERY.md Procedure B), then enable the channel again."
            ),
        }
    return {"ok": True, "enabled": True, "channel": name}


def disable_channel(channel: str, *, via: str = "") -> dict[str, Any]:
    """Disable a channel immediately; appended to the audit log.

    A consent write that does not land is reported as a FAILURE — the
    caller must not believe the disable was recorded when it was not.
    """
    name = (channel or "").strip().lower()
    if name not in CHANNELS:
        return {
            "ok": False,
            "enabled": False,
            "error": f"unknown_channel: {channel!r}",
        }
    via_error = _validate_via(via)
    if via_error:
        # The disable did not happen: report the channel's ACTUAL state,
        # never a blanket enabled:true.
        return {
            "ok": False,
            "enabled": is_enabled(name),
            "channel": name,
            "error": via_error,
        }
    try:
        _append_consent({"action": "disable", "channel": name, "via": via})
    except OSError as exc:
        log.error("Channel %r disable NOT recorded: %s", name, exc)
        return {
            "ok": False,
            "enabled": is_enabled(name),
            "channel": name,
            "error": f"consent_write_failed: {exc}",
        }
    log.info("Channel %r disabled via %r", name, via)
    return {"ok": True, "enabled": False, "channel": name}


def channel_manifest(channel: str) -> ConnectorManifest | None:
    """The proof-of-value declaration for a channel."""
    spec = CHANNELS.get((channel or "").strip().lower())
    return spec.manifest if spec else None


# ---------------------------------------------------------------------------
# Interactive send approval (the send boundary)
# ---------------------------------------------------------------------------


def _canonical_draft(draft: dict[str, Any]) -> bytes:
    """Canonical bytes of the (to, subject, body) triple being approved."""
    return _canonical_bytes(
        {
            "to": str(draft.get("to") or "").strip(),
            "subject": str(draft.get("subject") or "").strip(),
            "body": str(draft.get("body") or "").strip(),
        }
    )


def _content_hash(draft: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_draft(draft)).hexdigest()


def draft_approval_token(draft: dict[str, Any]) -> str:
    """Content digest of a draft: SHA-256 of the canonicalized payload.

    This is a DISPLAY / reconciliation helper only — it is deterministic
    and computable by anyone, so it is NOT proof of approval and the send
    path NEVER accepts it as authorization. Genuine approval evidence is
    the ``send_approval`` record written by ``request_send_approval``
    (nonce + content hash + timestamp, HMAC-signed into the consent log).
    """
    return _content_hash(draft)


def _stdin_is_tty() -> bool:
    """True only when a human is plausibly at a REAL terminal: BOTH stdin
    and stdout must be TTYs.

    stdin carries the typed answer; stdout carries the draft display. If
    stdin is a TTY but stdout is piped away, the user would type the
    approval value blind — never seeing the draft they approve — so that
    is NOT interactive: fail closed. (The historical name is kept as the
    single seam for the interactive boundary; it now checks both
    directions.)

    Split out (not inlined) so tests can simulate both sides of the
    interactive boundary deterministically.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _validate_draft(draft: Any) -> dict[str, Any] | None:
    """Return a failure dict for a non-draft send input, else None.

    Guards the send boundary against None / non-dict drafts with a clear
    error instead of an AttributeError deep in hashing code.
    """
    if not isinstance(draft, dict):
        return {
            "error": "invalid_draft",
            "instructions": (
                "Not sent. The draft must be a dict with "
                "'to'/'subject'/'body' keys."
            ),
        }
    return None


def _approval_input(prompt: str) -> str:
    """Read one line for the approval prompt, with an abandonment timeout.

    Split out (not inlined) so tests can simulate both sides of the
    interactive boundary deterministically. A user who walks away from
    the prompt must not hang the process forever: after
    APPROVAL_PROMPT_TIMEOUT_SECONDS with no answer, TimeoutError is
    raised (the caller treats it as an aborted approval). Uses
    select() on POSIX stdin; on platforms without selectable stdin it
    degrades to a plain blocking read.
    """
    print(prompt, end="", flush=True)
    readable: list | None = None
    try:
        import select as _select

        readable, _, _ = _select.select(
            [sys.stdin], [], [], APPROVAL_PROMPT_TIMEOUT_SECONDS
        )
    except Exception:
        readable = None  # no selectable stdin: fall back to blocking read
    if readable == []:
        raise TimeoutError(
            "send-approval prompt timed out after "
            f"{APPROVAL_PROMPT_TIMEOUT_SECONDS}s with no answer"
        )
    line = sys.stdin.readline()
    if line == "":
        raise EOFError("send-approval prompt: EOF on stdin")
    return line.rstrip("\n")


def _present_draft(draft: dict[str, Any], channel: str) -> None:
    """Show the exact payload awaiting approval on the terminal.

    Every displayed field is sanitized (see ``_sanitize_display``):
    ANSI escape sequences are stripped and control characters —
    newlines included — become visible placeholders. A hostile draft
    therefore cannot use cursor-movement escapes to overwrite the
    displayed To: line, nor a raw newline to inject a fake second To:
    line. The sanitization is DISPLAY ONLY: the approval record and the
    sent payload keep the true, unsanitized content.
    """
    to = _sanitize_display(str(draft.get("to") or "").strip())
    subject = _sanitize_display(str(draft.get("subject") or "").strip())
    body = str(draft.get("body") or "").strip()
    truncated = len(body) > 4000
    shown_body = _sanitize_display(body if not truncated else body[:4000])
    if truncated:
        shown_body += "\n[…body truncated…]"
    print(f"\n=== Veto send approval ({_sanitize_display(channel)}) ===")
    print(f"To:      {to or '(no recipient)'}")
    print(f"Subject: {subject or '(no subject)'}")
    print("--- body ---")
    print(shown_body)
    print("--- end body ---")


def request_send_approval(
    draft: dict[str, Any],
    *,
    channel: str = "gmail",
    expected_value: str,
    value_label: str = "value",
) -> dict[str, Any]:
    """Interactively ask the user to approve sending ``draft``.

    Presents the actual recipient/subject/body on the terminal and requires
    the user to type ``expected_value`` — a value that VARIES per send
    (e.g. the recipient address as shown in the display). A fixed string
    such as "send" or "yes" FAILS: the typed answer is compared ONLY
    against ``expected_value``. ``value_label`` names the value in the
    prompt WITHOUT echoing it (the user must read it from the display,
    not copy it from the prompt).

    ``expected_value`` is REQUIRED and must be non-empty: a missing or
    empty value refuses instead of degrading to a fixed string (legal
    exposure reduction, spec §6.2 — a hardcoded "yes" must fail).

    On a genuine approval, writes a ``send_approval`` record (nonce +
    content hash + timestamp, HMAC-signed into the consent log) and
    returns ``{"ok": True, "approval_id": nonce}``.

    Fails closed: a non-dict draft, no usable terminal (stdin and stdout
    not both TTYs — agents, scripts, MCP servers), EOF, a declined answer,
    a timed-out prompt, or an approval-write failure all return ``ok
    False`` and write no approval record. Nothing about this function's
    return value can be forged by a caller — only the interactive prompt
    itself mints records.
    """
    bad = _validate_draft(draft)
    if bad is not None:
        return {"ok": False, "approved": False, **bad}
    channel = _normalize_channel(channel)
    expected = (expected_value or "").strip()
    if not expected:
        # Fail closed: without a per-send varying value there is no
        # legal-hardening §6.2 confirmation to offer. Never degrade to a
        # fixed string.
        log.warning("Send approval refused: no expected_value supplied.")
        return {
            "ok": False,
            "approved": False,
            "error": "missing_expected_value",
            "instructions": (
                "Not sent. The approval requires a per-send varying value "
                "(e.g. the recipient address as shown); none was supplied, "
                "so the prompt cannot run. This is a caller bug — fix the "
                "call site, do not work around it."
            ),
        }
    label = (value_label or "value").strip() or "value"
    content_hash = _content_hash(draft)
    if not _stdin_is_tty():
        return {
            "ok": False,
            "approved": False,
            "error": "not_interactive",
            "instructions": (
                "Not sent. Sending requires a genuine interactive user "
                "approval, but there is no usable terminal: BOTH stdin and "
                "stdout must be TTYs (stdin carries the typed answer, "
                "stdout carries the draft display). Run from an interactive "
                "terminal, or obtain an approval_id via "
                "request_send_approval on a terminal first."
            ),
        }
    _present_draft(draft, channel)
    try:
        answer = _approval_input(
            f"Type the {label} exactly as shown above to approve sending, "
            "anything else to abort: "
        )
    except TimeoutError:
        log.info("Send approval abandoned (prompt timed out).")
        return {
            "ok": False,
            "approved": False,
            "error": "approval_timeout",
            "instructions": (
                "Not sent. The approval prompt timed out "
                f"({APPROVAL_PROMPT_TIMEOUT_SECONDS // 60} minutes) without "
                "an answer. Re-present the draft and try again."
            ),
        }
    except (EOFError, KeyboardInterrupt):
        log.info("Send approval aborted (no input).")
        return {
            "ok": False,
            "approved": False,
            "error": "approval_aborted",
            "instructions": "Not sent. No approval was given.",
        }
    if answer.strip() != expected:
        # The typed answer is compared ONLY against the per-send varying
        # value. A hardcoded "send"/"yes"/"y"/"ok" fails here by
        # construction (legal exposure reduction, spec §6.2).
        log.info("Send approval declined by user (value mismatch).")
        return {
            "ok": False,
            "approved": False,
            "error": "approval_declined",
            "instructions": (
                "Not sent. The typed value did not match the value shown "
                "in the display. Nothing was approved."
            ),
        }
    try:
        nonce = _mint_approval_record(
            record_action="send_approval",
            channel=channel,
            content_hash=content_hash,
        )
    except OSError as exc:
        log.error("Send approval NOT recorded: %s", exc)
        return {
            "ok": False,
            "approved": False,
            "error": f"approval_write_failed: {exc}",
        }
    log.info("Send approval recorded (nonce %s…, channel %s).", nonce[:8], channel)
    return {
        "ok": True,
        "approved": True,
        "approval_id": nonce,
        "content_hash": content_hash,
        "channel": channel,
    }


def _mint_approval_record(
    *, record_action: str, channel: str, content_hash: str
) -> str:
    """Write one approval record; return its nonce.

    The record binds nonce + content_hash + timestamp (+ channel) into
    the HMAC-signed consent log. ``record_action`` is ``"send_approval"``
    for draft sends and ``"action_approval"`` for generic per-action
    gates (legal-hardening commit 8, spec §6); both validate identically
    (see ``_validate_approval`` / ``_find_unconsumed_approval``).

    Raises OSError when the record cannot be persisted — the caller must
    fail closed (no record written means no approval exists).
    """
    nonce = secrets.token_hex(16)
    _append_consent(
        {
            "action": record_action,
            "channel": channel,
            "nonce": nonce,
            "content_hash": content_hash,
        }
    )
    return nonce


def _present_action_summary(summary: str, action_label: str) -> None:
    """Show the exact action awaiting approval on the terminal.

    Display-only sanitization (see ``_present_draft``): ANSI escape
    sequences are stripped and every other control character becomes a
    visible placeholder, so a hostile summary cannot rewrite the
    terminal. The approval record binds the TRUE summary bytes via the
    content hash.
    """
    print(f"\n=== Veto action approval ({_sanitize_display(action_label)}) ===")
    print(_sanitize_display(summary))
    print("=== end summary ===")


def request_action_approval(
    *,
    summary: str,
    expected_value: str,
    value_label: str = "value",
    action_label: str = "action",
    channel: str = "action",
) -> dict[str, Any]:
    """Generic per-action confirmation — legal-hardening commit 8, spec §6.

    The SAME approval machinery as ``request_send_approval`` (interactive
    TTY only, varying typed value, nonce + content-hash + TTL +
    single-use, HMAC-signed consent log), but for an arbitrary action
    summary instead of a to/subject/body draft. The approval record
    (record action ``"action_approval"``) validates through the identical
    path as send approvals; its content hash binds the EXACT summary
    bytes shown.

    ``expected_value`` is REQUIRED and must be non-empty: the per-action
    varying string the user must type (e.g. the company name as shown in
    the summary). A fixed string such as "yes" FAILS — the typed answer
    is compared only against ``expected_value``. ``value_label`` names
    the value in the prompt WITHOUT echoing it: the user must read it
    from the summary, not copy it from the prompt.

    Fails closed exactly like ``request_send_approval``: a missing/empty
    expected value, no usable terminal (stdin and stdout not both TTYs —
    agents, scripts, MCP servers), EOF, a declined answer, a timed-out
    prompt, or an approval-write failure all return ``ok False`` and
    write no approval record. This function never exits the process;
    callers that must refuse-and-exit (spec §6.3) do so themselves —
    see ``circuit_breaker.require_action_confirmation``.

    There is no batch approval, no confirm-all, no remembered
    confirmation, and no session-spanning approval: one call approves
    one action, and the approval is single-use.
    """
    expected = (expected_value or "").strip()
    if not expected:
        log.warning("Action approval refused: no expected_value supplied.")
        return {
            "ok": False,
            "approved": False,
            "error": "missing_expected_value",
            "instructions": (
                "Not approved. The approval requires a per-action varying "
                "value (e.g. the company name as shown); none was supplied, "
                "so the prompt cannot run. This is a caller bug — fix the "
                "call site, do not work around it."
            ),
        }
    if not isinstance(summary, str) or not summary.strip():
        return {
            "ok": False,
            "approved": False,
            "error": "missing_summary",
            "instructions": (
                "Not approved. There is no action summary to show the user; "
                "approving blind is not permitted."
            ),
        }
    channel = _normalize_channel(channel)
    label = (value_label or "value").strip() or "value"
    action = (action_label or "action").strip() or "action"
    content_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    if not _stdin_is_tty():
        return {
            "ok": False,
            "approved": False,
            "error": "not_interactive",
            "instructions": (
                "Not approved. Action approval requires a genuine "
                "interactive user approval, but there is no usable "
                "terminal: BOTH stdin and stdout must be TTYs (stdin "
                "carries the typed answer, stdout carries the action "
                "summary). Run from an interactive terminal."
            ),
        }
    _present_action_summary(summary, action)
    try:
        answer = _approval_input(
            f"Type the {label} exactly as shown above "
            f"to submit this {action}, anything else to abort: "
        )
    except TimeoutError:
        log.info("Action approval abandoned (prompt timed out).")
        return {
            "ok": False,
            "approved": False,
            "error": "approval_timeout",
            "instructions": (
                "Not approved. The approval prompt timed out "
                f"({APPROVAL_PROMPT_TIMEOUT_SECONDS // 60} minutes) without "
                "an answer. Re-present the summary and try again."
            ),
        }
    except (EOFError, KeyboardInterrupt):
        log.info("Action approval aborted (no input).")
        return {
            "ok": False,
            "approved": False,
            "error": "approval_aborted",
            "instructions": "Not approved. No approval was given.",
        }
    if answer.strip() != expected:
        log.info("Action approval declined (value mismatch).")
        return {
            "ok": False,
            "approved": False,
            "error": "approval_declined",
            "instructions": (
                "Not approved. The typed value did not match the value "
                "shown in the summary. Nothing was approved."
            ),
        }
    try:
        nonce = _mint_approval_record(
            record_action="action_approval",
            channel=channel,
            content_hash=content_hash,
        )
    except OSError as exc:
        log.error("Action approval NOT recorded: %s", exc)
        return {
            "ok": False,
            "approved": False,
            "error": f"approval_write_failed: {exc}",
        }
    log.info(
        "Action approval recorded (nonce %s…, channel %s).", nonce[:8], channel
    )
    return {
        "ok": True,
        "approved": True,
        "approval_id": nonce,
        "content_hash": content_hash,
        "channel": channel,
    }


# In-process single-use enforcement: an approval_id claimed here cannot be
# reused while this process lives. The in-process mark is the fast path
# only — a claim counts ONLY when its ``approval_consumed`` marker
# persisted to the signed log (see _consume_approval_locked): a marker
# write that fails rolls the in-process mark back, so the approval stays
# claimable and a failed claim is never reported as success.
# Maps nonce -> consumption timestamp; entries older than the approval
# TTL are pruned so long-lived processes don't grow this unboundedly.
_CONSUMED_APPROVALS: dict[str, float] = {}


def _prune_consumed_approvals(now: float | None = None) -> None:
    """Drop in-process consumption markers older than the approval TTL.

    A marker older than APPROVAL_TTL_SECONDS (plus margin) can never
    matter again: even the underlying approval record would be expired.
    Called from the consumption/validation paths; idempotent.
    """
    now = time.time() if now is None else now
    horizon = APPROVAL_TTL_SECONDS * 2
    stale = [nonce for nonce, ts in _CONSUMED_APPROVALS.items() if now - ts > horizon]
    for nonce in stale:
        del _CONSUMED_APPROVALS[nonce]


def _validate_approval(
    approval_id: str, content_hash: str, channel: str | None = None
) -> str | None:
    """None when the approval record authorizes this exact content.

    Returns an error code otherwise: the record must exist, be bound to
    ``content_hash``, be fresher than APPROVAL_TTL_SECONDS, be unused,
    and — when ``channel`` is given — be bound to that channel (an
    approval for one channel never authorizes a send on another).
    ``channel`` is case-normalized before comparison. Any consent-store
    integrity failure fails closed.
    """
    if channel is not None:
        channel = _normalize_channel(channel)
    entries, integrity_ok = _read_consent()
    if not integrity_ok:
        return "consent_store_unverifiable"
    match: dict[str, Any] | None = None
    for entry in entries:
        # Both record types validate identically: send_approval (draft
        # sends) and action_approval (generic per-action gates, legal
        # exposure reduction commit 8). The security-relevant binding is
        # nonce + content_hash + timestamp + channel in all cases.
        if entry.get("action") in ("send_approval", "action_approval") and entry.get("nonce") == approval_id:
            match = entry  # newest wins; log is oldest-first
    if match is None:
        return "no_such_approval"
    if match.get("content_hash") != content_hash:
        return "approval_content_mismatch"
    if channel is not None and match.get("channel") != channel:
        return "approval_channel_mismatch"
    try:
        age = time.time() - float(match.get("ts", 0))
    except (TypeError, ValueError):
        return "approval_malformed"
    if age < 0 or age > APPROVAL_TTL_SECONDS:
        return "approval_expired"
    _prune_consumed_approvals()
    for entry in entries:
        if (
            entry.get("action") == "approval_consumed"
            and entry.get("nonce") == approval_id
        ):
            return "approval_already_used"
    if approval_id in _CONSUMED_APPROVALS:
        return "approval_already_used"
    return None


def _find_unconsumed_approval(
    content_hash: str, channel: str | None = None
) -> str | None:
    """Newest fresh, unconsumed approval_id for this exact content, if any.

    Lets a caller that obtained an interactive approval (e.g. the wizard's
    preview → approve step) send without threading the approval_id through
    every layer. Safe: such a record can only exist if the user genuinely
    approved this exact content within the TTL, and it is still single-use.
    When ``channel`` is given, only approvals bound to that channel are
    considered (``channel`` is case-normalized before comparison).
    """
    if channel is not None:
        channel = _normalize_channel(channel)
    entries, integrity_ok = _read_consent()
    if not integrity_ok:
        return None
    now = time.time()
    _prune_consumed_approvals(now)
    consumed = {
        e.get("nonce")
        for e in entries
        if e.get("action") == "approval_consumed"
    }
    best: str | None = None
    for entry in entries:  # oldest-first; keep newest match
        if entry.get("action") not in ("send_approval", "action_approval"):
            continue
        if entry.get("content_hash") != content_hash:
            continue
        if channel is not None and entry.get("channel") != channel:
            continue
        nonce = entry.get("nonce")
        if not nonce or nonce in consumed or nonce in _CONSUMED_APPROVALS:
            continue
        try:
            age = now - float(entry.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if 0 <= age <= APPROVAL_TTL_SECONDS:
            best = nonce
    return best


_APPROVAL_REFUSALS = {
    "consent_store_unverifiable": (
        "Not sent. The consent store failed integrity verification, so no "
        f"approval can be trusted. Recovery procedure: {RECOVERY_DOC}."
    ),
    "no_such_approval": (
        "Not sent. No user approval record exists for this send. "
        "Approvals are created only by an interactive user approval — "
        "self-computed tokens and caller-asserted flags are not accepted."
    ),
    "approval_content_mismatch": (
        "Not sent. The approval record is bound to different content — the "
        "draft changed after the user approved it (or was never approved). "
        "Re-present the draft and obtain a fresh approval."
    ),
    "approval_channel_mismatch": (
        "Not sent. The approval record is bound to a different channel — "
        "an approval for one channel never authorizes a send on another. "
        "Obtain an approval for the correct channel."
    ),
    "approval_expired": (
        "Not sent. The user approval expired "
        f"({APPROVAL_TTL_SECONDS // 60}-minute TTL). Re-present the draft "
        "and obtain a fresh approval."
    ),
    "approval_already_used": (
        "Not sent. This approval was already used — approvals are "
        "single-use. Obtain a fresh approval for another send."
    ),
    "approval_malformed": (
        "Not sent. The approval record is malformed and cannot be trusted."
    ),
    "approval_consume_failed": (
        "Not sent. The approval was valid, but its single-use consumption "
        "marker could not be persisted to the consent store, so the claim "
        "was REFUSED rather than risk a double-spend. Nothing was spent: "
        "resolve the filesystem error and authorize again — the approval "
        "is still valid and can be claimed on retry."
    ),
}


def _consume_failed_refusal(channel: str) -> dict[str, Any]:
    """Refusal for a claim whose consumption marker did not persist."""
    return {
        "sent": False,
        "channel": channel,
        "error": "approval_consume_failed",
        "instructions": _APPROVAL_REFUSALS["approval_consume_failed"],
    }


def authorize_send(
    draft: dict[str, Any],
    *,
    channel: str = "gmail",
    approval_id: str | None = None,
    expected_value: str,
    value_label: str = "value",
) -> tuple[str | None, dict[str, Any] | None]:
    """The send boundary. Returns ``(approval_id, None)`` or
    ``(None, refusal_dict)``.

    ``approval_id`` names a ``send_approval`` record minted by
    ``request_send_approval``. Without one, a fresh unconsumed record for
    this exact content is accepted; otherwise the interactive prompt runs
    (TTY only) and a non-interactive caller is refused. There is no
    parameter combination by which a caller can authorize its own send.
    A non-dict draft is refused outright (invalid_draft), and the
    approval's channel binding must match ``channel`` (case-insensitive).

    ``expected_value`` (REQUIRED) is the per-send varying string the
    interactive prompt requires the user to type (e.g. the recipient
    address as shown in the draft display); a fixed string such as
    "send" can never satisfy it (legal exposure reduction, spec §6.2).

    ATOMIC CLAIM: a successful return means the approval was validated
    AND consumed as one atomic step under the cross-process
    consent-store lock, with the ``approval_consumed`` marker persisted to
    the signed log — two processes racing on the same approval cannot both
    succeed; the loser fails closed with ``approval_already_used``. The
    guarantee holds EXACTLY when the marker write persists: if persisting
    it fails, the claim fails too — ``authorize_send`` refuses with
    ``approval_consume_failed`` (the in-process mark is rolled back so a
    retry can claim again) and never reports success, so a single-use
    approval cannot be double-spent by a failed-then-retried claim.
    Callers must treat the returned approval_id as already spent (a further
    ``consume_approval`` is a harmless no-op). A refused downstream send
    does NOT refund the approval — fail closed.
    """
    bad = _validate_draft(draft)
    if bad is not None:
        out = {"sent": False, "channel": _normalize_channel(channel), **bad}
        return None, out
    channel = _normalize_channel(channel)
    content_hash = _content_hash(draft)
    with _consent_store_lock():
        if approval_id is not None:
            problem = _validate_approval(
                approval_id, content_hash, channel=channel
            )
            if problem is None:
                try:
                    _consume_approval_locked(approval_id, channel=channel)
                except OSError as exc:
                    # The marker did not persist: the claim FAILED. Refuse
                    # (never report success); _consume_approval_locked
                    # already rolled the in-process mark back so a retry
                    # can claim the approval again.
                    log.error(
                        "APPROVAL: consumption marker for %r failed to "
                        "persist (%s); claim refused, not recorded.",
                        approval_id, exc,
                    )
                    return None, _consume_failed_refusal(channel)
                return approval_id, None
            return None, {
                "sent": False,
                "channel": channel,
                "error": problem,
                "instructions": _APPROVAL_REFUSALS[problem],
            }
        found = _find_unconsumed_approval(content_hash, channel=channel)
        if found is not None:
            problem = _validate_approval(found, content_hash, channel=channel)
            if problem is None:
                try:
                    _consume_approval_locked(found, channel=channel)
                except OSError as exc:
                    log.error(
                        "APPROVAL: consumption marker for %r failed to "
                        "persist (%s); claim refused, not recorded.",
                        found, exc,
                    )
                    return None, _consume_failed_refusal(channel)
                return found, None
            # Belt-and-braces: _find_unconsumed_approval already screens
            # for validity, so this should not happen — fall through to
            # the interactive prompt (the safe direction), never to an
            # authorization.
    # No usable approval: the interactive prompt runs OUTSIDE the lock (a
    # user may take minutes to answer; consent writes must not block that
    # long), then the just-minted approval is claimed atomically. Any
    # ambiguity fails closed.
    res = request_send_approval(
        draft,
        channel=channel,
        expected_value=expected_value,
        value_label=value_label,
    )
    if not res.get("ok"):
        return None, {
            "sent": False,
            "channel": channel,
            "error": res.get("error", "approval_required"),
            "instructions": res.get(
                "instructions", "Not sent: no user approval."
            ),
        }
    minted = res["approval_id"]
    with _consent_store_lock():
        problem = _validate_approval(minted, content_hash, channel=channel)
        if problem is None:
            try:
                _consume_approval_locked(minted, channel=channel)
            except OSError as exc:
                log.error(
                    "APPROVAL: consumption marker for freshly minted "
                    "approval %r failed to persist (%s); claim refused, "
                    "not recorded.",
                    minted, exc,
                )
                problem = "approval_consume_failed"
            else:
                return minted, None
    log.error(
        "APPROVAL: freshly minted approval %r failed to claim (%s); "
        "failing closed.",
        minted[:8], problem,
    )
    return None, {
        "sent": False,
        "channel": channel,
        "error": problem or "approval_claim_failed",
        "instructions": _APPROVAL_REFUSALS.get(
            problem or "",
            "Not sent: the approval could not be claimed. Re-present the "
            "draft and obtain a fresh approval.",
        ),
    }


def _consume_approval_locked(approval_id: str, *, channel: str) -> None:
    """Mark an approval single-use; the caller must hold
    ``_consent_store_lock``.

    Raises OSError when the ``approval_consumed`` marker cannot be
    persisted. The in-process mark is rolled back first, so the approval
    is left unspent — the caller decides the failure policy:

    * ``authorize_send`` treats it as a FAILED CLAIM: it refuses with
      ``approval_consume_failed`` (never reports success), and the
      rolled-back mark lets a retry claim the approval again. This is
      the module's write-failure convention, same as
      enable/disable/approval-mint.
    * ``consume_approval`` (transport layer) re-sets the in-process mark
      and logs loudly without raising — its contract is pinned by the
      browser-apply unit's B1 test; the cross-process replay window on a
      failed marker write there is the documented residual risk.

    The log itself is the cross-process source of truth: if the write
    actually landed before failing (e.g. an fsync error after a
    successful write), the log scan still sees the marker and refuses
    reuse — the safe direction.
    """
    _prune_consumed_approvals()
    previous = _CONSUMED_APPROVALS.get(approval_id)
    _CONSUMED_APPROVALS[approval_id] = time.time()
    try:
        _append_consent_locked(
            {
                "action": "approval_consumed",
                "channel": channel,
                "nonce": approval_id,
            }
        )
    except OSError:
        # The claim did NOT persist: roll back the in-process mark so the
        # approval is left unspent. The caller applies its failure policy.
        if previous is None:
            _CONSUMED_APPROVALS.pop(approval_id, None)
        else:
            _CONSUMED_APPROVALS[approval_id] = previous
        raise


def consume_approval(approval_id: str, *, channel: str = "gmail") -> None:
    """Mark an approval single-use, immediately before the send attempt.

    Idempotent: ``authorize_send`` already claims (validates + consumes)
    the approval atomically, so the transport layer's follow-up call
    here is a no-op when the mark is already set — it never writes a
    duplicate ``approval_consumed`` entry. A failed send does not refund
    the approval (fail closed — the attempt may have gone out). Stale
    in-process markers are pruned so long-lived processes stay bounded.

    Failure policy: a consumption write that does not persist is
    ERROR-logged and the in-process mark is KEPT — same-process reuse
    stays blocked and no exception propagates to the transport caller.
    This contract is pinned by the browser-apply unit's B1 test; the
    cross-process replay window on a failed marker write is the
    documented residual risk
    (docs/i09/decisions/communication-connectors.md, routed to the operator). A
    claim inside authorize_send instead FAILS the claim outright — see
    _consume_approval_locked.
    """
    channel = _normalize_channel(channel)
    _prune_consumed_approvals()
    if approval_id in _CONSUMED_APPROVALS:
        return  # already claimed: idempotent, no duplicate log entry
    try:
        with _consent_store_lock():
            _consume_approval_locked(approval_id, channel=channel)
    except OSError as exc:
        # Transport-layer failure policy: keep the in-process mark (the
        # approval stays spent for this process), log loudly, do not
        # raise into the transport caller.
        _CONSUMED_APPROVALS[approval_id] = time.time()
        log.error(
            "APPROVAL: approval %r marked consumed in-process, but the "
            "consumed marker failed to persist (%s). In-process reuse is "
            "still blocked; a restarted process could theoretically see "
            "the unconsumed record within its TTL — treat sends after log "
            "failures as suspect.",
            approval_id, exc,
        )


# ---------------------------------------------------------------------------
# Gmail adapter (delegates to email_sync; consent-gated here)
# ---------------------------------------------------------------------------


class GmailChannel:
    """Consent-gated adapter over the existing email_sync module."""

    name = "gmail"

    def _require_enabled(self) -> dict[str, Any] | None:
        if not is_enabled(self.name):
            return {
                "ok": False,
                "error": "channel_not_enabled",
                "instructions": (
                    "The gmail channel is not enabled. Enable it explicitly "
                    "first (enable_channel('gmail', confirm=True, "
                    "via='cli-wizard')); it never auto-enables."
                ),
            }
        return None

    def scan_recruiter_emails(
        self, query: str = "", max_n: int = 50
    ) -> dict[str, Any]:
        """Classify recruiter replies — read-only triage."""
        blocked = self._require_enabled()
        if blocked:
            return blocked
        import email_sync

        return {
            "ok": True,
            "channel": self.name,
            "results": email_sync.scan_recruiter_emails(query, max_n=max_n),
        }

    def draft_followup(
        self, application_id: str, kind: str = "check_in"
    ) -> dict[str, Any]:
        """Build a follow-up draft and its content digest. Does NOT send.

        The wizard / UI shows ``draft`` to the user; the user then approves
        via ``request_send_approval`` (interactive), which returns the
        ``approval_id`` the send step requires. ``approval_token`` here is
        a content digest for display/reconciliation only — it authorizes
        nothing.
        """
        blocked = self._require_enabled()
        if blocked:
            return blocked
        import email_sync

        draft = email_sync.draft_followup(application_id, kind=kind)
        return {
            "ok": True,
            "channel": self.name,
            "draft": draft,
            "approval_token": draft_approval_token(draft),
        }

    def send_followup(
        self,
        draft: dict[str, Any],
        *,
        confirm: bool = False,
        approval_token: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a reviewed draft — only with a genuine user approval.

        Gates, all fail closed:
        1. the channel must be explicitly enabled (verified consent log);
        2. ``authorize_send`` must yield an approval: either ``approval_id``
           naming a valid, fresh, unconsumed interactive approval for this
           exact draft, or a fresh unconsumed approval already on file —
           otherwise the interactive prompt runs (TTY) or the send is
           refused (no TTY).

        ``confirm`` and ``approval_token`` are DEPRECATED: they authorize
        nothing. Passing them only routes through the same interactive
        approval — a caller cannot self-assert its way to a send. The
        actual send (and approval consumption) happens inside
        ``email_sync.send_followup``, which enforces the identical
        boundary for direct callers.
        """
        bad = _validate_draft(draft)
        if bad is not None:
            return {"sent": False, "channel": self.name, **bad}
        blocked = self._require_enabled()
        if blocked:
            return {"sent": False, **blocked}
        if confirm:
            log.warning(
                "GmailChannel.send_followup: confirm= is deprecated and is "
                "not proof of approval; an interactive user approval is "
                "still required."
            )
        if approval_token is not None:
            log.warning(
                "GmailChannel.send_followup: approval_token is deprecated — "
                "it is a content digest, not proof of approval, and is "
                "ignored for authorization."
            )
        import email_sync

        # Copy: never mutate the delegated module's result dict in place.
        result = dict(email_sync.send_followup(draft, approval_id=approval_id))
        result["channel"] = self.name
        return result
