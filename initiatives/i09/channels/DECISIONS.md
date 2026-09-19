# Initiative 09 — communication connectors: decision records

## 2026-09-13 — Round-4 rework: atomic-claim fail-open, quarantine result shape, key permissions, dual-TTY approval

**scope_driven:** true — scoped to the round-4 blind reviewer's findings
against the round-3 rework (MAJOR 1, MINORs 2–6, two nits). The reviewer
verified the six prior-round blockers (quarantine, enable divergence,
real-email test, terminal injection, quarantine race, repair surfacing)
as FIXED; none of those code paths were touched.

**product_goal:** "unauthorized sends, down" (unchanged) + "failed
consent/approval writes reported as success, down: the count of consent
or consumption-marker writes that fail to persist yet are reported as
successful operations trends to zero — every failed write is a failed
operation."

**Context.** The round-4 blind review (KICK_BACK on the round-3 rework)
verified all prior blockers as FIXED and raised: the atomic claim fails
OPEN when the consumption-marker write fails (MAJOR 1 — a double-spend
window: `authorize_send` returned success while no `approval_consumed`
entry existed in the log, so a second process could consume the same
single-use approval), plus MINORs on the repair's CLI naming, the
quarantine failure-result shape, the permanently-unaudited repair, the
stdin-only TTY check, and unchecked key-file permissions.

**Decisions.**

* *MAJOR 1 — a consumption marker that did not persist fails the claim.*
  `_consume_approval_locked` now raises OSError when the
  `approval_consumed` append fails, rolling back the in-process
  `_CONSUMED_APPROVALS` mark first so the approval is left unspent.
  `authorize_send` catches it at all three claim sites and refuses with
  the named refusal `approval_consume_failed` — never reports success —
  so a single-use approval cannot be double-spent by a failed-then-
  retried claim. The transport-layer `consume_approval` instead applies
  its own long-standing failure policy, pinned by the browser-apply
  unit's B1 test: it logs ERROR loudly, keeps the in-process mark
  (same-process reuse stays blocked), and does not raise into the
  transport caller — the cross-process replay window on a failed marker
  write there is the documented residual risk
  (docs/i09/decisions/communication-connectors.md, routed to the operator).
  The module docstring "CLAIM (atomic)", `authorize_send`'s
  "ATOMIC CLAIM", and the round-3 MAJOR 4 entry now state the guarantee
  holds exactly when the marker write persists, and name the failure
  mode. Rationale: the module's own convention — enable/disable/
  approval-mint all treat a failed write as a failed operation — now
  covers the claim too; a "successful" claim with no persisted marker is
  precisely the double-spend the atomic claim was built to prevent.
* *MINOR 2 — warning names the path that exists.*
  `enable_channel`'s integrity warning now leads with
  `registry.quarantine_consent_log()`, naming the planned
  `veto channel repair` CLI only as the future shortcut (the CLI is
  spec'd in `integration_notes.md` for the integration sweep — it is not
  live yet).
* *MINOR 3 — failure results match the documented shape.* Every
  `quarantine_consent_log` failure branch now returns `integrity_ok`,
  `reasons`, and `instructions` alongside `error` — RECOVERY.md documents
  the result as `{"ok", "quarantined", "quarantine_file", "integrity_ok",
  "reasons"}` plus `error`/`instructions` on failure — instead of
  correcting the runbook. The `reasons` classification now runs before
  the fresh-key force-gate so the refusal carries it too.
* *MINOR 4 — a repair whose audit write failed can be re-audited.* New
  public entry point `record_quarantine_audit(quarantine_file,
  quarantined, reasons)` appends the signed `consent_quarantine` entry;
  the `quarantine_audit_write_failed` branch re-verifies the store and
  returns instructions naming the exact re-audit call (re-running the
  full repair would report "nothing to quarantine"). The standing
  promise — "the repair itself is a signed consent_quarantine entry" —
  is now satisfiable even on this path. RECOVERY.md Procedure B
  documents the re-audit step.
