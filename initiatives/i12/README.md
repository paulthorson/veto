# Initiative 12 — Public growth & education

Build status (2026-09-13): machinery built and tested. **NOT public.** No launches,
visibility changes, social posts, or marketing pushes happen without the operator's explicit order.

## What this package is

The Q3-2027 roadmap scope for earning distribution through utility: interactive
public tools, shareable artifacts, a transparent changelog, an education library,
onboarding experiments, and consent-based analytics with a hard privacy tripwire.

## Modules

| Module | Epic | Purpose |
|---|---|---|
| `contracts.py` | — | Pinned contracts: Initiative 04 decoder interface (mirrors `jd_decoder` API) and Initiative 10 packaging interface (expected; currently unshipped — adapters raise `PackagingUnavailable`, never fake it) |
| `tools.py` | 1 | JD decoder demo, fit explainer, application-risk check, role comparison (≤4). Local-only; proposal-only; no submit path exists |
| `privacy.py` | 2, 6 | Single definition of "content field": forbidden field-name patterns + content heuristics (email/phone, resume-JD markers, long free text) |
| `share.py` | 2 | Privacy-safe score cards, interview plans, progress snapshots — scrubbed by `privacy.py`, methodology link attached |
| `changelog.py` | 3 | Transparent changelog: shipped / rejected / limited + why. Missing rejections or limitations are flagged as defects |
| `onboarding.py` | 5 | 9-cell onboarding experiment (role maturity × tech comfort), deterministic assignment, dark patterns banned by code-checked rules |
| `telemetry.py` | 6 | Consent-gated analytics (default OFF) + the Q3 exit-gate tripwire |

## The analytics tripwire (Q3 exit gate, built as code)

`telemetry.TelemetryStore`:

1. Every event is schema-allowlisted (unknown fields rejected) and content-scanned.
2. Any content field → `TripwireTripped`: analytics shut off, offending payload plus live events persisted to the access-controlled quarantine (0600 files, never the word "sealed" — no encryption is implemented) and live copy deleted, incident filed.
3. Re-enable requires, in order: clearing entry by a **named** contracted security/privacy specialist (name + reason ≥ 20 chars) → verification by the **independent framework reviewer** (must differ from the specialist) → no operator veto.
4. `paul_veto(reason)` blocks re-enable unconditionally. Only the human clears a veto (human process).
5. Reporting refuses vanity metrics: `applications_per_day` and friends raise — the roadmap guardrail is enforced in code.

Prove it: `python -m pytest tests/test_i12_telemetry.py -x -q` — includes an
injection test (resume text smuggled into an event) that must trip the wire.

## CLI

```
python -m initiatives.i12 tools jd --text "..." --json
python -m initiatives.i12 tools risk --job-json job.json --skills "python,sql"
python -m initiatives.i12 share score-card --role "Backend Eng" --company "Acme" \
    --score 82 --verdict strong --reasons "a|b" --factors-json '[]' --markdown
python -m initiatives.i12 changelog
python -m initiatives.i12 onboarding --role-maturity switching --tech-comfort low
python -m initiatives.i12 telemetry --consent on --state /tmp/t.json
python -m initiatives.i12 telemetry --record tool_opened tool=jd_decoder,surface=terminal,session_id=abc --state /tmp/t.json
```

## Site integration

This package does not touch `site/` (owned by the P0 craft team). Exact specs
for the marketing-site surfaces these tools need are in
[`integration_notes.md`](integration_notes.md) — flagged to the program
coordinator for craft-team coordination.

## Assumptions (flagged per build directive)

- **A1 (re-cut evidence gate):** public-growth scope built without six months of outcome data, per the operator's directive.
- **A2:** built against assumed Initiative 04 decoder / Initiative 10 packaging interfaces via pinned contracts; adapters degrade honestly if they differ.
- **A3:** public-tool usage converts to qualified activation, not vanity traffic — instrumented for the distinction from day one (`qualified_activation` metric; vanity metrics banned in code).
- **ABSOLUTE:** no public launch without the operator's explicit order.
