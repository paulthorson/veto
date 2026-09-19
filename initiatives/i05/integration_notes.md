# Initiative 05 — integration notes

Exact wiring for the files this initiative must NOT edit
(`cli.py`, `webui.py`, `dashboard.py`, `server.py`). The owning teams
apply these snippets; nothing here changes behavior until they do.

All snippets assume `from initiatives.i05 import ...` is importable
from the repo root (it is: `initiatives/` is a package).

## 1. `cli.py` — register the studio command group

Add, next to the other crew command registrations:

```python
def cmd_studio(args: argparse.Namespace) -> int:
    from initiatives.i05.studio import main as studio_main
    return studio_main(args.studio_args)

# in the subparser setup:
studio_p = sub.add_parser(
    "studio", help="Veto Application Studio (Initiative 05)")
studio_p.add_argument("studio_args", nargs=argparse.REMAINDER,
                      help="arguments forwarded to studio.py")
studio_p.set_defaults(func=cmd_studio)
```

Terminal equivalent (works today, no cli.py change needed):

```bash
.venv/bin/python initiatives/i05/studio.py <subcommand> --help
```

## 2. `webui.py` — page/route contracts (for the webui owner)

Four surfaces; each is a thin form over an i05 module. All
rendering must go through the plain-text/JSON helpers (never raw
strings) so the ATS copy contract and the no-submit boundary hold.

| Route (suggested) | Module call | Notes |
|---|---|---|
| `/studio/evidence` | `evidence_library.list_items()` / `approve_item()` | Approval buttons; approved items cannot be deleted (module enforces) |
| `/studio/ats` | `ats_check.ats_readiness_check()` + `render_report_text()` | Copy is contract-checked; never add ranking language in templates |
| `/studio/diff` | `diff_explain.side_by_side()` + `explain_resume_diff()` | "Review" toggle calls `mark_reviewed()` |
| `/studio/packet` | `packet.build_packet()` / `refresh_checklist()` / `approve_packet()` | **No submit button.** Approval only. |
| success toast | `success_feedback.success_moment()` | Render only when `allowed`; always show a dismiss control; never animate when `reduced_motion` |

### Success-moment web contract (Epic 6)

```python
moment = success_feedback.success_moment(kind, context={
    "state": outcome_state,          # from Initiative 00 outcome events
    "gate_open": init00_gate_allows_delight(),
    "user_initiated": True,          # only for user-completed actions
    "reduced_motion": prefers_reduced_motion(),
})
if moment["allowed"]:
    render_toast(moment["message"],
                  dismiss_key=moment["dismiss_key"])
# moment["animation"] is always None — do not invent animation here.
```

Prohibited: showing a moment when `allowed` is False (rejection,
veto, error, blocked, empty), re-showing a dismissed moment, or
animating under reduced motion.

## 3. `dashboard.py` — KPI hooks (for the dashboard owner)

Suggested cards, all read-only:

- Evidence library: approved vs total items
  (`evidence_library.list_items()` counts).
- Packet funnel: packets by status (`draft`/`approved`) — scan
  `initiatives/i05/packets/*.json`.
- Trace health: share of statements with approved backing across
  variants (`versions.trace_report()` per variant).

No write actions from the dashboard in v1.

## 4. `server.py` — API shape (for the server owner)

Suggested read-mostly routes; all request/response bodies are JSON:

- `GET /api/studio/packets` → list packet summaries
  (`packet.get_packet` per id).
- `POST /api/studio/packets/{id}/review` `{change_id}` →
  `packet.mark_change_reviewed`.
- `POST /api/studio/packets/{id}/approve` → `packet.approve_packet`
  (returns 409 with blockers when the checklist is pending).
- `GET /api/studio/evidence?approved_only=1` →
  `evidence_library.list_items`.
- `POST /api/studio/feedback/dismiss` `{dismiss_key}` →
  `success_feedback.dismiss`.

**There is deliberately no `POST .../submit` route.** Submission
stays in the governed apply flow and requires the operator's explicit
per-application confirmation. Do not add one here.

## 5. Phone coverage

Phone access rides the existing `serve --host lan` web UI (token
auth) — no new native work. The plain-text renderers
(`render_report_text`, `render_explanations_text`,
`render_packet_text`, `success_moment` payloads) are the phone
fallback: they are readable over the existing responsive web UI and
in the terminal via `studio.py`.

## Coverage decision (terminal / web / phone)

- Terminal: full coverage via `initiatives/i05/studio.py` (this
  initiative, shipped).
- Web + phone: integration contracts and exact snippets above;
  implementation belongs to the webui/server owners because
  `webui.py`/`server.py` are outside this initiative's file scope and
  under active edit by other teams.
- Cost of the omission: packet/diff/ATS/evidence flows are
  terminal-only until the webui owner wires the routes above.
- Owner of the follow-up: the operator (per program rules, the decision
  owner is named here).
