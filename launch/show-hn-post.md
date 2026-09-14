# Show HN draft

**Title:** Show HN: An MCP server that applies to jobs, and refuses to lie for you

**Body:**

I built an MCP server that searches jobs across 4 boards (Greenhouse, Lever,
Ashby, Adzuna — public and official APIs only; Glassdoor is an honest stub
that refuses instead of faking it) and helps
an AI agent apply — with a twist: it's governed. It will refuse to submit an
application it can't defend.

The parts I think are actually new:

**Apply-time grilling.** Before an application proceeds, the agent generates
deterministic questions from the gaps between your profile and the job —
missing skills, unquantified experience, logistics, motivation — and sends
them over chat or WhatsApp. The application stays `grill_pending`
until you answer. It turns out a one-line "1. lots 2. 15GB 3. 2GB"
reply parses fine once you stop assuming one answer per message.

**A constitutional veto screen on every application.** Before anything is
submitted, the intended application goes through a veto check, a
cover-letter honesty scan (claims a skill your profile doesn't support?
blocked), and qualification scoring against the job description. Score under
0.40 and it's `governance_blocked`. Only a human can clear a veto.

**Fill-only, always.** This is the policy I'm most opinionated
about: automation may fill an application form but must never submit it.
The code enforces it — the browser path fills the form and stops, the ATS
path is preview-only, and a submit attempt raises a fill-only violation.
The submit click is always yours.

**Boring-but-real compliance machinery:** public and official APIs only —
no scraping, no logged-in sessions — an honest `veto/<version>` User-Agent,
robots.txt obeyed fail-closed, max 5 applications a day with a polite delay
between requests. Every allow/block decision goes to a verdict ledger.

Also in the box: a paced application queue, recruiter-email classification
with stage-sync proposals, company briefs and interview prep built only from
real profile achievements (facts it can't verify are marked `unverified`,
never invented), and funnel analytics.

Honest limitations:

- No scraping, no logged-in sessions, no CAPTCHA to bypass — it only talks
  to public and official job-board APIs. Some runs will just return less.
- The governance framework is a separate install; without it the
  deterministic compliance gates still hold, marked `compliance-only`.

It's open source — repo link in my profile / link in bio. Apache 2.0,
standard stuff.

What I'd actually like feedback on: the fill-only policy (too strict?
not strict enough?), and whether the grill questions are the right ones —
what would you want an agent to ask *you* before it applied in your name?
