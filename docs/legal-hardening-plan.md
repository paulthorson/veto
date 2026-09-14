# Veto — Legal Exposure Reduction: Written Plan (Commit 1)

Status: plan only. No code in this commit changes behavior.
Scope authority: `review-terms/legal-exposure-spec.md` (spec) + scout material
`scout-2.4-2.5-sweep.md`, `scout-10.5-10.6.md`, `scout-dataflow.md`, `scout-domains.md`.
All file:line references below were re-verified against the working tree on
2026-09-14. Where the spec's line references are stale, the re-derived line is
given and the stale one is marked `(spec §N stale — re-derived)`.
This document is a technical plan. It states mechanics, not legal conclusions.

Commit order (one concern per commit; deletion commits stay separate from
replacement commits):
- Commit 2: user-agent honesty (spec §2)
- Commit 3: delete easy_apply.py
- Commit 4: delete remaining LinkedIn code paths + extended session-machinery
  deletion (§10.5 A1)
- Commit 5: delete Indeed and ZipRecruiter scrapers
- Commit 6: fill-only conversion (spec §5.1) + AutomationControlled removal
  (§10.5 A3)
- Commit 7: bring-your-own-listing path (spec §5.2)
- Commit 8: circuit breakers (spec §6; mechanism specified in §10.3 below)
- Commit 9: request hygiene (spec §7)
- Commit 10: bind fix (spec §8.1)
- Commit 11: site build fix (spec §8.2)
- Commit 12: test contract (spec §11)

The git history rewrite called for by the spec's final instruction is already
done: impersonating UA strings were purged from all history and appear in the
tree only as `VETO-REDACTED-UA` markers (not violations).

---

## 10.1 Files deleted (whole-file) and the capability the user loses

| File | Commit | Capability lost |
|---|---|---|
| `easy_apply.py` | 3 | Authenticated LinkedIn Easy Apply automation. The highest-risk module: logged-in browser sessions on LinkedIn, form fill and review-step assist on Easy Apply flows. |
| `tests/test_easy_apply.py` | 3 | Test coverage for the deleted module; deleted with it (a broken suite is not an option; the legal-posture contract suite in commit 12 replaces what matters). |
| `providers/ziprecruiter.py` | 5 | ZipRecruiter HTML scraping (search + JSON-LD detail parse). ZipRecruiter vanishes as a searchable board. |
| Any `tests/` module whose ONLY subject is a deleted module | 3/5 | Only those fully tied to deleted code; tests shared with surviving providers stay. |

Whole-file deletion is the minority: most of §3 removes code *paths* inside
surviving files (see §10.2). `linkedin_optimizer.py` is NOT deleted — the scout
confirmed it performs pure local text analysis with zero network calls, so the
§3.7 keep-condition is already satisfied; it survives untouched.

---

## 10.2 Files modified and the specific behavior change

### Commit 2 (spec §2 — user-agent honesty)
- `providers/_common.py`: delete `USER_AGENTS` list (:25-34) and the
  `random.choice(USER_AGENTS)` call site in `make_client()` (:68); delete the
  docstring "rotated user agent" (:65); delete the "Rotated to look like
  ordinary browser traffic. Add more as needed." comment; add to
  `sanitize_proxy_env` (:39-58) a one-line docstring stating it never sets or
  selects a proxy. Keep `polite_delay` (:77-81) as-is, optionally raised to
  2-4s, jitter retained.
- `server.py`: delete `USER_AGENTS` list (:225-233) and the
  `random.choice(USER_AGENTS)` call site in `_headers()` (:245); delete the
  module-docstring sentence (:20) and `_headers()` docstring (:243) describing
  user-agent rotation; delete the "Rotated to look like ordinary browser
  traffic" comment. Keep `_polite_delay` (:251-253) as-is.
- NEW: one module (recommending `providers/_common.py`) defines the single
  honest constant `veto/<version> (+https://github.com/paulthorson/veto)`
  and `server.py` imports it. After this commit no code issues a UA containing
  Mozilla, Chrome, Safari, Gecko, WebKit, AppleWebKit, KHTML, Firefox, or
  Version.
