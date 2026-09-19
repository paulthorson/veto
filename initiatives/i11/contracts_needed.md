# Initiative 11 — contract-first: what we need from Initiatives 09 and 10

Built in parallel against interface contracts. The manifest schema is
published early so both teams can conform: see
`manifest/MANIFEST_SCHEMA.json` (v1, stable for Q3).

## From Initiative 09 (Connector reliability & coverage)

09's provider contract kit defines the normalized search/detail schema,
budgets, and cooldowns. What 11 needs:

1. **Destination registry.** 09 publishes the canonical list of connector
   hostnames (e.g. `api.greenhouse.io`, board API hosts). Extension
   manifests declare `permissions.network.destinations` as bare hostnames;
   09's registry is the recommended source so extension authors don't
   guess hostnames. Format: JSON list of `{host, connector, capability}`.
2. **Conformance hook.** 09's provider conformance tests should accept an
   extension-declared destination and assert it resolves to a known
   connector — this closes the "extension talks to a lookalike host" gap.
3. **Budget sharing.** 09's per-connector budgets/cooldowns and 11's
   per-extension token buckets compose: the host applies the *tighter* of
   the two. 09 should expose `budget_for(host) -> {calls_per_minute,
   cooldown_s}` for the host to query.

**Degradation rule (assumption flag):** if 09's contracts change or land
late, the platform still ships: destinations remain a plain hostname
allowlist, budgets fall back to the manifest's `rate_limit`, and the 09
hooks above become no-ops. Nothing in 11 *requires* 09 at runtime.

## From Initiative 10 (Packaging & secure sync)

10 owns the one-command installer, schema migrations, backup/restore, and
release channels. What 11 needs:

1. **Package envelope.** 10's packaging consumes 11's signing envelope
   (`signing/sign.py`: `{"alg", "package_sha256", "publisher",
   "signed_at", "sig"}` in `.veto-signature.json`) verbatim — do not
   re-envelope. 10's installer must verify the signature *before*
   unpacking and refuse unsigned packages on the `stable` channel.
2. **Install records.** 10's installer should read/write 11's
   `InstallStore` (`extensions_installed.json`: ext_id → {version,
   package_sha256, publisher, upgraded_from}) so version pinning survives
   10-driven upgrades. Upgrades to a new extension version require the
   recorded upgrade review (`record_upgrade` refuses without one).
3. **Revocation distribution.** 10's release channels distribute 11's
   `RevocationList`; the installer refuses revoked (id, version) pairs.
4. **Backup scope.** Extension `.data/` directories are user data —
   include them in 10's encrypted backup; exclude nothing silently.

**Degradation rule (assumption flag):** if 10 lands late, 11 ships
standalone: `crew.py extension install` verifies + pins locally, and the
registry stays a local JSON file. No network distribution is required for
the Q3 exit gate.

## What 11 publishes (stable)

- `manifest/MANIFEST_SCHEMA.json` — the manifest contract (v1).
- `sandbox/host.py:register_data_provider(scope, fn)` — the data seam.
- `signing/sign.py` — package hash + signature envelope.
- `policy_kit/checks.py:ALL_CHECKS` — the conformance suite 09/10 can reuse.
- `registry/registry.py:GATES` — the four review gates.

## Assumption flags (explicit)

- **Q3 re-cut evidence gate:** this platform is built on the assumption
  that the extension-platform scope is correct without six months of
  outcome data (the operator's directive). Flagged as assumption-built.
- **09/10 contracts:** built on the assumption they land as described;
  both degradation rules above keep 11 shippable if they don't.
- **Developer demand:** built on the assumption extension developers
  show up; the scaffold proves its value with the self-built reference
  extension (`reference/role-radar-digest`), which passes the full policy
  suite and the adversarial suite today.
