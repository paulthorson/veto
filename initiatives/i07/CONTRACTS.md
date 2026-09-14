# Initiative 07 — Consent Handshake Contracts

Published contracts for the mentor-introduction consent system. These are
the normative definitions the code implements; reviewers judge the code
against this document, not the other way around.

Version: 2 · Date: 2026-09-13 · Status: draft for adversarial review

Changes in v2 (blind-review KICK_BACK rework): pre-consent previews scrub
free-text fields for contact patterns (s3); the audit trail is a real
hash chain with `verify_audit()` (s6); malformed timestamps fail closed
instead of crashing (s5); a mentor's withdrawal of a pending request arms
the 30-day cooldown (s5); store writes are atomic and a corrupt store
refuses to operate (s6-adjacent, code); withdrawal contract narrowed to
live stages to match the code (s4); the dead `synthetic` parameter on
`mentor_respond` is removed.

## 1. Parties and identities

* **Mentee** — the local Veto user requesting an introduction. Identified
  by `mentee_id` (local) plus a `mentee_label`: a display name **or
  initials** — the mentee's privacy choice, symmetric with the mentor's.
* **Mentor** — a person with an opted-in mentor card in the local
  directory. Identified by the card id (derived from their LinkedIn URL).

Identity attributes (demographics, background, etc.) are **never**
collected by the system. The match questionnaire accepts `identity_preferences`
**only when voluntarily supplied** by the mentee, stores them verbatim on
the handshake record, and never uses them for ranking — they are a note to
the human reader, not a model input.

## 2. What counts as consent

Consent is an explicit, recorded act by an identified party:

* Mentee consent = calling `request_introduction` with a stated `goal`.
  A request without a goal is rejected — it is not a consent-bearing act.
* Mentor consent = `mentor_respond(..., decision="approve", channel=...)`
  where `channel` names how the mentor communicated the decision
  (e.g. `linkedin_dm`, `email`, `in_person`). The channel is REQUIRED.

**Anti-fabrication rule:** there is no code path that records a party's
consent without that party's explicit recorded act. In the local-first
pilot, the mentor's reply arrives out-of-band (LinkedIn, email) and the
user records it; the receipt requires the channel as provenance.

**Trust boundary (stated plainly):** in the local-first deployment the
`actor` argument is caller-asserted — the device belongs to one user, and
the tool trusts that user to record each party's acts honestly (their own
consent as mentee; the mentor's reply as reported to them). The code
cannot cryptographically authenticate the remote mentor; it can only
refuse to *invent* consent, require provenance, and keep a tamper-evident
append-only audit trail. A future hosted deployment must replace
caller-asserted actors with authenticated identities before any pilot
that involves real remote parties.

## 3. Revelation rule

Neither party's contact detail is revealed to the other until the
handshake is in state `mutual` (both consents recorded):

