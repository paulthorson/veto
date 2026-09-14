# Initiative 09 — integration notes (for the later integration sweep)

**Do not edit cli.py, webui.py, dashboard.py, or server.py in this
initiative.** The snippets below are exact, copy-paste-ready wiring for the
integration sweep. Each snippet names its target file, insertion point, and
the import it needs. All of them are additive: no existing behavior changes.

Conventions used below: `BASE_DIR` is the repo root (already defined in
cli.py, webui.py, and server.py; the dashboard.py snippet is pure text and
needs no path work).

---

## 1. server.py — MCP tools

### 1a. Connector manifests + conformance (read-only tools)

Insert after the existing provider tool registrations (near the other
`register_tools` calls):

```python
from providers import _contract as _i09_contract

@mcp.tool()
def connector_manifest(connector: str) -> dict:
    """Return the proof-of-value declaration for a connector.

    Every connector declares: what it can do, why it may be blocked,
    what data it sends, and which actions require confirmation.
    """
    manifest = _i09_contract.manifest_for(connector)
    if manifest is None:
        return {"ok": False, "error": f"unknown connector {connector!r}",
                "known": sorted(_i09_contract.PROVIDER_MANIFESTS)}
    return {"ok": True, "manifest": manifest.to_dict()}

@mcp.tool()
def connector_conformance(connector: str) -> dict:
    """Run the capability-contract checks for one provider (no network)."""
    from providers import ashby, greenhouse, lever
    makers = {"greenhouse": greenhouse.GreenhouseProvider,
              "lever": lever.LeverProvider, "ashby": ashby.AshbyProvider}
    maker = makers.get(connector)
    if maker is None:
        return {"ok": False, "error": f"no conformance harness for {connector!r}"}
    results = _i09_contract.conformance_suite(maker(tokens=[]))
    return {"ok": True, "passed": _i09_contract.suite_passed(results),
            "checks": results}
```

### 1b. Reliability scorecard (read-only tool)

```python
from providers import scorecard as _i09_scorecard

@mcp.tool()
def provider_scorecard(format: str = "json") -> str:
    """Reliability scorecard: freshness, error state, last success, and
    capability label per provider. Local-first; reads the on-device health
    store only."""
    rows = _i09_scorecard.scoreboard()
    return _i09_scorecard.to_json(rows) if format == "json" \
        else _i09_scorecard.render_terminal(rows)
```

### 1c. ATS apply-path honesty (read-only tool)

```python
import ats_apply as _i09_ats

@mcp.tool()
def describe_apply_path(board: str) -> dict:
    """Honest apply-path declaration: direct-submit availability, why not,
    and the recommended path. Read-only."""
    return _i09_ats.describe_apply_path(board)
```

### 1d. Channel + calendar tools (consent-gated)

**STATUS: HELD — open veto.** Channel *enablement* is under the open
Rule 1 veto (`init-09-comms-rereview`; see
`initiatives/i09/channels/DECISIONS.md`). Until it clears, this section
wires NO enablement/handoff behavior. Specifically:

* The MCP surface must NOT pass a caller-asserted `confirm` through to
  `enable_channel`: an MCP agent can always self-assert `confirm=True`,
  so a caller-asserted boolean can never evidence user opt-in.
* The `via="api"` allowlist entry must NOT be treated as a user opt-in
  source for agent-driven calls — `via="api"` records that the CALL
  came over the MCP API, not that a human opted in.

**Required interactive enablement ceremony** (what the wired tool must
do once the veto clears — this is the ceremony, specified now, wired
later):

1. The tool presents the channel's full ConnectorManifest to the user
   (four proof-of-value sections), on the user's own screen.
2. The USER — never the calling agent — completes the opt-in: types
   `enable <channel>` at an interactive terminal or taps "Enable" in
   the web UI.
3. Only that interactive event may call
   `enable_channel(channel, confirm=True, via="cli-wizard"|"web-ui")`.
   `via="api"` is never accepted for enablement from an agent call.

