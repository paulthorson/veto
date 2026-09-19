# Initiative 06 — integration notes (verified against the repo)

> **Verification stamp.** Every claim below was checked against the code
> on 2026-09-13 (`cli.py`, `server.py`, `webui.py`, `dashboard.py`,
> `mock_interview.py`, `soft_skills.py`, `skill_gaps.py`,
> `ai_proficiency.py`, `initiatives/i06/longitudinal.py`) and every
> documented command was run via `python3 cli.py …` to prove it works
> verbatim. Prior versions of this document contained fictitious wiring
> (a `dispatch` handler map that does not exist, a `mock-interview`
> command group that does not exist, and "pending" wiring that had
> already merged). Those sections are deleted, not patched.

> **Scope note.** This initiative may NOT edit `cli.py`, `webui.py`,
> `dashboard.py`, `server.py`, or `initiatives/i06/longitudinal.py`.
> No further wiring is required in `cli.py` or `server.py` — both are
> already merged (see status table). The only genuinely remaining work
> is the `webui.py` / `dashboard.py` guided-path registration for the
> new i06 surfaces, which is specified as a contract below rather than
> as a snippet.

## Status of wiring (actual repo state)

| Surface | Status | Detail (verified) |
|---|---|---|
| `mock_interview.py` CLI + MCP | ✅ wired | `mock-start` / `mock-answer` / `mock-summary` / `mock-rubric` via `cli.py`; `mock_rubric` + interview tools via `server.py` |
| `soft_skills.py` CLI + MCP | ✅ wired | `soft-skills {areas,assess,drill,review,negotiate,counter,log,progress,lab-kinds,lab-start,lab-review,lab-summary}`; lab scenario tools via `server.py` |
| `skill_gaps.py` CLI + MCP | ✅ wired | `skill-gaps {analyze,plan,mark,observe,progress,recommend,focus,unfocus}`; `practice_observed_gaps`, `practice_progress`, `recommend_next_drill`, `select_practice_focus`, `clear_practice_focus` tools via `server.py` |
| `ai_proficiency.py` CLI + MCP | ✅ wired | `ai-skills {tracks,diagnose,answer,plan,lesson,exercise,submit,progress,lab-tracks,lab-exercise,lab-submit}`; `ai_lab_tracks`, `ai_lab_exercise`, `ai_lab_submit` tools via `server.py` |
| `cli.py` top-level registration | ✅ already merged | All four module names sit in `_PLUGIN_CLI_MODULES` (`cli.py:384` — `mock_interview` at :396, `soft_skills` at :397, `ai_proficiency` at :398, `skill_gaps` at :403). `_plugin_cli_handlers()` (`cli.py:425`) imports each, calls its `register_cli(sub)`, and merges the returned `{command: handler}` dicts. There is no `dispatch` map anywhere in `cli.py` (zero grep hits). |
| `server.py` tool registration | ✅ already merged | All four module names sit in the `_PLUGIN_MODULES` tuple (`server.py:163–201`). `_register_plugin_tools()` (`server.py:1593`) calls each module's `register_tools(mcp)` automatically. All four modules expose `register_tools`. |
| `webui.py` | ⚠️ partial — old surfaces only | API handlers exist for the pre-i06 surfaces (mock start/answer/summary; soft assess/drill/review/negotiate; ai tracks/plan/lesson/exercise/submit/progress/diagnose; skill_gaps analyze/plan/mark). **No handlers exist** for the new i06 surfaces: `mock_rubric`, `lab-kinds` / `lab-start` / `lab-review` / `lab-summary`, `observe` / `recommend` / `focus` / `unfocus`, `lab-tracks` / `lab-exercise` / `lab-submit` (verified by grep — zero hits). |
| `dashboard.py` | ⚠️ partial — old surfaces only | Interactive workflows exist (`wf_mock_interview`, `wf_soft_skills`, `wf_ai_proficiency`, `wf_skill_gaps`, lines 751–870) but do not cover the new surfaces (interview modes / disclosed rubric, lab scenarios, observed-gap recommend/focus, AI lab). |

## 1. How cli.py registration actually works

`cli.py` never calls `register_cli` per module and never keeps a
`dispatch` dict. The mechanism is two pieces:

1. The module-name tuple `_PLUGIN_CLI_MODULES` (`cli.py:384`). All four
   i06 modules are already listed there.
