# Risk constitution — Veto

This is the policy that `governance/risk_policy.py` enforces, adjudicated
through the user's agentic-governance framework (veto screen + verdict
ledger). It exists because job hunting with automation touches other
people's infrastructure: scraping can violate Terms of Service, and
aggressive automation can get accounts restricted.

## Tiers

- **official** — Greenhouse, Lever, Ashby, Adzuna. Public/official APIs.
  Preferred for everything; the only tier allowed in `strict` mode.
- **scraping** — LinkedIn, Indeed, Glassdoor, ZipRecruiter. HTML or guest
  endpoints. Works until it doesn't. May violate the site's ToS; the site
  can rate-limit (429), block (403), or CAPTCHA at any time, and logged-in
  automation risks account restrictions.

## Rules

1. **Strict mode** uses official-tier boards only. No scraping, no
   exceptions. This is the only ToS-clean-by-construction mode.
2. **Standard mode** permits scraping-tier boards only after the user
   explicitly acknowledges the ToS risk (recorded with a timestamp).
3. Scraping-tier searches are **daily-budgeted** (small by design) and
   **circuit-broken**: a 403/429/CAPTCHA triggers exponential cooldown
   (1h, doubling, capped at 24h). The agent backs off; it never retries
   aggressively.
4. Applications move at a **human pace**: max 5 per day across all boards,
   with randomized pauses between them. No bulk blasts, ever.
5. **Fill-only on scraping-tier boards**: automation may fill an
   application form but must NEVER perform the final submit click. The
   human reviews and submits. Full browser submission on a scraping-tier
   board is blocked by policy — use the fill-only flow or an official ATS
   endpoint instead.
6. **No credential storage.** No logged-in LinkedIn automation beyond what
   the user explicitly supervises in a headed browser.
7. Every search result is **tagged with its risk tier**, and every apply
   preview shows the **risk disclosure** for its board.
8. Every allow/block decision is written to the **verdict ledger** —
   the audit trail. `audit_risk()` reviews budgets, cooldowns, and caps
   for anomalies.

## Fail-open / fail-closed

- A governance **veto hit** or a **failed veto check** blocks the action.
  Only a human can clear a veto.
- If the framework is **unavailable**, the deterministic compliance gates
  still hold (fail-safe); the decision is marked `compliance-only`.
- A **ledger outage** never blocks or crashes an action; it is logged.

## The limen engine

Limen is the reference engine that powers the framework: it spawns
workers, isolates work, manages jobs, and merges, while the framework
supplies the harnesses. `governance/engine.py` bridges it:

- `detect_engine()` reports whether the limen CLI is installed and which
  engine the framework wizard recorded (`limen`, `claude-code`, `cursor`,
  `paperclip`, `custom`).
- `run_governed_job()` runs any automation (queue runs, watch checks,
  bulk flows) through the engine's lifecycle: **spawn** (risk
  adjudication — a veto stops the job before any work), **work**,
  **review** (adversarial, advisory), **merge** (verdict recorded; the
  human remains the merge authority via confirm gates).

Install limen (`github.com/overment/limen`) and select it in the
framework wizard to orchestrate through the limen CLI; the harnesses
govern either way.

## What this does not do

It does not make scraping ToS-compliant. It makes the risk explicit,
bounded, consented, and auditable — and gives the user a one-switch
escape hatch (`strict` mode) that removes the risk entirely.
