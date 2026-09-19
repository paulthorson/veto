# Initiative 12 — WS6 telemetry decision record (2026-09-13)

Constitutional format: every entry carries genuine options (Rule 2, each
phrased "trades X for Y"), a `schedule_driven` flag (Rule 3), and an
`ops_goal` naming a metric and direction (Rule 4).

## Context

The blind adversarial review of `initiatives/i12/telemetry.py` returned
KICK_BACK with four Rule 1 blockers: the veto did not halt recording, the
tripwire did not persist offending payloads, corrupt state silently reset,
and separation of duties was name-string inequality. This record captures
the fix decisions, including the residual trust boundary the code cannot
close (local code cannot verify the human behind the keyboard) and the
honest scanner-behavior evidence: `privacy.scan_payload` catches the
phone-shaped evasion but NOT the bare-name `guide_id` evasion today, which
is why token validation screens values instead of relying on the scanner
alone. The re-reviewer must re-run
`test_scanner_behavior_on_evasions_is_honest` after the privacy hardening
lands.

### D1: veto halts recording immediately (C1)
- **Chosen: `record()` and the `enabled` property check the veto; a veto
  filed mid-run raises `VetoInForce` on the next record.**
  - Trades away uninterrupted collection during incident review to get a
    hard guarantee that a veto means "stop collecting now", not "stop
    re-enabling later".
- **Rejected: veto blocks only `reenable()` (status quo ante).**
  - Trades away the mid-run halt guarantee to get simpler state checks —
    rejected: the review showed collection continuing after a veto is
    exactly the failure the veto exists to prevent.
- `schedule_driven`: false — correctness-driven; the veto is a safety
  control, not a schedule item.
- `ops_goal`: "record attempts while a veto is in force that succeed —
  zero (proven in tests)".

### D2: quarantine persists the offending payloads, fail-safe trip (C2, C10)
- **Chosen: `_trip()` writes the full triggering payload plus the live
  events present at trip time into quarantine, access-controlled
  (directory 0700, files 0600); if the write fails, analytics are still
  shut off and the incident still recorded — the wire never fails open.**
  - Trades away "quarantine never holds content" purity to get forensic
    evidence the specialist can actually investigate; trades away
    best-effort shutoff to get a guaranteed halt.
- **Rejected: manifest-only quarantine (finding descriptions, no payloads).**
  - Trades away forensic evidence to get zero content at rest — rejected:
    the specialist cannot investigate what was not kept, and the old
    manifest falsely claimed "full event payloads quarantined".
- **Rejected: abort the trip if the quarantine write fails.**
  - Trades away the guaranteed shutoff to get storage atomicity —
    rejected: a safety mechanism that fails open on a disk error is not a
    safety mechanism.
- `schedule_driven`: false.
- `ops_goal`: "trips with a quarantine write failure that leave analytics
  enabled or the incident unrecorded — zero (proven in tests)".

### D3: corrupt state fails closed (C3)
- **Chosen: on unreadable/unparsable/misshapen state, stash the file aside
  (`.corrupt-<timestamp>` suffix), log loudly to stderr, and raise
  `CorruptStateError` — never return a fresh state.**
  - Trades away self-healing startup to get veto/incident/consent
    durability: a silent reset erases exactly the records the tripwire
    exists to protect.
- **Rejected: catch-and-return-fresh (status quo ante).**
  - Trades away durability to get an always-bootable store — rejected: one
    bad byte would have wiped an operator veto.
- `schedule_driven`: false.
- `ops_goal`: "corrupt-state startups that silently reset — zero (proven
  in tests)".

### D4: separation of duties via registry + distinct identities + hash chain (C4)
- **Chosen: a human-populated roles registry (`i12_telemetry_roles.json`,
  ships empty, populated by operator/ops via `set_roles`); clearing requires
  the registry-named specialist, verification requires the registry-named
  reviewer, and they must be different people; every clearing/veto/purge
  action is appended to a hash-chained JSONL log verified on every load;
  the residual trust boundary is documented honestly (see below).**
  - Trades away frictionless clearing (ops must populate the registry
    before any clearing) to get attributable, auditable, tamper-evident
    clearing.
- **Rejected: name-string inequality (status quo ante).**
  - Trades away real accountability to get zero setup — rejected by the
    adversarial review: one actor with Python access could play all three
    parts with invented names.
- **Rejected: cryptographic human identity (hardware keys / SSO).**
  - Trades away shippability to get strong identity proof — rejected: no
    such infrastructure exists for this local single-user tool; documented
    as the residual risk instead of pretended away.
- **Residual trust boundary (documented in the module docstring):** the
  module checks registry membership and distinctness; it cannot verify the
  human behind the keyboard. Ultimate enforcement of human independence is
  a human process owned by operator/ops — they populate the registry, they run
  the clearing, they answer for it.
