# Compliance & risk policy

Automated job hunting touches other people's infrastructure. Scraping can
violate a site's Terms of Service; aggressive automation can get accounts
restricted. This document is the plain-English version of the machine-
enforced policy in `compliance.py`, `governance/risk_policy.py`, and
`governance/risk_constitution.md`. The code is the source of truth - this
page explains what it does and why.

## Board tiers

| Tier | Boards | What it means |
|---|---|---|
| **official** | Greenhouse, Lever, Ashby, Adzuna | Public or official APIs. Preferred for everything. |
| **scraping** | Glassdoor (locked stub — performs no scraping) | Tier reserved for HTML or guest-endpoint scraping. Glassdoor's provider is a locked stub that raises `NotImplementedError` — it performs no scraping and makes no network requests to Glassdoor. |

Note: the LinkedIn, Indeed, and ZipRecruiter providers were removed during
the legal exposure reduction program. They are not present in
`compliance.py`'s `RISK_TIER`, there are no provider modules for them, and
Veto performs no searching or scraping on those sites.

Every search result is tagged with its board's tier, and every apply
preview shows the risk disclosure for its board.

## Modes

- **strict** - official-tier boards only. No scraping, no exceptions. The
  only ToS-clean-by-construction mode.
- **standard** (default) - scraping-tier boards are allowed only after the
  user explicitly acknowledges the ToS risk. The onboarding wizard asks for
  this; the acknowledgment is recorded with a timestamp in
  `compliance.json`.

## Budgets and circuit breakers

- **Search budgets:** 1000 searches/day per official board,
  40/day per scraping board. Small by design.
- **Circuit breaker:** a 403, 429, or CAPTCHA trips a cooldown - 1 hour,
  doubling on consecutive blocks, capped at 24 hours. The agent backs off;
  it never retries aggressively and never attempts to bypass a CAPTCHA.
  **Recovery:** consecutive genuine successful fetches clear the block.
  After 3 successful fetches in a row with no intervening block, the
  cooldown resets and the next block starts again at 1 hour. Any new
  block resets the success streak, so intermittent failures still
  escalate. Only real successful fetches count - a CAPTCHA or 403/429
  never does.
- **Application cap:** max 5 applications per day across all boards, with
  randomized 2–7 minute pauses between them. No bulk blasts, ever.

## Fill-only on scraping-tier boards

Automation may **fill** an application form on a scraping-tier board but
must **never** perform the final submit click. The human reviews and
submits. Full browser submission on a scraping-tier board is blocked by
policy - the code refuses it (`governance/risk_policy.py`'s `adjudicate_apply`
returns `allowed: False` for `via="browser"` on scraping boards). Use the
fill-only browser hook or an official ATS endpoint instead.

## What the gates check, in order

Every `apply_to_job(..., confirm=True)` passes through, before anything is
submitted or recorded:

1. **Governance precheck** - constitutional veto screen, cover-letter
   honesty (no invented skills/years), qualification scoring. See
   "Governance" in the README.
2. **Apply-time grilling** (when enabled) - the application pauses until
   the candidate answers the grill questions (chat, WhatsApp, or Gmail).
   Nothing proceeds on an incomplete grill.
3. **Risk-policy adjudication** - daily cap, board tier, fill-only
   enforcement, and a second governance veto screen on the planned action.

Every `search_jobs` call adjudicates each board first: blocked boards raise
for an explicit board choice and are skipped for `"all"`.

## Fail-safe behavior

- A governance **veto hit** - or a **failed veto check** - blocks the
  action. Only a human can clear a veto (fix the application/profile and
  re-run).
- If the governance framework is **unavailable**, the deterministic
  compliance gates still hold; the decision is marked `compliance-only`.
- A **ledger outage** never blocks or crashes an action; it is logged.

## Audit surface

- `risk_status()` (MCP tool) - per-board tier and whether search is
  currently allowed, daily cap usage, registered plugins.
- `governance_status()` (MCP tool) - whether the agentic-governance
  framework is active, its domains, and the gating policy.
- `compliance.json` - budgets, cooldowns, acknowledgments (local,
  gitignored).
- The verdict ledger - every allow/block decision, written via the
  framework when available.

## The one rule

Nothing is ever submitted without the user's explicit confirmation, and
scraping-tier submissions are never fully automated. The automation does
the tedious parts - searching, tailoring, filling, tracking - and the
human makes every consequential decision.
