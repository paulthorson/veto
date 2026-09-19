# Initiative 12 — site/ integration notes

**For the P0 craft team. Initiative 12 does not touch `site/`; these are exact
specs for the marketing-site surfaces the public tools need. Flagged to the
program coordinator for craft-team coordination. Nothing here is approved for
implementation until the craft team schedules it — and nothing goes public
without the operator's explicit order.**

All specs assume the page renders output from `initiatives.i12` (JSON dicts),
never invents copy, and links methodology at `docs/i12/methodology.md`.

## 1. JD decoder demo embed (`/tools/jd-decoder`)

- **Input:** textarea, "Paste a job description", character count, explicit note: "Runs on your device. Nothing is uploaded."
- **Output:** verdict badge (strong/mixed/caution) + top 3 quoted reasons + transparency score + overwork flags (phrase + plain-English read) + vagueness panel + growth/green-flag lists.
- **Required footer:** limitations note (from `tools.LIMITATIONS_NOTE`) + methodology link. No account CTA above the result; a single honest next step below: "Want this against your real profile? Install the local app."
- **States:** empty (example JD button), loading (local, instant — show subtle progress), error (decoder unavailable → honest message from contract status).

## 2. Fit explainer demo (`/tools/fit-explainer`)

- **Input:** compact typed profile only — skills (comma list), seniority (select), locations, remote checkbox. Explicit label: "Demo profile — not your resume. We never ask for resume uploads here."
- **Output:** five factor rows with scores and one-line "why" each + veto banner if vetoed + note that the full app scores against a real evidence library.
- **Guardrail:** the page must not imply this equals full-app scoring.

## 3. Risk check + role compare (`/tools/risk-check`, `/tools/compare`)

- Risk check: findings list grouped by severity (blocker/warning/info), each with finding + next step; "proposal only — this page cannot apply anywhere" badge.
- Compare: table of up to 4 roles × (fit, risk count, compensation as quoted-or-"not listed", location, readiness). Truncation note when more than 4 supplied.

## 4. Shareable artifact rendering (`/share`)

- Renders `share.render_markdown` output for score cards, interview plans, progress snapshots.
- Each render shows the privacy note and methodology link. The page must refuse to render any artifact that fails the privacy scan (defensive; builder already scans).

## 5. Transparent changelog (`/changelog`)

- Renders `changelog.render_markdown()`: sections Shipped / Rejected / Limited, each entry with date, why, limitation.
- If health flags `missing_rejections`/`missing_limitations`, show the warning banner — do not hide it.

## 6. Education library (`/learn`)

- Index of `docs/i12/education/*.md` (qualified-applications, evidence-writing, interview-preparation, automation-risk) + methodology page.
- Render markdown with the repo's existing docs styling; keep the "further reading" cross-links working.

## 7. Analytics consent notice (site-wide, when analytics ship)

- Analytics default OFF everywhere. The opt-in control states exactly what is collected (metadata tokens: tool, surface, anonymous session id) and what is never collected (resume text, JD text, names, contact details).
- Link the tripwire description: content detection → shutoff + quarantine + named-specialist clearing. This is a trust feature; do not bury it.

## 8. Install CTA honesty

- Until Initiative 10 ships, the install CTA links to the README quickstart and says the one-command installer is on the roadmap. `contracts.install_path_status()` is the source of truth — the site must query it, not hardcode copy.

## Design constraints for the craft team

- Zero-radius, stepped-motion grammar per the Veto design system; `prefers-reduced-motion` respected.
- No dark patterns anywhere on these pages (see `onboarding.DARK_PATTERN_RULES` — the same rules apply to marketing copy; `python -m initiatives.i12 onboarding --check-copy <file>` can scan page copy).
- No vanity metrics displayed publicly (no "10,000 decodes run" counters).

## 9. Onboarding qualified-activation -> telemetry wiring (follow-up for the integration sweep)

`initiatives/i12/onboarding.py` defines the qualified-activation outcome as a
pure function (`detect_qualified_activation`) over the fixed
`QUALIFIED_ACTIVATION_SCHEMA`; `telemetry.py` is intentionally untouched.
The integration sweep must wire them:

- When analytics consent is ON, call `onboarding.detect_qualified_activation(session_events, assigned_path)` at session end (or when a `workflow_completed` event arrives).
- If the verdict is activated, record it via `telemetry.record("workflow_completed", workflow=..., session_id=..., duration_s=..., completed=True, ...)` — the emitted event dict already matches telemetry's `workflow_completed` schema shape plus `path_id`/`steps_completed`, which telemetry's schema-allowlist must accept or map (telemetry change needs its own review; do not silently drop fields).
- Consent OFF: never call `record`; the pure detection function is safe to run locally for product logic since it emits no telemetry itself.
- Also emit `telemetry.record("onboarding_path_assigned", ...)` from `assign_path`'s `ASSIGNMENT_LOG` entries (sha256 token prefix, never the raw token) once consent is on.
- Guardrail: the reporting layer must surface qualified-activation rates per `goal_workflow`, never pooled across different goal workflows (see the analysis plan in `experiment_summary`); application volume remains a banned metric.