- `schedule_driven`: false.
- `ops_goal`: "clearings completed by non-registry identities — zero
  succeed (proven in tests)".

### D5: strict schema — required fields, pinned domains, no dead kinds (C5, C8)
- **Chosen: every schema field required; `tool`/`surface`/`kind` values
  checked against the pinned `TOOLS`/`SURFACES`/`ARTIFACT_KINDS` domains;
  the dead `count` kind removed (its branch deleted, docstring updated).**
  - Trades away forward-compat leniency (old/partial events are rejected,
    not tolerated) to get strict contracts the scanner and the tripwire
    can reason about.
- **Rejected: permissive validation (unknown values pass if token-shaped).**
  - Trades away contract strictness to get flexibility — rejected: the
    review's `tool="resume_uploader"`, `surface="dark_web"` cases showed
    permissiveness is a smuggling vector.
- `schedule_driven`: false.
- `ops_goal`: "schema-invalid events recorded — zero (proven in tests)".

### D6: tightened token grammar, scanner as backstop (C6)
- **Chosen: `session_id` restricted to 1–32 alphanumerics; all other token
  fields screened against email/phone shapes, forbidden content patterns,
  and resume/JD markers at validation time; the privacy scanner remains
  the backstop (proven by test against the real `scan_payload`).**
  - Trades away free-form session ids and prose-ish token values to get
    layered smuggling resistance: grammar kills structured PII shapes,
    the screen kills marker-shaped values, the scanner catches the rest.
- **Rejected: 64-char loose tokens + scanner-only.**
  - Trades away layered defense to get flexibility — rejected: the review
    showed names and phone numbers fit comfortably in 64 chars, and the
    honest scanner test proves `scan_payload` does NOT catch the bare-name
    `guide_id` evasion today.
- `schedule_driven`: false.
- `ops_goal`: "constructed token evasions (`john_smith_5551234567`,
  `resume_tips_jane_doe_acme_corp`) recorded — zero (proven in tests)".

### D7: pluggable notifier, default stderr + marker file (O3)
- **Chosen: notifier configured at store creation — a callable receiving
  the incident dict, or an argv command hook (no shell); the default
  writes a loud stderr block AND drops a `PENDING-NOTIFICATION-<id>.json`
  marker file next to the state file. The roles registry doubles as the
  contact directory; the midnight-pager runbook lives in the module
  docstring.**
  - Trades away a guaranteed human wake-up to get a zero-config default
    that never silently swallows a trip; the operator configures a real
    pager hook in daemon contexts.
- **Rejected: mandatory pager configuration.**
  - Trades away out-of-box usability to get guaranteed escalation —
    rejected: a local dev tool must trip safely with no ops setup; the
    default's stderr + marker file is the honest middle.
- **Rejected: trip with no out-of-band signal (exception only).**
  - Trades away the pager path to get less code — rejected: an exception
    the caller swallows is a silent trip.
- `schedule_driven`: false.
- `ops_goal`: "trips with no out-of-band signal (stderr, marker, or hook)
  — zero (proven in tests)".

### D8: access-controlled quarantine + explicit-confirm retention (O2, O4)
- **Chosen: quarantine directory 0700, payload files 0600, described
  honestly as "access-controlled" — the word "sealed" dropped everywhere
  (no encryption is implemented); retention via `purge_quarantine`, which
  lists candidates and deletes only with explicit `confirm=True`, and
  never touches evidence for open or unverified incidents.**
  - Trades away the stronger word to get honesty; trades away automatic
    disk hygiene to get evidence preservation — retention is always a
    deliberate, audited human act.
- **Rejected: "sealed" quarantine wording without encryption.**
  - Trades away honesty to get stronger-sounding docs — rejected: the
    review caught it and the claim is now exactly what the code does.
- **Rejected: time-based auto-expiry of quarantine files.**
  - Trades away disk tidiness to get evidence safety — rejected:
    auto-deleting the evidence of an open incident is the failure mode.
- `schedule_driven`: false.
- `ops_goal`: "open-incident evidence deleted by purge — zero (proven in
  tests)".

### D9: veto lift and verification nits (C4, nit 10)
- **Chosen: `lift_paul_veto(confirmation)` requires the operator's explicit typed
  confirmation (≥12 chars), recorded verbatim in the audit log;
  `verify_clearance` rejects empty `evidence_ref` and duplicate
  verifications; `status()["tripwire_armed"]` reflects actual state
  instead of hardcoded `True`.**
  - Trades away one-call convenience to get an auditable human act and
    honest status reporting.
- **Rejected: bare `lift_paul_veto()` call.**
  - Trades away the audit trail to get convenience — rejected: a veto
    lifted by a stray call is no veto.
- `schedule_driven`: false.
- `ops_goal`: "bare veto lifts and duplicate verifications that succeed —
  zero (proven in tests)".