- (§10.5 A2 fold-in) `briefs.py`: delete `_USER_AGENT` (:63-67); `:134`
  `default_fetch()` uses the new honest constant for its GETs to
  `https://html.duckduckgo.com/html/`.
- (§10.5 A2 fold-in) `browser_apply.py`: delete the hardcoded Playwright
  `user_agent` block (:3835-3850); the context takes either the honest constant
  or no override at all.
- (`§10.5 A2` note) `easy_apply.py:264-269` and `wizard.py:250-253` hold the
  same pattern but die with their owning commits (3 and 4) — commit 2 does not
  touch them, it only names them as covered.
- Spec §2.4/2.5 sweep: after commit 2, a tree-wide grep for Mozilla,
  USER_AGENTS, "user agent"/"user-agent", rotate*/rotat*/rotation must return
  only benign senses (crypto/key rotation, log rotation, on-call rotation demo
  copy, and robots.txt fixture literals) — the sweep file
  `scout-2.4-2.5-sweep.md` enumerates them for the commit-12 tests to exclude.

### Commit 4 (spec §3.2, §3.3, §3.6 + §10.5 A1 extension)
- `server.py`: delete the LinkedIn provider path — `SEARCH_URL`/`DETAILS_URL`
  (:447-450), the GET calls (:475, :527), and the HTML/CSS parse helpers
  (:534-538 region). LinkedIn disappears from search results.
- `wizard.py`: delete the LinkedIn public-profile scrape (:247-258). The
  wizard path that fetched it either calls the §5.2 paste path or errors.
- `browser_apply.py`:
  - Delete `BOARD_LOGIN_URLS` entries for linkedin (:252), indeed (:253),
    glassdoor (:254), ziprecruiter (:255). (Spec §3.6 named only LinkedIn and
    Indeed and cited a stale line; §10.5 A1 decision: all four go, because the
    mechanism is the violation, not the URLs. `glassdoor`/`ziprecruiter` have no
    other use; the dict shrinks to the `None` ATS entries.)
  - Delete `login_session` (:1204-1340) — the entire save path.
  - Delete `session_path` (:266) and `has_saved_session` (:272).
  - Delete the auto-replay path in `apply_via_browser` (:3818-3842) — no saved
    session is ever loaded.
  - Delete `_LOGIN_PATH_HINTS` (:262) if it has no other use.
  - Delete the `login_session` CLI surface (subcommand/flag) if present.
  - Pre-existing `sessions/*.json` files: the code refuses to load them (there
    is no loader left), and a loud message at startup/run tells the user the
    files are dead and to delete them. We do NOT auto-delete user files
    containing credentials — the user deletes them after reading the message.
- `dashboard.py:623`: the line referencing `browser_apply.has_saved_session`
  is updated (the helper no longer exists); the dashboard stops displaying
  saved-session status.

### Commit 5 (spec §3.4, §3.5)
- Delete `providers/ziprecruiter.py` (see §10.1).
- `server.py`: delete `IndeedProvider` (:329-419) — search GET (:345),
  viewjob GET (:419), CSS parsers. Indeed disappears as a searchable board.
- Provider registry/contract lists and their tests are updated to drop
  `ziprecruiter` and `indeed` entries.

### Commit 6 (spec §5.1 — fill-only + §10.5 A3)
- `browser_apply.py`:
  - Delete the submit machinery: `_submit_bound_form` (:1034), the
    isolated-world submitter locators/pattern selectors (:970-1033 region),
    and the approval-mint/burn logic tied to browser submission (:3122-3397
    region). (Spec §5.1 cited `:618-633` — stale, re-derived.)
  - `apply_via_browser` becomes fill-only: it fills every field, leaves the
    browser open on the completed form, and returns control. It never issues
    `HTMLFormElement.prototype.submit`, never clicks a submit control, never
    POSTs.
  - (§10.5 A3) Remove `--disable-blink-features=AutomationControlled` from the
    Chromium launch args (:3828) and the `login_session` launch args (:1258,
    dies with the function); delete the self-describing evasion paragraph in
    the module docstring (:114-128). Justification: the flag's stated purpose is
    bot-detection evasion; with unattended submission gone, there is no
    remaining legitimate use. `--no-sandbox` stays (plain environment flag).
