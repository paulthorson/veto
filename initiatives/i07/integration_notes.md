# Initiative 07 — Integration Notes (for the integration sweep)

**Do not edit `cli.py`, `webui.py`, `dashboard.py`, or `server.py` from
this package.** The snippets below are exact, copy-pasteable wiring for
the integration sweep that owns those files. All functions referenced
exist and are tested.

## Decision record: surface coverage

**Terminal:** SHIPPED in-package — `python -m initiatives.i07.cli`
(`initiatives/i07/cli.py`) provides the full guided path: mentor opt-in,
match questionnaire, introduction request/respond/withdraw/list/reveal,
session kit (agenda/packet/questions/note/followup/feedback), warm-path
planner, safety (block/unblock/report/delete-me). Every consequential
step previews before writing and asks for confirmation; Ctrl-C aborts
with no writes. Empty/loading/blocked/failure states are honest text.

**Local web + phone:** DEFERRED to the integration sweep (disjoint file
scope — this initiative may not touch `webui.py`/`dashboard.py`). The
snippets below wire the same flows. Rationale: the i07 flows are
API-complete and CLI-verified; web/phone are presentation over the same
functions, and the sweep owns those files to avoid merge conflicts with
other initiatives.

**Guided path / escape hatch (all surfaces):**
1. Discover (match questionnaire → redacted previews) → 2. Request
(preview + confirm) → 3. Track (list; mentor reply recorded with
channel) → 4. Mutual → reveal contact → 5. Session kit → 6. Feedback.
Escape hatch at every step: withdraw (either party, any time), block,
report, delete. Blocked/empty/expired states render as plain-language
messages with a next step, never dead ends.

## 1. cli.py — register the i07 wizard

```python
# In cli.py, alongside other register_cli calls:
from initiatives.i07 import cli as i07_cli

def _cmd_i07(args):
    raise SystemExit(i07_cli.main(args.i07_args))

# argparse wiring (pass-through):
p_i07 = subparsers.add_parser("mentors2", help="Mentor matching & warm paths (Initiative 07)")
p_i07.add_argument("i07_args", nargs=argparse.REMAINDER, help="see: python -m initiatives.i07.cli --help")
p_i07.set_defaults(func=_cmd_i07)
```

Simpler alternative the sweep may prefer: document
`python -m initiatives.i07.cli --help` in the `mentors` command help text.

## 2. server.py — MCP tool registration

```python
# In server.py's tool-registration section:
from initiatives.i07 import consent, safety, session_kit, warm_path
import mentors as _mentors

@mcp.tool()
def mentor_match(answers_json: str) -> str:
    """Rank mentors (consent-preview: contact redacted until mutual consent)."""
    import json as _j
    return _j.dumps(_mentors.matchmake(_j.loads(answers_json), consent_preview=True))

@mcp.tool()
def intro_request(mentor_id: str, goal: str, label: str) -> str:
    """Request a mentor introduction (records YOUR consent; nothing is sent)."""
    import json as _j
    return _j.dumps(_consent.request_introduction(mentor_id, "me", label, goal))

@mcp.tool()
def intro_respond(handshake_id: str, decision: str, channel: str) -> str:
    """Record the mentor's out-of-band reply. decision=approve|decline; channel required."""
    import json as _j
    return _j.dumps(_consent.mentor_respond(handshake_id, decision, channel=channel))

@mcp.tool()
def intro_withdraw(handshake_id: str) -> str:
    """Withdraw from an introduction (either party, any time)."""
    import json as _j
    return _j.dumps(_consent.withdraw(handshake_id, "me"))

@mcp.tool()
def warm_path_plan(company: str) -> str:
    """Draft warm outreach to a company. Drafts only — never sends."""
    import json as _j
    return _j.dumps(_warm.plan_warm_path(company))
```

## 3. webui.py / dashboard.py — routes (phone-safe)

Suggested routes; all read from / write to the i07 functions above:

| Route | Function | Notes |
|---|---|---|
| `GET /mentors` | `_mentors.matchmake({}, consent_preview=True)` empty-state | Honest empty directory message |
| `POST /mentors/match` | `matchmake(answers, consent_preview=True)` | Questionnaire form |
| `POST /mentors/intro` | `consent.request_introduction(...)` | Preview + confirm screen first |
| `GET /mentors/intros` | `consent.list_handshakes("me")` | State chips; withdraw buttons |
| `POST /mentors/intros/<id>/respond` | `consent.mentor_respond(...)` | Channel field required |
| `POST /mentors/intros/<id>/withdraw` | `consent.withdraw(...)` | Confirm dialog |
| `GET /mentors/session/<id>` | `session_kit.build_agenda/context_packet` | 403-style message pre-mutual |
| `POST /mentors/warm-path` | `warm_path.plan_warm_path(...)` | Draft review list; "Send" buttons must NOT exist — drafts are copied by the user |

**Phone constraints:** all forms single-column, tap targets ≥44px, no
iOS input zoom (font-size ≥16px) — follow the existing phone-access pass
patterns in webui/.

## 4. Data files created by this package

All under `initiatives/i07/` (local-first, never transmitted):

- `handshakes.json` — handshake records (tombstoned on deletion)
- `consent_audit.jsonl` — append-only transition log (no delete API)
- `safety.json` — block list + quarantines
- `reports.jsonl` — abuse reports (append-only)
- `sessions.json` — session notes / follow-ups / feedback

Warm-path drafts live OUTSIDE the repo tree (never transmitted, never
committed): `~/.local/share/veto/warm_path_drafts.jsonl` (honors
`XDG_DATA_HOME`), since drafts contain real contact names.

## 5. Contracts

Normative contracts: `initiatives/i07/CONTRACTS.md` (revelation rule,
withdrawal, anti-fabrication, segregation, assumption flags).
