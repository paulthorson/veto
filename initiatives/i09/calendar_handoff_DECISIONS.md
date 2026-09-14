# calendar_handoff — decision records

Initiative 09, Epic 4. Kept alongside `calendar_handoff.py` so Rule 3/4
(scope / product-goal) review checks have explicit material to evaluate.

## Scope

**This unit does:**

- Build interview-prep and follow-up-reminder event DRAFTS from
  application records. Drafting is side-effect free: it reads records,
  changes nothing, touches no calendar.
- Return unconfirmed drafts with review instructions when
  `confirm_and_handoff` is called without `confirm=True`.
- On `confirm=True`, validate every draft through the single `_validate_draft` gate — field types (start_iso/end_iso str, attendees a list of str, attendees_confirmed a strict bool, event_type in its known vocabulary), timezone-aware start/end with start < end, no placeholder/blank/near-miss company, role, or title unless explicitly overridden, attendees stripped unless explicitly confirmed per draft — then run the human confirmation flow (`circuit_breaker.require_action_confirmation`): the events are rendered on the terminal and the human must type a per-call random confirmation value. Only that typed value authorizes the handoff PAYLOAD the wiring layer executes against the user's connected calendar. `confirm=True` REQUESTS the prompt; it never asserts approval, and no caller can pre-assert the typed value. Non-interactive callers fail closed with SystemExit(2). Both hand-constructed drafts and serialized dicts funnel through this one gate; there is no second path around it.
- Attach a stable per-draft `idempotency_key` (content hash naming the
  EVENT, not the confirm attempt) so the wiring layer can upsert double
  confirms: create when the key is unknown, apply as an UPDATE when a
  known key arrives with different event content (e.g. attendees newly
  confirmed), and drop only byte-identical re-confirms. Key-only dedupe
  is explicitly NOT the contract — it would silently swallow a confirmed
  re-confirm (lost update). See decision 5.
- Fail closed on ambiguous input: naive datetimes, unparseable dates,
  negative/zero-length prep windows, and empty handoffs all raise or
  return `ok:false` with a named error instead of guessing.

**This unit does NOT do:**

- Write to any calendar itself. The actual write happens at the wiring
  layer (server.py / cli.py / webui.py) through the user's own calendar
  grant (google-calendar skill). This module names the destination and
  declares exactly what data each event carries.
- Send attendee invitations on the user's behalf without explicit
  per-draft opt-in (`attendees_confirmed=True`).
- Guess missing times or timezones: drafts without times are returned
  for the user to complete, never handed off.
- Check that a calendar is connected. This module never touches a
  calendar, so it cannot verify connectivity — that check belongs to the
  wiring layer (server.py / cli.py / webui.py), which executes the
  payload through the google-calendar skill and must refuse when no
  calendar is connected. The manifest therefore does not advertise
  "no calendar connected" as a block reason; every manifest claim is
  true of THIS module.

## Product goal + metric

**Goal:** make confirming an interview-prep or follow-up event feel
instant and safe — the user reviews a fully-formed draft once, confirms,
and the event lands on their real calendar exactly as reviewed, with no
surprise invites and no duplicate events.

**Metric (success criteria for this unit):**

1. Zero calendar writes without the human typing the per-call
   confirmation value on an interactive terminal (gate holds under
   test).
2. Zero payloads containing attendees the user did not explicitly
   confirm (enforced by stripping at payload build time).
3. Zero ambiguous datetimes reaching a payload: every handed-off event
   carries a timezone-aware start/end with start < end.
4. Confirming the same draft twice produces identical idempotency keys,
   so the wiring layer can guarantee no duplicate events.

## Key decisions

Every entry carries explicit `scope_driven:` and `product_goal:` fields so
the Rule 3 (scope) / Rule 4 (product-goal) mechanical checks have
per-entry material to evaluate.

