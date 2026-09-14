#!/usr/bin/env python3
"""Capability broker: the ONLY host surface extension code can reach.

Security design — read before modifying:

Extension code receives an ``ExtensionContext`` whose methods are
defined HERE, so ``ctx.<method>.__globals__`` exposes only this
module's safe namespace. That namespace deliberately contains NOTHING
dangerous:

- no Host class, no confirmation broker, no mint, no HMAC secret;
- no token registry: per-load dispatch bindings live in
  ``_dispatch.py`` and are reached exclusively through a
  *function-local* lazy import, so ``_dispatch`` never appears in this
  module's ``__globals__``;
- the ``Broker`` holds only an *opaque integer handle* (plus its own
  ext_id string). The handle is data, not a reference: it cannot be
  dereferenced, called, or walked to the Host by any means available
  to extension code. Only host-side ``_dispatch.resolve()`` maps it
  back to (Host, ext_id), and ``resolve`` is never exposed to
  extension code. A weakref would be the wrong primitive here — it IS
  dereferenceable by anything that can call it; an integer yields
  nothing even to a scan-bypassing dunder walk.

- every error crossing back into extension code is sanitized: the
  dispatcher re-raises a fresh ``type(msg)`` instance *outside* any
  ``except`` block, so ``__context__`` is None, and binding resolution
  runs inside a nested helper frame that has already unwound before
  the raise — so the traceback contains no frame whose ``f_locals``
  hold the live Host or the resolver (a traceback-visible host frame
  would hand extension code the Host via frame-locals introspection).

- dispatch goes through the module-level ``_broker_call(binding,
  op_kind, *args)`` — deliberately NOT a ``Broker`` method. A
  bound-method frame necessarily exposes ``self`` in ``f_locals``, and
  the sanitized exception's traceback keeps every still-stacked frame
  alive for extension code to walk; the 2026-09-13 blind re-review
  demonstrated end-to-end self-minting from exactly that: reading
  ``f_locals["self"]`` from the ``Broker._call`` frame, then
  ``broker._call(lambda h, e: h)`` to receive the live Host, because
  ``_call`` invoked a *caller-supplied* ``op(host, ext_id, *args)``.
  ``self`` cannot be deleted from a bound-method frame, so the fix
  removes the capability instead of hiding it: ``_broker_call`` accepts
  NO caller-supplied callable — the op is selected from the fixed
  ``_OP_HANDLERS`` table by ``op_kind`` — and its frames hold only the
  opaque integer binding, the op-kind string, and plain
  extension-supplied data. There is no ``_call(op)`` or equivalent
  host-passing invocation anywhere for extension code to reach, from
  traceback frames or otherwise.

What this closes (verified by tests + adversarial suite):
- self-minting confirmations via plain attributes, ``__globals__``,
  ``__class__`` / ``__closure__`` chains, ``getattr`` tricks, or
  traceback frame reads — the binding is an opaque integer, not a
  token into a reachable registry and not a dereferenceable weakref,
  and there is no plain-attribute path to the Host;
- scope escalation / kind downgrade (the manifest is deep-frozen by the
  host; enforcement reads the frozen manifest, never ctx state).

Residual (see SECURITY_RESIDUAL.md): in-process Python is not a
security boundary against a sophisticated adversarial author. The
layers above close every demonstrated bypass class; the remaining
residual is documented there, and in-process execution is gated behind
the host's explicit ``unsafe_local_only`` flag.
"""

from __future__ import annotations

from typing import Any, NoReturn


class BrokerError(RuntimeError):
    """Sanitized broker failure surfaced to extension code."""