* Pre-consent, each side sees only **redacted previews**: name/initials,
  industry, role, seniority band, topics, bio, availability text, rating
  summary, match reasons. No LinkedIn URL, no email, no phone, no exact
  contact path — unless the mentor explicitly opted their LinkedIn URL
  into the public discovery card (`preferred_contact="linkedin_public"`).
  **Scrubbing (normalize-then-detect, 2026-09-14):** preview text is first
  normalized — NFKD, the full Unicode `Cf`/`Mn`/`Me` classes (zero-width
  and format characters) removed for detection, braille blanks and
  Hangul fillers (`U+2800`, `U+3164`, `U+115F`, `U+1160`) treated as
  blank separators, and a generated Unicode UTS #39 `confusables.txt`
  fold table (data-driven, not hand-enumerated) mapping every visual
  homoglyph to its ASCII lookalike — Cyrillic/Greek/Armenian lookalikes,
  izhitsa, palochka, lunate sigma, script-g, Komi de, fullwidth,
  mathematical, and circled forms. The visual fold is checked BEFORE
  NFKD (when a character has both a visual confusable and a
  compatibility decomposition, the spoof model wins: e.g. lunate sigma
  U+03F2 folds to `c`, not its NFKD target `ς`), with a handful of
  documented overrides where confusables.txt has dead ends. Digit
  sources/targets are excluded from the table, and the `0→o`/`4→a`
  "digit shadow" exists ONLY as a detector-local copy for the
  at/dot and obfuscated-dot detectors, so real digit detection is never
  corrupted. Sequence folding (`%40`, `%2E`) runs AFTER normalization
  and invisible-strip, closing the fullwidth-percent and
  ZWSP-split-sequence hole. Contact patterns are detected on the
  NORMALIZED text with spans mapped back onto the original, consuming
  any invisible residue. This closes the entire Unicode-evasion class by
  construction: fullwidth `＠`/`．`, ideographic `。`, IDN/homoglyph
  domains (Cyrillic, Greek, accented, zero-for-o), URL-encoded `%40`,
  `at`/`dot` and bracketed obfuscation (`[at]`, `[dot]`, `hxxp`,
  `linkedin[.]com`), spelled-out, vanity, and slash-separated numbers
  (`five five five 0132`, `1-800-FLOWERS`, `415/555/0132`), messaging
  handles (Telegram/Signal/Skype/Discord/WeChat/`name is` proximity/
  bare `@handle` ≥4 chars with an ASCII lookbehind so `@ noon` and
  CJK-adjacent handles behave), IPv4 literals, and `(0)` trunk
  prefixes. Strings containing bidi OVERRIDE controls (`U+202A`–`U+202E`)
  are fail-closed: the whole field is withheld, because overrides
  reorder what the reader SEES, so span detection cannot see what the
  reader sees. The modern isolate controls (`U+2066`–`U+2069`) are the
  Unicode-recommended mechanism and appear in legitimate RTL text, so
  they no longer withhold — contact patterns around them are scrubbed
  normally. Scrubbing is recursive over nested dicts (keys AND values),
  lists/tuples, and sets/frozensets — there is no container-shaped hole;
  tuple/frozenset types are preserved, dict keys are scrubbed without
  ever becoming unhashable, recursion is depth-capped (100) and fails
  closed past it, and UTF-8 bytes carrying contact content return a
  bytes marker. `topics` is a closed vocabulary: non-member entries are
  dropped, not scrubbed. Email addresses, phone numbers, and URLs are
  replaced with a `[redacted]` marker. Phone detection is
  shape-agnostic: US 10-digit and 7-digit local forms, international
  numbers anchored on the leading `+` country-code prefix with digit
  groups of any length (India `+91 98765 43210`, Brazil `+55 11 91234
  5678`, Kenya `+254 722 123456`, UK `+44 20 7946 0018`, FR/DE/JP …),
  and parenthesized area codes with non-US groupings (`(020) 7946
  0018`); extension tails (`x123`, `ext 7`, `#456`, `x12-34`) are
  folded into the number. URL detection covers schemed (`https://…`),
  `www.`-prefixed, obfuscated-dot, and bare domains on ANY TLD —
  there is deliberately no TLD allowlist (an allowlist fails open on
  every new gTLD); a bare domain is any dot-form whose final label is
  all letters (2+ chars), optionally followed by `:<port>`, which
  structurally excludes version numbers (`3.12`), decimals (`2.0`), and
  section refs (`4.5`) from ever matching. The numeric-typed fields pass
  genuine numbers through unchanged; a contact string placed in one of
  them (only possible via a hand-edited store) is scrubbed like any
  other field. The promise above holds even when a mentor's own card
  text contains contact details; the `linkedin_public` opt-in is the
  only exception and is deliberate — and the opt-in covers ONLY the
  profile URL: if the URL smuggles contact content in its path or query
  string, the field fails closed to `[redacted]`.
* Blocked and quarantined actors are excluded from discovery entirely
  (`safety.discovery_exclusions` → `matchmake(..., exclude_mentor_ids=...)`),
  not just from handshakes.
* `reveal_contact` refuses in every non-`mutual` state, including
  `withdrawn`-after-`mutual`. Block status is re-checked at reveal time.

## 4. Withdrawal

