# Initiative 11 — Governed extension platform

Let contributors add workflows without weakening the refusal model that
makes Veto distinct.

## Proof of value

**A new extension can prove what it accesses and cannot bypass the core
confirmation or governance gates.** Proven adversarially, not by
assertion: `policy_kit/attacks.py` runs 14 hostile extensions through the
real host — self-confirm, forged token, exfiltration to undeclared host,
filesystem escape, banned core import, platform-internals import,
stdlib fs bypass, bare open(), ctx-host traversal self-mint (the
reviewer's demonstrated introspection vectors), scope escalation, kind
downgrade, audit tamper, post-load manifest mutation, undeclared action —
and every one is blocked
(`tests/test_ext_adversarial.py`; `crew.py extension adversarial`).

## Epics

| # | Epic | Module | Status |
|---|------|--------|--------|
| 1 | Plugin manifest | `manifest/` — `schema.py`, `MANIFEST_SCHEMA.json` (published contract) | done |
| 2 | Capability sandbox | `sandbox/` — `host.py` (broker + confirmation tokens), `fs.py`, `net.py`, `audit.py` | done |
| 3 | Policy test kit | `policy_kit/` — `checks.py` (10 reusable checks), `attacks.py` (14 attacks) | done |
| 4 | Developer scaffold | `scaffold/template.py` — generates MCP tool, CLI command, wizard step, web card, tests, docs, retro entry | done |
| 5 | Signed packages | `signing/sign.py` — provenance, version pinning, upgrade review, revocation | done |
| 6 | Curated registry | `registry/registry.py` — compatibility, security, UX, governance gates | done |

## How the hard rule holds

1. **Declared capabilities, frozen at load.** The manifest is the only
   authority; the runtime snapshots it. Post-load edits are inert.
2. **Brokered powers.** Extension code receives one object — `ctx` — with
   only its declared scopes, fs roots, and network destinations.
3. **Confirmation cannot be self-minted.** `request_confirmation()` creates
   a *pending* request; only human surfaces (`extension confirm`, web UI)
   call `mint()`. Tokens are HMAC-bound to (extension, action, exact
   params), single-use, 15-minute expiry. Forged/replayed/swapped tokens
   are rejected.
4. **Install-time import scan.** Extension code importing `server`,
   `compliance`, `apply_queue`, `browser_apply`, `governance`, or raw
   socket/subprocess primitives is rejected before it ever loads.
5. **Governance veto.** `confirm-required` actions pass the user's veto
   screen in the host; a veto blocks even a confirmed action.
6. **Host-only audit.** Hash-chained, append-only; extension code gets a
   read-only view of its own entries, never a write handle.

## Surfaces

- **Terminal:** `cli.py extension <list|verify|install|run|request|pending|confirm|policy-test|adversarial|scaffold|audit-verify>`
- **MCP:** `extensions_list`, `extensions_verify`, `extensions_run`,
  `extensions_request_confirmation`, `extensions_pending_confirmations`,
  `extensions_policy_test`, `extensions_scaffold`
  (mint is deliberately NOT an MCP tool — the human gate stays human)
- **Web/phone:** specified in `integration_notes.md` for the integration
  sweep (extensions page, pending-approval UI, web cards).

## Reference extension

`reference/role-radar-digest/` — built with the scaffold: read-only
digest of watches/applications, local draft, notification proposal. No
network, no confirm-required actions, no PII. Passes all 10 policy checks;
signed.

## Threat-model boundary (honest)

Extensions run in-process (local-first, like pip packages). The
enforcement is: manifest validation + deep-frozen capabilities + install-
time import scan + capability broker (no ``ctx._host`` path to the
confirmation broker) + host-only confirmation minting + host-only audit.
File access goes only through ``ctx.fs`` and network only through
``ctx.http`` — ``os``/``pathlib``/``open()``/``socket`` and friends are
rejected at install time, and the extension can never import this
platform's own package to reach the host secret.

**Disclosed residual risk:** a deliberately adversarial author with
source access could reach host internals through Python introspection
(``__globals__`` / ``__class__`` chains) or dynamic-import tricks
(``__import__``/``eval``, which bypass the static AST import scan) — true
isolation needs the roadmap's out-of-process execution step, not claimed
today. That path is controlled by three independent gates: the registry's
human security review (a person reads the code before publish — the scan
is a tripwire, the review is the control), signed packages
(accountability), and the append-only audit log (detection). The
documented, tested guarantee: nothing reachable through the extension
API — including every ``ctx`` attribute — can self-confirm, widen a
capability, downgrade an action kind, or write the audit log. The
adversarial suite (`policy_kit/attacks.py`, 14 attacks) proves it.