Wire-ready (read-only / fail-safe) tools:

```python
from initiatives.i09.channels import registry as _i09_channels

@mcp.tool()
def channel_enable(channel: str) -> dict:
    """HELD pending the open enablement veto: no caller-asserted confirm
    may authorize opt-in, so there is no wired behavior. Returns
    instructions for the interactive path."""
    return {"ok": False, "enabled": False,
            "error": "enablement_ceremony_undecided",
            "instructions": (
                "Channel enablement over MCP is held pending the open "
                "Rule 1 veto (initiatives/i09/channels/DECISIONS.md). A "
                "caller-asserted confirm cannot authorize opt-in and "
                "via='api' is not evidence of user consent. Use the "
                "interactive wizard (cli) or web UI, where the user "
                "enables the channel themselves.")}

@mcp.tool()
def channel_disable(channel: str) -> dict:
    # Disable is the fail-safe direction: always permitted, even by agents.
    return _i09_channels.disable_channel(channel, via="api")

@mcp.tool()
def channel_status() -> dict:
    """Which channels are enabled + the consent audit trail."""
    return {"enabled": _i09_channels.enabled_channels(),
            "known": sorted(_i09_channels.CHANNELS),
            "audit": _i09_channels.consent_history()[-20:]}

@mcp.tool()
def calendar_propose(kind: str, application: dict, when: str = "",
                     days_after: int = 7) -> dict:
    """Draft (only) an interview-prep event or follow-up reminder."""
    if kind == "interview_prep":
        draft = _i09_cal.propose_interview_prep(application, interview_at=when or None)
    else:
        draft = _i09_cal.propose_followup_reminder(application, days_after=days_after)
    return {"ok": True, "draft": draft.to_dict(),
            "note": "Draft only — nothing written. Confirm via calendar_handoff."}

@mcp.tool()
def calendar_handoff(drafts: list) -> dict:
    """HELD pending the open enablement veto: no caller-asserted
    ``confirm`` passthrough. An agent could self-assert it, so until the
    interactive handoff ceremony (present drafts, user confirms each one
    individually) is specified and reviewed, this tool presents the
    drafts with instructions and hands off NOTHING."""
    # HELD: answer BEFORE touching the payload. A malformed draft must
    # yield the held instructions — never an exception from parsing
    # CalendarDraft(**d) first.
    count = len(drafts) if isinstance(drafts, list) else 0
    return {"ok": False, "handed_off": 0,
            "error": "handoff_ceremony_undecided",
            "draft_count": count,
            "instructions": (
                "Calendar handoff over MCP is held pending the open "
                "veto. Present each draft to the user; only an "
                "interactive user confirmation (one per draft, in the "
                "wizard or web UI) may call "
                "calendar_handoff.confirm_and_handoff(confirm=True).")}
```

### 1e. Record provider health on every fetch (one line per provider call)

Target files: `providers/adzuna.py`, `providers/ashby.py`,
`providers/greenhouse.py`, `providers/lever.py`,
`providers/ziprecruiter.py`. Insertion point: at the end of each
provider's `search()` and `get_details()`, just before the `return`.
(`providers/glassdoor.py` raises `NotImplementedError` on every call, so
it needs no recording.) Alternative single-point option:
`providers/_common.py:154` (`fetch_json`, the shared helper used by
adzuna/ashby/greenhouse/lever) — but it does not receive the provider
name, so it would need an additive `provider: str = ""` kwarg with every
call site updated.

Honest status as of 2026-09-13: **providers do NOT currently record.**
Zero provider modules import or call `provider_health`. Today the only
writers are:

- `watch.py:151` — `_record_watch_health()` calls
  `provider_health.record_fetch(board_name, ok, note=note)` after a watch
  run (`watch.py:119`, `:122`);
- `providers/health_contract.py:229` (`class ProviderHealthAdapter`) / `:267`
  (`ProviderHealthAdapter.record()`) — calls `ph.record_captcha(...)` /
  `ph.record_fetch(...)` (at `:283`, `:292`, `:307`) through 03's public
  write API (see below).