- `ats_apply.py`:
  - Remove the submit paths: `_submit_verified` (:253-296, including the
    `client.post` at :296), the `confirm`-gated apply flow (:334-346), and
    `apply_via_ats`'s submission behavior (:407-416).
  - Delete or neuter the `_ENDPOINTS` flip-to-verified re-arm: the module
    docstring's "can be enabled by flipping one entry to `verified` with its
    field spec" (:34-38) and `can_apply_direct()` (:120-132) must not survive
    in a form that lets a future single-line flip restore network submission.
    Fill-only + user-submits is the permanent architecture.
- Tests `tests/test_i05_packet.py:220` (no-submit contract for the packet path)
  must pass unchanged; the commit-12 contract suite (spec §11) asserts the same
  property tree-wide for browser and ATS paths.

### Commit 7 (spec §5.2 — bring-your-own-listing)
- Add one primary input path: paste-a-job-description (text), paste-a-URL
  (user's browser did the fetching — the URL is fetched only from the user's
  local paste, no automated board crawling), or drop a saved HTML file; Veto
  parses it locally into the standard job dict.
- Paste-a-JD becomes the PRIMARY flow in the CLI wizard and web UI, not a
  fallback behind dead search buttons. Search UI for the removed boards is
  either removed or explicitly re-wired to this path.
- This is the replacement the product's value depends on; §10.4 states the
  honest cost of the demotion from "find jobs" to "process jobs you found".

### Commit 8 (spec §6 — circuit breakers; mechanism specified in §10.3)

### Commit 9 (spec §7 — request hygiene, every remaining provider)
- `providers/_common.py`: `robots_allows` (:91-97) changes from fail-open to
  fail-closed. If `/robots.txt` cannot be fetched or parsed, the URL is treated
  as disallowed and is not requested.
- `compliance.py`: verify and preserve the existing 403/429 cooldown machinery
  (`record_block`, :290-308; cooldown constants, :79-81) — a 403/429 stops the
  provider and starts a cooldown, never a retry-through.
- Preserve `provider_health.record_captcha` and `providers/session_rescue.py`
  exactly as-is (:380-420: pause-and-hand-off only). Never solve, never route
  around. (Note: spec §7.4 cites `compliance.py:79-81` for 403/429 handling;
  re-derived — those lines hold the cooldown constants; the handling itself is
  at :290-308.)
- Assert tree-wide: no code path constructs or reads a proxy configuration
  (commit-12 test per spec §11).

### Commit 10 (spec §8.1)
- Note on the stale reference: spec §8.1 cited `server.py:635-647` for a
  uvicorn 0.0.0.0 bind. Re-derived 2026-09-14: no uvicorn exists anywhere in
  the current tree — `server.py` ends with stdio `mcp.run()` (:1659-1662).
  The only 0.0.0.0 surface is `webui.py` (`--host 0.0.0.0`, :1862-1875,
  :1937). Commit 10 verifies that finding holds and, for the web UI, defaults
  the bind to 127.0.0.1; LAN access remains an explicit `--host` flag AND
  requires the bearer token (already the shipped behavior per the token-auth
  file, verified by commit 10).
- Commit-10 verification (2026-09-14, exact lines re-derived; they drifted
  from the commit-1 note): the complete tracked-tree bind inventory is
  `webui.py:1871` (`serve(port, host="127.0.0.1", token=None)` — creates
  `_Server((host, port), _Handler)`), `webui.py:1680`
  (`resolve_bind_host`: the explicit `--host lan` maps to the auto-detected
  LAN IP; explicit IPs and `0.0.0.0` pass through unchanged),
  `webui.py:1920` (`cmd_serve` falls back to `"127.0.0.1"` when the flag is
  absent), `webui.py:1989` (`_main` argparse `--host` default
  `127.0.0.1`), and `cli.py:582` (`serve` subcommand `--host` default
  `127.0.0.1`). No other tracked file binds a socket: `server.py` is stdio
  only, `wizard.py:666` is a UDP `connect()` for IP display (never a bind).
- Token gate verified: every `/api/*` route is gated by
  `_Handler._require_auth` (:1748) in both `do_GET` (:1781) and `do_POST`
  (:1828) BEFORE routing — including nonexistent `/api/` paths — so no
  route can be added below the gate; fail-closed, never served open. The
  token file (`token_file_path` :1597, default `~/.veto_webui_token`) is
  created at mode 0600 on first start (`load_or_create_token` :1603) and
  existing files with looser permissions are tightened to 0600 with a
  stderr warning.