2. `_plugin_cli_handlers(sub)` (`cli.py:425`): for each name in the
   tuple it `__import__`s the module, calls `module.register_cli(sub)`,
   and merges each returned `{command: handler}` mapping into the
   handlers dict (failures warn by name and skip — never silently).

Registering a future module means adding one string to that tuple —
nothing else. No per-module snippet is needed here because the i06
modules are already registered.

## 2. How server.py registration actually works

`server.py` never calls `register_tools` per module. The mechanism is
two pieces:

1. The module-name tuple `_PLUGIN_MODULES` (`server.py:163–201`). All
   four i06 modules are already listed there.
2. `_register_plugin_tools()` (`server.py:1593`): iterates the tuple and
   calls each module's `register_tools(mcp)`; failures log a warning
   and the module's tools stay disabled.

No further server.py wiring is needed.

## 3. Command reference (every line run verbatim on 2026-09-13)

All commands run as `python3 cli.py <…>` from the repo root (the `veto`
entrypoint shells to `cli.py`). Session ids below come from the
start commands; substitute your own.

```
# Mock interview (no "mock-interview" group exists — these are the real commands)
mock-start "Stripe" "Backend Engineer" --mode adversarial   # positionals: company, role; --mode: recruiter|hiring_manager|peer|executive|adversarial
mock-answer <session_id> "your answer text"
mock-summary <session_id>
mock-rubric                                                # disclosed rubric card

# Soft-skills lab
soft-skills lab-kinds
soft-skills lab-start conflict                             # kind: conflict|influence|negotiation|ambiguous_stakeholder
soft-skills lab-review <session_id> "your response text"
soft-skills lab-summary <session_id>

# Skill gaps (action is positional; flags are --window/--repeat/--threshold/--skill)
skill-gaps observe --window 3 --repeat 2 --threshold 70
skill-gaps recommend --window 3 --repeat 2 --threshold 70
skill-gaps focus --skill evidence                          # NOT "skill-gaps focus evidence" — the positional form errors
skill-gaps unfocus

# AI-proficiency lab
ai-skills lab-tracks
ai-skills lab-exercise --track engineer --exercise-type ethics   # exercise-type: ethics|evaluation|failure_analysis
ai-skills lab-submit --session-id <session_id> --response "..." # --session-id flag; the hint printed by lab-exercise ("ai-skills lab-submit <id> …") is itself broken — the positional id errors with "unrecognized arguments"
```

### First-timer dead-end remediation

- There is **no `mock-interview` command group.** `mock-interview start`
  errors. Use `mock-start "Company" "Role"` (positionals, no `--track`).
- `skill-gaps focus evidence` errors with
  `unrecognized arguments: evidence`. Use
  `skill-gaps focus --skill <dimension>`.
- `ai-skills lab-submit <id> --response …` errors even though the
  tool's own hint prints it. Use
  `ai-skills lab-submit --session-id <id> --response …`.
- `--json` on `soft-skills` must come **before** the sub-action
  (`soft-skills --json lab-kinds`), not after it.

### session_id recovery

No module ships a `list` / `recent` command — there is no way to ask
the CLI for past session ids (verified: zero such subcommands in all
four `register_cli` definitions). If you lose a session id, read the
store files directly (JSON, in the repo root next to the modules):

- mock interviews → `mock_sessions.json`
- soft-skills drills → `soft_skill_sessions.json`
- soft-skills lab scenarios → `soft_skill_lab_sessions.json`
- ai-proficiency exercises + lab → `ai_proficiency.json`
  (`sessions` / `lab_sessions` keys)

## 4. webui.py / dashboard.py guided-path contract

### What exists today vs what is future

The terminal CLI above is flag-and-positional memorization, **not** a
wizard. Until the UI shells register the new surfaces, the terminal is
the only complete path — these notes scope the word "guided" to that
reality. The actual guided wizard (card → question → answer →
scored feedback → next question) is **future work**: it does not
exist in `webui.py` or `dashboard.py` today.

### Guided-path options (constitutional rules 2–4)

**Option A — terminal-CLI-first (current).**
The commands in §3 are the path; onboarding is the command reference
plus the dead-end remediation above. This option trades away guided
wizardry, per-step progress indicators, and dismissible error-banner
states to get zero new UI build cost and the widest possible surface
(the CLI already covers all four modules).

