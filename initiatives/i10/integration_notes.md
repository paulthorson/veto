# Initiative 10 — integration notes (for the later wiring sweep)

The i10 modules are self-contained under `initiatives/i10/`. They do NOT
touch `cli.py`, `webui.py`, or `dashboard.py`. Paste the snippets below
into those files during the integration sweep. `server.py` is in i10's
file scope — the MCP registration snippet is provided but NOT applied,
so the sweep can land it together with the CLI wiring.

Conventions used below: `DATA_DIR` resolves to the installed data dir
(`~/.local/share/veto/data`, or `install.json`'s `data_dir`).

---

## 1. `cli.py` — new subcommands

Add after the existing `doctor` registration (or wherever subcommands
are wired):

```python
# --- Initiative 10: packaging, backup & secure sync ---
from initiatives.i10 import installer as i10_installer
from initiatives.i10 import schema_migrate as i10_migrate
from initiatives.i10 import backup as i10_backup
from initiatives.i10 import sync as i10_sync
from initiatives.i10 import channels as i10_channels

def _i10_data_dir(args):
    if getattr(args, "data_dir", ""):
        return args.data_dir
    from pathlib import Path
    import json
    rec = Path.home() / ".local" / "share" / "veto" / "install.json"
    if rec.exists():
        try:
            return json.loads(rec.read_text())["data_dir"]
        except (KeyError, ValueError):
            pass
    return "."

p = sub.add_parser("install", help="Install Veto (env checks, deps, onboarding)")
p.add_argument("--yes", action="store_true"); p.add_argument("--with-browser", action="store_true")
p.set_defaults(func=lambda a: i10_installer.main(["install"] + (["--yes"] if a.yes else []) + (["--with-browser"] if a.with_browser else [])))

p = sub.add_parser("uninstall", help="Remove Veto (data kept unless --purge-data)")
p.add_argument("--purge-data", action="store_true")
p.set_defaults(func=lambda a: i10_installer.main(["uninstall"] + (["--purge-data"] if a.purge_data else [])))

p = sub.add_parser("migrate", help="Apply pending schema migrations")
p.add_argument("--data-dir", default=""); p.add_argument("--dry-run", action="store_true")
p.set_defaults(func=lambda a: i10_migrate.main(["--data-dir", _i10_data_dir(a), "migrate"] + (["--dry-run"] if a.dry_run else [])))

p = sub.add_parser("backup", help="Encrypted backup / verify / restore / drill")
p.add_argument("op", choices=["create", "verify", "restore", "drill"])
p.add_argument("--data-dir", default=""); p.add_argument("--file", default="")
p.add_argument("--selective", default="")
p.set_defaults(func=lambda a: i10_backup.main(_i10_backup_args(a)))

p = sub.add_parser("sync", help="Opt-in encrypted device sync (local-only default)")
p.add_argument("op", choices=["enable", "disable", "status", "run"])
p.add_argument("--data-dir", default=""); p.add_argument("--vault", default="")
p.set_defaults(func=lambda a: i10_sync.main(_i10_sync_args(a)))

p = sub.add_parser("channel", help="Release channels: versions / check / promote")
p.add_argument("op", choices=["versions", "check", "promote", "notes"])
p.add_argument("--data-dir", default="")
p.set_defaults(func=lambda a: i10_channels.main(_i10_channel_args(a)))
```

`_i10_backup_args`, `_i10_sync_args`, `_i10_channel_args` translate the
parsed args into the module argv lists; passphrases must come from
`getpass.getpass()` or `$VETO_BACKUP_PASSPHRASE` — never from argv.

---

## 2. `webui.py` — serve the PWA bundle + wiring

Mount the i10 PWA bundle at the web root (new static routes; no changes
to existing routes):

```python
from initiatives.i10 import pwa as i10_pwa
PWA_DIR = i10_pwa.PWA_DIR  # initiatives/i10/pwa/

# Serve (exact framework depends on webui.py's server):
#   /manifest.webmanifest -> PWA_DIR/"manifest.webmanifest"  (application/manifest+json)
#   /service-worker.js    -> PWA_DIR/"service-worker.js"     (application/javascript, Serve from ROOT scope)
#   /deferred-queue.js    -> PWA_DIR/"deferred-queue.js"
#   /offline.html         -> PWA_DIR/"offline.html"
#   /icons/icon-192.png, /icons/icon-512.png -> served from webui static icons dir
```

In `index.html` `<head>`:

```html
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0d1117">
```

Before `</body>` (after `app.js`):

```html
<script src="/deferred-queue.js"></script>
<script>
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/service-worker.js");
}
</script>
```

Read views served by the existing dashboard API are offline-safe via the
service worker's network-first-with-cache-fallback strategy; mutations
should go through `VetoQueue.submit(method, url, body)` so they defer
when offline. Add a queue badge element with
`data-veto-queue-count` anywhere in the nav.

---

## 3. `dashboard.py` — terminal dashboard

No changes required. Optional: surface backup freshness in the terminal
dashboard via `initiatives.i10.backup`'s audit log
(`<data-dir>/backup_audit.jsonl`, last `backup_created` entry) — a
one-line "last backup: <date>" indicator. Left to the dashboard team's
discretion.

---

## 4. `server.py` — MCP tool registration (i10 file scope; NOT yet applied)

```python
from initiatives.i10 import backup as i10_backup
from initiatives.i10 import sync as i10_sync
from initiatives.i10 import schema_migrate as i10_migrate
from initiatives.i10 import channels as i10_channels

@mcp.tool()
def veto_backup_create(data_dir: str, out_path: str) -> dict:
    """Create an encrypted backup. Passphrase comes from the secure vault,
    never from the tool call."""
    raise NotImplementedError("wire passphrase via secure vault in the sweep")

@mcp.tool()
def veto_backup_verify(backup_path: str) -> dict: ...
@mcp.tool()
def veto_drill(data_dir: str) -> dict:
    """Run the disaster-recovery drill (backup → restore to scratch →
    100% checksum verification). Live data untouched."""
@mcp.tool()
def veto_migrate_status(data_dir: str) -> dict: ...
@mcp.tool()
def veto_sync_status(data_dir: str) -> dict:
    """Sync is local-only unless the user opted in; this tool only reports."""
```

Passphrase handling for MCP tools MUST go through the Secure Vault
capture flow — never a plaintext tool argument. The sweep owns this.

---

## 5. Assumptions the wiring sweep should know

- `outcome-event` (`outcome-min-v0`) is the published frozen contract.
  `profile` (`profile-v1`) and `evidence-store` (`evidence-v0`) are
  i10-defined interfaces; when Initiative 01 publishes the real
  contracts, update `initiatives/i10/schemas.py` registry entries (status
  → `published`) and add migrations — the framework handles the rest.
- Legacy `applications.json` is deliberately NOT a named stable schema;
  backup/sync exclude it. If the capture team promotes it (or an
  equivalent) to a stable schema, add a registry entry.
- `cryptography` is a new runtime dependency (added to
  `requirements.txt`); the Docker image needs a rebuild.

---

## 6. PWA wiring requirements — WS5 review fixes (2026-09-13)

The bundle (`initiatives/i10/pwa/`) and this webui wiring MUST land
atomically. `pwa.validate_bundle()` fails honestly (`webui-wiring`
check) until every marker below is present — CI stays red, not green,
until then. Decision record: `initiatives/i10/DECISIONS_pwa.md`.

### 6a. Serve the bundle (extends §2)

Serve from the web root, exact paths (the SW resolves `sw-version.js`
relative to its own root scope):

- `/manifest.webmanifest` → `PWA_DIR/"manifest.webmanifest"` (`application/manifest+json`)
- `/service-worker.js` → `PWA_DIR/"service-worker.js"` (`application/javascript`, root scope)
- `/sw-version.js` → `PWA_DIR/"sw-version.js"` (GENERATED by
  `python -m initiatives.i10.pwa stamp`; `application/javascript`)
- `/deferred-queue.js` → `PWA_DIR/"deferred-queue.js"`
- `/offline.html` → `PWA_DIR/"offline.html"`
- `/icons/icon-192.png`, `/icons/icon-512.png` → webui static icons dir
- `/app.css`, `/app.js` → the app shell (the SW precaches these as
  OPTIONAL: install succeeds without them, but the shell is degraded)

In `index.html` `<head>`: `<link rel="manifest" href="/manifest.webmanifest">`
+ `<meta name="theme-color" content="#0d1117">`. Before `</body>` (after
`app.js`): `<script src="/deferred-queue.js"></script>` plus the
`navigator.serviceWorker.register("/service-worker.js")` snippet from §2.

### 6b. Enqueue ownership — exactly one path (B1)

The PAGE owns the deferred queue. The service worker, on a failed
mutation, answers `202 {queued:true}` and NOTHING else — no
postMessage, no fan-out. The old `veto:queue-mutation` message protocol
is REMOVED; do not re-add it.

- Route ALL mutations through `VetoQueue.submit(method, url, body)` —
  it sends an `Idempotency-Key` header on the original attempt and
  enqueues exactly once (by key) on `202 {queued:true}` or network
  failure.
- Any custom fetch wrapper MUST funnel 202-queued responses through the
  single choke point `VetoQueue.enqueueRequest({method, url, body,
  idempotencyKey})` — never write to IndexedDB directly.
- Pass a caller-owned idempotency key for user-initiated actions that
  can be retried from the UI (e.g. derived from the draft/action id);
  `submit()` generates one when absent.
- While a `submit()` is in flight, disable its trigger (button/form) —
  double-clicks with distinct keys are distinct queue entries by design.

### 6c. Server idempotency contract (B1)

Every state-changing endpoint MUST accept the `Idempotency-Key` header:

- First sight of a key: process normally, store the result keyed by it.
- Repeat of a key: return the stored result (2xx), do NOT re-apply.
  A `409` is also acceptable and the client treats it as "already
  applied" (entry dropped, not dead-lettered).
- Without this, replay-after-ambiguous-failure (request reached the
  server, response was lost) can double-apply — e.g. submit the same
  job application twice. This is the highest-stakes defect class in the
  PWA surface; the client sends the key, the server must honor it.

### 6d. UX contract — user-visible queue (B1)

- Badge: any element with `data-veto-queue-count` shows the pending
  count (hidden at 0); `document.body` gets class `veto-has-queued`.
  Put the badge in the nav (§2).
- "Will replay N actions" confirmation: `VetoQueue.replay()` ALWAYS
  shows a confirmation dialog ("Veto will replay N queued action(s)…")
  before sending anything, unless called as `replay({confirmed: true})`
  after the webui's own user-visible confirm step. There is intentionally
  NO silent auto-replay: the `online` event only refreshes the badge and
  fires `veto:queue-changed` on `document` (detail: `{pending}`) — the
  webui renders its own "N actions pending — Review & sync" prompt from
  that event and calls `replay()`.
- Dead-letter UI: render `VetoQueue.deadLetter()` with per-entry Retry
  (`retryDead(id)`) and Discard (`discardDead(id)`). Permanent (4xx)
  entries need user judgment; retryable (429/5xx-exhausted) entries can
  be retried after connectivity recovers.

### 6e. Cache versioning runbook (F5/F6)

- `CACHE_NAME = "veto-shell-<16-hex-content-hash>"`, stamped into
  `pwa/sw-version.js` by `python -m initiatives.i10.pwa stamp`.
- After ANY change under `pwa/`, re-run `stamp` and commit the new
  `sw-version.js`. CI (`sw-version-fresh` check) fails on a stale stamp,
  so a deploy can never silently serve a stale shell.
- Old versioned caches are purged on SW `activate`. Read-API cache is
  separate (`<CACHE_NAME>-api`), TTL 15 min, 200-entry cap with
  oldest-first eviction; only 2xx responses are cached anywhere.

### 6f. Test gate (F7)

`tests/i10_pwa_harness.mjs` (node) loads the REAL `service-worker.js`
and `deferred-queue.js` with stubbed platform globals and asserts
install/fetch/queue/replay behavior end to end (55 assertions:
single-enqueue, cross-tab dedup, idempotency-key on replay, confirm
gate, 429/5xx bounded retries, 409 semantics, TTL/eviction). It runs
inside `tests/test_i10_channels_pwa.py::TestPwaJsHarness`.

Static validation (`pwa.validate_bundle`) canNOT prove runtime behavior.
The Q3 exit-gate matrix MUST include a real-device test: install the PWA
on a phone, go offline, perform a mutation, confirm exactly one queue
entry and one replay with the idempotency key — before the PWA is
declared shippable.