def _safe_str(value: Any) -> str:
    """Stringify attacker-influenced data without ever raising.

    A bare ``str()`` on attacker-influenced data invokes its
    ``__str__``, which hostile code may override with an implementation
    that RAISES. If that raise escapes from inside a frame whose
    ``f_locals`` hold the live Host, the raw exception bypasses the
    traceback sanitizer entirely — the 2026-09-13 blind re-review
    demonstrated end-to-end self-minting from exactly this (hostile
    ``__repr__`` in a host frame raising ``Boom``; ``str(Boom)``
    raising again in the sanitizer; the raw escape's ``__context__``
    chain reaching the ``_dispatch`` frame's live ``host``).

    Every stringification of attacker-influenced data on the dispatch
    path MUST go through this helper (or ``_safe_repr``) — never a bare
    ``str()``, ``!r`` f-string, or ``json.dumps(..., default=str)``.
    """
    try:
        return str(value)
    except Exception:
        pass
    try:
        return "<unprintable " + type(value).__name__ + ">"
    except Exception:
        # A hostile metaclass could make even __name__ access raise.
        return "<unprintable object>"


def _safe_repr(value: Any) -> str:
    """``repr()`` on attacker-influenced data without ever raising.

    Same rationale as :func:`_safe_str`: a hostile ``__repr__``
    raising inside a host-holding frame would let a raw exception
    escape the traceback sanitizer.
    """
    try:
        return repr(value)
    except Exception:
        pass
    try:
        return "<unrepresentable " + type(value).__name__ + ">"
    except Exception:
        return "<unrepresentable object>"


# ---------------------------------------------------------------------------
# Fixed operation table + module-level dispatcher.
# ---------------------------------------------------------------------------
#
# The op handlers are FIXED: each takes (host, ext_id, *args) and the
# host is supplied exclusively by the dispatcher below, never by
# extension code. They live in this module's (extension-visible)
# ``__globals__`` harmlessly — invoking one requires a Host argument
# that extension code cannot obtain.
_OP_HANDLERS: dict[str, Any] = {
    "data_for": lambda h, e, s: h._data_for(e, s),
    "fs_for": lambda h, e: h._fs_for(e),
    "http_for": lambda h, e: h._http_for(e),
    "draft_artifact": lambda h, e, n, c: h._draft_artifact(e, n, c),
    "queue_notification": lambda h, e, t, b: h._queue_notification(e, t, b),
    "request_confirmation":
        lambda h, e, a, p: h._request_confirmation(e, a, p).pending_id,
    "run_action":
        lambda h, e, a, t, p: h.run_extension_action(
            e, a, confirmation_token=t, **p),
    "audit_view_for": lambda h, e, l: h.audit.entries_for(e, limit=l),
}


