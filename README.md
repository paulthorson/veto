# Veto — fill forms, never submit

> Most job bots optimize for volume. Veto searches official job-board
> APIs, grills you on resume gaps (chat or WhatsApp), tailors packets —
> and **fills forms only. It never submits.** The final click is always
> yours.

Veto is an open-source [MCP](https://modelcontextprotocol.io/)
server for governed job search. It handles the tedious parts —
searching, tailoring, filling forms, tracking, following up — and
enforces judgment calls most bots skip. Product home:
[www.vetomcp.com](https://www.vetomcp.com).

## The 60-second tour

1. **Search.** One query across Greenhouse, Lever, Ashby, and Adzuna —
   public or official APIs only — plus your own saved listings. Deduped,
   tier-tagged, and ranked.
2. **Grill.** Before anything is recorded or filled, it interrogates
   *you* — missing skills, unquantified experience, salary, logistics —
   over chat or WhatsApp. Answer on your phone; the application waits.
   (Gmail grill is not available on a clean install without an external
   CLI — `hatch_gws_cli` — that is not publicly distributed.)
3. **Tailor.** Resume bullets front-loaded with the posting's keywords,
   cover letter referencing the actual role. It never invents experience
   — gaps are reported, not padded.
4. **Fill, don't submit.** The agent fills the application form and stops
   at the review screen. *You* click submit. The software never touches
   the submit button — on any site, ever.
5. **Track.** Stages, follow-up nudges, optional recruiter-email sync
   (same Gmail install limit as above), response analytics by board.

## The three refusals

1. **It won't invent experience.** The cover letter is honesty-scanned;
   any skill or year count your profile can't back up blocks the
   application. Qualification scoring warns or blocks on real gaps.
2. **It won't burn your accounts.** Veto uses public or official APIs
   only — no scraping, no logged-in sessions — identifies itself with an
   honest `veto/<version>` User-Agent, and obeys robots.txt fail-closed
   ([capability report](docs/capability-report.md) §2, §3, §8, §9 item 2). A
   hard daily cap of 5 applications and a polite delay between requests.
   No bulk blasts, ever.
3. **It won't go rogue.** A constitutional veto screen reviews every
   planned application; every allow/block decision lands in an audit
   ledger. Nothing is ever submitted without your explicit confirmation.

The full policy: [`docs/compliance.md`](docs/compliance.md).

## Submission requires your explicit confirmation

This software can fill application forms, but it never submits
them. You review the completed form and click submit yourself.
Application packets are built and reviewed separately from
submission; the packet builder has no submit capability at all,
enforced by test.

You are the applicant. You are responsible for the accuracy of
every claim in anything submitted and for the volume of what you
send.

## Runs on your machine — nothing reaches the author

Veto runs on your own computer. Sitting idle, the web UI and the
MCP server open zero outbound connections
([capability report](docs/capability-report.md) §1a, §1c). When you
ask it to search, it contacts only the public job-board APIs you
asked it to search, and makes no connections it wasn't asked to
make (report §1–§2, §8). Optional notifications (ntfy, webhook) are
off by default and send nothing until you configure them (report
§4). Gmail features (grill channel, email sync) are unavailable on a
clean public install — they depend on the external `hatch_gws_cli`
Gmail CLI, which is not publicly distributed. Where that CLI is present,
Veto never sees, stores, or transmits your Google tokens (report §7).
Tokens and credentials it does hold live only in files only you can
read (mode `0600`, report §6). The author operates no service and
receives nothing: no copy of your resume, application history,
credentials, or correspondence ever reaches the author (report §9 item 4).

## Third-party services

Veto talks to other companies' services: public job-board APIs
(Greenhouse, Lever, Ashby, Adzuna), DuckDuckGo for company research
you request, and — only if you configure them — ntfy.sh or a webhook
you chose. Gmail is unavailable on a clean public install (external
`hatch_gws_cli` only).
Each of those services has its own terms of use, and some of them
restrict automation — you are responsible for complying with them.
The author is not affiliated with, endorsed by, or authorized by
any of them. Veto does not try to defeat or work around access
controls, bot detection, or login walls: it identifies itself with
an honest `veto/<version>` User-Agent and never imitates a browser
(report §2), obeys robots.txt fail-closed (report §3), and never
operates a logged-in session on your behalf (report §9.2).

## Quickstart

```bash
git clone https://github.com/paulthorson/veto
cd veto   # the directory you cloned it into, if named otherwise
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. Onboarding: profile, LinkedIn import, channel choice, risk acknowledgment
.venv/bin/python wizard.py

# 2. Search
.venv/bin/python cli.py search "software engineer" --location "New York, NY"

# 3. Preview, then confirm — the grill runs before anything is recorded
.venv/bin/python cli.py apply <job_id> --resume ~/resume.pdf
.venv/bin/python cli.py apply <job_id> --resume ~/resume.pdf --confirm
```

Or point any MCP client at it — ready-to-paste config in
[`mcp-config.json`](mcp-config.json). See [Connect](#connect).

---


## Tools

| Tool | Description |
|---|---|
| `search_jobs(query, location, board="all", limit=10, remote_only=False)` | Search postings. Returns jobs with `id`, `title`, `company`, `location`, `url`, `board`, `snippet`. |
| `get_job_details(job_id)` | Full description, requirements, and `apply_url` for one job. |
| `apply_to_job(job_id, resume_path, cover_letter="", answers={}, confirm=False, profile=None, headless=True)` | **Dry-run preview by default.** With `confirm=True`: Phase 1 records the application locally (default), or Phase 2 fills the form in headless Chromium when `VETO_BROWSER_APPLY=1` and a `profile` dict are provided — then stops at the review screen so you can click submit yourself. `profile` defaults to your saved wizard profile. The software never submits. |
| `get_profile()` | Returns the applicant profile saved by the onboarding wizard (`profiles/profile.json`), or setup instructions if it doesn't exist yet. |
| `list_boards()` | Supported boards and their status (`active` / `stub`). |
| `track_applications()` | Everything recorded in `applications.json`. |

## Enhancement plugins

Beyond the core tools above, the server ships a full application
pipeline. Every plugin registers its own MCP tools (via
`register_tools`) and CLI commands (via `register_cli`), and every
risk-sensitive path is adjudicated by the risk policy (see
`docs/compliance.md`).

| Area | Tools | What it does |
|---|---|---|
| **Apply-time grilling** | `grill_start`, `grill_answer`, `grill_status`, `grill_ingest_reply` | Before an application proceeds, the agent grills the candidate with deterministic questions derived from job/profile gaps (missing skills, unquantified experience, logistics, motivation). Channels: chat (default) or WhatsApp — chosen in the onboarding wizard. Gmail grill depends on external `hatch_gws_cli` (not publicly distributed) and is unavailable on a clean public install; when that CLI is present, replies match a `[grill:<id>]` subject tag. `apply_to_job` returns `grill_pending` until the grill is complete. See `docs/grill.md`. |
| **Direct ATS apply** | `apply_via_ats` | Official ATS application endpoints only (Greenhouse/Lever/Ashby with employer-issued keys). Investigated honestly: no verified anonymous endpoint exists, so the tool returns a dry-run preview (zero network calls) and never submits. |
| **Drip queue** | `queue_add`, `queue_list` (+ `queue-run` CLI) | Persistent, paced application queue. Respects the daily cap, pauses 2–7 minutes between applications, and adjudicates the whole run before starting. |
| **Email sync** | `scan_recruiter_emails`, `draft_followup_email` | Requires external `hatch_gws_cli` (not publicly distributed); without it these tools report the CLI as missing. Classifies recruiter email, proposes application stage updates (applies only on high-confidence unambiguous matches), and drafts check-in / thank-you / nudge follow-ups. Sending needs `confirm=True`. |
| **Interview prep** | `company_brief`, `prep_interview` | Company briefs (facts marked `unverified` when unfetched; no invented funding/headcount) and STAR stories built only from real profile achievements with metrics preserved verbatim. |
| **Analytics** | `application_analytics` | Funnel, response rate by board, median time to response, stale applications. |
| **Doctor** | `doctor` | Health check: profile, compliance acknowledgment, Playwright, sessions, preferences, boards. |
| **Risk audit** | `risk_status` | The audit surface: per-board tier and search-allowed state, daily cap usage, registered plugins. |

## Onboarding wizard

Run the interactive wizard once to build your saved profile — it asks
basic questions (name, contact, target titles, locations, remote
preference, salary, skills, work authorization) and can **ingest your
LinkedIn data** to pre-fill experience, education, and skills:

```bash
.venv/bin/python wizard.py        # or: .venv/bin/python cli.py wizard
```

LinkedIn ingestion options:

1. **Data-export ZIP (recommended).** In LinkedIn: *Settings & Privacy →
   Data Privacy → Get a copy of your data* → request and download the
   archive, then give the wizard the ZIP path. It parses `Profile.csv`,
   `Positions.csv`, `Education.csv`, and `Skills.csv` — no LinkedIn
   login or scraping needed.
2. **Skip** — answer everything manually.

This writes `profiles/profile.json` (gitignored — your data stays
local). Then build your resume:

```bash
.venv/bin/python resume_builder.py
```

which emits `profiles/resume.md`, `profiles/resume.html`, and
`profiles/resume.pdf` (PDF needs `reportlab`, included in
requirements.txt). The saved profile is also the default `profile` for
`apply_to_job`'s Phase 2 browser auto-fill, and `get_profile()` exposes
it to the agent.

## Boards

- **Greenhouse** — active. Public Job Board API, no auth
  (`boards-api.greenhouse.io`). Searches a curated employer list in
  `providers/boards.json` (Stripe, Airbnb, Coinbase, …) — add your own
  board tokens there.
- **Lever** — active. Public postings API, no auth (`api.lever.co`).
  Curated company list in `providers/boards.json`.
- **Ashby** — active. Public posting API, no auth (`api.ashbyhq.com`).
  Curated board list in `providers/boards.json`.
- **Adzuna** — key-gated stub. Set `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`
  (from developer.adzuna.com) to activate; without them `search_jobs`
  returns a clear error.
- **Glassdoor** — locked stub. Reports itself as unavailable rather than
  scraping; no guest-endpoint or HTML scraping is performed.
- **Your saved listings** (`board="user"`) — paste text, a URL, or an HTML
  file; searched explicitly with `board="user"`, never queried on `all`.

The LinkedIn, Indeed, and ZipRecruiter providers were removed in the
post-hardening pass (no scraping, no logged-in sessions, fill-only). Use
the official-API boards above, or add any posting as a saved listing.

## Setup

```bash
cd veto   # the directory you cloned it into, if named otherwise
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Web site

Product marketing lives at [https://www.vetomcp.com](https://www.vetomcp.com).
This product repo does not ship the marketing site source tree.

## Run

The server speaks MCP over stdio:

```bash
.venv/bin/python server.py
```

## CLI

`cli.py` exposes the same tools from a terminal — no MCP client config
needed:

```bash
.venv/bin/python cli.py boards
.venv/bin/python cli.py search "software engineer" --location "New York, NY"
.venv/bin/python cli.py search "software engineer" --board greenhouse --limit 5
.venv/bin/python cli.py search "software engineer" --remote --json
.venv/bin/python cli.py show <job_id>
.venv/bin/python cli.py applications
.venv/bin/python cli.py profile
.venv/bin/python cli.py wizard
```

Applying is a two-step flow. First a dry run (prints a preview, exits
non-zero, records nothing):

```bash
.venv/bin/python cli.py apply <job_id> --resume ~/resume.pdf
```

Then, when you're happy with the preview, record it:

```bash
.venv/bin/python cli.py apply <job_id> --resume ~/resume.pdf --confirm
```

Add `--json` to `search`, `show`, or `applications` for machine-readable
output, and `--profile path/to/profile.json` to `apply` to pass applicant
details for Phase 2 browser auto-fill. `wizard` launches the interactive
onboarding wizard (builds your saved profile, optionally ingesting your
LinkedIn export); `profile` shows the saved profile summary.

Note: each CLI invocation is a fresh process, so the dry-run preview
shows job title/company as "Unknown" — the job id itself is
self-describing, so `show` and `apply` still resolve the right posting.

## Dashboard

Two local, offline-first surfaces for browsing every tool without
memorizing commands. Both read the same local JSON state files; nothing
leaves your machine.

**Terminal dashboard** — KPI header (applications, avg fit score, day
streak, interviews, follow-ups due) plus a grouped menu (FIND / TAILOR /
TRAIN / WIN / GOVERN) where each tool runs as a guided step-by-step
workflow:

```bash
.venv/bin/python cli.py dashboard
```

**Web dashboard** — a localhost web UI (vanilla HTML/CSS/JS, no build
step, no CDNs, no external fonts) with KPI cards, SVG charts (funnel,
fit-score distribution, 30-day activity heatmap, skill gaps), and a
wizard modal for every tool:

```bash
.venv/bin/python cli.py serve            # opens http://127.0.0.1:8765 in your browser
.venv/bin/python cli.py serve --port 9000 --no-browser
```

The server binds `127.0.0.1` only and serves a JSON API (`GET
/api/tools`, `GET /api/kpis`, `POST /api/run`). Actions that send email
or record applications are confirm-gated: the first call
returns the exact text to approve plus a hash, and nothing executes
until you confirm that exact preview — the same semantics as the CLI's
`--confirm` flows.

## Connect

A ready-to-paste stdio config lives at
[`mcp-config.json`](mcp-config.json) — edit the paths to match your
checkout:

```json
{
  "mcpServers": {
    "veto": {
      "command": "/path/to/veto/.venv/bin/python",
      "args": ["/path/to/veto/server.py"],
      "env": {}
    }
  }
}
```

The server communicates over stdio using **newline-delimited JSON-RPC**
(one JSON message per line), as implemented by MCP Python SDK 2.x.

### Claude Desktop

Add the `veto` entry above to your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows —
merge it into the existing `mcpServers` object, don't replace the file).
Restart Claude Desktop afterwards.

### Muse CLI / other MCP clients

Muse CLI and most MCP clients accept the same `mcpServers` shape — point
them at the command/args above. For clients that take a single server
entry, drop the `mcpServers` wrapper and use just the `veto` object.

## How applying works

1. `search_jobs` → pick a job → `get_job_details` to read the description.
2. `apply_to_job(job_id, resume_path, ...)` **without** `confirm` returns a
   dry-run preview: job info, resume check, typical form fields, direct
   `apply_url`.
3. Re-call with `confirm=True` → the application is appended to
   `applications.json` with status `confirmed`, and you get the `apply_url`
   to finish submitting in your browser.

**Phase 2 (browser automation):** `browser_apply.py` implements Playwright-based
form filling. It detects visible inputs/textareas/selects/file-uploads,
fills them from a `profile` dict, uploads the resume, screenshots the
completed form, and stops. It never clicks submit — in a headed browser
the window stays open on the completed form so you review it and click
submit yourself.

## Phase 2: browser automation

`browser_apply.py` (Playwright, sync API) fills application forms in
headless Chromium. It never submits:

- `extract_form_fields(page)` → list of `{name, label, type, selector}`
  for visible inputs / textareas / selects / file inputs.
- `fill_application(page, profile, resume_path)` → dict of fields actually
  filled. `full_name` is split into first/last when the form asks for them
  separately; the resume is uploaded to file inputs when it exists on disk.
  Checkboxes/radios (attestations, EEO questions) are intentionally left
  for the human.
- `apply_via_browser(apply_url, profile, resume_path, headless=True, confirm=False)`
  → `{ok, fields_filled, fields_detected, screenshot, final_url, browser_open,
  handoff, error}`. It **never clicks submit** — `confirm` is accepted
  for compatibility but changes nothing (fill-only). Cookie/consent banners are
  dismissed best-effort; failures return error dicts, never exceptions.

### Enabling Phase 2

```bash
.venv/bin/pip install -r requirements.txt   # includes playwright
.venv/bin/python -m playwright install chromium
export VETO_BROWSER_APPLY=1
```

> **Migration note:** environment variables were renamed from `JOB_MCP_*` to
> `VETO_*` in this release (`JOB_MCP_BROWSER_APPLY` → `VETO_BROWSER_APPLY`,
> `JOB_MCP_WEBHOOK` → `VETO_WEBHOOK`). The old names are no longer read —
> update any exported `JOB_MCP_*` variables to their `VETO_*` equivalents,
> or the corresponding features will silently stay off.

> **Note:** if the Chromium binary download fails on your network, browser
> mode degrades gracefully — `browser_apply` returns an error dict when the
> browser binary is missing. The `playwright` Python package itself installs
> fine; retry the install where you have working access to the Playwright
> CDN.

Then call:

```python
apply_to_job(
    job_id,
    resume_path="~/resume.pdf",
    confirm=True,
    profile={
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+1 555-0100",
        "location": "New York, NY",
        "linkedin_url": "https://www.linkedin.com/in/adalovelace",
        "website": "https://adalovelace.dev",
        "cover_letter": "Dear hiring team, ...",
    },
)
```

Without `VETO_BROWSER_APPLY=1`, `confirm=True` keeps the Phase 1
behavior (local log only + direct `apply_url` for manual submission).

Screenshots land in `veto/screenshots/` (pre-fill preview and a
post-fill shot). Each confirmed Phase 2 attempt appends to
`applications.json` with `status` of `browser_filled` or
`browser_failed`, the screenshot paths, and `"submitted": false` —
always verify on the live site and click submit yourself before treating
an application as complete.

### Profile schema

All keys optional. Explicit `first_name`/`last_name` win over splitting
`full_name`.

| Key | Used for |
|---|---|
| `full_name` / `first_name` / `last_name` | Name fields |
| `email` | Email fields |
| `phone` | Phone/tel fields |
| `location` | Location/city/address fields |
| `linkedin_url` | LinkedIn URL fields |
| `website` | Website/portfolio fields |
| `cover_letter` | Cover-letter textareas |

## ⚠️ Risks / Terms-of-Service warning

> The full policy lives in [`docs/compliance.md`](docs/compliance.md) —
> tiers, budgets, circuit breakers, fill-only rules, and the audit
> surface. The code (`compliance.py`, `governance/risk_policy.py`) is the
> source of truth.

- **No scraping boards remain.** The four active providers
  (Greenhouse, Lever, Ashby, Adzuna) use public or official APIs only.
  Glassdoor is a locked stub that performs no scraping; the LinkedIn,
  Indeed, and ZipRecruiter providers were removed. The scraping-tier
  machinery (budgets, circuit breaker, ToS-risk acknowledgment) stays in
  the codebase but has no live boards to govern.
- This server stores **no credentials** and performs **no logged-in
  automation**.
- **Never auto-submit applications** without the user's explicit review:
  `apply_to_job` requires `confirm=True`, and even then Phase 1 only
  *records* the application locally — the human completes the final submit.
- **Phase 2 browser automation is fill-only.** Driving real application
  forms with a headless browser can still trigger anti-bot measures
  (CAPTCHAs, IP throttles) on the target site. Mitigations built in: no
  credential storage, no logged-in sessions, nothing is ever submitted by
  the software (the submit click is always yours), screenshots for human
  review, one application per tool call. **Keep a measured pace** (a
  handful of applications per day with pauses between them, not bulk blasts), expect some sites to
  block automation outright, and always verify the filled form on the live
  site before you click submit.
- Job data returned is a snapshot; always verify details on the live posting
  before applying.

## Project layout

```
veto/
├── server.py           # MCP server (providers, tools, Phase 2 wiring)
├── cli.py              # terminal CLI mirroring the MCP tools
├── wizard.py           # onboarding wizard → profiles/profile.json
├── resume_builder.py   # resume.md / resume.html / resume.pdf from profile
├── browser_apply.py    # Phase 2 Playwright form-fill automation (fill-only; never submits)
├── providers/          # API-based board providers (no auth needed)
│   ├── __init__.py
│   ├── _common.py      # shared HTTP/polite-delay/id helpers
│   ├── boards.json     # curated Greenhouse/Lever/Ashby board tokens
│   ├── greenhouse.py
│   ├── lever.py
│   ├── ashby.py
│   └── adzuna.py       # key-gated stub (ADZUNA_APP_ID/ADZUNA_APP_KEY)
├── profiles/           # your data (gitignored: profile.json)
│   ├── profile.json    # wizard output — your saved applicant profile
│   ├── resume.md
│   ├── resume.html
│   └── resume.pdf
├── screenshots/        # Phase 2 form screenshots (created on first run)
├── requirements.txt    # includes playwright (Phase 2) + reportlab (resume PDF)
├── README.md
├── mcp-config.json     # ready-to-paste stdio client config
├── applications.json   # local application log (starts empty)
└── .venv/              # Python virtualenv (not committed)
```

## Quick self-test

```bash
.venv/bin/python - <<'EOF'
import server
print(server.list_boards())
jobs = server.search_jobs("software engineer", "New York, NY", limit=5)
print(f"{len(jobs)} jobs")
for j in jobs[:5]:
    print(j["board"], "|", j["title"], "|", j["company"], "|", j["location"])
EOF
```

## CI/CD

[![CI](https://github.com/paulthorson/veto/actions/workflows/ci.yml/badge.svg)](https://github.com/paulthorson/veto/actions/workflows/ci.yml)

### Continuous integration

Every push and pull request to `main` runs `.github/workflows/ci.yml`
on Ubuntu with Python 3.12:

1. `pip install -r requirements.txt` (pip cache enabled)
2. `python -m playwright install --with-deps chromium` — if this fails,
   the step is allowed to fail and the E2E browser tests skip gracefully
   (skips don't fail the run)
3. Compile check over every `.py` file (excludes `.venv/`, `.git/`)
4. `python -m unittest discover -s tests -v`

### Releases

Push a tag like `v0.1.0` and `.github/workflows/release.yml`:

1. Builds the Docker image and pushes it to
   `ghcr.io/paulthorson/veto` with the version tag and `latest`
2. Creates a GitHub Release for the tag with auto-generated notes

```bash
git tag v0.1.0 && git push origin v0.1.0
```

### Docker

The image runs the MCP server on stdio (no ports — see
[`Dockerfile`](Dockerfile)):

```bash
docker run -i ghcr.io/paulthorson/veto:latest
```

To build locally:

```bash
docker build -t veto .
```

## Lifecycle & monitoring

### Application stages

Every recorded application now carries a pipeline stage. Stages:
`applied` → `interviewing` → `offer`, plus `rejected`, `withdrawn`,
`ghosted`. Each entry keeps a `stage_history` (`{stage, at, note}` events)
and an optional `follow_up_due` date.

| Tool | What it does |
|---|---|
| `update_application(job_id_or_index, stage, note="")` | Move an application to a new stage (validates the stage, appends history). Moving to `interviewing` sets a follow-up reminder 7 days out; terminal stages (`offer`/`rejected`/`withdrawn`) clear it. Accepts a `job_id` or a numeric index into `track_applications()`. |
| `application_stats()` | `{total, by_stage, by_board, response_rate}` — response rate = (interviewing + offer) / total. |
| `nudge_followups()` | Applications with `follow_up_due` today or earlier (stages `applied`/`interviewing`). |
| `export_applications_csv(path="applications.csv")` | Dump everything to CSV for spreadsheets / portability. |

Legacy entries (recorded before stages existed) are backfilled in memory
as `applied` on read — the file is only rewritten when you actually
update something.

### Job watches

Save a search once, get alerted only about *new* postings:

```bash
# via MCP tools
add_watch("swe-nyc", "software engineer", "New York, NY", "greenhouse")
check_watches()   # -> {watch_name: {new_jobs: [...], total_seen: N}}
```

The **first** check of a watch only records a baseline and reports no new
jobs — by design, so you're not spammed with everything that already
existed. Watches persist in `watches.json` (gitignored).

**Cron:** `python watch.py` checks all saved watches, prints JSON, exits 0:

```cron
0 9 * * * cd ~/workspace/veto && .venv/bin/python watch.py
```

### Multiple profiles

`profiles.py` adds named profiles (e.g. one per target role) without
breaking the wizard's single `profiles/profile.json`:

1. explicit name → `profiles/<name>.json`
2. `PROFILE_NAME` env var → `profiles/<env>.json`
3. `profiles/default.json` if it exists
4. legacy `profiles/profile.json`

```python
from profiles import load_profile, save_profile, list_profiles
load_profile("backend")          # explicit
load_profile()                   # PROFILE_NAME env -> default.json -> profile.json
save_profile({...}, "frontend")  # writes profiles/frontend.json
```

All `profiles/*.json` files are gitignored — profiles never get committed.

## Application quality

Features that keep search results clean and applications competitive:

- **Dedup** (`dedup.py`) — `search_jobs` automatically collapses duplicate
  postings: exact URL matches, exact (company, title) matches, and fuzzy
  title matches (≥0.85 similarity) for the same company. The most
  informative entry survives. Recall-friendly: nothing ambiguous is merged.
- **Tailoring** (`tailor.py`, `tailor_application` tool) — builds a
  per-job resume + 3-paragraph cover letter from your saved profile:
  keyword-matched experience bullets are front-loaded and the letter
  references the actual role/company. **Honesty rule:** it never invents
  skills, titles, or experience — gaps are reported as `missing_skills`,
  not added to the resume.
- **Search filters** — `search_jobs(..., salary_min=120000,
  seniority="senior")`. Post-filtering on listing text (`$150k`,
  `$150,000`, `$75/hr` → annualized; title keywords like senior/jr/staff/
  principal), because most boards don't expose these as search parameters.
  Jobs with no detectable salary/seniority are kept, never dropped.
- **Company preferences** (`prefs.py`, `update_preferences` tool) —
  `preferences.json` (gitignored; copy `preferences.example.json` to start)
  with `blocked_companies`, `preferred_companies`, `blocked_keywords`.
  Blocked matches are removed; preferred companies are flagged
  `"preferred": true` and sorted first.

```python
update_preferences(
    blocked_companies=["Acme Corp"],
    preferred_companies=["Initech"],
    blocked_keywords=["unpaid internship"],
)
search_jobs("software engineer", "New York, NY",
            salary_min=140000, seniority="senior")
tailor_application(job_id)  # uses your saved profile
```

## Governance

`apply_to_job(..., confirm=True)` is gated through your agentic governance
framework ([agentic-governance](https://github.com/paulthorson/agentic-governance))
when it is installed. Discovery order: `GOVERNANCE_ROOT` env var, then
`~/agentic-governance`, then `~/adversarial-agents`. Without it, behavior is
unchanged.

The gate runs **before** any browser submission or local logging, in this
order:

1. **Constitutional veto screen** (`universal` domain) on a neutral summary
   of the intended application, plus an advisory adversarial review whose
   prompt is returned for the calling agent to execute.
2. **Cover-letter honesty check** — the letter is veto-scanned, and any
   skills/years-of-experience it claims that your profile doesn't support
   block the application. Don't claim what you can't back up.
3. **Qualification assessment** — required skills and years of experience
   are extracted from the job description and scored against your profile
   (`profiles/profile.json`, via the onboarding wizard). Score ≥ 0.65
   proceeds silently; 0.40–0.65 proceeds with a `qualification_warning`;
   below 0.40 (or a severe experience gap) blocks.

Fail-open / fail-closed policy:

| Check | On framework error |
|---|---|
| Veto screen | **Fail closed** — blocks (`governance_error`) |
| Cover-letter honesty | Fail closed on framework errors; heuristic errors warn |
| Qualification | **Fail open** — warns, never blocks; missing profile warns (`qualification_unknown`) but never blocks |
| Adversarial review | Advisory — warns, never blocks |

A blocked application returns `{"status": "governance_blocked", "reason": ...}`
and records a veto verdict in the governance ledger — but **nothing is
submitted and nothing is logged to `applications.json`**. Only a human can
clear a veto: fix the application/profile/cover letter and re-run (the
framework's `overturn_verdict` tool also exists for its own ledger).

`governance_status()` reports whether the framework is active, its root, and
its domains. Verdicts are written to the framework's `runs/verdicts.jsonl`,
or to `runs/governance-verdicts.jsonl` here when the framework checkout
isn't writable; override with `GOVERNANCE_VERDICT_LOG`.

## Testing

The suite lives in `tests/` and uses only the standard library
(`unittest` — no extra test dependencies).

```bash
# run everything with a per-module summary table
.venv/bin/python tests/run.py
# or plain unittest discovery
.venv/bin/python -m unittest discover -s tests
# verbose
.venv/bin/python tests/run.py -v
```

| Module | What it covers |
|---|---|
| `test_sanity` | `py_compile` on every project `.py`; MCP stdio smoke test (initialize → `tools/list` shows the expected tools); CLI `--help` exits 0 and `boards` lists boards (read-only) |
| `test_unit_providers` | Provider parsers fed realistic fixtures (Greenhouse/Lever/Ashby API JSON): asserts the canonical job-dict shape (`id/title/company/location/url/board/snippet`) and job-ID encode→decode round-tripping |
| `test_unit_forms` | Form logic on a fake page (no browser): hidden/submit/invisible inputs excluded from detection; `full_name` splitting (explicit first/last win); email/phone/location/linkedin/cover-letter mapping; resume uploaded only when the file exists; checkboxes/radios skipped; empty profile doesn't crash |
| `test_unit_profile_resume` | LinkedIn export-ZIP parsing (crafted `Profile.csv`/`Positions.csv`/`Education.csv`/`Skills.csv`), profile merging, interactive wizard via mocked input (writes to a temp dir, never the real profile), resume Markdown/HTML generation and escaping |
| `test_e2e` | **Real browsers**: drives headless Chromium *and* Firefox (Playwright) against a local test form — `confirm=False` fills + screenshots without submitting; `confirm=True` is accepted for compatibility but changes nothing (fill-only: no submission receipt exists in the result). **Live**: `search_jobs("software engineer", "New York, NY", board="all")` through the real server code must return results from at least one provider |

### Installing browsers

```bash
.venv/bin/python -m playwright install chromium firefox
# smaller headless-only shell builds also work:
.venv/bin/python -m playwright install --only-shell chromium
```

On Debian/Ubuntu the browsers may also need system libraries:
`sudo .venv/bin/python -m playwright install-deps`.

### If the browser download fails

On some networks the Playwright CDN (`cdn.playwright.dev`) accepts the
connection but the file transfer stalls, so neither Chromium nor Firefox
can be downloaded. The browser E2E tests detect this at runtime
(`tests/browsers.py` probes each browser with a real launch) and **skip
gracefully** instead of failing. On any machine where the browsers install
cleanly, the same tests run for real — no code changes needed.

Note on Firefox: Playwright drives Firefox (Gecko) through its own driver
rather than the legacy Marionette protocol, but it exercises the same
Gecko engine end-to-end. The `browser_kind` parameter on
`browser_apply.apply_via_browser` (`"chromium"` default, `"firefox"`
optional) selects the engine.