Until this sweep adds the lines below, the scorecard (1b) renders "no
data" rows for every provider — which is honest, but not useful. Add, per
provider, one import and one record line (additive; health must never
break a fetch, so wrap in try/except like `watch.py`'s helper does):

```python
# insertion: top of providers/<provider>.py
import provider_health

# insertion: end of search(), before `return jobs`
provider_health.record_fetch(self.name, True)
# on a failed attempt (before early `return []` or in the except block):
provider_health.record_fetch(self.name, ok=False, note=str(exc)[:140])
# on CAPTCHA observation (observation only — never bypassed):
provider_health.record_captcha(self.name, "challenged")  # or "blocked"
# on site pushback:
provider_health.cooldown(self.name, seconds, reason="http_429")
```

Board names to record under are the provider classes' `name` attributes:
`adzuna` (adzuna.py:53), `ashby` (ashby.py:53), `greenhouse`
(greenhouse.py:48), `lever` (lever.py:48), `ziprecruiter`
(ziprecruiter.py:115). Equivalent through the adapter (same write path):
`health_contract.record_fetch(ProviderHealthAdapter(), self.name,
success=..., error_state=...)`.

The scorecard (1b) reads through `providers/health_contract.py`, which
adapts 03's `provider_status()` rows onto `HealthSnapshot` — see
`snapshot_from_status` for the field mapping. If 03 renames fields,
update that one function. `health_contract.py` never edits
`provider_health.py`'s *source* — but `ProviderHealthAdapter.record()`
**does write to 03's store through its public API** (`ph.record_captcha`
/ `ph.record_fetch`, health_contract.py:283, :292, :307). So there is more than
one write path today: watch.py's `_record_watch_health`, the adapter's
`record()`, and the `FileHealthStore` fallback used when 03's module is
not importable. Routing every future write through
`health_contract.record_fetch()` keeps this to one declared call pattern;
merging the write paths into one is an Initiative 03 architecture call,
not this sweep's.

---

## 2. cli.py — new commands (additive; new subparsers only)

```python
# in the CLI registration section:
from providers import scorecard as _i09_scorecard
from initiatives.i09.channels import registry as _i09_channels
from initiatives.i09 import calendar_handoff as _i09_cal
import ats_apply as _i09_ats

p = subparsers.add_parser("scorecard", help="Provider reliability scorecard.")
p.add_argument("--json", action="store_true")
p.set_defaults(func=lambda a: print(
    _i09_scorecard.to_json() if a.json
    else _i09_scorecard.render_terminal()))

p = subparsers.add_parser("connector", help="Connector manifest / conformance.")
p.add_argument("name")
p.add_argument("--conformance", action="store_true")
def _cli_connector(a):
    import json as _j
    from providers import _contract as _c
    if a.conformance:
        from providers import ashby, greenhouse, lever
        makers = {"greenhouse": greenhouse.GreenhouseProvider,
                  "lever": lever.LeverProvider, "ashby": ashby.AshbyProvider}
        maker = makers.get(a.name)
        if maker is None:
            print(_j.dumps({"ok": False,
                            "error": f"no conformance harness for {a.name!r}"},
                           indent=2))
        else:
            results = _c.conformance_suite(maker(tokens=[]))
            print(_j.dumps({"ok": True, "passed": _c.suite_passed(results),
                            "checks": results}, indent=2))
    else:
        m = _c.manifest_for(a.name)
        print(_j.dumps({"ok": True, "manifest": m.to_dict()} if m else
                       {"ok": False,
                        "error": f"unknown connector {a.name!r}"}, indent=2))
p.set_defaults(func=_cli_connector)

p = subparsers.add_parser("channel", help="Explicit channel opt-in/out.")
p.add_argument("action", choices=["enable", "disable", "status", "repair"])
p.add_argument("name", nargs="?")
p.add_argument("--confirm", action="store_true")
p.add_argument("--force", action="store_true",
               help="repair: confirm quarantining the ENTIRE consent log "
                    "when every line fails verification (e.g. after a key "
                    "rotation with no backup to restore). Without --force "
                    "the repair refuses loudly and advises restoring the "
                    "key backup first — quarantined entries can never be "
                    "re-trusted.")
def _cli_channel(a):
    import json as _j
    if a.action == "status":
        print(_j.dumps({"enabled": _i09_channels.enabled_channels()}, indent=2))
    elif a.action == "enable":
        print(_j.dumps(_i09_channels.enable_channel(
            a.name, confirm=a.confirm, via="cli-wizard"), indent=2))
    elif a.action == "repair":
        # Audit-preserving repair for a torn/tampered consent log
        # (RECOVERY.md Procedure B): bad lines are MOVED to
        # channel_consent.quarantine.jsonl (never deleted); the repair
        # itself is a signed consent_quarantine entry in the log.
        print(_j.dumps(_i09_channels.quarantine_consent_log(force=a.force),
                       indent=2))
    else:
        print(_j.dumps(_i09_channels.disable_channel(a.name, via="cli-wizard"), indent=2))
p.set_defaults(func=_cli_channel)

p = subparsers.add_parser("apply-path", help="Honest ATS apply-path declaration.")
p.add_argument("board")
p.set_defaults(func=lambda a: print(__import__("json").dumps(
    _i09_ats.describe_apply_path(a.board), indent=2)))
```