def _broker_call(binding: int, op_kind: str, *args: Any) -> Any:
    """Run one FIXED brokered op through the opaque binding.

    Module-level on purpose — never a ``Broker`` method. A bound-method
    frame necessarily exposes ``self`` in ``f_locals``, and the
    sanitized exception's traceback keeps every still-stacked frame
    alive for extension code to walk. The 2026-09-13 blind re-review
    demonstrated the kill chain this enables: the old
    ``Broker._call(op, *args)`` invoked a caller-supplied
    ``op(host, ext_id, *args)``, so reading ``f_locals["self"]`` from
    the ``_call`` frame handed the attacker a hostile-op re-entry point
    (``broker._call(lambda h, e: h)`` → live Host → self-minted
    confirmation → confirm-required action executed).

    This dispatcher therefore accepts NO caller-supplied callable: the
    op is selected from the fixed ``_OP_HANDLERS`` table by
    ``op_kind``. This frame's ``f_locals`` hold only the opaque integer
    binding, the op-kind string, and plain extension-supplied data — no
    object references at all, and in particular no object from which a
    host-passing invocation is reachable.

    Traceback hygiene: binding resolution runs inside the nested
    ``_dispatch`` frame — the ONLY frame whose ``f_locals`` ever hold
    the live Host or the resolver — which has already unwound by the
    time the sanitized exception is raised. The closure is then deleted
    from this frame's locals: calling it directly would re-raise the
    *raw* exception whose traceback re-exposes the host frame.

    Any exception — from op-kind lookup, binding resolution, or the
    host op — is re-raised type-preserved but severed from host frames
    (see ``_reraise_sanitized``).
    """
    def _dispatch() -> Any:
        from ._dispatch import resolve as _resolve  # local: keeps
        # _dispatch out of this module's (extension-visible)
        # __globals__; and out of THIS frame's f_locals, so the
        # resolver is never traceback-visible either.
        handler = _OP_HANDLERS[op_kind]  # fail fast: before host binds
        host, ext_id = _resolve(binding)
        return handler(host, ext_id, *args)

    try:
        return _dispatch()
    except Exception as exc:
        # Infallible capture. str(exc) invokes the exception's
        # __str__, which attacker-influenced code may override with a
        # hostile implementation that RAISES. If that raise escaped
        # here, the new exception would propagate RAW — skipping
        # `del _dispatch` and `_reraise_sanitized` below — carrying
        # __context__ into the _dispatch frame whose f_locals hold the
        # live Host (demonstrated end-to-end 2026-09-13: hostile
        # __repr__ + hostile __str__ -> recovered Host -> self-minted
        # confirmation -> confirm-required action executed with zero
        # human approval). _safe_str can never raise, so `del
        # _dispatch` and `_reraise_sanitized` ALWAYS run and no raw
        # exception ever escapes the sanitizer.
        exc_type, exc_msg = type(exc), _safe_str(exc)
    # The _dispatch frame has unwound; drop the closure so this
    # frame's f_locals offer no re-entry into a host-holding frame.
    del _dispatch
    _reraise_sanitized(exc_type, exc_msg)


def _reraise_sanitized(exc_type: type, exc_msg: str) -> NoReturn:
    """Re-raise ``exc_type(exc_msg)`` with no host linkage.

    MUST be called outside any ``except`` block: the fresh exception is
    constructed and raised here, so ``__context__`` is None and no
    reference to the original exception object is retained.

    Traceback hygiene: the fresh exception's traceback necessarily
    includes every frame still on the stack at raise time — including
    this frame and its caller. Those frames' ``f_locals`` must therefore
    contain no path to the live Host. ``_broker_call`` guarantees this:
    it is module-level (no ``self`` in its frame) and takes no
    caller-supplied callable, and binding resolution runs inside a
    nested helper frame that has already unwound before this raise
    executes.
    """
    try:
        fresh = exc_type(exc_msg)
    except Exception:
        # Unusual exception types whose constructor rejects a plain
        # message still surface, without leaking the original object.
        # The label lookup is guarded too: a hostile metaclass could
        # make even __name__ access raise, and _safe_str keeps the
        # message construction infallible.
        try:
            label = exc_type.__name__
        except Exception:
            label = "error"
        fresh = BrokerError(_safe_str(label) + ": " + exc_msg)
    raise fresh


