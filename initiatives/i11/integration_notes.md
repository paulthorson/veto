# Initiative 11 — integration notes (for the later integration sweep)

**Do not edit `cli.py`, `server.py`, `webui.py`, or `dashboard.py` in the
Initiative 11 workstream.** Apply the snippets below in a dedicated
integration pass owned by the program coordinator.

## 1. `cli.py` — nothing required

`crew.py:register_cli()` now returns `{"crew": ..., "extension": ...}` and
`crew` is already in `cli.py:_PLUGIN_CLI_MODULES`, so `cli.py extension
<action>` works today with zero `cli.py` edits (verified:
`cli.py extension list` → `{"extensions": []}`).

Optional (integration sweep): group `extension` under a nicer help heading.
No functional change needed.

## 2. `server.py` — nothing required for tools; wire data providers

`server.py` calls `register_tools(mcp)` on every `_PLUGIN_MODULES` entry;
`crew.register_tools` now registers the seven `extensions_*` MCP tools, so
they are live with zero `server.py` edits.

**Required wiring (data providers):** the host ships with synthetic
fixtures. Replace them with real providers at server startup — insert once,
near the other `register_*` calls:

```python
# --- Initiative 11: extension host data providers (integration sweep) ---
from initiatives.i11.sandbox import host as _ext_host

_ext_host.register_data_provider("jobs:read", lambda: _recent_jobs())
_ext_host.register_data_provider("applications:read", lambda: _read_applications())
_ext_host.register_data_provider("profile:read", lambda: _load_profile())
_ext_host.register_data_provider("outcomes:read", lambda: _read_outcomes())
_ext_host.register_data_provider("lifecycle:read", lambda: _read_lifecycle())
_ext_host.register_data_provider("watches:read", lambda: _read_watches())
```

Each provider is `() -> JSON-serializable`. The broker scope-checks and
PII-redacts before extension code sees the data.

## 3. `webui.py` / `dashboard.py` — extensions page + confirm UI (human surface)

The confirmation gate's human surface lives here. Minimum viable page:

- `GET /extensions` — table of installed extensions: name, version, trust
  (`signed`/`local-unsigned`), and the `manifest_summary()` capability
  disclosure (data scopes, network destinations, actions, PII mode).
- `GET /extensions/pending` — pending confirmation requests with the exact
  action + params shown in plain language; **Approve** / **Deny** buttons.
- `POST /extensions/pending/<pending_id>/approve` — server-side calls
  `host.confirmations.mint(pending_id)` and shows the single-use token with
  "paste into `extension run --token …` or approve-and-run here".
- Extension web cards: render each installed extension's `web_card.html`
  snippet (required states: empty/loading/blocked/error/ready).

Security notes for the sweep:
- Every `/extensions/*` route sits behind the existing token auth.
- `mint` is never exposed as an MCP tool or a GET — POST only, human click.
- Capability disclosure text comes from the signed manifest, not from
  extension-provided strings (extensions must not narrate their own
  permissions).

## 4. Phone surface

The web UI is responsive; `/extensions` and `/extensions/pending` work
from the phone over the existing LAN + token-auth path. No separate phone
work: approving a confirmation from the phone is the same POST.

## 5. Dashboard (terminal)

Add an `extensions` screen to the terminal dashboard reusing
`crew.ext_list()` / `crew.ext_pending()`; approvals shell out to
`extension confirm <pending-id>` (human at the keyboard — the mint stays
human-gated).

## Decision record

- Scope: terminal (`extension` CLI) + MCP tools ship in Initiative 11;
  web/phone confirm UI is specified here for the integration sweep.
  No surface is omitted silently — this note is the record.
