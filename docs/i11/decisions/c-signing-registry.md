# Decision Record: unit c fixer — signing + registry KICK_BACK rework

Run ID: 2026-09-13-c-signing-registry-fixer
Lane: Develop & Deliver
Started: 2026-09-13
Human arbiter: Paul Thorson

**This file is append-only.** Nothing above a committed line is edited. A correction is a new
entry that references the entry it corrects.

---

## 1. Request

> You are the rework fixer for Veto Initiative 11, unit c (signing + registry). A blind review
> returned KICK_BACK with two blockers (one triggering the Rule 1 constitutional veto on silent
> divergence) plus majors. Fix ALL findings in initiatives/i11/signing/ and initiatives/i11/registry/
> (and their tests). [...] Add a decision record with genuine options (why Ed25519-over-hash,
> why sidecar JSON) — "trades X for Y" sentences, convenience_driven field.

Solution shape assumed by the request: fix the listed findings in place; no redesign of the
four-gate model or the local-first JSON stores.

## 2. Artifacts produced

| Artifact | Path |
|---|---|
| Shared store machinery (schema version, atomic writes, .bak, flock, fail-loud reads) | `initiatives/i11/signing/store.py` |
| Hardened signing: fail-loud dev key, version compare, authenticated revocation, validated upgrades, rollback | `initiatives/i11/signing/sign.py` |
| Hardened registry: human-gate-only record_gate + audited override path, key continuity, monotonic versions, install-time re-verification, append-only gate audit, fail-closed revocation | `initiatives/i11/registry/registry.py` |
| Fix-proving tests (42 tests) | `tests/test_ext_signing_registry.py` |
| This decision record | `docs/i11/decisions/c-signing-registry.md` |

## 3. Decisions

### D1: Ed25519 signatures over the package hash (not HMAC, not hash-only)
- **Chose.** Keep Ed25519 (`ed25519-v1`): anyone can verify with the publisher's public key, only
  the private-key holder can sign, and the registry pins each publisher's public key so
  install-time verification is against the PINNED key, not a self-asserted one.
- **Over.**
  - HMAC with a shared secret: trades away public verifiability (any verifier would hold the
    secret and could forge) to get simpler key management — rejected; a curated registry's whole
    point is third-party-verifiable provenance.
  - Hash-only (sha256 recorded, no signature): trades away publisher identity entirely to get
    zero key management — rejected; it detects substitution but cannot say *who* published,
    which the revocation and key-continuity fixes depend on.
- **Because.** The review's fixes (authenticated revocation, key-continuity on resubmit,
  pinned-key install verification) all require asymmetric identity; Ed25519 gives it with
  small keys and fast verification.
- **convenience_driven.** false. Ed25519 is the less convenient option (key generation,
  sidecars, pinning); it was chosen because nothing weaker supports the threat model.
- **Confidence.** high. Covered by sign/verify/tamper/wrong-key tests.
- **Unknown at decision time.** Key-rotation story for a compromised publisher key (pinning
  currently has no rotation path; flagged, not solved here).

### D2: Signature sidecar JSON next to the package (not embedded in the manifest)
- **Chose.** Keep `.veto-signature.json` as a sidecar: the manifest stays the publisher's
  human-authored contract while the signature envelope is machine-managed and excluded from
  the hashed file set.
- **Over.**
  - Embed the signature in the manifest: trades away the clean manifest/package boundary (the
    signer would have to hash "the manifest minus the signature field" — a self-referential
    hash) to get one fewer file — rejected; self-describing signed envelopes are a classic
    footgun.
  - Central signature database: trades away package portability (a copied directory stops
    verifying) to get tidier directories — rejected; local-first means the directory is the
    unit of distribution.
- **Because.** `package_hash` excludes the sidecar by name, so signing never changes the hash
  it signs; verification is "hash the dir, compare, verify" with no special-casing.
- **convenience_driven.** false. The sidecar is slightly less convenient to carry around than
  an embedded field; the hash-exclusion correctness won.
- **Confidence.** high. Tamper test proves the exclusion works both directions.
- **Unknown at decision time.** None material.

### D3: Fail loudly on corrupt stores, keep a .bak of the last good state
- **Chose.** All three `_read_all` paths now go through `JsonStore.read()`, which raises
  `CorruptStoreError` (never returns empty) on invalid JSON, wrong shape, or unknown schema
  version; every write preserves the previous valid file at `<path>.bak` and is atomic
  (temp file + fsync + `os.replace`) under an exclusive `flock`.