class Broker:
    """Extension-reachable operations, dispatched through an opaque binding.

    Only the eight brokered operations exist here. The binding is an
    opaque integer handle resolving to (Host, ext_id) host-side; the
    ext_id used for every dispatch is the one bound at load time, never
    extension-supplied, so a handle cannot be re-scoped across
    extension boundaries.

    Security: this class exposes NO host-passing invocation. Dispatch
    goes through the module-level ``_broker_call`` with a fixed op
    kind — there is no ``_call(op, *args)`` and no parameter anywhere
    that accepts a caller-supplied callable, so a hostile op can never
    be threaded to the Host. Bound-method frames of this class that end
    up on a sanitized traceback therefore hold only ``self`` (opaque
    integer handle + bound ext_id string) and plain data: no path to
    the Host via traceback introspection.
    """

    def __init__(self, binding: int, ext_id: str) -> None:
        self._binding = binding  # opaque int handle; not dereferenceable
        self._ext_id = ext_id    # bound at load; never extension input

    # -- the complete extension-visible operation set ----------------------
    # Each method names a FIXED op kind; the op itself is selected from
    # _OP_HANDLERS inside _broker_call. Extension-supplied values are
    # data only — never invoked.
    def data_for(self, scope: str) -> Any:
        return _broker_call(self._binding, "data_for", scope)

    def fs_for(self) -> Any:
        return _broker_call(self._binding, "fs_for")

    def http_for(self) -> Any:
        return _broker_call(self._binding, "http_for")

    def draft_artifact(self, name: str, content: str) -> str:
        return _broker_call(self._binding, "draft_artifact", name, content)

    def queue_notification(self, title: str, body: str) -> str:
        return _broker_call(self._binding, "queue_notification", title, body)

    def request_confirmation(self, action_id: str,
                             payload: dict[str, Any]) -> str:
        return _broker_call(self._binding, "request_confirmation",
                            action_id, payload)

    def run_action(self, action_id: str,
                   confirmation_token: str | None,
                   params: dict[str, Any]) -> dict[str, Any]:
        return _broker_call(self._binding, "run_action",
                            action_id, confirmation_token, params)

    def audit_view_for(self, limit: int) -> list[dict[str, Any]]:
        return _broker_call(self._binding, "audit_view_for", limit)


# ---------------------------------------------------------------------------
# Extension context (the ONLY thing extension code receives)
# ---------------------------------------------------------------------------

class ExtensionContext:
    """Capability-scoped API surface handed to extension code.

    Defined in this safe-globals module so ``ctx.<method>.__globals__``
    exposes nothing dangerous. Deliberately narrow: data(), fs, http,
    draft_artifact(), queue_notification(), request_confirmation(),
    run_action(), audit_view(). No reference to the Host (no ``_host``
    attribute — the context holds an opaque integer dispatch handle
    only), no mint_confirmation, no audit writes, no secret access.
    Capability data exposed here is derived from the deep-frozen
    manifest and cannot be mutated.
    """

    def __init__(self, broker: Broker, ext_id: str,
                 manifest: Any) -> None:
        self._broker = broker
        self._ext_id = ext_id
        self._manifest = manifest  # deep-frozen; mutation raises TypeError

    @property
    def scopes(self) -> frozenset:
        """Declared data scopes (immutable view)."""
        return frozenset(self._manifest["permissions"]["data"])

    @property
    def action_kinds(self) -> dict[str, str]:
        """Declared action id -> kind (immutable snapshot)."""
        return {a["id"]: a["kind"]
                for a in self._manifest["permissions"]["actions"]}

    # -- data ---------------------------------------------------------------
    def data(self, scope: str) -> Any:
        return self._broker.data_for(scope)

    # -- sandboxed fs / net ---------------------------------------------------
    @property
    def fs(self) -> Any:
        return self._broker.fs_for()

    @property
    def http(self) -> Any:
        return self._broker.http_for()

    # -- drafts (local artifacts only, never sent) ------------------------------
    def draft_artifact(self, name: str, content: str) -> str:
        return self._broker.draft_artifact(name, content)

    # -- notifications: queued as proposals, never sent -------------------------
    def queue_notification(self, title: str, body: str) -> str:
        return self._broker.queue_notification(title, body)

    # -- confirmation: request only; minting is host-only ------------------------
    def request_confirmation(self, action_id: str,
                             **params: Any) -> str:
        """Request human confirmation for a confirm-required action.

        Returns a pending id. The host UI shows the pending request; after
        the human approves, the host mints a single-use token bound to the
        exact params. Call run_action with the same params + token.
        """
        return self._broker.request_confirmation(
            action_id, {"action": action_id, "params": params})

    def run_action(self, action_id: str, confirmation_token: str | None = None,
                   **params: Any) -> dict[str, Any]:
        return self._broker.run_action(action_id, confirmation_token, params)

    def audit_view(self, limit: int = 50) -> list[dict[str, Any]]:
        """Read-only view of this extension's own audit entries."""
        return self._broker.audit_view_for(limit)