* *MINOR 5 — approval needs a real terminal, both directions.*
  `_stdin_is_tty` (kept as the single interactive-boundary seam — the
  name is historical) now requires BOTH stdin and stdout to be TTYs:
  stdin carries the typed answer, stdout carries the draft display, and
  a stdin-TTY with stdout piped away would have the user typing "send"
  blind. Fails closed with `not_interactive` otherwise.
* *MINOR 6 — pre-existing key file permissions are checked.*
  `_load_or_create_key` now refuses to operate (OSError naming the file)
  when the existing key file is accessible beyond its owner (any
  group/other permission bit set) — a restored/copied world-readable key
  was previously used without complaint. The refusal fails closed
  through the existing `_read_consent` path (channels read disabled,
  sends refused, RECOVERY.md pointer).

**Alternatives considered.**

* *Documenting the stdin-only TTY check as a trade-off instead of
  fixing.* Rejected: a user typing "send" without seeing the draft breaks
  the boundary's core promise ("presents the ACTUAL recipient"); the fix
  is one more `isatty()` call.
* *Correcting RECOVERY.md instead of the quarantine failure results.*
  Rejected per the reviewer's preference: operators and the planned CLI
  parse one stable result shape; the code now matches the docs.

## 2026-09-13 — Round-3 rework: display sanitization, store locking, atomic approval claim, quarantine hardening

**scope_driven:** true — scoped to the round-3 review findings against
the send boundary and the consent store. Transport modules
(`email_sync`, `browser_apply`) and the real `cli.py`/wizard were OUT
of this rework's file scope; where a finding needed them, the exact
wiring is specified in `integration_notes.md` for the integration sweep
instead of being half-built here.

**product_goal:** "unauthorized sends, down" (unchanged) + "consent-store
repair audit-data loss, down: lines silently deleted or lost by a repair
trends to zero — 100% of moved lines preserved verbatim in the quarantine
file, plus a signed consent_quarantine audit entry per repair" — the send
boundary stays atomic and honest, and every repair path preserves the
audit trail.

**Context.** The round-3 blind review (KICK_BACK on the round-2 rework)
verified both original blockers (quarantine repair; the real-email test)
as FIXED and raised: a veto-class terminal-injection attack on the
approval display (BLOCKER 1), a quarantine concurrency data-loss window
(MAJOR 2), no user-surface wiring for the quarantine repair (MAJOR 3), a
cross-process single-use race on approvals (MAJOR 4), and minors 6–12.

**Decisions.**

* *BLOCKER 1 — display sanitization.* `_present_draft` now strips ANSI
  escape sequences and replaces every other control character (newlines
  included) with a visible placeholder before printing. Sanitization is
  DISPLAY ONLY: the approval record's content hash and the sent payload
  keep the true bytes. Rationale: the boundary's promise is "presents
  the ACTUAL recipient" — an attacker-controlled draft must not be able
  to rewrite the terminal into showing a trusted address.
* *MAJOR 2 — consent-store lock.* A cross-process `fcntl.flock` on a
  sibling lock file (`channel_consent.lock`), held by `_append_consent`
  around every write and by `quarantine_consent_log` from the initial
  read through the atomic replace, so a concurrent append can never be
  lost. The lock is not reentrant (reentry raises loudly); internal
  `*_locked` helpers assume the caller holds it. Plain reads take no
  lock (`os.replace` is atomic; a torn concurrent read fails closed).