Either party may withdraw **at any stage while the handshake is live**
(`awaiting_mentor` or `mutual`), via `withdraw`. Terminal outcomes
(`declined`, `expired`) cannot be withdrawn from — the handshake already
ended, so there is nothing left to withdraw, and `withdraw` refuses with
an explicit error; withdrawing an already-`withdrawn` handshake is
likewise refused. Withdrawal is immediate and final:

* Pending requests end as `withdrawn`.
* Post-`mutual` withdrawal seals contact details for all future reads
  through that handshake. It cannot un-see details already read — the
  audit log records the revocation as a receipt for both sides.

## 5. Decline and expiry

* A mentor decline ends the handshake as `declined` and starts a 30-day
  re-request cooldown for that pair (anti-pestering). A mentor's
  withdrawal of a **pending** request arms the same cooldown: withdrawing
  a pending request is a refusal by another name, and without this the
  decline cooldown could be laundered through a withdraw. A mentee's
  withdrawal of their own pending request does NOT arm a cooldown — that
  is the requester's own consent loop, not a refusal.
* Unanswered requests expire after 7 days (`expired`); the mentee may
  re-request after expiry.
* Malformed timestamps fail closed, never crash the read path: a corrupt
  `expires_at` cannot prove a request is within its TTL, so the request
  is expired; a corrupt `updated_at` on a declined/withdrawn handshake
  keeps the cooldown enforced. Corruption is logged loudly and recorded
  in the audit trail.
* At most one `awaiting_mentor` handshake per mentor/mentee pair, and at
  most 5 new requests per mentee per rolling 24h (rate limits; see
  `safety.py` for the general engine).

## 6. Audit