- Documented boot behavior for `--host lan` with no token file: the server
  does NOT refuse; it generates the token first (`cmd_serve` :1942-1955),
  prints it exactly once, and only then binds. So LAN exposure always has
  the token installed before any socket listens.
- Commit-10 tests added: `tests/test_bind_hardening.py` (16 tests, 5
  subtests) — default bind is 127.0.0.1 in `serve()`'s signature, both
  CLI parsers, and `cmd_serve`; a real `serve(0)` socket's kernel
  getsockname proves it cannot accept off-interface connections; LAN
  requires the explicit `--host` flag; the `--host lan` + missing-token
  flow generates the token first and prints it once; the auth gate
  precedes routing on every `/api/*` path.
- The commit-12 test asserts the uvicorn default bind is 127.0.0.1 (spec §11)
  against whichever component can bind.

### Commit 11 (spec §8.2)
- Vendor the built CSS into `site/src/vendor/` (`tokens.css` from
  `~/workspace/veto-design-system/packages/tokens/dist/tokens.css` and
  `veto-web.css` from the `@veto/web` dist), change `site/src/main.tsx:4-5`
  to relative imports, delete the two `file:../../veto-design-system/...`
  deps in `site/package.json`. Publishing the tokens package is rejected: both
  local packages are UNLICENSED and the design-system repo's public flip is
  pending — licensing is a human decision (spec §12), not a worker's.

### Commit 12 (spec §11 — test contract)
- One module. Its docstring states these tests encode the project's legal
  posture and that removing one changes that posture, not merely behavior.
- Required assertions: no submit POST / no submit-control click on a
  third-party domain; UA contains none of the banned tokens; no
  `random.choice` on a UA list; confirmation refuses on non-TTY; hardcoded
  confirmation fails; robots failure means no request; no proxy-construction
  code path; ceilings not raisable from config; uvicorn default bind
  127.0.0.1; `tests/test_i05_packet.py:220` passes unchanged.

---

## 10.3 Confirmation mechanism (commit 8, exact specification)

`confirm=True` as a function parameter dies. It is a config flag any script or
agent can pass and proves nothing. The replacement is a confirmation that
cannot be satisfied programmatically: a human reads the exact application
summary on screen and types a value that varies per application.

### CLI experience, step by step
1. The user runs the apply command in a real terminal. Veto fills the form in
   the browser (commit 6), then prints the full application summary —
   company name, job title, every field value as filled, destination URL —
   to stdout and pauses.
2. Before any outbound application action is permitted, Veto checks
   `sys.stdin.isatty()` AND `sys.stdout.isatty()`. If either is false, it
   prints a refusal and exits nonzero. There is NO environment variable that
   overrides this. There is NO CI escape hatch. There is no `--yes`,
   no `--confirm`, no `--non-interactive` flag that can reach this path —
   such flags must not exist for this action, and if found they are removed.
3. On a TTY, Veto prompts: "Type the company name exactly as shown above to
   submit this application: " The expected value is the company name rendered
   in the summary in step 1 — a DIFFERENT string for every application.
4. The user types the company name. A hardcoded "yes", "y", "ok", "send", or
   any fixed string FAILS the prompt (it is compared only against the
   per-application expected value). On mismatch: the action is refused, nothing
   is submitted, and the user may retry or abort — retries do not remember
   anything; each attempt re-renders the summary.
5. On exact match, the single application proceeds: the filled form is
   presented for the user's own submit click (fill-only, commit 6) or the
   single bound action executes. The approval is single-use: it authorizes
   exactly this one application, it is consumed the moment it is used, it has
   a short TTL, and it cannot authorize a second application.
6. There is no batch confirm. No confirm-all. No remembered confirmation. No
   session-level approval spanning multiple applications. Ten applications
   mean ten prompts, ten typed company names.

### Web UI experience, step by step
1. The user opens the local web UI (127.0.0.1 default) and reaches the
   per-application review screen, which renders the same full summary as the
   CLI: company name, job title, every field value as filled, destination URL.
