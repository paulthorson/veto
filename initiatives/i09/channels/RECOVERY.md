# Channel consent store — integrity-key recovery runbook

Applies to: `initiatives/i09/channels/registry.py` (the channel consent /
send-approval store used by the `gmail` channel and every channel send).

This runbook covers two failure modes:

* **Procedure A — key loss / rotation** (below): the machine key is
  missing or deliberately replaced; every entry becomes unverifiable.
* **Procedure B — torn / tampered consent log** (below A): the key is
  fine, but one or more lines fail verification (malformed JSON,
  missing or mismatched signature). The store reads as failed even
  though most entries are good — the quarantine repair restores it
  without deleting anything.

## What the key is

The store is an append-only JSONL log (`channel_consent.jsonl`) where every
entry is HMAC-SHA256-signed with a **machine-local integrity key** —
`channel_consent.key`, 32 random bytes, written `0o600` next to the log,
generated once on first use, never hardcoded, never leaves the machine.

The key is NOT an encryption key: the log is plaintext (it is a local
audit trail). The key proves *integrity* — that an entry was written by
this machine's registry and not tampered with afterward.

## What key loss looks like (fail closed, by design)

If the key is lost (deleted, corrupted, unreadable, or the log was copied
from another machine), **nothing silently degrades**:

* `is_enabled()` returns `False` for every channel — channels read as
  disabled.
* Every send is refused (`consent_store_unverifiable` / `not_interactive`).
* The log carries ERROR entries pointing here, e.g.:

```
CONSENT INTEGRITY: cannot load machine key (...); treating existing
consent data as unverifiable — all channels read as disabled and pending
send approvals are void. Recovery procedure:
initiatives/i09/channels/RECOVERY.md.
```

There is deliberately no way to "accept" the old entries: with the key
gone, a signature can no longer be checked, and trusting them would let an
attacker who planted entries in the log bypass every consent gate.

## Recovery procedure (operator steps)

1. **Confirm the diagnosis.** Check the ERROR logs above; confirm
   `channel_consent.key` is missing/corrupt (or its backup is the one you
   want to abandon). If the key file merely has wrong permissions, fixing
   permissions (`chmod 600`) and restarting is enough — no re-keying
   needed.

2. **Re-key.** Either:
   * call `registry.rotate_consent_key()` — it archives the current key
     (if any) as `channel_consent.key.<epoch>.bak` (`0o600`) and forces a
     fresh key to be generated on next use; or
   * delete `channel_consent.key` — a fresh 32-byte key is generated
     automatically on the next consent write.

   Both paths are equivalent: the new key will NOT verify anything signed
   with the old one.

3. **Verify the new key exists and is private.**
   `ls -l channel_consent.key` → `-rw-------` (0o600), 32+ bytes. The
   registry enforces this on write; the check is a sanity step.

4. **Re-establish consents.** Every channel must be explicitly re-enabled
   by the user — there is no bulk restore (the old entries are
   cryptographically dead). Run the guided wizard (or call
   `enable_channel(name, confirm=True, via="cli-wizard")` interactively)
   for each channel the user wants back. `enabled_channels()` should list
   them afterward.

5. **Re-approve pending sends.** All `send_approval` records are void.
   Re-present each draft to the user and collect a fresh interactive
   approval (`request_send_approval`); approvals are 15-minute TTL,
   single-use.

6. **Audit.** `consent_history(channel, with_integrity=True)` returns
   `(entries, integrity_ok)` — confirm `integrity_ok` is `True` and the
   history shows the fresh enables. Any remaining ERROR integrity logs
   mean some old/foreign entry is still in the file.

## Operational guidance

* **Back up the key with the machine's other secrets** (same backup that
  holds SSH keys / credential files). Restoring the key file restores
  trust in the existing log — no re-enabling needed.

---

## Procedure B — torn / tampered consent log (quarantine repair)

**Symptoms.** A single malformed line (torn write, manual edit) or a
tampered entry (missing/mismatched signature) in `channel_consent.jsonl`
sets the whole store's integrity flag False — by design, fail closed:

* `is_enabled()` returns `False` for EVERY channel, even ones that were
  properly enabled minutes ago.
* `consent_history()` still shows the verified entries, but the store is
  untrusted; every send is refused (`consent_store_unverifiable` /
  `not_interactive`).
* ERROR logs name the bad line, e.g.:

```
CONSENT INTEGRITY: malformed JSON on line 3 of .../channel_consent.jsonl;
line ignored, store treated as untrusted.
```

* Subtle trap this procedure fixes: `enable_channel(...)` may return
  `ok: true` (the write landed) while `is_enabled()` still reads
  `False` — that divergence is SURFACED honestly as an
  `integrity_warning` naming this procedure. It is a dead end until the
  store is repaired.

The repair is audit-preserving: nothing is silently deleted (the audit
trail is the feature).

**Repair procedure (operator steps).**

1. **Confirm the diagnosis.** Look for the `CONSENT INTEGRITY` ERROR
   logs above (they name the line number and the reason) and/or run
   `consent_history(channel, with_integrity=True)` — the second element
   is `False` when the store failed verification. Confirm the key file
   itself is intact (if the key is the problem, do Procedure A first).