- **Over.**
  - Return-empty on corruption (status quo): trades away operator truth (a corrupt registry
    reads as "nothing submitted", a corrupt revocation list as "nothing revoked") to get
    never-crashing reads — rejected outright; this was the Rule 1 veto trigger.
  - No backup, just atomic writes: trades away recovery after corruption/disaster to get
    simpler writes — rejected; a local-first single file has no replica to fall back on, so
    the `.bak` is the replica.
- **Because.** Silent divergence is the exact failure the constitution vetoes; loud failure
  plus a recovery path is the only honest shape.
- **convenience_driven.** false. Raising breaks callers (e.g. `crew.py ext_install` now
  propagates on a corrupt revocation file); that breakage is the point.
- **Confidence.** high. Tests corrupt all three stores and assert the raise; backup test
  asserts `.bak` holds the previous valid state.
- **Unknown at decision time.** Whether operators will actually notice a loud failure in a
  headless run — the error message names the backup path explicitly to close that gap.

### D4: record_gate is human-gates only; automated overrides are a separate audited path
- **Chose.** `record_gate` refuses `compatibility`/`security` with a pointer to
  `override_automated_gate`, which requires a named human reviewer (not `(automated)`), a
  written justification, and an explicit `authorized_by` reference; the override is flagged in
  the append-only gate history.
- **Over.**
  - Keep record_gate accepting all gates: trades away the entire automated-gate guarantee to
    get a simpler API — rejected; this was the second BLOCKER.
  - Remove overrides entirely (automated gates immutable): trades away operability on
    scanner false positives to get a smaller API — rejected; a security scanner *will* false
    positive, and the escape hatch must exist but be attributable.
- **Because.** The failure mode was "one call turns a failed security gate into pass"; the
  fix makes that call not exist and gives the legitimate need (false positives) a loud,
  attributable path instead of a quiet one.
- **convenience_driven.** false. Two methods and three required fields are less convenient
  than one; attribution won.
- **Confidence.** high. Test asserts `record_gate(id, "security", verdict="pass")` is
  refused and that each missing override field is refused independently.
- **Unknown at decision time.** What counts as a valid `authorized_by` reference long-term
  (currently a free string naming the human authority; a ticket/identity system would pin
  this down).

### D5: Resubmission preserves human verdicts on same version, requires re-review on new version
- **Chose.** Key continuity is absolute (a changed pinned key under an ext_id is rejected as
  substitution); versions must be monotonic (downgrades rejected). Same-version resubmission
  re-runs automated gates and preserves prior human verdicts; a newer version resets
  ux/governance to pending.
- **Over.**
  - Preserve human verdicts across versions too: trades away review validity (the verdicts
    were about different bytes) to get frictionless updates — rejected; a "pass" on v1.0.0
    must not bless v2.0.0's new code.
  - Require full re-review on every resubmission: trades away the legitimate "re-ran the
    scanners after fixing a finding" flow to get maximal strictness — rejected; same bytes
    need no new human judgment, and friction here punishes fixing findings.
- **Because.** The verdicts attach to bytes, not to ext_ids; version monotonicity plus
  key continuity makes "which bytes" unambiguous, and the policy follows from that.
- **convenience_driven.** false. Resetting human gates on every new version is operationally
  annoying; correctness of "pass means these bytes" won.
- **Confidence.** high. Tests cover substitution rejection, downgrade rejection, verdict
  preservation on same version, and reset on new version.
- **Unknown at decision time.** Whether `compare_versions` handles the project's future
  versioning scheme (documented as simple dotted comparison; pre-release suffixes compare
  lexically).

### D6: installable() re-hashes bytes and re-verifies against the pinned key
- **Chose.** The gatekeeper re-hashes the source dir and calls `verify_package` with the
  pinned key on every `installable()` check; missing source, hash drift, or signature failure
  each refuse install with a specific error.
- **Over.**
  - Trust the recorded hash (status quo): trades away install-time integrity to get a fast
    check — rejected; the recorded hash is a claim about the past, not evidence about now.
  - Re-verify only the signature, not the hash: trades away detection of unsigned byte
    changes that predate signing to get a shorter check — rejected; both are one hash apart
    in cost.
- **Because.** The review's phrase was "the gatekeeper must check, not delegate" — a
  registry that says "pass" while the bytes on disk changed is the registry lying.
- **convenience_driven.** false. Re-hashing on every check costs I/O; integrity won.
- **Confidence.** high. Test tampers with bytes post-registration and asserts refusal;
  test deletes the source dir and asserts refusal.