2. There is a modal (not a checkbox, not a button alone): the modal shows the
   summary and contains a text input with the instruction "Type the company
   name exactly as shown to submit."
3. The typed value is checked in a LOCAL UI event handler (JavaScript running
   in the user's browser) against the company name rendered on that screen.
   A hardcoded string fails. Mismatch keeps the modal open with an error;
   nothing is sent.
4. On match, the modal closes and the single application is authorized —
   single-use, TTL-bound, exactly like the CLI. The next application opens a
   fresh modal.
5. There is NO programmatic path: no API endpoint that accepts a
   pre-computed confirmation, no query parameter or POST field that skips the
   modal, no headless/browser-automation route that can reach the handler.
   The server-side action endpoint requires the single-use approval record
   minted only by the human interaction in step 3.

### Under an agent driving the CLI
An agent (Muse, a script, a cron job) driving the CLI runs with stdin that is
not a TTY — a pipe, a pty-less subprocess, a redirect. Step 2 above fires: the
action refuses and exits nonzero. There is no way for the agent to satisfy the
prompt: no env var, no flag, no config file, no remembered approval, no
"agent mode" bypass. Deliberately so. If an agent has TTY control, the
varying-value requirement (§6.2) additionally raises the cost of mindless
approval — and the existing nonce+content-hash+TTL+single-use machinery
(`initiatives/i09/channels/registry.py:1427-1532`) already binds any approval
to the exact bytes shown, so an agent cannot reuse or forge one. (The standing
debate about whether the varying value adds anything over content-hash binding
is recorded in §10.6 B2 and deferred until after landing.)

### Ceilings
Applications-per-day and per-provider request ceilings are constants in code,
not config values. Raising them requires editing source and re-running — that
is the point. Commit 12 tests that no config file, env var, or CLI flag can
raise them. (§10.6 B4 records the narrower alternative and is deferred until
after landing.)

---

## 10.4 Honest UX cost per cut capability

- **easy_apply.py deleted.** Replacement: fill-only browser flow + typed
  company-name confirmation (§5.1, §10.3). Cost: the highest-value automation
  in the product is gone. A flow that was "Veto applies on LinkedIn while you
  watch" becomes "Veto fills the form, you read a summary, you type the company
  name, you click submit, ten times for ten jobs." High-volume applying gets
  materially slower and more tedious. That is accepted.
- **LinkedIn guest API + profile scrape deleted (server.py:447-475,
  wizard.py:247-258).** Replacement: paste-a-JD / paste-a-URL / drop saved
  HTML (§5.2). Cost — stated without softening, in the spirit of scout B3:
  LinkedIn is the dominant job board. Deleting even its unauthenticated public
  fetches demotes Veto from "find jobs for me" to "process jobs you found."
  The user now does discovery in their own browser — searching LinkedIn,
  opening postings, copying text — and Veto only does the thinking on what
  the user hands it. This is the largest UX cost in the program. It is also
  the one most likely to make the product feel broken to a new user who
  expects search to work. Accepted.
- **Indeed search + ZipRecruiter scrape deleted.** Replacement: same §5.2
  path. Cost: two more discovery surfaces gone; users who lived on Indeed
  lose in-product search entirely and must bring every listing by hand.
- **Saved login sessions deleted (all four boards).** Replacement: none —
  there is no replacement, deliberately. Cost: every browser-apply run now
  starts logged-out; on boards that require login to view or submit an
  application form, the user logs in by hand in the headed browser each time,
  or the flow cannot proceed. Pre-existing `sessions/*.json` files stop
  working with a message telling the user to delete them. Convenience that
  looked like "remember me" was actually Veto operating an authenticated
  session on ToS-restricted platforms; it is gone.
- **AutomationControlled flag removed.** Replacement: none. Cost: some apply
  flows that refuse to serve automation-flagged browsers may stop loading
  pages for the fill step. If that happens, the user completes those
  applications fully by hand. Accepted.
- **Fill-only submission everywhere.** Replacement: the user's own click.
  Cost: Veto can no longer "submit while you get coffee." Every application
  ends with the user at the keyboard.
- **robots.txt fail-closed.** Replacement: none. Cost: a provider whose
  robots.txt is temporarily unfetchable goes dark silently instead of
  degrading gracefully; users will see boards disappear with no actionable
  error. (§10.6 B1 argues the narrower alternative; deferred.)
- **Un-raisable ceilings + per-application typed confirmation.** Cost:
  power users lose the ability to tune throughput, and every application
  carries a transcription step that punishes typos and high volume.
  (§10.6 B2/B4 argue the narrower alternatives; deferred.)

---

## 10.5 Unlisted §1 violations — decisions

Four items the spec's §§2-8 did not list, found by the scout. Decision on
each, as plan author:

**(a) A1 — Saved-login-session machinery (Glassdoor/ZipRecruiter + generic
save/replay). DECISION: extend commit 4 beyond the spec.** The spec's §3.6
named only LinkedIn and Indeed and cited a stale line (`browser_apply.py:58`
is inside the module docstring). The real machinery: `BOARD_LOGIN_URLS`
(:251-259) holds FOUR boards — linkedin, indeed, glassdoor, ziprecruiter —
and the violation is the mechanism, not the URLs: `login_session` (:1204-1340)
saves Playwright `storage_state` (bearer-equivalent cookies/localStorage) to
`sessions/{board}.json` (:1294), and `apply_via_browser` auto-reloads any
saved session into every run (:3818-3842). Veto *operates an authenticated
session* on ToS-restricted platforms whenever a session file exists — exactly
what §1 lens (b) forbids — regardless of who typed the password. Deleting the
login URLs alone would leave the replay path and pre-existing
`sessions/*.json` files live. So commit 4 deletes: all four login URLs, the
`login_session` save path, `session_path`/`has_saved_session` (:266-275), and
the auto-replay path. Pre-existing `sessions/*.json` files: the code refuses
to load them (no loader remains) and tells the user, in plain text, that the
files are dead and should be deleted by hand. Rationale for not auto-deleting:
they contain live credentials; deleting user credential files silently is
worse than a loud message.

**(b) A2 — Hardcoded impersonating UAs outside the two USER_AGENTS lists.
DECISION: fold into commit 2.** `briefs.py` `_USER_AGENT` (:63-67, used at
:134 for outbound GETs to `https://html.duckduckgo.com/html/`) and
`browser_apply.py`'s hardcoded Playwright `user_agent` (:3835-3850, sent with
every request the automated browser makes) both get the §2.2 honest-constant
replacement. Spec §2.2's "ONE honest constant, defined in exactly one module
and imported everywhere" already implies this; §2.1's required actions just
didn't name them. (`easy_apply.py` and `wizard.py` instances die with commits
3 and 4; commit 2 does not touch them.)