---

## 3. dashboard.py (terminal) — scorecard section

Add a "Providers" section to the terminal dashboard render, after the
existing queue/health sections:

```python
from providers import scorecard as _i09_scorecard

def render_provider_section() -> str:
    return "\nPROVIDERS\n" + _i09_scorecard.render_terminal() + "\n"
```

Call `render_provider_section()` in the main dashboard assembly. The
renderer is pure text and performs no network calls.

---

## 4. webui.py (local web) — scorecard card + connector cards

Add a `/api/scorecard` route returning `scorecard.to_json()` (the JSON
form is designed for the web/phone surfaces), and render one card per row
with columns: provider, status, freshness, error, last success,
capability, budget, cooldown, captcha, streak. (The display headers
`error`/`streak` map to the row keys `error_state`/`recovery_streak` —
scorecard.py:116-117.) Rows with
`"status": "no data"` render an explicit "no data yet" empty state —
never a fake healthy row.

Add a `/api/connector/<name>` route returning the manifest JSON, rendered
as four labeled sections (can do / may be blocked / data sent / needs
confirmation) on the connector detail card.

Session-rescue handoffs (`result["rescue"]` from browser fills) render as
a prominent paused-state card: reason, screenshot, what-to-do guidance,
resume token. The web UI must surface the "Veto never bypasses CAPTCHAs"
note verbatim from `session_rescue.refusal_note()`.

---

## 5. What NOT to wire (deliberate)

* MCP `channel_enable` with a caller-asserted `confirm` passthrough:
  held under the open Rule 1 enablement veto (section 1d). No `confirm`
  may flow from an MCP call into `enable_channel`, and `via="api"` must
  never be read as user opt-in, until the veto clears and the
  interactive enablement ceremony is wired.
* MCP `calendar_handoff` with a caller-asserted `confirm` passthrough:
  same veto, same reason — the interactive per-draft confirmation
  ceremony is specified but not yet wired.
* WhatsApp/Discord/Messenger channels: declaration + registry only. No
  transport is wired until the corresponding skill is connected AND the
  user explicitly enables the channel. Do not add send paths for them in
  this sweep.
* `ats_direct` verified submits: the registry has no verified endpoints;
  `apply_direct` with confirm=True still returns "not available". Do not
  wire a submit button to it until an endpoint flips to "verified" with a
  field spec and passes review.
* Calendar writes: `confirm_and_handoff` returns a payload; the sweep must
  execute it through the google-calendar skill with the user's grant —
  never invent a direct API write.
