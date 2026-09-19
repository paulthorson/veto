# Veto capability report — runtime observation

Directive §8 (execute.md). Commit observed: `22b2a4d`
("feedback: local capture only, no automated send (directive §5, Rule 2)").
Date of observation: 2026-09-14.

Note: runtime observation was performed at `22b2a4d`; commits since are
documentation-only and do not change runtime behavior.

Method: a clean worktree of HEAD was created at `~/workspace/capability-tree`
and all runtime observation was done there, under a socket-logging harness
(`PYTHONPATH=/tmp/harness`, module `socklog`) that patches
`socket.socket.connect`, `socket.create_connection`, `urllib.request.Request` /
`urllib.request.urlopen`, and `httpx.Client.request` / `httpx.AsyncClient.request`
to log every connection attempt and every outgoing request's method, URL,
headers, and query params. Raw harness logs are archived at
`~/workspace/capability-report-logs/` (`s1_idle.log`, `s1b_client.log`,
`s2_greenhouse.log`, `s3*.log`, `s4_notify.log`, `s6*.log`, `s7_mcp.log`,
`s8_ddg.log`, `s9_bind.log`, `s10_wizard.log`).

Every finding below is labeled **runtime-observed** (seen in a harness log)
or **static-only** (read from source / built artifacts, not executed).

Environment note: this sandbox routes all outbound traffic through an egress
proxy (`hatch-egress-proxy:3128`, set via `https_proxy`/`HTTPS_PROXY` env).
Harness `connect` lines therefore show TCP to the proxy; the `httpx.request` /
`urllib.request` lines show the software's actual destinations. The proxy is
sandbox infrastructure, not a destination the software chooses.

---

## 1. Outbound connections opened by the software — runtime-observed

### 1a. Web UI server, idle 10 s — **runtime-observed**

Started `python3 webui.py --no-browser --port 18881` under the harness and let
it sit with no client traffic. Harness log `s1_idle.log` contains exactly one
line — the harness-arm marker — and **zero connection attempts**:

```json
{"t": 1789392476.221574, "kind": "harness", "info": {"event": "harness_armed", "argv": ["-c"]}}
```

**Finding: the web UI server opens no outbound connections while idle.** The
only file it writes at startup is the auth token (see §6).

### 1b. Web UI server, serving local client traffic — **runtime-observed**

Started on port 18882; `curl`ed `GET /`, `GET /app.js` (both 200), and
`GET /api/boards` (401 — token auth correctly enforced on API routes).
Harness log `s1b_client.log`: **zero non-loopback connections.** Serving pages
and static assets to a local browser triggers no outbound traffic.
(`webui.py:1919-1921` — `--host` defaults to `127.0.0.1`; `webui.py:1880` —
`resolve_bind_host(getattr(args, "host", None) or "127.0.0.1")`.) — static-only
for the defaults; the binding itself was confirmed at runtime:

```
$ ss -tln
LISTEN 0  0  127.0.0.1:18883  0.0.0.0:*
```

**Finding: default bind is 127.0.0.1 only (runtime-observed via `ss`).**
LAN binding exists only via the explicit `--host lan` / `--host 0.0.0.0` flag
(`webui.py:1611-1612`, `webui.py:1838-1840` warns that 0.0.0.0 is plain HTTP
with no TLS).

### 1c. MCP server, import + 10 s idle — **runtime-observed**

Imported `server.py` (all tools registered) and ran `mcp.run()` under the
harness for 10 s. Harness log `s7_mcp.log`: **zero non-loopback connections.**

**Finding: starting the MCP server opens no outbound connections.** All
network activity is on-demand, per user-invoked tool call.

### 1d. Onboarding wizard, full default run — **runtime-observed**

Ran `wizard.py` to completion in a clean worktree under the socket
harness, feeding empty stdin so every prompt took its default (LinkedIn
ingestion defaulted to Skip; grill channel defaulted to chat). The wizard
saved `profiles/profile.json` inside the worktree and exited 0. Harness
log `s10_wizard.log` contains exactly one line — the harness-arm marker —
and **zero connection attempts**:

```json
{"t": 1789402753.973982, "kind": "harness", "info": {"event": "harness_armed", "argv": ["-c"]}}
```

