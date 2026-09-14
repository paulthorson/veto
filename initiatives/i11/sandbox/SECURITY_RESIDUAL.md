# Sandbox security residual: what in-process execution cannot guarantee

**Status:** known, accepted with mitigations, documented for re-review.

## The honest claim

In-process Python execution of extension code is **not** a security
boundary against a fully adversarial extension author. This is a
property of the language runtime, not of this implementation: a
sufficiently determined author can use object-graph and dunder
introspection to reconstruct powerful runtime paths (e.g. walking
`object.__subclasses__()` from any live object to reach runtime
internals) that no allow-list of attribute names can enumerate
completely. We do not claim otherwise.

## What the current design *does* guarantee

The layers in `initiatives/i11/sandbox/` close every bypass class
demonstrated in the 2026-09-13 adversarial review, and each is
regression-tested in `tests/test_ext_sandbox.py`:

1. **No self-minting.** The confirmation broker is reachable from
   extension code only through an opaque integer dispatch handle
   (`_dispatch.py`). The handle is data, not a reference: it cannot
   be dereferenced, called, or walked to the Host by any means
   available to extension code — there is no weakref to call, no
   token to look up, no `_BROKER_HOSTS`-style registry. Only
   host-side `_dispatch.resolve()` maps it back to (Host, ext_id),
   and `resolve` is never exposed to extension code. The exact
   reviewer proof-of-concept (walk `ctx` → broker → host → `mint`)
   now fails at the first step, and — unlike a weakref design — even
   a scan-bypassing dunder walk dead-ends at the integer: the
   binding's safety does not depend on the static scan being perfect.
   (Verified: `str.format` attribute chains always return `str`
   — repr leakage at most, never live object references.)
2. **No capability widening.** The manifest is deep-frozen into
   `types.MappingProxyType` (a true read-only view — `dict`
   base-class mutators cannot bypass it). Host enforcement reads the
   frozen manifest, never extension-reachable mutable state.
3. **No dynamic code loading.** The static scan rejects `import`,
   `__import__`, `eval`, `exec`, `compile`, and `open` at install
   time; the module is then executed with a stripped builtins subset
   in which those names do not exist at all. Both layers fail closed.
4. **No signature spoofing.** Trust labels are derived from the
   packaging signature, never caller-supplied: a present-but-invalid
   sidecar fails the load; a valid embedded-key signature is labeled
   `signed-integrity` (self-asserted, not provenance); only a pinned
   publisher key earns `signed-verified`.
5. **No silent displacement.** A duplicate extension id raises
   `ExtensionAlreadyLoadedError` instead of overwriting `_loaded`.
6. **Bounded blast radius.** Per-file and per-extension filesystem
   write quotas, audit entry/byte caps, draft and notification count
   quotas.
7. **Governance fails closed.** The veto screen uses the real
   framework entry point (`load_framework().check_veto()`); an
   unevaluated screen reads as a veto, never as a pass.

Additionally, all exceptions crossing back into extension code are
re-raised sanitized — host frames and `Host` references are never
carried into the extension's traceback.

## The residual risk

A sophisticated adversarial author who passes install-time review
could still, in principle, use dunder introspection to escape the
in-process sandbox. The realistic threat model for this platform is
**reviewed, signed extensions** (the registry path pins publisher
keys), where install-time human review plus the layers above make
in-process execution proportionate. Unreviewed third-party code must
never be loaded in-process.

## Required control: explicit opt-in

`Host.load_extension()` **refuses** in-process execution unless the
caller passes `unsafe_local_only=True`, acknowledging this residual.
There is no silent path. Callers that load extensions (the registry
install path, the CLI, tests) must pass the flag deliberately.

## Long-term direction

The sound boundary is **out-of-process execution**: run extension
code in a separate process (or container) with IPC capability
brokers, so the extension's address space never shares objects with
the host. The current `Broker`/`ExtensionContext` API is the seam:
the context already speaks in capability operations
(`ctx.data/fs/http`), which can be re-targeted at an IPC transport
without changing extension code. Migrating execution out-of-process
removes the residual class entirely; the opt-in flag and this
document exist to keep the in-process path honest until then.