**(c) A3 — `--disable-blink-features=AutomationControlled`. DECISION: remove.**
It is self-described bot-detection evasion — the module docstring (:114-128)
says so plainly. Its purpose dies with unattended submission. Placement:
commit 6's fill-only rewrite of `apply_via_browser` — its `login_session`
use dies with commit 4 (A1), its `easy_apply` use dies with commit 3, leaving
only the fill-only browser, which has no legitimate need to hide its
automation signal. Remove the flag from the Chromium launch args (:3828) and
delete the evasion paragraph from the docstring.

**(d) A4 — `notify.py` ntfy/webhook POSTs. DECISION: keep, document.**
`_post_ntfy` (:321-330, `https://ntfy.sh/<topic>`) and `_post_webhook`
(:336-353, user-configured `JOB_MCP_WEBHOOK`) send to the user themselves —
opt-in self-notification via the user's own env vars to documented public
services. Not a §1 violation. It is recorded here so the §13 capability
report is complete.

**Line-number verification log (2026-09-14, re-derived — spec refs marked):**
- Spec `browser_apply.py:58` (§3.6) — stale; real targets :251-259, :1204-1340,
  :3818-3842 as above.
- Spec `browser_apply.py:618-633` (§5.1) — stale; real submit machinery
  :970-1090 (submitter locate/selectors), :1034 `_submit_bound_form`,
  :3122-3397 (approval mint/burn tied to browser submit).
- Spec `server.py:635-647` (§8.1) — stale; no uvicorn in the current tree at
  all; server ends at stdio `mcp.run()` (:1659-1662). The 0.0.0.0 surface is
  `webui.py:1862-1875, :1937`. Commit 10 verifies and re-tests.