**Option B — web-UI-first "Interview lab" (future).**
One card listing the 5 interview modes and 4 lab scenarios, each
showing its disclosed brief BEFORE the user starts (no blind starts);
guided flow mode card → question → answer box → scored feedback with
per-dimension bars → next question, with a "round 2 of 3" progress
indicator on every step. This option trades away the CLI's zero-cost
coverage and phone-Safari token-auth simplicity to get true guided
wizardry, visible escape hatches, and error-banner states.

**cost_driven decision (recorded).** Until Option B is built, Option A
is the guided path *because building Option B costs a web-UI
implementation, test, and maintenance pass across four new surfaces*.
What the user gives up: step-by-step wizardry, in-flow error banners,
and per-step progress indicators. What the team saves: that entire
UI build. Revisit when the i06 surfaces stabilize or usage demands it.

**Business goal / KPI.** No i06-specific KPI with a metric and
direction exists anywhere in the repo, and inventing one here would
be fiction. Routing to the human gate: the roadmap's Q1 exit gate
names the adjacent metrics the gate could adopt — *successful task
completion* and *time-to-review-ready work* (annual roadmap, candidate
workbench row) and *weekly active studio/lab users* (Q1 exit gate,
"Q1 day 10" record). The gate (the operator + independent framework
reviewer, deadline 2027-01-10) should pick one and set its direction;
until then this document names no metric.

### Contract for whoever wires webui.py / dashboard.py

1. **Entry:** a single "Interview lab" card listing the 5 interview
   modes and 4 soft-skill scenarios as cards, each showing its
   disclosed brief (persona / setup / what-good-looks-like) BEFORE
   the user starts — no blind starts.
2. **Guided flow:** mode card → question → answer box → scored
   feedback with per-dimension bars → next question. Progress
   indicator ("round 2 of 3") on every step.
3. **Escape hatch:** a visible "End session" control on every
   step; ending early keeps scored rounds in history and marks the
   session incomplete — every screen offers a way out and preserves
   partial progress, so the user is never trapped in a flow.
4. **Proof of value:** a "Progress" view backed by
   `practice_progress()` + `recommend_next_drill()` showing
   per-dimension trends (improving / steady / declining) and the
   next specific drill with the evidence sessions cited.
5. **States:**
   - *Empty:* no sessions yet → show mode/scenario cards + "run
     your first practice round" CTA.
   - *Loading:* skeleton cards while `start_*` resolves.
   - *Blocked:* voice toggle shows the voice disclosure
     (service / payload / retention) and requires the consent
     checkbox before the mic enables — the mic MUST stay disabled
     until `grant_consent()` succeeds.
   - *Failure:* any `{"error": ...}` from the tools renders as a
     dismissible banner with the error text; the session state is
     preserved.
6. **Voice:** text input is the default and always present. The voice
   toggle calls `initiatives.i06.voice`:
   `describe_status()` → `propose_connected(...)` (shows disclosure)
   → `grant_consent()` → `transcribe()`. Never transmit audio
   before consent; never auto-enable.

## 5. Phone workflow

Phone access goes through the existing `serve --host lan` web UI
(token auth) — no Initiative 06-specific phone code. The contract
above applies unchanged on small screens (single-column cards,
full-width answer box, sticky progress bar).

## 6. Observed-gap human gate — Q1 day 10 = 2027-01-10

"Q1 day 10" is **January 10, 2027**: the annual roadmap defines Q1 as
January–March 2027 (`Veto Annual Roadmap.txt`, portfolio map), and
`initiatives/i06/longitudinal.py` records
`RULE_APPROVAL_DEADLINE = "2027-01-10"`.

The recommendation engine ships with a PARAMETERIZED rule — no
hardcoded weakness labels: `threshold=70`, `window=3`,
`repeat_count=2`, and explicit user focus selection overrides
everything. **The operator + an independent framework reviewer must approve
the threshold/minimum by 2027-01-10.**

Verified live in the code (`longitudinal.py:134`,
`RULE_PENDING_APPROVAL = True`, hardcoded until an explicit approval
record exists — none exists yet): every recommendation payload ships

```json
"rule": {"threshold": 70, "window": 3, "repeat_count": 2,
         "description": "…", "pending_approval": true,
         "approval_deadline": "2027-01-10",
         "approval_note": "the operator and the independent framework reviewer must approve window/repeat_count/threshold by 2027-01-10 (roadmap Q1 2027, day 10). Until an approval is recorded, these numbers are the unapproved baseline."}
```

Confirmed by running `skill-gaps recommend --window 3 --repeat 2
--threshold 70 --json` — the returned `rule.pending_approval` is
`true` and the deadline is `2027-01-10`.