**Finding: the onboarding wizard opens no outbound connections**, even
with LinkedIn ingestion in the flow — the only LinkedIn path is parsing a
local data-export ZIP the user supplies. This is the runtime confirmation
of the §8 result: no `linkedin.com` destination exists anywhere in the
software.

### Third-party domains contacted at runtime — **runtime-observed**

Across all scenarios, the only third-party domains the software itself
requested were:

| Scenario | Destination domain | Purpose |
|---|---|---|
| Greenhouse provider search (§2) | `boards-api.greenhouse.io` | public job-board API |
| BYO-listing fetch (§3) | `example.com` (robots.txt only; target never fetched — fail-closed) | user-supplied URL hygiene check |
| Company brief fetch (§8 note) | `html.duckduckgo.com` | user-invoked company research |

No other third-party domain was contacted in any scenario. Idle state contacts
none at all.

---

## 2. Provider search against a public live API — **runtime-observed**

Ran `GreenhouseProvider(tokens=['airbnb']).search('software engineer', '', limit=1, remote_only=False)` under the harness (`s2_greenhouse.log`). Exactly **one** outbound request:

```
REQ GET https://boards-api.greenhouse.io/v1/boards/airbnb/jobs
    headers: {'host': 'boards-api.greenhouse.io',
              'accept-encoding': 'gzip, deflate',
              'connection': 'keep-alive',
              'user-agent': 'veto/0.1.0 (+https://github.com/paulthorson/veto)',
              'accept': 'application/json,text/html,*/*;q=0.8',
              'accept-language': 'en-US,en;q=0.9'}
```

**Egressed data:** nothing beyond the request line and standard headers. No
query params (the URL carries no `?` — the keyword/location filtering happens
client-side after the board listing is fetched; the user's search words
`"software engineer"` were **not** sent to the API). No cookies, no
authorization headers, no user identity of any kind. The User-Agent is the
single honest identifier defined once at `providers/_common.py:32-36`
(static-only):

```python
VETO_USER_AGENT = (
    f"veto/{_VETO_VERSION} (+https://github.com/paulthorson/veto)"
)
```

It identifies the tool and never imitates a browser. One job returned
("Mobile Software Engineer, Quality Platform | Airbnb"). A 1–2 s politeness
delay applies between per-token requests (`providers/_common.py:82-86`,
static-only).

---

## 3. Bring-your-own-listing one-time URL fetch — **runtime-observed**

Ran `byol.fetch_url('https://example.com/')` under the harness (`s3e.log`).
Observed egress:

```
httpx.request GET https://example.com/robots.txt
create_connection ['hatch-egress-proxy', '3128']
connect {'family': 10, 'host': 'fd8b:4f84:7d32:99::1', 'port': 3128}
```