* *MAJOR 4 — atomic approval claim.* `authorize_send` now validates AND
  consumes the approval as one atomic step under the store lock; the
  loser of a cross-process race fails closed with
  `approval_already_used`. `consume_approval` is idempotent (the
  transport layer's follow-up call is a no-op once claimed). A refused
  downstream send does not refund the approval — fail closed. The
  interactive prompt itself runs OUTSIDE the lock (a user may take
  minutes to answer); the just-minted approval is then claimed
  atomically, failing closed on any ambiguity.
  [Round-4 correction: the "atomic" claim above was overstated — it holds
  exactly when the `approval_consumed` marker write persists. Round 3's
  code returned success when that write failed (a fail-open double-spend
  window). Round 4: a marker-write failure now fails the claim
  (`approval_consume_failed`, in-process mark rolled back so a retry can
  claim again) — see the round-4 entry above.]
* *MAJOR 3 — repair wiring.* `quarantine_consent_log` gained `force=`
  and the CLI spec in `integration_notes.md` §2 gained a `channel
  repair` action with `--force`. GAP (honest): the real `cli.py` and the
  opt-in wizard live outside this initiative's file scope
  ("Do not edit cli.py… in this initiative"), so the wiring is specified
  exactly in `integration_notes.md` for the integration sweep — it is
  NOT yet live in the CLI, and the wizard does not offer repair yet.
  `enable_channel`'s integrity warning now leads with
  `registry.quarantine_consent_log()` (the path that exists), naming the
  planned `veto channel repair` CLI only as the future shortcut, and
  RECOVERY.md Procedure B documents the repair end-to-end.
* *MINOR 6 — quarantine dedup.* Records already present in the
  quarantine file (by `(line_no, original_line)`) are not appended
  again, so a re-run after a rewrite failure cannot duplicate records.
* *MINOR 7 — rotation audit.* `rotate_consent_key` now writes a signed
  `consent_key_rotated` audit entry under the fresh key (with the backup
  path), so rotations are visible in `consent_history`.
* *MINOR 8 — signed quarantine records.* Each quarantine record carries
  an HMAC `mac` (same machine key, same scheme as consent entries;
  verifiable with `_verify_entry`) — tampering with the quarantine file
  is detectable. Chosen over leaving it unsigned: the quarantine file IS
  the audit trail for destroyed trust, so it must be as tamper-evident
  as the log.
* *MINOR 11 — case normalization.* One `_normalize_channel` helper;
  `request_send_approval`, `authorize_send`, `_validate_approval`,
  `_find_unconsumed_approval`, and `consume_approval` all normalize, so
  `channel="Gmail"` binds the same as `"gmail"`.
* *MINOR 12 — fresh-key safeguard.* When EVERY log line fails
  verification (the signature of a lost/replaced key, not a torn line),
  `quarantine_consent_log` REFUSES loudly without `force=True`, advising
  the operator to restore the key backup first — quarantining would
  archive all entries permanently and they could never be re-trusted.
  `veto channel repair --force` is the explicit confirmation.

**Alternatives considered.**

* *Lock-free quarantine (copy-then-compare).* Rejected: without a lock
  there is no way to close the read→replace window against a concurrent
  `O_APPEND` writer; the lock is the standard primitive and `fcntl` is
  already the platform.
* *Claim-then-validate (append consumption first, validate after).*
  Rejected in favor of validate-then-consume under one lock: the lock
  makes the ordering moot, and validate-first keeps invalid approvals
  from ever writing consumption entries.
* *Refusing to sanitize and instead rejecting hostile drafts.* Rejected:
  the approval prompt must show the user what would actually be sent —
  including weird bytes — so they can judge it; hiding the draft would
  weaken the human review the boundary depends on.

## 2026-09-13 — Interactive confirmation at the send boundary (Rule 1 rework)

**scope_driven:** true — scoped to the send boundary, where both Rule 1
attacks were demonstrated. Enablement, wizard UX, and standing-user-key
designs were deliberately OUT of this rework's scope so the fix shipped
in one review cycle.

**product_goal:** "unauthorized sends, down" — the sends that must trend
to zero are sends with no genuine interactive user approval. The boundary
makes unauthorized sends structurally impossible, not merely
discouraged.

**Context.** The Rule 1 blind RE-review (`init-09-comms-rereview`,
KICK_BACK) demonstrated two live attacks against the consent/token send
boundary:

1. A caller with zero user involvement minted its own "approval token"
   (`draft_approval_token` — a public deterministic SHA-256 of public
   content), then called `send_followup(confirm=True,
   approval_token=self_minted)` and sent arbitrary email. The token bound
   content to content, not a human approval event to content.
2. `email_sync.send_followup` could be called directly with a
   caller-asserted `confirm=True`, bypassing the registry/token layers
   entirely.

**Decision.** Implement interactive confirmation AT THE SEND BOUNDARY —
the send path itself presents the actual recipient/subject/body and
requires a genuine interactive user approval before sending:

* `registry.request_send_approval(draft)` prints the exact payload on the
  terminal and requires the user to type `send`. Only this function can
  mint an approval record: `{nonce, content_hash(to/subject/body),
  timestamp}`, HMAC-signed into the consent log.
* `registry.authorize_send` accepts only (a) an `approval_id` naming a
  valid, fresh (15-min TTL), unconsumed approval for the EXACT draft, or
  (b) a fresh unconsumed approval already on file for that exact
  content. Otherwise it runs the interactive prompt (TTY) or REFUSES
  (no TTY — agents, scripts, MCP servers fail closed).
* `email_sync.send_followup` enforces the identical boundary internally —
  it never trusts caller-passed flags. `confirm=True` is demoted to a
  deprecated no-op (warns, still routes through the interactive prompt).
  `draft_approval_token` remains as a display/reconciliation digest only;
  the send path never accepts it as authorization.
* Approvals are single-use (`consume_approval` before the transport
  attempt; a failed send does not refund).

**Alternatives considered.**

* *Signature from a user-held key.* Stronger for headless/agentic flows,
  but no user-held signing key exists in this codebase and introducing
  key management (generation, storage, agent access) is a larger,
  riskier change. Revisit if a standing user key (e.g. SSH agent) is
  adopted project-wide.
* *Removing `confirm=` from signatures entirely.* Would break existing
  callers with TypeError; demotion to a fail-closed no-op was chosen so
  old call sites refuse safely with instructions instead of crashing.
* *Per-channel send methods each implementing their own prompt.* Rejected:
  one boundary in `email_sync.send_followup` (enforced via
  `authorize_send`) covers every caller, including direct calls —
  closing the bypass the review demonstrated.

**Residual question (for the operator — veto clearance).** Channel *enablement*
is still a caller-asserted `confirm=True` + enumerated `via` source.
A rogue enable alone can never cause a send (the send boundary is
independent), but whether enablement itself should be interactive is
undecided. The veto still needs the operator's explicit clearance after
re-review — disclosure does not clear it.

**scope_driven:** true — shipping caller-asserted enablement NOW is
faster; interactive enablement is deliberately deferred to the open
veto rather than holding up the send-boundary fix.

**product_goal:** "unauthorized sends, down" (unchanged) — the residual
risk is a rogue *enable*, which alone can never cause a send.

**Trade-off (named).** This decision ships faster at the cost of
weaker interactive enablement assurance: an agent could self-assert
`confirm=True` + `via="api"` for enablement — the `via="api"`
allowlist entry must NEVER be read as user opt-in. Mitigation: the
independent send boundary means a rogue enable buys an attacker
nothing without a genuine interactive send approval; the MCP surface
for enablement is held unwired pending the veto (see
`integration_notes.md` section 1d).

**Human gate (formal).** Only the human clears a veto (framework
`overturn_verdict` is human-only). Action required from the operator: grant or
deny explicit clearance for (a) shipping interactive enablement as
specified, or (b) accepting the residual caller-asserted enablement
indefinitely. Routed to the operator — pending.

## 2026-09-13 — Consent-store integrity and fail-loud writes (kept)

**scope_driven:** true — kept minimal: fail-loud writes, a signed
append-only log, and a key-loss runbook. No new UI, no encryption, no
remote attestation.

**product_goal:** "untrusted consent acceptance, down" — tampered or
malformed consent entries must never be trusted; the number of
untrusted entries accepted into enablement/send decisions trends to
zero.

Prior rework, attack-verified by the RE-reviewer and kept intact: consent
writes that do not land are reported as operation FAILURES (never as
success), and the consent log is HMAC-signed with a machine-local key —
tampered/malformed entries are loudly logged and never trusted
(fail closed). `consent_history(..., with_integrity=True)` exposes the
integrity flag so callers can distinguish "no consents" from
"store unverifiable". Key loss fails closed with a recovery procedure —
see `RECOVERY.md`. The audit-preserving repair for torn/tampered lines
is `quarantine_consent_log()` (RECOVERY.md, Procedure B): bad lines are
moved — never deleted — to `channel_consent.quarantine.jsonl` with a
signed `consent_quarantine` audit entry.