- **Unknown at decision time.** Performance on very large extension dirs (no caching yet;
  acceptable for a local curated registry).

### D7: Revocation authenticated by publisher-key signature, registry-bound to the pin
- **Chose.** `RevocationList.revoke` requires an Ed25519 signature over the canonical
  revocation payload by the claimed publisher key (no free-string `revoked_by`);
  `Registry.revoke` additionally binds to the key the registry pinned for that ext_id, and
  refuses when the entry has no pinned key.
- **Over.**
  - Free-string revoked_by (status quo): trades away all authorization to get a one-line
    API — rejected; anyone could revoke anything, this was a MAJOR.
  - Human-authority-only path (Paul clears): trades away publisher self-service (a
    publisher who discovers their own compromise must wait for a human) to get simpler
    authorization — considered; the publisher-key path was chosen because the publisher is
    usually first to know of compromise, and the pinned key already exists as the trust
    anchor. Documented as the road not taken, not as wrong.
- **Because.** Revocation is a security-critical write; its authorization must be
  cryptographic and bound to the same pin the install path trusts, or the two paths can
  disagree about who is authoritative.
- **convenience_driven.** false. Callers must now produce a signature (`sign_revocation`
  helper provided); unforgeability won.
- **Confidence.** high. Tests cover anonymous refusal, wrong-key refusal, tampered-reason
  refusal, and the registry-level pin binding.
- **Unknown at decision time.** Revocation of a version whose publisher key is itself
  compromised (needs the key-rotation story from D1).

### D8: Rollback retains the prior exact hash and bytes location
- **Chose.** `record_install`/`record_upgrade` retain `previous_package_sha256` and
  `previous_source_path`; `InstallStore.rollback()` restores them (and a second rollback
  rolls forward again, so the operation is symmetric).
- **Over.**
  - Version-string-only history (status quo): trades away recoverability to get a smaller
    record — rejected; a version string is not bytes, and "roll back to 1.0.0" is meaningless
    if 1.0.0's bytes are gone.
  - Full byte snapshots per version: trades away disk simplicity to get guaranteed
    recoverability even if the source dir moves — rejected for now; the retained source
    path plus hash lets the operator verify, and snapshots are a distribution concern.
- **Because.** The finding asked for "known-good bytes", and only an exact hash plus a
  location identifies known-good bytes.
- **convenience_driven.** false. Retaining prior state on every write is bookkeeping;
  recoverability won.
- **Confidence.** high. Tests assert retention fields, rollback restore, and roll-forward
  symmetry.