Every transition appends one JSON line to `consent_audit.jsonl` with
timestamp, event, handshake id, actor, detail, `prev_hash` (the previous
line's `entry_hash`, or `GENESIS` for the first line), and `entry_hash`
(SHA-256 over the canonical JSON of the record). This is a real
hash chain: `verify_audit()` recomputes every `entry_hash` and checks
every `prev_hash` link, and fails closed — any forged, edited, reordered,
unparseable, or hash-less line is reported with its line number.
**There is no API to edit or delete audit entries.** Deletion requests
redact handshake payloads but leave a tombstone (`deleted: true`, no
PII) in the trail. Lines written before the hash chain existed (no hash
fields) do NOT verify — archive pre-chain audit files rather than mixing
formats.

**Deletion decision (explicit, 2026-09-13):** the audit trail is not a
recoverable PII loophole. On `delete_mentor_data` / `delete_mentee_data`,
the free-text `detail` field — which carries goals, labels, and note
excerpts — is replaced with a redaction marker on every entry tied to the
deleted party's handshakes or actor id. Audit LINES are never removed and
the structural fields (timestamp, event, handshake id, actor) are kept,
so the chain of evidence stays intact: the scrub re-chains the file
(recomputing `prev_hash`/`entry_hash` in order) so `verify_audit()`
stays green. The erasure receipt (`deletion` event) is left untouched as
proof the erasure happened.

The handshake store (`handshakes.json`) is written atomically (tmp file +
fsync + rename), so a torn write cannot leave a half-written store.
A store that exists but does not parse raises `CorruptStoreError` and
every entry point refuses to operate on it (fail closed) instead of
silently treating it as "no handshakes".

## 7. Segregation from job-application decisions

Mentorship data — cards, questionnaires, handshakes, ratings, session
notes — **must never influence job-application decisions** (fit scores,
shortlists, application queues). `safety.segregation_check()` scans the
mentorship stores and the application stores for cross-references and
fails loudly if any exist. This is a hard boundary, not a guideline.

## 8. Honesty invariants (cold-start)

* Empty mentor directory / no capacity / no path: the system says so
  plainly and offers next steps. It never invents mentors, people, or
  sessions.
* Synthetic fixtures (tests, demos) carry `SYNTHETIC-` prefixed ids and
  `synthetic: true`. Production code paths reject synthetic records
  where identity matters.
* The warm-path planner (epic 5) and the introduction handshake DRAFT
  only — Veto never sends messages, connection requests, or emails on
  anyone's behalf. Sending is always the human's explicit act in their
  own client.

## 9. Abuse controls (epic 6, `safety.py`)

* Block: a blocked actor never appears in matches/previews and cannot
  start or answer handshakes with the blocker; pending handshakes between
  the pair auto-withdraw.
* Report: reports carry a fixed category and free-text detail; 2+
  distinct reporters mark an actor `pending_human_review` (hidden from
  discovery, pending handshakes withdrawn, audit entry written). There is
  no auto-takedown: the real-party pilot human gate is Paul + an
  independent reviewer, and only `clear_quarantine(..., reviewed_by=...)`
  lifts the hold. Reporter ids are caller-asserted in the local-first
  deployment (§2 trust boundary), so two reports are a tripwire for human
  review, not a verdict — sockpuppet reports cannot be distinguished from
  real ones by the code, which is exactly why the human gate exists.
* Rate limits: token-bucket per (actor, action), enforced on `report`,
  `block`, and `clear_quarantine` only — the old `DEFAULT_LIMITS`
  entries for other actions were dead config (zero enforcement points)
  and were deleted rather than left as false advertising. Buckets are
  in-memory per process and do NOT survive CLI re-invocation: a soft
  in-session guard, honestly labeled. Introduction caps per pair and per
  day live in `consent.py`; contact-reveal limits in `session_kit.py`.
* Deletion: full erase of a mentor card, handshake payloads, session
  notes, and reports on request. Session notes are scrubbed BEFORE
  handshakes are tombstoned (tombstones carry no `mentor_id`, so the
  reverse order silently scrubbed zero notes and left note text
  verbatim). Free-text PII in the consent audit trail is scrubbed per
  §6 (chain kept intact, `verify_audit()` stays green). Block entries
  keep the id only (safety needs to remember who is blocked, never why
  in PII terms).
* Fail-closed safety state (Rule 1 fix): a corrupt `safety.json` raises
  `SafetyStateError` with a loud log line naming the file and the parse
  error — it is never silently replaced with an empty state (which
  deleted every block and quarantine with no log). Unreadable state is
  deny: `is_blocked`/`is_quarantined` return True,
  `consent_block_check` refuses, `discovery_exclusions` raises (callers
  treat as deny-all). Writes against unreadable state refuse entirely so
  an empty state never overwrites the real one.

## 10. Assumption flags

* **NETWORK-LAYER EVIDENCE GATE (assumption-built):** the roadmap holds
  network work until the individual product proves itself. This package
  is built anyway on the explicit assumption that Q4/Q1 product
  usefulness justifies the network layer. If the assumption breaks, this
  package is shelved behind the same evidence gate — it degrades to the
  pre-existing local-only cold-contact flow and nothing is lost.
* **Cold start:** built on the assumption that mentor supply and mentee
  demand exist. All empty-marketplace states are honest and guiding;
  activity is never faked.
* **Human gates (not build gates):** the two-sided-consent + abuse-case
  suite must pass Paul and the independent framework reviewer before any
  pilot; Q2-day-10 pilot targets need Paul + contracted user researcher +
  independent reviewer approval. The code ships the suite; humans clear
  the gates.

## 11. Enforcement choke points (cross-module dependencies, 2026-09-13)

Discovery-exclusion enforcement must live in exactly one choke point: inside `mentors.matchmake` itself (implemented by the mentors.py fixer), which applies `safety.discovery_exclusions(blocker_id)` on every call path — the caller-passed `exclude_mentor_ids` pattern was advisory-only and empirically leaked blocked/quarantined mentors on shipped surfaces (cli.py being the sole correct caller). Likewise, block/quarantine re-checks on private-data reads must live inside `session_kit._mutual_handshake` (implemented by the session_kit.py fixer), because post-`mutual` withdrawal no longer revokes access; `safety.py` exposes the predicates (`is_blocked`, `is_quarantined`, `consent_block_check`, `discovery_exclusions`, `quarantine_status`) but implements neither choke point — this section is the wiring note, not the implementation. Dependency (not implemented here): the block check on `mentor_respond` is owned by the consent.py fixer.
