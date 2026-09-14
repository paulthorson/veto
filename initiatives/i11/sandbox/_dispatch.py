#!/usr/bin/env python3
"""Host-side dispatch bindings for loaded extensions.

CRITICAL INVARIANT — read before modifying:

This module is HOST-SIDE ONLY. It must never be referenced from the
module globals of any extension-reachable code. ``broker.py`` resolves
bindings exclusively through a *function-local* lazy import, so the
name ``_dispatch`` never appears in ``ctx.<method>.__globals__``.

The registry maps opaque integer handles to ``_Binding(host, ext_id)``
records. The handle is DATA, not a reference: it cannot be
dereferenced, called, or walked to the Host by any means available to
extension code — there is no weakref to call, no token to look up.
Only host-side ``resolve()`` maps a handle back to (Host, ext_id), and
``resolve`` is never exposed to extension code (not in
``broker.py``'s module globals, not in the extension builtins, not
importable — the static scan rejects all imports).

Each handle is bound permanently to exactly one (Host, ext_id) at
``register()`` time and can never be re-scoped: the ext_id used for
every dispatch is the one stored in the binding, never
extension-supplied, so a handle cannot be wielded across extension
boundaries. ``revoke()`` removes the record; resolving a revoked or
never-issued handle fails closed with ``RuntimeError`` (stale
generations are rejected, not resurrected).

Liveness: the registry holds its bindings strongly; ``revoke()`` (via
``Host.unload_extension``) drops them. The extension-reachable side
holds only the integer.

Why not a weakref: a weakref IS a dereferenceable reference — any
primitive that calls it (including a scan-bypassing dunder walk)
yields the live ``_Binding`` and its Host. An integer yields nothing.
The binding's safety therefore does not depend on the static scan
being perfect; even a scan bypass cannot mint confirmations from an
integer.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any


class _Binding:
    """Host-side record for one loaded extension: (Host, ext_id)."""

    __slots__ = ("host", "ext_id")

    def __init__(self, host: Any, ext_id: str) -> None:
        self.host = host
        self.ext_id = ext_id


_registry: dict[int, _Binding] = {}
_counter = itertools.count(1)
_lock = threading.Lock()


def register(host: Any, ext_id: str) -> int:
    """Create a dispatch binding for one loaded extension.

    Returns an opaque integer handle — never the Host, never a
    dereferenceable reference. The handle is permanently bound to
    exactly this (Host, ext_id).
    """
    handle = next(_counter)
    with _lock:
        _registry[handle] = _Binding(host, ext_id)
    return handle


def resolve(handle: int) -> tuple[Any, str]:
    """Map a handle back to (Host, ext_id). Host-side only; fails closed.

    Unknown, revoked, or non-integer handles raise ``RuntimeError`` —
    stale generations are rejected, never resurrected.

    The handle must be a genuine ``int`` (``type(handle) is int``): a
    ``bool`` is an ``int`` subclass and ``True == 1``, so without this
    guard ``resolve(True)`` would alias the first-issued binding — and
    handles are small sequential ints, guessable by anyone with
    white-box ``_broker_call`` access. ``bool`` and ``int`` subclasses
    are rejected, not coerced.
    """
    if type(handle) is not int:
        raise RuntimeError("extension binding handle must be an int")
    with _lock:
        binding = _registry.get(handle)
    if binding is None:
        raise RuntimeError("unknown or revoked extension binding")
    return binding.host, binding.ext_id


def revoke(handle: int) -> None:
    """Revoke a binding: drop the (Host, ext_id) record.

    After revocation the handle resolves to ``RuntimeError`` — the
    extension's Broker becomes inert.
    """
    with _lock:
        _registry.pop(handle, None)