- Spec `compliance.py:79-81` (§7.3) — partially stale as cited; those lines
  hold the cooldown constants; the 403/429 handling is at :290-308.
- Spec `providers/_common.py:91-97` (§7.2) — current: `robots_allows`,
  fail-open docstring.
- All other cited lines (UA lists, BOARD_LOGIN_URLS, session paths,
  ats_apply submit paths, AutomationControlled sites, Indeed/LinkedIn
  providers, wizard profile scrape) verified current as of today.

---

## 10.6 Where the constraints look stricter than necessary

Recorded in writing, as required. Each states the narrower alternative and
the honest counter. **These are considered AFTER the work lands, not before.**
Nothing here delays or narrows any commit above.

**B1. §7.2 — robots.txt fail-CLOSED on fetch/parse failure.** Narrower
alternative: fail closed when robots.txt IS fetched and disallows, but on
fetch/parse failure fail open with a logged warning and a short retry; and
don't apply robots checks to documented-API providers (Greenhouse/Lever/Ashby/
Adzuna — their module headers state "no auth" documented JSON APIs) since
robots.txt governs crawling, not documented APIs. This preserves every
§1-relevant property. Honest counter: fail-open on transient failure is
exactly the loophole the spec wants closed; one DNS blip should not silently
re-enable a scrape, and the current posture ("fail-open, current behavior")
is what made the spec demand fail-closed. Cost of the spec's version: one DNS
blip disables a provider silently and biases the product toward
best-engineered sites.

**B2. §6.2 — per-application varying typed confirmation vs the existing
type-"send" + content-bound approval.** The existing machinery
(`initiatives/i09/channels/registry.py:1427-1532`) already enforces §1's core:
only the interactive prompt mints approvals (:1441-1446), non-TTY is refused
(:1448-1463), the nonce is bound by content-hash to the exact bytes shown
(:1479-1499), approvals are single-use with a TTL (:1535+). The varying value
guards only against habitual, mindless typing of a fixed string — but an agent
with TTY control can type a company name as easily as "send", so it does NOT
strengthen the anti-agent property §1 actually demands; content-hash binding
already guarantees approval of *these exact bytes*. Narrower alternative:
keep the existing boundary. Honest counter: the varying value raises the cost
of mindless approval, and §1's "human hand at the moment it happens" can be
read as demanding attention, not just presence. Cost of the spec's version: a
transcription test on every application in the CLI, a bespoke web-UI widget,
typo failures, and real friction on legitimate high-volume search.

**B3. §3 deleting unauthenticated public-data fetches (LinkedIn guest API,
ZipRecruiter SSR).** §1 lens (b) itself treats the public-information posture
as a mitigator — driving a logged-in session is "categorically worse than
fetching public data." Narrower alternative: keep unauthenticated public-data
fetches only, with the honest UA and full request hygiene, and zero
authenticated sessions. That eliminates lens (b) entirely. Honest counter:
§1(a) as written covers this ("the author has shipped a tool built for that
purpose"); LinkedIn's terms restrict automated access, period, not just
authenticated access — deletion may be required by the letter of §1(a). Cost
of the spec's version: the largest UX cost in the program, stated in §10.4 —
LinkedIn is the dominant board and its deletion demotes Veto from "find jobs
for me" to "process jobs you found."

**B4. §6.5 — ceilings as un-raisable code constants.** Once §6.1-6.4 hold, the
human is the rate limiter; the marginal §1 value of an un-raisable ceiling is
near zero. Narrower alternative: code defaults plus config that can raise up
to a hard-coded absolute max. Honest counter: un-raisable constants are
directly §11-testable, and any configurability is a future footgun someone
will widen. Cost of the spec's version: power users lose throughput tuning.

**Proportionate — no narrowing case recorded:** deleting `easy_apply.py`
(highest lens-(b) risk in the codebase); §6.3's no-env-override rule (an env
bypass IS the "config flag" §1 forbids); the git history rewrite (the repo is
still private, so this is the lifetime-minimum-cost moment).

---

*End of plan. This file is the commit-1 deliverable per spec §10.*