`example.com/robots.txt` returned 404 (no body) → `robots_allows()` is
**fail-closed** (`providers/_common.py:96-104`, static-only: "an unreadable
robots file is not permission") → the target URL was **never requested**.
Result: `{'error': 'robots.txt disallows fetching https://example.com/'}`.

**Finding: the fetch path made exactly one outbound request (the robots.txt
hygiene check) and zero requests to the user-supplied URL in this case.**
When robots.txt permits, the design is: one robots.txt fetch, one polite
delay, then exactly one GET of the URL with the honest Veto User-Agent, capped
at 2 MiB (`byol.py:385-428`, static-only for the permitted path, which was not
triggered here).

Harness note: the log also contains a `urllib.request` line for the same
robots.txt URL. That object is created internally by httpx's own cookie
handling (`httpx/_models.py:1106`, `extract_cookies` → `_CookieCompatRequest`);
it is **not** an additional outbound request. Only the `httpx.request` /
`connect` lines represent real egress.

---

## 4. ntfy / webhook paths when unconfigured — **runtime-observed**

With no ntfy/webhook configuration (fresh `HOME`, no env vars), under the
harness (`s4_notify.log`):

- `notify.event_enabled('some_unknown_event_xyz')` → `False`
- `notify.event_enabled('job_found')` (catalogued, no prefs) → `False`
- `notify.send(...)` fired for **every** event in `EVENT_CATALOG`, plus
  `event=None`

Harness log: **zero connections of any kind** (`egress line count: 0`).

Static-only confirmation of the defaults (`notify.py`):

- `notify.py:211-226` — `event_enabled()`: "Only catalogued events the user
  has explicitly enabled may be delivered. Unknown/None events are DISABLED."
- `notify.py:56` — `_NTFY_BASE = "https://ntfy.sh"` (sink base; unused unless
  a generated topic exists).
- `notify.py:321` — `NTFY_TOPIC` env default `""`; guessable values are
  rejected (`notify.py:327-331`).
- `notify.py:450` — `JOB_MCP_WEBHOOK` env default `""`; webhook fires only
  when `"webhook"` is in the user's allowed channels.

**Finding: with no configuration, the notify paths make zero network
connections, and remote sinks are unreachable by construction (no topic, no
URL, events default-disabled).**

Notification payload contents today — quoted verbatim (static-only,
`notify.py:459-495`):

ntfy (`_post_ntfy`): POST to `https://ntfy.sh/<topic>` with the message body
as the raw POST body and the title in the **`Title`** header:

```python
req = urllib.request.Request(
    f"{_NTFY_BASE}/{topic}",
    data=body.encode("utf-8"),
    headers={"Title": title},
    method="POST",
)
```

webhook (`_post_webhook`): POST of JSON `{"title", "body", "ts"}` to the URL in
`JOB_MCP_WEBHOOK`:

```python
payload = json.dumps(
    {
        "title": title,
        "body": body,
        "ts": datetime.now(timezone.utc).isoformat(),
    },
    ensure_ascii=False,
).encode("utf-8")
```

**What leaves the machine when the user enables a sink:** the notification
title and body (typically a job title and company name), plus a timestamp for
webhook. No resume text, no credentials.

---

## 5. Site (marketing/docs site) — mixed

The marketing site lives in
[`paulthorson/veto-mcp`](https://github.com/paulthorson/veto-mcp)
([https://www.vetomcp.com](https://www.vetomcp.com)); path citations below
(`site/…`) refer to that repo. Built with `npm run build` (vite; succeeded —
no `file:../../veto-design-system` dependency remains in `site/package.json`).
Served `site/dist` over local HTTP.

### Routes — static-only (`site/src/App.tsx:1150-1230`)

Hash-routed SPA (`useRoute()` on `window.location.hash`):

- `#/` — home
- `#/demo` — interactive demo
- `#/docs` — docs
- `#/dashboard` — mission-control dashboard (reads localStorage only; see §6)
- `#/decoder/try` — JD decoder
- `#/decoder/r/<payload>` — shared decode result; payload is base64url in the
  fragment, decoded client-side (`site/src/lib/jdShare.ts`,
  `site/src/pages/DecoderSharePage.tsx:1-20`)
- `#/features/<slug>` — per-feature spec pages

### URL fragments are never sent in HTTP requests — **runtime-observed**

Requested `http://127.0.0.1:18884/#/decoder/try` from a local static server.
Server access log:

```
127.0.0.1 - - [14/Sep/2026 13:31:11] "GET / HTTP/1.1" 200 -
```

**The fragment `#/decoder/try` never arrived at the server** — only `GET /`.
By HTTP design the fragment is client-side-only, and the built bundle contains
no code that transmits `location.hash` anywhere (bundle grep: 7
`window.location` uses, all for route reading / document.title; the single
`fetch(` in the bundle is React's same-origin module-preload, not a data
fetch). The decoder share payload therefore travels only inside the link the
user copies — it is never sent to any server by the page.

### Decoder page: no analytics, error reporting, or third-party scripts — static-only

- Built bundle (`dist/assets/index-COUkmr0L.js`, 711 kB) grep for
  `gtag|google-analytics|sentry|posthog|mixpanel|amplitude|hotjar|fullstory|
  datadog|newrelic|bugsnag|logrocket|plausible|umami|clarity`: the only hits are
  the English word "clarity" in marketing copy ("communication clarity") and a
  minified variable fragment `gTag` — **no analytics, error-reporting, or
  tracking SDK is bundled or referenced.**
- `DecoderPage.tsx` / `DecoderSharePage.tsx`: zero `fetch`/`axios`/
  `XMLHttpRequest`/`sendBeacon` calls; the pages decode locally and render.
- No third-party `<script>` tags in `dist/index.html` and no external
  `<link>`s at all — the Google Fonts `<link rel="preconnect">` tags and
  stylesheet were removed when the two font families were self-hosted
  (see below).

### What the site loads and stores — static-only

- Loads: same-origin `index.html`, `./assets/index-*.js`, `./assets/index-*.css`,
  `./favicon.svg`, `./icons.svg`, plus self-hosted woff2 font files under
  `./assets/` (Press Start 2P and VT323, 400 normal, `@font-face` with
  `font-display: swap`; previously fetched from `fonts.googleapis.com` /
  `fonts.gstatic.com`, removed 2026-09-14). The site now makes **zero
  third-party network requests** — no scripts, no analytics, no external
  fonts. Verified static-only (`grep` over `dist/` finds no
  `fonts.googleapis.com` / `fonts.gstatic.com` reference; the built CSS
  references only relative local `./assets/*.woff2` URLs) and
  runtime-observed (local serve of `dist/` — access log shows only
  same-origin `GET /`, `GET /assets/...` requests).
- Stores: `#/dashboard` reads `localStorage["veto.dashboard.v1"]` when present
  (see §6), else seeded demo data (`site/src/pages/DashboardPage.tsx:16-29`).
  The decoder pages store nothing. No cookies, no IndexedDB.

---

## 6. Credential / token stores — **runtime-observed** unless noted

Each generation path was executed under the harness with a redirected `HOME`;
permissions were read from the created files.

| Store | Write location | Observed mode | Observed content |
|---|---|---|---|
| Web UI auth token | `~/.veto_webui_token` (`VETO_WEBUI_TOKEN_FILE` override) | `0600` (`-rw-------`, 44 bytes) | random token; created on first `serve` (`webui.py:1516-1525`, `webui.py:1556-1557` — `os.open(..., 0o600)`; `webui.py:1571`, `webui.py:1585` re-assert 0600) |
| Channel-consent HMAC key | `channel_consent.key` — **sibling of the consent log, which defaults to `<repo>/channel_consent.jsonl`** (`initiatives/i09/channels/registry.py:137`, `registry.py:160-164`) | `0600` | 32 random bytes, generated once (`registry.py:280-331` — `os.open(key_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)` + `os.fchmod(fd, 0o600)`) |
| Extension host secret | `~/.veto/extension_host.key` (`VETO_HOST_KEY_FILE` override) | `0600` | 32 random bytes (`initiatives/i11/sandbox/host.py:110-126` — `os.open(..., 0o600)`) |

Surprise vs. the task's assumption: `channel_consent.key` does **not** default
under `~/.veto` — it defaults next to the consent log in the repo tree
(`<repo>/channel_consent.key`), because `_consent_key_path()` is defined as
"a sibling of the consent log so tests that redirect CONSENT_LOG into a temp
dir get an isolated key automatically" (`registry.py:160-164`, static-only).

### `veto.dashboard.v1` — static-only

The task's assumed key exists in the site, not the Python backend:
`site/src/dash/types.ts:8`:

```ts
export const VETO_DASH_KEY = 'veto.dashboard.v1';
```

It is a **localStorage key holding job-search lifecycle data**, not a
credential. `DashboardPage.tsx:16-29` reads it and requires
`parsed.applications` to be an array — i.e. `{applications: [...]}` lifecycle
entries. It is read locally by the dashboard page and **never transmitted**
(no fetch/XHR anywhere in the site bundle — §5). The only browser-stored
credential-adjacent value is `webui/app.js:21` `TOKEN_KEY = "veto_token"` in
`sessionStorage` — the web UI auth token, sent only as a `Bearer` header back
to the local web UI itself (`webui/app.js:33-38`).

Harness logs for the key-generation runs (`s6_keys.log`, `s6b.log`) show zero
outbound connections — key generation is purely local.

---

## 7. Gmail OAuth scopes — UNKNOWN (external configuration)

Gmail grill and email sync depend on `hatch_gws_cli`, an external CLI
that is not publicly distributed; on a clean public install those
features report the CLI as missing and do not run.

**No OAuth scope strings exist anywhere in the Veto repo.** All Gmail access
goes through the external Gmail skill's CLI (`email_sync.py:27-28`):

```
All Gmail access goes through the Gmail skill's CLI
(``hatch_gws_cli gmail ...``); see ``/opt/hatch/skills/gmail/SKILL.md``.
```

`email_sync.py:59` — `GMAIL_CLI = "hatch_gws_cli"`; `email_sync.py:198-213` —
`_run_gmail_cli()` shells out to `hatch_gws_cli gmail ...` and parses JSON.
Veto never sees, stores, or transmits Google OAuth tokens — the grant lives
entirely inside the external `hatch_gws_cli` installation, whose scope
manifest is not part of this repo. Per the task instruction this is reported
as **UNKNOWN**: scopes are defined only in the external `hatch_gws_cli`
configuration, not in Veto. (Static-only.)

---

## 8. Every non-empty configurable destination URL — static-only, verbatim

"Configurable destination" = a URL literal the software will contact (or offer
to contact) that is not purely documentary. Role notes whether it is a
**pull target** (data comes in), a **push sink** (data goes out), or inert.

### Outbound data sinks (push) — require explicit user opt-in

| URL (verbatim) | Location | Role |
|---|---|---|
| `_NTFY_BASE = "https://ntfy.sh"` | `notify.py:56` | push sink base; only contacted after `setup_ntfy()` generates a high-entropy topic (§4) |
| _(empty default)_ `os.environ.get("JOB_MCP_WEBHOOK", "")` | `notify.py:450` | push sink; unset by default → never contacted (§4) |

### Inbound pull targets (data comes in; nothing of the user's is sent beyond a GET)

| URL (verbatim) | Location | Role |
|---|---|---|
| `LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"` | `providers/greenhouse.py:87` | pull target — public job-board API (§2 observed) |
| `DETAIL_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"` | `providers/greenhouse.py:88-91` | pull target — job detail |
| `LIST_URL = "https://api.lever.co/v0/postings/{company}?mode=json"` | `providers/lever.py:92` | pull target — public postings API |
| `DETAIL_URL = "https://api.lever.co/v0/postings/{company}/{posting_id}?mode=json"` | `providers/lever.py:93` | pull target — posting detail |
| `BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"` | `providers/ashby.py:85` | pull target — public job-board API |
| `BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"` | `providers/adzuna.py:36` | pull target — needs user's own `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` (server.py list_boards notes) |
| `"https://html.duckduckgo.com/html/?" + urlencode({"q": query})` | `briefs.py:124` | pull target — user-invoked company-brief web research (§8 note: observed GET with veto UA, query in `?q=`) |
| `LIMEN_REPO = os.environ.get("LIMEN_REPO", "https://github.com/overment/limen.git")` | `scripts/limen_nightly.py:35` | **pull target, not a data sink** — `git clone` source for a nightly script; nothing of the user's is sent there |
| `WHATSAPP_CONNECT_URL = "https://agent.meta.ai/connect/channel?service=whatsapp"` | `grill.py:67`, `mock_interview.py:84` | inert constant — a human-facing "connect WhatsApp" link printed in chat text, never fetched by the software |

### Inert / documentary (never fetched by the software)

- `providers/_contract.py:822,854,887,913,970` — `official_interface=` doc URLs
  (developer docs for the board APIs).
- `ats_apply.py:14-40,72-101` — endpoint strings inside the fill-only
  apply-plan preview (documents where a submit *would* go; the submit path is
  deleted — see §9).
- `VETO_USER_AGENT` embeds `https://github.com/paulthorson/veto`
  (`providers/_common.py:34`) — an identifier string in a header, not a
  destination.
- `site/dist/index.html` — canonical/og URLs
  `https://www.vetomcp.com/` (metadata only; marketing site in
  `paulthorson/veto-mcp`). (Google Fonts were the site's only third-party
  requests until 2026-09-14; the two families are now self-hosted woff2 —
  §5.)
- `initiatives/i11/policy_kit/*` and `reference/apply-pilot/extension.py`
  contain `https://evil.example/...`, `https://api.example.com/...` —
  adversarial test fixtures for the sandbox policy kit, never contacted.

---

## 9. What the software does NOT do — static-only unless noted

Claims a reader might reasonably assume, verified against the tree at HEAD:

1. **It never submits a job application by itself.**
   `browser_apply.py:10-14` — "NEVER submits… There is no submit code path
   here: no click on a submit control, no programmatic form submission, no
   application POST." `ats_apply.py:2-7` — "Direct ATS apply-plan preview…
   dry-run preview only. It never submits anything… the submit path is
   deleted." `server.py` MCP instructions — "apply_to_job NEVER submits…
   the user completes the submission in their browser."
2. **It never operates an authenticated session on a platform.**
   Providers use only unauthenticated public APIs (Greenhouse/Lever/Ashby
   public boards, Adzuna with the user's own developer key). Glassdoor is a
   deliberate stub: "no reliable access without login"
   (`providers/glassdoor.py:1`). No login/password/session-cookie handling
   exists in `providers/`.
3. **It never sends analytics, crash reports, or telemetry anywhere.**
   No sentry/posthog/mixpanel/telemetry SDK or endpoint exists in the Python
   tree or the site bundle (§5). The one analytics-*named* module,
   `initiatives/i12/telemetry.py`, is **local-only** (no network imports;
   events are recorded to a local state file), **consent-gated, default OFF**,
   content-scanned with a fail-closed tripwire, and has no network sink.
   Nothing in it can reach the author or any third party.
4. **No user data ever reaches the author or author-operated infrastructure.**
   Runtime-observed destinations (§1–§4, §8 note) are: public job-board APIs,
   a user-supplied URL (via robots.txt-gated fetch), DuckDuckGo HTML (user-
   invoked research), and — only if the user explicitly configures them —
   ntfy.sh / a user-chosen webhook. The author operates no service, receives
   no copy of resumes, application history, credentials, or correspondence.
   (Rule 2 posture; consistent with the directive's §4 cut of the
   contribution pipeline and §5's local-only feedback.)
5. **The site collects nothing.** No cookies, no localStorage writes except
   the dashboard's optional local lifecycle cache (`veto.dashboard.v1`) and
   the web UI's session token; no analytics; the only third-party requests are
   Google Fonts.
6. **Notification sinks default off and unknown events default disabled**
   (runtime-observed, §4) — nothing leaves the machine unless the user turns
   a named sink on.

---

## 10. Coverage checklist (the 12 required items)

1. Web UI + MCP idle connections — §1a/§1c — **runtime-observed** ✔
2. Provider search vs. live API, destinations + egress — §2 — **runtime-observed** ✔
3. BYO-listing one-time URL fetch — §3 — **runtime-observed** ✔
4. ntfy/webhook zero connections when unconfigured — §4 — **runtime-observed** ✔
5. Site: routes, fragments, decoder page — §5 — **runtime-observed** (fragment test) + **static-only** (bundle/route analysis) ✔
6. Credential/token stores: locations + 0600 — §6 — **runtime-observed**; `veto.dashboard.v1` nature — **static-only** ✔
7. Gmail OAuth scopes — §7 — **UNKNOWN**, external `hatch_gws_cli` — **static-only** ✔
8. Every non-empty configurable destination URL, verbatim — §8 — **static-only** ✔
9. "What the software does NOT do" — §9 — **static-only** ✔
10. Every finding labeled runtime-observed / static-only — throughout ✔
11. Report committed at `docs/capability-report.md` — this file ✔
12. Onboarding wizard full default run, zero connections — §1d — **runtime-observed** ✔

## Surprises vs. static evidence

- **Fail-closed robots check dominated the BYO scenario:** `example.com`
  returns 404 for `/robots.txt`, so the target URL was never fetched at all —
  the fetch path made exactly one outbound request (the robots.txt check).
  Static reading would suggest "one fetch of the URL"; runtime shows the
  hygiene gate fires first.
- **Search keywords are not sent to the board API:** the Greenhouse GET
  carried no query params; filtering is client-side. Less egress than a static
  read of `search(query, ...)` implies.
- **`channel_consent.key` defaults to the repo tree**, not `~/.veto`
  (`<repo>/channel_consent.key`, sibling of the consent log).
- **i12 has a consent-gated, default-off, local-only telemetry module.**
  It does not contradict Rule 2 (no network sink), but its name alone could
  alarm a reader — worth one sentence in user-facing docs.
- **The site used to load Google Fonts** — the only third-party request in
  the whole frontend. Not analytics, but it was a third-party request on
  every page load, decoder included. Self-hosted 2026-09-14
  (readme-hardening §9): both families are now local woff2, and the site
  makes zero third-party requests.
- **Sandbox proxy:** all outbound TCP in these logs terminates at the
  environment's egress proxy; the software's chosen destinations are the API
  hosts in the `httpx.request` lines. Report readers reproducing this outside
  the sandbox will see direct connections instead.