- **Unknown at decision time.** What happens when the retained source path is gone too
  (rollback restores the record; byte recovery then needs the operator's backup).

### D9: Append-only gate history; schema versions; honest provenance; locking
- **Chose.** Every gate change appends `{gate, prior_verdict, new_verdict, reviewer, date,
  notes, override, authorized_by?, recorded_at}` to `gate_history` (never edited);
  all three stores write `{"schema_version": 1, ...}` (legacy bare payloads still read);
  `submit()` without a publisher key records `pubkey_provenance: "self-asserted"`, never
  `"pinned"`; `JsonStore` locks reads (shared) and writes (exclusive) with documented
  single-writer discipline.
- **Over.**
  - In-place verdict overwrite (status quo): trades away the audit trail to get a smaller
    entry — rejected; "who changed this verdict and from what" is the governance record.
  - Recording the sidecar key as pinned when no key was supplied: trades away honesty to
    get a uniformly "pinned" column — rejected; self-asserted integrity is not provenance
    and must not be labeled as such.
- **Because.** Each of these was a separate minor/major; the common thread is that the
  store must not misrepresent its own state.
- **convenience_driven.** false throughout.
- **Confidence.** high. Tests assert history triples `(none→pending→fail→pass)`, schema
  version on disk, legacy reads, and provenance flags.
- **Unknown at decision time.** None material.

## 4. Options presented

| Option | Trades away | To get |
|---|---|---|
| Ed25519 over package hash (chosen) | Simpler key management | Publicly verifiable publisher provenance |
| HMAC with shared secret | Public verifiability (verifiers could forge) | Simpler key management |
| Hash-only, no signature | Publisher identity entirely | Zero key management |
| Signature sidecar JSON (chosen) | One fewer file | No self-referential manifest hash |
| Embed signature in manifest | Clean manifest/package boundary | One fewer file |
| Fail loud + .bak on corruption (chosen) | Never-crashing reads | Operator truth + recovery path |
| Return-empty on corruption | Truth about store state | Never-crashing reads |
| record_gate human-only + audited override (chosen) | Single simple API | Automated gates that can't be quietly flipped |
| Immutable automated gates | Operability on scanner false positives | Smallest API |
| Preserve verdicts on same version / re-review on new (chosen) | Frictionless updates | Verdicts that mean "these bytes" |
| Preserve verdicts across versions | Review validity | Frictionless updates |
| installable() re-hashes + re-verifies (chosen) | Fast checks | Install-time integrity evidence |
| Trust recorded hash | Install-time integrity | Fast checks |
| Publisher-signed, pin-bound revocation (chosen) | One-line revoke API | Unforgeable, correctly-scoped revocation |
| Human-authority-only revocation | Publisher self-service on compromise | Simpler authorization |
| Rollback retains prior hash + path (chosen) | Smaller records | Rollback to known-good bytes |
| Append-only gate history (chosen) | Smaller entries | "Who changed what, from what" audit |
| Honest self-asserted provenance (chosen) | Uniform "pinned" column | No mislabeled trust |

## 5. Neutral facts issued to the Ops Advocate

Not applicable — fixer loop, no advocate run. Findings came from the blind review recorded as
KICK_BACK for case tag init-11-c-signing-registry.

## 6. Verdicts

Not applicable — no critic/advocate/reviewer loop was run for the fixer pass itself. The
KICK_BACK findings and their responses:

| Finding | Response | Status |
|---|---|---|
| BLOCKER (Rule 1): `_read_all` swallows JSONDecodeError → empty | `JsonStore` raises `CorruptStoreError`, keeps `.bak`, atomic locked writes | revised |
| BLOCKER: `record_gate` accepts/overwrites any gate | Human-gates-only `record_gate`; `override_automated_gate` with human reviewer + justification + `authorized_by`, flagged in history | revised |
| MAJOR: no install-time re-verification | `installable()` re-hashes bytes + `verify_package` against pinned key | revised |
| MAJOR: no key continuity / version monotonicity on resubmit | Key substitution rejected; downgrades rejected; same-version preserves human verdicts, new version re-reviews | revised |
| MAJOR: unauthenticated upgrade reviews | Full review validation (reviewer/date/notes/verdict) + version ordering | revised |
| MAJOR: revocation authorization documented-only; Registry without RevocationList silently skips | Publisher-key signature required, registry-bound to pin; `Registry(..., None)` raises | revised |
| MAJOR: no rollback | Prior exact hash + source path retained; `rollback()` (+ roll-forward) | revised |
| MAJOR: no audit trail on gate changes | Append-only `gate_history` (who/when/prior/new) | revised |
| MINOR: schema versions | `schema_version: 1` on all three stores; legacy bare payloads still read | revised |
| MINOR: `_dev_private_key` silent ephemeral fallback | Raises `RuntimeError` when the key can't be persisted | revised |
| MINOR: submit() without pubkey records self-asserted key as pinned | `pubkey_provenance` honestly `"self-asserted"`/`"none"`/`"pinned"` | revised |
| MINOR: file locking | `flock` shared/exclusive + documented single-writer discipline | revised |
| MINOR: 8-vs-14 attack count in docs | NOT in fixer scope: the discrepancy is in `initiatives/i11/README.md` (lines 22/86), outside signing/ and registry/; truth is 14 (`ATTACKS` in `policy_kit/attacks.py`). Flagged for the owning unit. | flagged |

## 7. Worker response to verdicts

All KICK_BACK findings above are revised except the attack-count doc discrepancy, which lives
outside the fixer's directories and is flagged rather than touched.

## 8. Gate

Gate: n/a (fixer pass; commit sweep is the later human-gated step).
Routed because: scheduled gate — the later sweep commits, and only Paul clears.

### Human decision
- Arbiter: Paul Thorson
- Date: pending (commit sweep)
- Decision: pending
- Reason: —
- Vetoes cleared: none

## 9. Skips

| What was skipped | Instructed by | Recorded at |
|---|---|---|
| Committing any changes | Task instruction ("leave changes uncommitted") | 2026-09-13 |
| Reconciling the 8-vs-14 attack count | Out of scope: `initiatives/i11/README.md` is owned by another unit; flagged in §6 | 2026-09-13 |
| Publisher key-rotation story | Not requested by the review; noted as unknown in D1/D7 | 2026-09-13 |