| # | Decision | Rationale | scope_driven | product_goal |
|---|----------|-----------|--------------|--------------|
| 1 | Unconfirmed attendees are stripped, not rejected | Keeps handoff fail-safe toward the event itself: the user still gets their reminder on the calendar, but no invitation is ever sent without explicit naming. | scope_driven: yes — stripping unconfirmed attendees is exactly the declared scope ("no invites without per-draft opt-in"); sending invites unprompted is outside this unit's scope. | product_goal: serves metric 2 (zero payloads containing unconfirmed attendees) and the goal's "no surprise invites". |
| 2 | Naive datetimes raise instead of assuming UTC | Silent UTC-stamping moved interview times by hours for non-UTC users. Fail-closed forces the caller to be explicit. | scope_driven: yes — fail-closed on ambiguous input is declared in the Scope section above; guessing a timezone would exceed the unit's mandate. | product_goal: serves metric 3 (zero ambiguous datetimes reaching a payload) and the goal's "lands exactly as reviewed". |
| 3 | Malformed dates raise instead of falling back to `now()` | A typo in a date silently becoming "today" put reminders on the wrong day. A clear error beats a plausible-looking wrong date. | scope_driven: yes — declared in Scope ("fail closed on ambiguous input ... instead of guessing"); a silent fallback would exceed the unit's mandate. | product_goal: serves metric 3 (zero ambiguous datetimes reaching a payload) and the goal's "lands exactly as reviewed". |
| 4 | Placeholder content blocks handoff unless `allow_placeholders=True` | Confirming "Interview prep — Unknown role @ Unknown company" onto a real calendar is a worse outcome than refusing and asking for the real details. The gate matches the canonical placeholders, blank/whitespace strings, and case-insensitive near-misses ("unknown company") on company, role, AND title — a hand-constructed or from_dict draft with `company=""` is treated as missing, not as "has a company". | scope_driven: yes — placeholder blocking is declared in Scope ("no placeholder/blank/near-miss company, role, or title unless explicitly overridden"). | product_goal: drives a placeholder-free metric — zero payloads carrying "Unknown company"/"Unknown role" (including blank and near-miss equivalents), direction: downward (fewer garbage events). It is traded off against refusal: the unit prefers `ok:false` with a named error over shipping a wrong event, serving the goal's "the event lands on their real calendar exactly as reviewed". |
| 5 | Idempotency key is a content hash on the draft, not a random UUID | A random key changes per confirm and cannot dedupe; a content hash is stable across retries of the same draft. The hash excludes confirmation-state fields (attendees_confirmed) so confirming attendees does not change the key. TRADE-OFF (acknowledged): because the key is stable across a confirm → set attendees_confirmed → re-confirm cycle, the wiring layer CANNOT distinguish that re-confirm from a retry on the key alone — key-only dedupe would silently drop the confirmed invite (a lost update: the payload says attendees included, but nothing new is written, and retrying keeps producing the same key forever). The chosen contract is therefore upsert-on-key, not dedupe-on-key: the wiring layer must create on an unknown key, APPLY as an update when a known key arrives with different event content, and drop only byte-identical re-confirms. The payload `note` states this exact dedupe-vs-update rule. | scope_driven: yes — the idempotency key is declared in Scope ("stable per-draft idempotency_key ... so the wiring layer can upsert double confirms"). | product_goal: serves metric 4 (confirming the same draft twice produces identical idempotency keys) — identical re-confirms are still dropped, so "no duplicate events" holds, while the update rule guarantees a newly confirmed invite is actually written instead of silently lost. |
| 6 | Draft builders accept dicts back via `from_dict` | The `confirm=False` path returns serialized dicts for review; accepting them back on confirm closes the round trip without a type mismatch. There is exactly one validator (`_validate_draft`): hand-constructed drafts and dicts both funnel through it — field type checks (start_iso/end_iso str, title/description/company/role/application_id str, event_type in its known vocabulary, reminders_min a list of ints, attendees a list of str, attendees_confirmed a strict bool), then `_parse_when` on start/end (naive / non-string / unparseable rejected), then start < end. Failures are dropped and named, never written. | scope_driven: yes — what the user gives up: dict inputs are NOT a general-purpose event format; unknown keys are silently ignored, missing keys fall back to dataclass defaults only for fields that have them (the five required fields have no defaults and raise TypeError when omitted), and only drafts shaped exactly like CalendarDraft round-trip. What ships faster: the confirm=False review output can be fed straight back into confirm=True with no re-typing, so the wiring layer never needs a separate "finalize" call — the review→confirm round trip is the one object declared in Scope ("return unconfirmed drafts with review instructions ... on confirm=True ... return a handoff PAYLOAD"). | product_goal: serves the goal's "the event lands exactly as reviewed" — metric: review→confirm round-trip fidelity failures (fields in the confirm=True payload that differ from what the confirm=False review path returned), direction: downward to zero. The review output and the confirm input are the same object, so any mismatch is a defect, not a feature of the format. |
| 7 | Connectivity is not checked by this module; the manifest no longer lists "no calendar connected" as a block reason | Checking connectivity would require touching a calendar or its grant — outside this unit's side-effect-free scope (drafting touches no calendar). Advertising a block this module cannot enforce would be a lie the review loop would catch. | scope_driven: yes — Scope says this module names the destination and declares the data but never performs the write; a connectivity check is part of the write path, so it belongs at the wiring layer (server.py / cli.py / webui.py), which must refuse when no calendar is connected. | product_goal: keeps the manifest an honest declaration — the goal's "instant and safe" fails if the handoff payload advertises guarantees it does not enforce. |
| 8 | `confirm=True` no longer asserts confirmation — it requests the human confirmation flow (directive §6.1, 2026-09-14) | The old contract let ANY caller authorize a calendar write by passing the literal `True` — a code path (agent wiring, bug, confused deputy) could hand events to the google-calendar skill without the human ever confirming. Option (a) — removing the parameter — was rejected: it would kill the review→confirm round trip (decision 6), leaving no way to preview drafts without a TTY prompt, or make the handoff unreachable. Option (b) keeps `confirm` as the fail-closed default (`False` → review payload, no prompt) but redefines `True` as "ask the human now": authorization is the human typing a per-call random value on an interactive terminal via `circuit_breaker.require_action_confirmation` (typed varying value, single-use approval, SystemExit(2) on non-TTY). The value is generated inside the module and never leaves the prompt, so no caller can pre-assert it — `confirm=True` can never be wired to the google-calendar skill as an authorization. | scope_driven: yes — the confirmation gate lives in this module ("the confirmation gate lives here", Scope) and this closes the one path around it: a caller-asserted boolean was a second, unaudited gate. | product_goal: serves metric 1 (zero calendar writes without the human's typed confirmation) and the goal's "safe" — the user still reviews once and confirms once, but the confirmation is now unforgeable by code. |