2. **Quarantine — from the CLI.** Run:

   ```
   veto channel repair
   ```

   This calls `registry.quarantine_consent_log()` and prints the result
   as JSON: `{"ok", "quarantined", "quarantine_file", "integrity_ok",
   "reasons"}`. On failure the result additionally carries `"error"` (a
   snake_case failure code) and `"instructions"` (operator next steps);
   `integrity_ok` and `reasons` are present on every branch, success or
   failure. It:

   * re-verifies every line against the machine key;
   * MOVES each unverifiable line to `channel_consent.quarantine.jsonl`
     (0o600, next to the log) as a quarantine record holding the line's
     ORIGINAL BYTES verbatim plus reason, line number, timestamp, and an
     HMAC signature — nothing is deleted, ever;
   * rewrites the consent log with only the verified entries (atomic
     replace, fsync). The repair holds a cross-process lock from the
     initial read through the replace, so entries written by another
     process mid-repair are never lost;
   * appends a signed `consent_quarantine` audit entry naming the
     quarantine file and the quarantined count, so the repair itself is
     visible in `consent_history`;
   * re-verifies the repaired log and reports `integrity_ok`.

   A zero-quarantine result (`quarantined: 0, integrity_ok: True`) means
   the log was already clean — nothing changed.

   **If it refuses** with `"error": "quarantine_requires_confirmation"`:
   every line failed verification — that looks like a lost or replaced
   integrity key, NOT a torn line. Do NOT force yet. The output lists
   `key_backups_found` (`channel_consent.key.*.bak` next to the log):
   restore the right backup over `channel_consent.key` and the whole log
   re-trusts with no data movement. Only when you are certain the key is
   gone for good (e.g. after a deliberate rotation with no backup to
   restore) re-run with explicit confirmation:

   ```
   veto channel repair --force
   ```

   (Python REPL equivalent: `registry.quarantine_consent_log(force=True)`.)

   **If it fails** with `"error": "quarantine_audit_write_failed"`: the
   repair itself succeeded (the bad lines are in the quarantine file and
   the log was rewritten) but the signed `consent_quarantine` audit entry
   could not be appended — the repair is unaudited. Do NOT re-run the
   repair (it would report "nothing to quarantine"). Follow
   `result["instructions"]`: once the filesystem error is resolved, call
   `registry.record_quarantine_audit(quarantine_file=...,
   quarantined=..., reasons=...)` with the values from the failed result;
   it appends the same signed audit entry the repair would have written.

3. **Re-verify.** Confirm `consent_history(channel,
   with_integrity=True)` returns `integrity_ok: True`, and that the
   `consent_quarantine` audit entry is present. Confirm no new
   `CONSENT INTEGRITY` ERROR logs appear.

4. **Re-enable (only if needed).** Every verified `enable` entry
   survived, so enabled channels read as enabled again immediately —
   check `enabled_channels()`. If a channel's OWN `enable` entry was
   among the quarantined lines (its signature was bad), that channel
   must be explicitly re-enabled by the user
   (`enable_channel(name, confirm=True, via=...)`) — there is no bulk
   restore of quarantined entries, ever.

5. **Audit.** The quarantine file is the permanent record of what was
   moved: `channel_consent.quarantine.jsonl` is append-only JSONL, one
   record per quarantined line, with the original bytes. Investigate any
   `signature mismatch` reasons — a mismatched signature means the line
   was tampered with (or written under a different key), not merely
   torn.

**After a re-key (Procedure A), quarantine is the cleanup.** Rotation
invalidates every old entry by design; once channels are re-enabled and
fresh approvals collected, run `veto channel repair` to archive
the cryptographically-dead entries out of the log, keeping the audit
trail in the quarantine file. (If the repair refuses with
`quarantine_requires_confirmation` — every remaining line is dead —
re-run as `veto channel repair --force`; after a deliberate rotation
that confirmation is expected.)

**Never do this:** hand-edit the log to delete bad lines. Deletion is
silent and unaudited; quarantine is the only supported repair.
* **Do not copy `channel_consent.jsonl` between machines** and expect it
  to verify: the key is per-machine. A copied log fails closed, which is
  the safe direction.
* **Rotation is the same procedure** (`rotate_consent_key()`), used
  proactively when the key may have been exposed (e.g. a backup was
  shared). Exposure of the key alone does not decrypt anything (the log
  is plaintext), but it would let someone forge entries — rotate and
  re-enable.
* **Old entries are never re-trusted.** Even if the lost key is found
  later, `rotate_consent_key()` archives it; to re-trust the old log you
  must deliberately restore the archived `.bak` file over
  `channel_consent.key` — a conscious operator decision, never automatic.

## Design note (why fail-closed is the safe default)

The store gates real-world sends. If key loss *silently* invalidated
consents but sends still went out (or worse, silently re-enabled
channels), an attacker who deletes the key file could bypass the entire
consent layer. Failing closed — disabled channels, refused sends, loud
logs, and this runbook — keeps the failure visible and recoverable.
