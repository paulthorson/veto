# Requirement→Evidence Link Schema — `veto/evidence-map/v1`

**Status:** FROZEN (2026-09-13) — Initiative 04 build coordinator.
**Owner:** Initiative 04 (native fit decoder & career graph).
**Consumers:** Initiative 05 (application studio — evidence library), the
Initiative 04 evidence map UI surfaces (terminal / local web / phone).

This is the *contract* the program builds against. Field names, statuses,
and the validation rules below are frozen; changes require a new schema
version (`v2`) — never silent edits to `v1`.

---

## 1. Envelope

```jsonc
{
  "schema": "veto/evidence-map/v1",
  "job_id": "<opaque application/job id>",
  "job_title": "Senior Widget Engineer",
  "evidence_source_hash": "sha256:9f2c… (first 16 hex of the profile/resume text)",
  "created_at": "2026-09-13T18:00:00+00:00",
  "entries": [ /* link entries, §2 */ ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Always `veto/evidence-map/v1`. |
| `job_id` | yes | Opaque id joining to the application (Initiative 01's `application_id`). Never a company name in the clear unless the producer chooses it. |
| `job_title` | no | Human label for display. |
| `evidence_source_hash` | yes | SHA-256 (first 16 hex) of the exact profile/resume text analyzed. If the user edits their profile, the hash changes and stale maps are invalidated instead of silently reused. |
| `created_at` | no | ISO-8601 timestamp of analysis. |
| `entries` | yes | One entry per extracted requirement (§2). May be empty when no requirements were extractable — the map must then say so (`limitations` on the fit result), not invent requirements. |

## 2. Link entry

One requirement → one entry. Statuses are **tri-state by construction**;
there is no fourth "maybe" state that could become an unsupported-claim
path.

```jsonc
{
  "requirement": {
    "text": "5+ years Python",
    "source_quote": "…5+ years of Python experience…",
    "kind": "must_have"            // or "nice_to_have"
  },
  "status": "supported",           // or "gap" | "grill_question"
  "evidence": [
    {
      "evidence_id": "ev_001",
      "quote": "…6 years Python building data pipelines…",
      "profile_field": "experience[1].summary",
      "quantified": true
    }
  ],
  "grill_question_id": null        // set only when status == "grill_question"
}
```

### Status rules (frozen — enforced by `schemas.validate_evidence_map`)

- **`supported`** — requires ≥ 1 evidence item. Every item's `quote` is a
  **verbatim** substring of the profile/resume text (the same "every fact
  must exist in the text" philosophy as `honesty_scan`). `quantified`
  is true when a number appears on the same line as the keyword match.
  `profile_field` names the profile section the quote came from
  (e.g. `experience[1].summary`, `skills`, `summary`).
- **`gap`** — no evidence found. Carries **no** evidence items and no
  free-text assertions. The decoder never upgrades a gap.
- **`grill_question`** — ambiguous (keyword present but unquantified, or
  title-match uncertainty). Carries a `grill_question_id` linking to an
  **existing grill session question** — never a free-text system
  assertion — and no evidence items. Ambiguity is resolved by the grill,
  not asserted by the decoder.

## 3. Companion schemas (same package)

- `veto/fit-result/v1` — the fit decoder's result envelope: `fit_score`
  (0–100), `components` breakdown, `provenance`
  (`{"kind": "static"|"personalized"|"cohort-informed", "n": int}` per
  Initiative 02's *contracted* provenance contract), `limitations`
  (plain-language list of what the decoder cannot do — always shown,
  never fine print), and `evidence_map` embedding the map above.
- `veto/share-card/v1` — the private share-card payload. Enforces the Q1
  exit-gate privacy property: **no raw resume or job-description text
  beyond the fragments the user explicitly selected**. Scrub-style
  validation (`schemas.validate_share_card`) is anchored to the actual
  raw inputs: full raw lines (≥ 24 chars) plus an alignment-independent
  sliding 24-char window check over every payload string. Fragments the
  user opted in to share are passed as `allowed_quotes` and stripped
  before scrubbing; anything else verbatim from the raw inputs fails
  the card. Evidence quotes are capped at 200 chars, and the scrub
  refuses to certify when called without raw inputs (fail closed).
  There is no excerpt field on fit share cards.

## 4. What Initiative 05 consumes

05's evidence library may store link entries (`requirement` +
`evidence[]` items with `evidence_id`) keyed by `evidence_source_hash`,
so a resume variant branch (05's versioned variants) can re-attach
provenance without re-deriving quotes. 05 must re-run
`validate_evidence_map` on load and treat any violation as "evidence
needs re-approval", never as a silent pass.

## 5. Assumption flags

- Built against Initiative 01's *contracted* outcome-event schema
  (`outcome-min-v0`; `application_id` join key) and Initiative 02's
  *contracted* score-provenance shape — not their implementations.
  Until 02's engine lands, fit results carry
  `provenance: {"kind": "static", "n": 0}` and surfaces must display
  "static score — no outcome data yet" plainly.
- Built on the assumption the Q4 evidence threshold (≥ 20 resolved
  outcomes incl. ≥ 4 qualified replies) is reachable; below it, the
  product degrades gracefully to the static model with the confidence
  display above.
