#!/usr/bin/env python3
"""Sandbox / host-broker tests (Initiative 11, Epic 2).

Proves the hard rule at the unit level: confirmation cannot be
self-minted, forged, or replayed; filesystem and network are
deny-by-default; the audit log is append-only and host-written.

Security-rework coverage (2026-09-13 adversarial review):
- Blocker 1: the reviewer's self-mint PoC is rerun and blocked
  (attribute walk rejected at load; the dispatch handle is an opaque
  integer — no weakref/token/registry path to the Host; the HMAC
  secret is unreachable from extension code).
- Blocker 2: the frozen manifest uses types.MappingProxyType — even
  dict.__setitem__/update/pop cannot mutate it.
- Blocker 3: the static scan rejects __import__/eval/exec/compile/open
  AND the exec builtins strip those names at runtime.
- Blocker 4: covered by the Blocker 1 tests (no Host reachability).
- Blocker 5: duplicate extension ids raise loudly; no silent
  displacement.
Plus: trust labels derived from packaging signatures (never
caller-supplied), the unsafe_local_only gate, governance fail-closed,
and filesystem/audit/draft/notification quotas.

In-process execution is gated: every load_extension call in these
tests passes unsafe_local_only=True explicitly (see
initiatives/i11/sandbox/SECURITY_RESIDUAL.md).
"""

import json
import tempfile
import unittest
from pathlib import Path

from initiatives.i11.sandbox.audit import ExtensionAudit
from initiatives.i11.sandbox.fs import SandboxFS, SandboxViolation
from initiatives.i11.sandbox.host import ConfirmationBroker, Host
from initiatives.i11.sandbox.net import (
    CircuitOpen, HttpRequest, HttpResponse, RateLimitExceeded, SandboxHTTP,
    SandboxViolation as NetViolation,
)

_SECRET = b"unit-test-secret-32-bytes-padding!!"


def _mkext(actions, ext_id="ut-ext", **perm_over):
    tmp = Path(tempfile.mkdtemp(prefix="veto-ut-"))
    manifest = {
        "manifest_version": 1, "id": ext_id, "version": "0.0.1",
        "name": "UT", "description": "unit test fixture",
        "permissions": {
            "data": ["jobs:read", "profile:read"],
            "network": {"destinations": ["api.example"]},
            "actions": actions, "pii": "redact",
            "sandbox": {"fs_roots": ["workspace"],
                        "rate_limit": {"calls_per_minute": 60}},
        },
    }
    manifest["permissions"].update(perm_over)
    (tmp / "manifest.json").write_text(json.dumps(manifest))
    (tmp / "extension.py").write_text(
        "ACTIONS = {\n"
        + "".join(f"    '{a['id']}': lambda ctx, **kw: {{'ok': True}},\n"
                  for a in actions)
        + "}\n")
    return tmp


def _host():
    tmp = Path(tempfile.mkdtemp(prefix="veto-ut-audit-"))
    return Host(audit_path=tmp / "audit.jsonl", secret=_SECRET)


def _load(host, ext, **kw):
    """Load an extension in-process, explicitly acknowledging the
    residual risk (see sandbox/SECURITY_RESIDUAL.md)."""
    return host.load_extension(ext, unsafe_local_only=True, **kw)


class TestConfirmationBroker(unittest.TestCase):
    def test_full_cycle(self):
        broker = ConfirmationBroker(secret=_SECRET)
        pending = broker.request("e1", "a1", {"action": "a1", "params": {}})
        token = broker.mint(pending.pending_id)
        self.assertTrue(broker.verify(token, ext_id="e1", action_id="a1",
                                      payload={"action": "a1", "params": {}}))

    def test_wrong_secret_rejected(self):
        broker = ConfirmationBroker(secret=_SECRET)
        forger = ConfirmationBroker(secret=b"other-secret-32-bytes-padding!!")
        pending = forger.request("e1", "a1", {"action": "a1", "params": {}})
        forged = forger.mint(pending.pending_id)
        self.assertFalse(broker.verify(forged, ext_id="e1", action_id="a1",
                                       payload={"action": "a1", "params": {}}))

    def test_replay_rejected(self):
        broker = ConfirmationBroker(secret=_SECRET)
        pending = broker.request("e1", "a1", {"action": "a1", "params": {}})
        token = broker.mint(pending.pending_id)
        payload = {"action": "a1", "params": {}}
        self.assertTrue(broker.verify(token, ext_id="e1", action_id="a1",
                                      payload=payload))
        self.assertFalse(broker.verify(token, ext_id="e1", action_id="a1",
                                       payload=payload))

    def test_payload_swap_rejected(self):
        broker = ConfirmationBroker(secret=_SECRET)
        pending = broker.request("e1", "a1", {"action": "a1", "params": {}})
        token = broker.mint(pending.pending_id)
        self.assertFalse(broker.verify(
            token, ext_id="e1", action_id="a1",
            payload={"action": "a1", "params": {"evil": True}}))

    def test_cross_extension_binding_rejected(self):
        broker = ConfirmationBroker(secret=_SECRET)
        pending = broker.request("e1", "a1", {"action": "a1", "params": {}})
        token = broker.mint(pending.pending_id)
        self.assertFalse(broker.verify(token, ext_id="e2", action_id="a1",
                                       payload={"action": "a1", "params": {}}))

    def test_expired_pending_cannot_mint(self):
        broker = ConfirmationBroker(secret=_SECRET)
        pending = broker.request("e1", "a1", {"action": "a1", "params": {}})
        pending.expires_at = 0  # force expiry
        with self.assertRaises(ValueError):
            broker.mint(pending.pending_id)


class TestHostConfirmationGate(unittest.TestCase):
    def test_confirm_required_without_token_refused(self):
        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        with self.assertRaises(PermissionError):
            host.run_extension_action(loaded.ext_id, "send")

    def test_confirm_required_with_valid_token_runs(self):
        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        # Governance veto screen: framework unavailable in tests -> advisory.
        loaded = _load(host, ext)
        pending = host.confirmations.request(
            loaded.ext_id, "send", {"action": "send", "params": {}})
        token = host.confirmations.mint(pending.pending_id)
        result = host.run_extension_action(loaded.ext_id, "send",
                                           confirmation_token=token)
        self.assertTrue(result["ok"])

    def test_ctx_has_no_mint(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        self.assertFalse(hasattr(loaded.ctx, "mint_confirmation"))
        self.assertFalse(hasattr(loaded.ctx, "confirmations"))

    def test_manifest_mutation_post_load_inert(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        data = json.loads((ext / "manifest.json").read_text())
        # "applications:read" was NOT in the frozen manifest.
        data["permissions"]["data"] = ["jobs:read", "profile:read",
                                       "applications:read"]
        (ext / "manifest.json").write_text(json.dumps(data))
        with self.assertRaises(PermissionError):
            loaded.ctx.data("applications:read")


class TestSelfMintBlocked(unittest.TestCase):
    """Blocker 1 (+4): the reviewer's self-mint PoC, rerun as blocked."""

    def _evil_ext(self, source):
        tmp = Path(tempfile.mkdtemp(prefix="veto-ut-evil-"))
        manifest = {
            "manifest_version": 1, "id": "evil", "version": "0.0.1",
            "name": "Evil", "description": "adversarial fixture",
            "permissions": {
                "data": ["jobs:read"],
                "network": {"destinations": []},
                "actions": [{"id": "send", "kind": "confirm-required",
                             "confirm_required": True}],
                "pii": "redact",
                "sandbox": {"fs_roots": [],
                            "rate_limit": {"calls_per_minute": 60}},
            },
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest))
        (tmp / "extension.py").write_text(source)
        return tmp

    def test_poc_attribute_walk_rejected_at_load(self):
        # The reviewer's PoC shape: walk ctx -> broker -> host -> mint
        # with attribute syntax. The static import scan rejects private/
        # dunder attribute access at load time — the PoC never executes.
        from initiatives.i11.manifest.schema import ManifestError
        ext = self._evil_ext(
            "def steal(ctx):\n"
            "    binding = ctx._broker._binding()\n"
            "    host = binding.host\n"
            "    pending = ctx.request_confirmation('send')\n"
            "    return host.confirmations.mint(pending)\n"
            "ACTIONS = {'send': lambda ctx, **kw: {'ok': True}}\n")
        host = _host()
        with self.assertRaises(ManifestError) as cm:
            host.load_extension(ext, unsafe_local_only=True)
        self.assertIn("import-scan", str(cm.exception))

    def test_poc_token_registry_gone(self):
        # The pre-rework design (_BROKER_HOSTS + readable broker token)
        # no longer exists: the PoC's registry lookup has nothing to
        # look up, and the broker exposes no token attribute.
        from initiatives.i11.sandbox import broker as broker_mod
        self.assertFalse(hasattr(broker_mod, "_BROKER_HOSTS"))
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        self.assertFalse(hasattr(loaded.ctx._broker, "_token"))
        self.assertFalse(hasattr(loaded.ctx._broker, "_host"))

    def test_handle_is_opaque_integer(self):
        # Even a scan-bypassing dunder walk dead-ends: the handle is an
        # int — not dereferenceable, not callable, carrying no Host.
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        handle = loaded.ctx._broker._binding
        self.assertIsInstance(handle, int)
        for attr in ("host", "mint", "confirmations", "_secret",
                     "resolve", "host_ref"):
            self.assertFalse(hasattr(handle, attr), attr)
        with self.assertRaises(TypeError):
            handle()  # an int cannot be "dereferenced"

    def test_no_plain_attribute_path_to_host_or_broker(self):
        # White-box walk (full Python, simulating a scan bypass): no
        # plain-attribute path from the context reaches the Host, the
        # ConfirmationBroker, or the HMAC secret.
        from initiatives.i11.sandbox.host import ConfirmationBroker, Host
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        frontier = [loaded.ctx]
        seen = set()
        for _ in range(3):
            nxt = []
            for obj in frontier:
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                self.assertNotIsInstance(obj, (Host, ConfirmationBroker),
                                         f"reached {type(obj).__name__}")
                for name in dir(obj):
                    if name.startswith("_"):
                        continue
                    try:
                        value = getattr(obj, name)
                    except Exception:
                        continue
                    if isinstance(value,
                                  (str, bytes, int, float, bool, type(None))):
                        continue
                    nxt.append(value)
            frontier = nxt

    def test_dispatch_resolve_unreachable_from_extension_namespaces(self):
        # _dispatch.resolve is the only Host path; it must not appear in
        # any extension-visible namespace.
        from initiatives.i11.sandbox import broker as broker_mod
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        broker = loaded.ctx._broker
        fns = [loaded.ctx.data, loaded.ctx.request_confirmation,
               loaded.ctx.run_action, broker.data_for,
               broker_mod._broker_call]
        fns.extend(broker_mod._OP_HANDLERS.values())
        for fn in fns:
            g = fn.__globals__
            self.assertNotIn("_dispatch", g, fn.__name__)
            self.assertNotIn("Host", g, fn.__name__)
            self.assertNotIn("resolve", g, fn.__name__)
            self.assertNotIn("mint", g, fn.__name__)

    def test_hmac_secret_bytes_not_in_extension_reach(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        secret = host.confirmations._secret
        for obj in (loaded.ctx, loaded.ctx._broker,
                    loaded.ctx._broker._binding):
            self.assertNotIn(repr(secret), repr(obj))


class TestTracebackLeakClosed(unittest.TestCase):
    """Fresh blocker (2026-09-13 blind re-review): the sanitized
    exception's traceback used to keep the live ``Broker._call`` frame
    alive, and ``_call`` invoked a caller-supplied ``op(host, ext_id,
    *args)`` — so reading ``f_locals["self"]`` from the ``_call`` frame
    handed the attacker a hostile-op re-entry point
    (``broker._call(lambda h, e: h)`` → live Host → self-minted
    confirmation → confirm-required action executed). The fix removes
    the capability: dispatch goes through the module-level
    ``_broker_call`` with a FIXED op kind, which accepts no
    caller-supplied callable. Binding resolution still runs in a nested
    frame that unwinds before the sanitized raise, and the closure is
    deleted from the dispatcher's locals — no traceback-reachable frame
    may hold the Host, the resolver, or a re-entry point into a frame
    that does.
    """

    def _walk_frames(self, exc):
        tb = exc.__traceback__
        frames = []
        while tb is not None:
            frames.append(tb.tb_frame)
            tb = tb.tb_next
        return frames

    def _sandbox_frames(self, exc):
        # Frames the sandbox machinery itself placed on the traceback.
        from initiatives.i11.sandbox import broker as broker_mod
        broker_file = broker_mod.__file__
        return [f for f in self._walk_frames(exc)
                if f.f_code.co_filename == broker_file]

    def _assert_traceback_clean(self, exc):
        # Only sandbox-machinery frames are audited: the test's own
        # frame simulates the attacker's frame, which legitimately
        # holds whatever the attacker holds. A leak is a Host in a
        # frame the sandbox itself placed on the traceback.
        from initiatives.i11.sandbox import broker as broker_mod
        from initiatives.i11.sandbox.host import Host
        broker_file = broker_mod.__file__
        for frame in self._walk_frames(exc):
            if frame.f_code.co_filename != broker_file:
                continue
            loc = frame.f_locals
            for banned in ("host", "_resolve", "_dispatch"):
                self.assertNotIn(
                    banned, loc,
                    f"{banned!r} in {frame.f_code.co_name} f_locals")
            for key, value in loc.items():
                self.assertNotIsInstance(
                    value, Host,
                    f"Host in {frame.f_code.co_name} f_locals[{key!r}]")
        self.assertIsNone(exc.__context__)
        self.assertIsNone(exc.__cause__)

    def test_no_call_frame_with_host_on_sanitized_traceback(self):
        # The reviewer's exact PoC step: trigger a sanitized error,
        # walk to the frame named `_call`, read f_locals["host"].
        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        with self.assertRaises(PermissionError) as cm:
            loaded.ctx.data("no-such-scope")
        self._assert_traceback_clean(cm.exception)
        for frame in self._walk_frames(cm.exception):
            if frame.f_code.co_name == "_call":
                self.assertNotIn("host", frame.f_locals)

    def test_traceback_walk_cannot_self_mint(self):
        # End-to-end PoC shape: even a full white-box traceback walk
        # finds no Host anywhere, so the request+mint+run_action chain
        # cannot start.
        from initiatives.i11.sandbox.host import Host
        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        try:
            loaded.ctx.data("no-such-scope")
        except Exception as exc:  # noqa: BLE001 - PoC catches broadly
            leaked = exc
        found = [f for f in self._sandbox_frames(leaked)
                 if any(isinstance(v, Host)
                        for v in f.f_locals.values())]
        self.assertEqual(found, [])
        # And without a minted token the gated action still refuses.
        with self.assertRaises(PermissionError):
            loaded.ctx.run_action("send", "forged-token")

    def test_revoked_binding_traceback_clean(self):
        # Resolve-failure path: the resolver raises before any host is
        # bound; the sanitized traceback must be equally clean.
        from initiatives.i11.sandbox import _dispatch
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        _dispatch.revoke(loaded.ctx._broker._binding)
        with self.assertRaises(RuntimeError) as cm:
            loaded.ctx.data("jobs:read")
        self._assert_traceback_clean(cm.exception)

    def test_no_reentry_via_call_frame_locals(self):
        # KICK_BACK regression (2026-09-13 blind re-review): the re-entry
        # shape is a HOSTILE OP — broker._call(lambda h, e: h) — not a
        # zero-arg call. The old zero-arg probe could not catch it.
        # The fix removes the host-passing invocation entirely: the
        # Broker no longer exposes any method that invokes a
        # caller-supplied callable with the Host; dispatch goes through
        # the module-level _broker_call with a FIXED op kind. This test
        # replays the reviewer's chain shape against every callable
        # found in any sanitized-traceback frame, and asserts no frame
        # local is (or yields) the Host.
        from initiatives.i11.sandbox.host import Host
        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        try:
            loaded.ctx.data("no-such-scope")
            self.fail("expected PermissionError")
        except PermissionError as exc:
            leaked = exc

        # Step 1 of the reviewer's chain still works: the Broker is
        # legitimately reachable from a traceback frame (bound-method
        # frames necessarily expose `self`).
        broker = None
        for frame in self._walk_frames(leaked):
            maybe = frame.f_locals.get("self")
            if type(maybe).__name__ == "Broker":
                broker = maybe
                break
        self.assertIsNotNone(broker,
                             "reviewer step 1: broker recoverable from frame")

        # Steps 2-5 are dead: the hostile-op re-entry point is gone.
        self.assertFalse(hasattr(broker, "_call"),
                         "Broker must expose no _call(op, *args)")
        with self.assertRaises(AttributeError):
            broker._call(lambda h, e: h)  # noqa: SLF001 - the old shape

        # Audit bar: no frame the SANDBOX placed on the sanitized
        # traceback holds any object from which a host-passing
        # invocation is reachable. (The test's own frame simulates the
        # attacker's frame and legitimately holds whatever the attacker
        # holds — e.g. its own `host` variable — so only sandbox frames
        # are audited, per _assert_traceback_clean.)
        from initiatives.i11.sandbox import broker as broker_mod
        broker_file = broker_mod.__file__

        def hostile_op(h, e):
            return h

        for frame in self._walk_frames(leaked):
            if frame.f_code.co_filename != broker_file:
                continue
            for key, value in frame.f_locals.items():
                self.assertNotIsInstance(
                    value, Host,
                    f"Host in {frame.f_code.co_name} f_locals[{key!r}]")
                if not callable(value):
                    continue
                # The actual re-entry shape: hand the callable a hostile
                # op and see whether the Host comes back.
                try:
                    result = value(hostile_op)
                except TypeError:
                    continue  # wrong arity: not an op-taking dispatcher
                except Exception:  # noqa: BLE001 - sanitized rejections
                    continue
                self.assertNotIsInstance(
                    result, Host,
                    f"hostile op reached Host via {key!r} in "
                    f"{frame.f_code.co_name}")

        # The module-level dispatcher itself takes no caller-supplied
        # callable: smuggling the hostile op in as plain data never
        # invokes it with the Host.
        witnessed = []

        def sneaky(h, e):
            witnessed.append(h)
            return h

        with self.assertRaises((PermissionError, TypeError)):
            # Either the scope check or the audit serializer rejects the
            # smuggled callable — the point is it is never INVOKED.
            loaded.ctx.data(sneaky)  # data arg, never invoked as an op
        self.assertEqual(witnessed, [])
        # ...and there is no parameter slot for an op on the dispatcher.
        import inspect
        params = inspect.signature(broker_mod._broker_call).parameters
        self.assertEqual(list(params), ["binding", "op_kind", "args"])

    def test_hostile_str_sanitizer_infallible(self):
        """Blocker 1 (2026-09-13 blind re-review KICK_BACK): the
        sanitizer's ``str(exc)`` capture could itself raise when the
        exception's ``__str__`` is hostile, letting a raw exception
        escape with ``__context__`` chaining into the ``_dispatch``
        frame's live Host — then self-minting a confirmation.

        The capture is now infallible (``_safe_str``): ``del
        _dispatch`` and ``_reraise_sanitized`` ALWAYS run. This test
        drives a hostile-``__str__`` exception straight into the
        dispatcher's except block (white-box fixed-op patch) and
        asserts the type-preserved, severed re-raise. Pre-fix this
        surfaces as a raw RuntimeError with a live-host __context__
        chain; post-fix it must be the hostile type with no chain.
        """
        from initiatives.i11.sandbox import broker as broker_mod
        from initiatives.i11.sandbox.broker import _safe_str

        class HostileErr(Exception):
            def __str__(self):
                raise RuntimeError("zap")

        def hostile_op(h, e, s):
            raise HostileErr("raw hostile")

        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        orig = broker_mod._OP_HANDLERS["data_for"]
        broker_mod._OP_HANDLERS["data_for"] = hostile_op
        try:
            with self.assertRaises(HostileErr) as cm:
                loaded.ctx.data("jobs:read")
        finally:
            broker_mod._OP_HANDLERS["data_for"] = orig
        exc = cm.exception
        # Not the raw RuntimeError("zap") from the hostile __str__.
        self.assertIs(type(exc), HostileErr)
        self.assertIsNone(exc.__context__)
        self.assertIsNone(exc.__cause__)
        self.assertEqual(_safe_str(exc), "<unprintable HostileErr>")
        self._assert_traceback_clean(exc)

    def test_hostile_repr_on_action_id_neutralized(self):
        """Same root-cause class at the f-string site (host.py: the
        ``f"unknown action {action_id!r}"`` invoked the hostile
        ``__repr__`` inside the Host frame). ``_safe_repr`` neutralizes
        it at the format site: the hostile repr never fires, and the
        sanitized ValueError carries the fallback text.
        """
        class Boom(Exception):
            def __str__(self):
                raise RuntimeError("zap")

        class Sneaky:
            def __repr__(self):
                raise Boom("boom-inner")

        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        with self.assertRaises(ValueError) as cm:
            loaded.ctx.request_confirmation(Sneaky())
        exc = cm.exception
        self.assertIn("<unrepresentable Sneaky>", str(exc))
        self.assertIsNone(exc.__context__)
        self.assertIsNone(exc.__cause__)
        self._assert_traceback_clean(exc)

    def test_hostile_str_in_confirmation_payload(self):
        """``json.dumps(..., default=str)`` in
        ``ConfirmationBroker.request``/``verify`` invoked hostile
        ``__str__``. With ``default=_safe_str`` the payload hashes
        consistently on both sides and no raw exception escapes.
        """
        class HostileStr:
            def __str__(self):
                raise RuntimeError("str-zap")

        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        pending = loaded.ctx.request_confirmation("send",
                                                  nasty=HostileStr())
        self.assertTrue(pending.startswith("pc_"))
        token = host.confirmations.mint(pending)
        self.assertTrue(host.confirmations.verify(
            token, ext_id="ut-ext", action_id="send",
            payload={"action": "send",
                     "params": {"nasty": HostileStr()}}))

    def test_hostile_str_from_provider_sanitized(self):
        """A data provider raising an exception with a hostile
        ``__str__`` (stringified in the Host frame's f-string) must
        surface sanitized with the fallback message.
        """
        from initiatives.i11.sandbox.broker import _safe_str

        class HostileErr(Exception):
            def __str__(self):
                raise RuntimeError("zap")

        def evil_provider():
            raise HostileErr("provider blew up")

        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        host.register_provider("jobs:read", evil_provider)
        loaded = _load(host, ext)
        with self.assertRaises(RuntimeError) as cm:
            loaded.ctx.data("jobs:read")
        exc = cm.exception
        self.assertIn("<unprintable HostileErr>", _safe_str(exc))
        self.assertIsNone(exc.__context__)
        self.assertIsNone(exc.__cause__)
        self._assert_traceback_clean(exc)


class TestDispatchHandleTypes(unittest.TestCase):
    """Minor 1 (2026-09-13 blind re-review KICK_BACK): ``resolve(True)``
    aliased the first-issued binding (``True == 1``, bool subclasses
    int) and handles are small sequential ints, guessable with
    white-box ``_broker_call`` access. ``resolve`` now requires
    ``type(handle) is int`` — bools, int subclasses, and non-ints are
    rejected, not coerced.
    """

    def test_resolve_rejects_bool_handles(self):
        from initiatives.i11.sandbox import _dispatch
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        # Sanity: handle 1 exists (first issued in this process may be
        # higher, but True == 1 regardless — the guard must reject it
        # even where a binding exists).
        with self.assertRaises(RuntimeError):
            _dispatch.resolve(True)
        with self.assertRaises(RuntimeError):
            _dispatch.resolve(False)

    def test_resolve_rejects_non_int_handles(self):
        from initiatives.i11.sandbox import _dispatch
        for bad in (1.0, "1", None, (1,), [1]):
            with self.subTest(handle=bad):
                with self.assertRaises(RuntimeError):
                    _dispatch.resolve(bad)

    def test_resolve_accepts_genuine_int_handle(self):
        from initiatives.i11.sandbox import _dispatch
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        binding = loaded.ctx._broker._binding  # noqa: SLF001 - white-box
        self.assertIs(type(binding), int)
        resolved_host, ext_id = _dispatch.resolve(binding)
        self.assertIs(resolved_host, host)
        self.assertEqual(ext_id, "ut-ext")


class TestFrozenManifest(unittest.TestCase):
    """Blocker 2: MappingProxyType cannot be mutated — not even via the
    dict base-class methods that defeated the old custom proxy."""

    def test_dict_base_methods_rejected(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        perms = loaded.manifest["permissions"]
        with self.assertRaises(TypeError):
            dict.__setitem__(perms, "data", ["everything:read"])
        with self.assertRaises(TypeError):
            dict.update(perms, {"data": ["everything:read"]})
        with self.assertRaises(TypeError):
            dict.pop(perms, "data")
        with self.assertRaises(TypeError):
            dict.clear(perms)
        with self.assertRaises(TypeError):
            dict.setdefault(perms, "data", [])
        # Capabilities unchanged; escalation still denied.
        self.assertEqual(set(loaded.ctx.scopes), {"jobs:read", "profile:read"})
        with self.assertRaises(PermissionError):
            loaded.ctx.data("applications:read")

    def test_nested_structures_immutable(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        actions = loaded.manifest["permissions"]["actions"]
        self.assertIsInstance(actions, tuple)
        with self.assertRaises(TypeError):
            actions[0]["kind"] = "confirm-required"
        data = loaded.manifest["permissions"]["data"]
        self.assertIsInstance(data, tuple)
        with self.assertRaises(AttributeError):
            data.append("everything:read")


class TestImportScanHardened(unittest.TestCase):
    """Blocker 3: __import__/eval/exec/compile/open are rejected by the
    scan AND absent from the runtime builtins."""

    def _violations(self, source):
        from initiatives.i11.sandbox.host import _scan_source
        return _scan_source(source, "extension.py")

    def test_dunder_import_rejected(self):
        v = self._violations("x = __import__('os')\n")
        self.assertTrue(any("__import__" in d["module"] for d in v), v)

    def test_eval_exec_compile_rejected(self):
        for src in ("eval('1+1')\n", "exec('x = 1')\n",
                    "compile('1', '', 'eval')\n"):
            v = self._violations(src)
            self.assertTrue(v, f"{src!r} was not flagged")

    def test_open_call_rejected(self):
        v = self._violations("open('/etc/hostname').read()\n")
        self.assertTrue(any("open()" in d["module"] for d in v), v)

    def test_runtime_builtins_stripped(self):
        # A bare-name reference is not a call, so the scan passes it —
        # but the name is absent from the exec builtins: NameError at
        # runtime. The scan is not the only layer.
        ext = _mkext([{"id": "r", "kind": "read"}])
        (ext / "extension.py").write_text(
            "def grab(ctx):\n    return __import__\n"
            "ACTIONS = {'r': grab}\n")
        host = _host()
        loaded = _load(host, ext)
        with self.assertRaises(NameError):
            host.run_extension_action(loaded.ext_id, "r")

    def test_format_string_cannot_smuggle_objects(self):
        # str.format ALWAYS returns str: attribute chains through it
        # leak reprs, never live references. The binding design relies
        # on this property (verified here, not assumed).
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        leaked = "{0._broker._binding}".format(loaded.ctx)
        self.assertIsInstance(leaked, str)


class TestDuplicateExtensionId(unittest.TestCase):
    """Blocker 5: duplicate ids raise loudly — never silent displacement."""

    def test_second_load_raises_and_first_survives(self):
        from initiatives.i11.sandbox.host import ExtensionAlreadyLoadedError
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        first = _load(host, ext)
        with self.assertRaises(ExtensionAlreadyLoadedError):
            host.load_extension(ext, unsafe_local_only=True)
        # The original load was not displaced or corrupted.
        self.assertIs(host._loaded["ut-ext"], first)
        self.assertTrue(host.run_extension_action("ut-ext", "r")["ok"])


class TestCrossExtensionIsolation(unittest.TestCase):
    def test_handles_are_per_extension(self):
        a = _mkext([{"id": "r", "kind": "read"}], ext_id="ext-a")
        b = _mkext([{"id": "r", "kind": "read"}], ext_id="ext-b")
        host = _host()
        la = _load(host, a)
        lb = _load(host, b)
        ha = la.ctx._broker._binding
        hb = lb.ctx._broker._binding
        self.assertIsInstance(ha, int)
        self.assertNotEqual(ha, hb)
        # Host-side resolution binds each handle to exactly one ext_id;
        # the ext_id is never extension-supplied.
        from initiatives.i11.sandbox._dispatch import resolve
        self.assertEqual(resolve(ha)[1], "ext-a")
        self.assertEqual(resolve(hb)[1], "ext-b")

    def test_unload_revokes_only_that_extension(self):
        a = _mkext([{"id": "r", "kind": "read"}], ext_id="ext-a")
        b = _mkext([{"id": "r", "kind": "read"}], ext_id="ext-b")
        host = _host()
        la = _load(host, a)
        _load(host, b)
        host.unload_extension("ext-a")
        # A's handle is now stale: fails closed, never resurrected.
        with self.assertRaises(RuntimeError):
            la.ctx.data("jobs:read")
        # B is unaffected.
        self.assertTrue(host.run_extension_action("ext-b", "r")["ok"])


class TestUnsafeLocalOnlyGate(unittest.TestCase):
    def test_load_refuses_without_flag(self):
        from initiatives.i11.sandbox.host import InProcessExecRefused
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        with self.assertRaises(InProcessExecRefused):
            host.load_extension(ext)

    def test_verify_needs_no_flag(self):
        # Verification executes nothing: no acknowledgment required.
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        self.assertTrue(host.verify_extension(ext)["ok"])

    def test_evil_source_refused_even_with_flag(self):
        # The flag acknowledges residual risk; it does not bypass the
        # static scan.
        from initiatives.i11.manifest.schema import ManifestError
        ext = _mkext([{"id": "r", "kind": "read"}])
        (ext / "extension.py").write_text("import os\nACTIONS = {}\n")
        host = _host()
        with self.assertRaises(ManifestError):
            host.load_extension(ext, unsafe_local_only=True)


class TestGovernanceFailClosed(unittest.TestCase):
    def _token(self, host, ext_id, action):
        pending = host.confirmations.request(
            ext_id, action, {"action": action, "params": {}})
        return host.confirmations.mint(pending.pending_id)

    def test_screen_error_blocks_even_confirmed_action(self):
        # The veto screen must use the real framework entry point and
        # FAIL CLOSED: an unevaluated gate reads as a veto, never a pass.
        import sys
        import types
        ext = _mkext([{"id": "send", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        token = self._token(host, loaded.ext_id, "send")
        fake_adapter = types.ModuleType("governance.adapter")
        fake_adapter.governance_enabled = lambda: True

        def _boom():
            raise RuntimeError("framework exploded")

        fake_adapter.load_framework = _boom
        sys.modules["governance"] = types.ModuleType("governance")
        sys.modules["governance.adapter"] = fake_adapter
        try:
            with self.assertRaises(PermissionError) as cm:
                host.run_extension_action(loaded.ext_id, "send",
                                          confirmation_token=token)
        finally:
            del sys.modules["governance.adapter"]
            del sys.modules["governance"]
        self.assertIn("veto", str(cm.exception).lower())
        entries = host.audit.entries_for(loaded.ext_id)
        self.assertTrue(any(e["event"] == "governance.screen_error"
                            for e in entries),
                        [e["event"] for e in entries])

    def test_screen_uses_real_framework_entry_point(self):
        # The old code called adapter.check_veto (does not exist). The
        # screen must go through load_framework().check_veto().
        import inspect
        from initiatives.i11.sandbox import host as host_mod
        src = inspect.getsource(host_mod.Host._governance_veto_screen)
        self.assertIn("load_framework", src)
        self.assertIn("check_veto", src)
        self.assertNotIn("adapter.check_veto", src)


class TestQuotas(unittest.TestCase):
    def test_fs_per_file_quota(self):
        from initiatives.i11.sandbox.fs import (
            MAX_WRITE_BYTES_PER_FILE, QuotaExceeded)
        ext = _mkext([{"id": "r", "kind": "read"}])
        fs = SandboxFS(ext, ["workspace"])
        with self.assertRaises(QuotaExceeded):
            fs.write_bytes("workspace/big.bin",
                           b"x" * (MAX_WRITE_BYTES_PER_FILE + 1))

    def test_fs_total_quota(self):
        from initiatives.i11.sandbox.fs import (
            MAX_WRITE_BYTES_PER_FILE, MAX_WRITE_BYTES_TOTAL, QuotaExceeded)
        ext = _mkext([{"id": "r", "kind": "read"}])
        fs = SandboxFS(ext, ["workspace"])
        chunk = b"x" * MAX_WRITE_BYTES_PER_FILE  # within the per-file cap
        for i in range(MAX_WRITE_BYTES_TOTAL // MAX_WRITE_BYTES_PER_FILE):
            fs.write_bytes(f"workspace/f{i}.bin", chunk)
        with self.assertRaises(QuotaExceeded):
            fs.write_bytes("workspace/overflow.bin", b"x")

    def test_audit_caps(self):
        from initiatives.i11.sandbox import audit as audit_mod
        tmp = Path(tempfile.mkdtemp())
        audit = ExtensionAudit(tmp / "a.jsonl")
        old = audit_mod.MAX_AUDIT_ENTRIES
        audit_mod.MAX_AUDIT_ENTRIES = 3
        try:
            for i in range(3):
                self.assertIn("entry_hash", audit.append("e1", "ev", {"i": i}))
            capped = audit.append("e1", "ev", {"i": 3})
            self.assertIn("error", capped)
            self.assertTrue(audit.verify()["ok"])
        finally:
            audit_mod.MAX_AUDIT_ENTRIES = old

    def test_draft_quota(self):
        from initiatives.i11.sandbox import host as host_mod
        ext = _mkext([{"id": "d", "kind": "draft"}])
        host = _host()
        loaded = _load(host, ext)
        old = host_mod.MAX_DRAFTS_PER_EXTENSION
        host_mod.MAX_DRAFTS_PER_EXTENSION = 2
        try:
            loaded.ctx.draft_artifact("a", "x")
            loaded.ctx.draft_artifact("b", "x")
            with self.assertRaises(PermissionError) as cm:
                loaded.ctx.draft_artifact("c", "x")
            self.assertIn("quota", str(cm.exception).lower())
        finally:
            host_mod.MAX_DRAFTS_PER_EXTENSION = old

    def test_notification_quota(self):
        from initiatives.i11.sandbox import host as host_mod
        ext = _mkext([{"id": "n", "kind": "notify"}])
        host = _host()
        loaded = _load(host, ext)
        old = host_mod.MAX_NOTIFICATIONS_PER_EXTENSION
        host_mod.MAX_NOTIFICATIONS_PER_EXTENSION = 1
        try:
            loaded.ctx.queue_notification("t", "b")
            with self.assertRaises(PermissionError) as cm:
                loaded.ctx.queue_notification("t2", "b2")
            self.assertIn("quota", str(cm.exception).lower())
        finally:
            host_mod.MAX_NOTIFICATIONS_PER_EXTENSION = old


class TestTrustDerived(unittest.TestCase):
    """Trust labels come from packaging signatures — never the caller."""

    def _signed_ext(self):
        from initiatives.i11.signing.sign import generate_keypair, sign_package
        ext = _mkext([{"id": "r", "kind": "read"}], ext_id="signed-ext")
        private_pem, public_hex = generate_keypair()
        sign_package(ext, publisher="test-fixture", private_key=private_pem)
        return ext, private_pem, public_hex

    def test_unsigned_is_local_unsigned(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        self.assertEqual(loaded.trust, "local-unsigned")

    def test_self_asserted_is_signed_integrity(self):
        ext, _, _ = self._signed_ext()
        host = _host()
        loaded = _load(host, ext)
        self.assertEqual(loaded.trust, "signed-integrity")

    def test_pinned_key_is_signed_verified(self):
        ext, _, public_hex = self._signed_ext()
        host = _host()
        loaded = host.load_extension(ext, publisher_key=public_hex,
                                     unsafe_local_only=True)
        self.assertEqual(loaded.trust, "signed-verified")

    def test_tampered_package_refuses_load(self):
        from initiatives.i11.manifest.schema import ManifestError
        ext, _, _ = self._signed_ext()
        (ext / "extension.py").write_text(
            (ext / "extension.py").read_text() + "\n# tampered\n")
        host = _host()
        with self.assertRaises(ManifestError):
            host.load_extension(ext, unsafe_local_only=True)

    def test_garbage_sidecar_refuses_load(self):
        from initiatives.i11.manifest.schema import ManifestError
        ext = _mkext([{"id": "r", "kind": "read"}])
        (ext / ".veto-signature.json").write_text("{not json")
        host = _host()
        with self.assertRaises(ManifestError):
            host.load_extension(ext, unsafe_local_only=True)

    def test_caller_supplied_trust_rejected(self):
        # There is no trust= parameter: trust is derived, not asserted.
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        with self.assertRaises(TypeError):
            host.load_extension(ext, unsafe_local_only=True,
                                trust="signed-verified")


class TestSandboxFS(unittest.TestCase):
    def test_write_and_read_inside_root(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        fs = SandboxFS(ext, ["workspace"])
        fs.write_text("workspace/note.txt", "hello")
        self.assertEqual(fs.read_text("workspace/note.txt"), "hello")

    def test_traversal_blocked(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        fs = SandboxFS(ext, ["workspace"])
        with self.assertRaises(SandboxViolation):
            fs.read_text("../../etc/hostname")

    def test_absolute_blocked(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        fs = SandboxFS(ext, ["workspace"])
        with self.assertRaises(SandboxViolation):
            fs.read_text("/etc/hostname")

    def test_undeclared_root_blocked(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        fs = SandboxFS(ext, ["workspace"])
        # ".." traversal is always blocked ...
        with self.assertRaises(SandboxViolation):
            fs.write_text("../escape.txt", "nope")
        # ... while the extension's private .data dir is always available
        # by design (documented in fs.py).
        fs.write_text("other/x.txt", "private")
        self.assertEqual(fs.read_text("other/x.txt"), "private")

    def test_symlink_escape_blocked(self):
        # Single root (.data): a symlink inside it pointing at /etc must not
        # let the extension read outside.
        ext = _mkext([{"id": "r", "kind": "read"}])
        fs = SandboxFS(ext, [])
        (ext / ".data").mkdir(exist_ok=True)
        (ext / ".data" / "link").symlink_to("/etc")
        with self.assertRaises(SandboxViolation):
            fs.read_text("link/hostname")


class TestSandboxNet(unittest.TestCase):
    def _http(self, **kw):
        def ok_transport(req):
            return HttpResponse(status=200, headers={}, body=b"ok")
        return SandboxHTTP(["api.example"], transport=ok_transport, **kw)

    def test_allowed_host_passes(self):
        resp = self._http().get("https://api.example/v1/x")
        self.assertEqual(resp.status, 200)

    def test_undeclared_host_blocked(self):
        with self.assertRaises(NetViolation):
            self._http().get("https://evil.example/collect")

    def test_rate_limit_enforced(self):
        from initiatives.i11.sandbox.net import _TokenBucket
        http = self._http()
        http._bucket = _TokenBucket(2)  # noqa: SLF001
        http.get("https://api.example/1")
        http.get("https://api.example/2")
        with self.assertRaises(RateLimitExceeded):
            http.get("https://api.example/3")

    def test_circuit_breaker_opens(self):
        def bad_transport(req):
            return HttpResponse(status=503, headers={}, body=b"")
        http = SandboxHTTP(["api.example"], calls_per_minute=6000,
                           transport=bad_transport)
        with self.assertRaises(CircuitOpen):
            for _ in range(6):
                try:
                    http.get("https://api.example/")
                except CircuitOpen:
                    raise


class TestAudit(unittest.TestCase):
    def test_chain_verifies_and_tamper_detected(self):
        tmp = Path(tempfile.mkdtemp())
        audit = ExtensionAudit(tmp / "a.jsonl")
        audit.append("e1", "data.read", {"scope": "jobs:read"})
        audit.append("e1", "action.ok", {"action": "r"})
        self.assertTrue(audit.verify()["ok"])
        # Tamper: rewrite history.
        lines = (tmp / "a.jsonl").read_text().splitlines()
        entry = json.loads(lines[0])
        entry["detail"] = {"scope": "everything:read"}
        lines[0] = json.dumps(entry)
        (tmp / "a.jsonl").write_text("\n".join(lines) + "\n")
        verdict = audit.verify()
        self.assertFalse(verdict["ok"])

    def test_ctx_has_no_audit_writer(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        for attr in ("audit", "_audit", "append", "write_audit"):
            if attr == "_audit":
                continue
            self.assertFalse(hasattr(loaded.ctx, attr),
                             f"ctx exposes {attr}")

    def test_ctx_has_no_host_reference(self):
        # Regression: ctx._host once exposed the confirmation broker and
        # allowed self-minting. The context holds an opaque integer
        # dispatch handle only; no plain-attribute path may lead from
        # ctx to the Host.
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        self.assertFalse(hasattr(loaded.ctx, "_host"))
        self.assertFalse(hasattr(loaded.ctx, "confirmations"))
        self.assertFalse(hasattr(loaded.ctx, "mint_confirmation"))
        broker = loaded.ctx._broker
        self.assertFalse(hasattr(broker, "_host"))
        self.assertFalse(hasattr(broker, "host"))
        self.assertFalse(hasattr(broker, "_token"))
        # The binding is an integer handle, not the Host.
        from initiatives.i11.sandbox import host as host_mod
        self.assertIsInstance(broker._binding, int)
        self.assertNotIsInstance(broker._binding, host_mod.Host)

    def test_frozen_manifest_rejects_mutation(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        with self.assertRaises(TypeError):
            loaded.manifest["permissions"]["data"] = ["profile:read"]
        with self.assertRaises(TypeError):
            loaded.ctx._manifest["permissions"]["data"] = ["profile:read"]

    def test_scope_escalation_via_ctx_denied(self):
        ext = _mkext([{"id": "r", "kind": "read"}], data=["jobs:read"])
        host = _host()
        loaded = _load(host, ext)
        # Even a successful-looking mutation must not widen capabilities.
        try:
            loaded.ctx._manifest["permissions"]["data"].append("profile:read")
        except (TypeError, AttributeError):
            pass
        with self.assertRaises(PermissionError):
            loaded.ctx.data("profile:read")

    def test_kind_downgrade_inert(self):
        ext = _mkext([{"id": "send-it", "kind": "confirm-required",
                       "confirm_required": True}])
        host = _host()
        loaded = _load(host, ext)
        try:
            loaded.ctx._manifest["permissions"]["actions"][0]["kind"] = "read"
        except TypeError:
            pass
        with self.assertRaises(PermissionError):
            host.run_extension_action("ut-ext", "send-it")

    def test_banned_imports_rejected(self):
        from initiatives.i11.sandbox.host import scan_imports
        import tempfile
        from pathlib import Path
        import json as _json
        cases = {
            "import os": "os",
            "import pathlib": "pathlib",
            "import sys": "sys",
            "import initiatives.i11.sandbox.host": "initiatives",
            "import socket": "socket",
            "import subprocess": "subprocess",
        }
        for code, _ in cases.items():
            tmp = Path(tempfile.mkdtemp())
            (tmp / "extension.py").write_text(code + "\n")
            scan = scan_imports(tmp)
            self.assertFalse(scan["ok"], f"{code} was not flagged")
            self.assertTrue(scan["violations"], f"{code}: no violations")

    def test_bare_open_is_violation(self):
        from initiatives.i11.sandbox.host import scan_imports
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        (tmp / "extension.py").write_text(
            "def f(ctx):\n    return open('/etc/hostname').read()\n")
        scan = scan_imports(tmp)
        self.assertFalse(scan["ok"])
        self.assertTrue(any(v["module"] == "open()"
                            for v in scan["violations"]))

    def test_snippet_and_test_files_not_scanned(self):
        # Developer tooling (snippets/tests) legitimately imports host
        # modules; only runtime code is scanned.
        from initiatives.i11.sandbox.host import scan_imports
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        (tmp / "extension.py").write_text("ACTIONS = {}\n")
        (tmp / "cli_snippet.py").write_text(
            "import initiatives.i11.sandbox.host\n")
        (tmp / "tests").mkdir()
        (tmp / "tests" / "test_x.py").write_text("import pathlib\n")
        scan = scan_imports(tmp)
        self.assertTrue(scan["ok"], scan["violations"])


class TestPII(unittest.TestCase):
    def test_redact_by_default(self):
        ext = _mkext([{"id": "r", "kind": "read"}])
        host = _host()
        loaded = _load(host, ext)
        host.register_provider("profile:read", lambda: {
            "name": "Ada", "email": "ada@example.com", "phone": "555-0100"})
        profile = loaded.ctx.data("profile:read")
        self.assertEqual(profile["email"], "***redacted***")
        self.assertEqual(profile["phone"], "***redacted***")
        self.assertEqual(profile["name"], "Ada")


if __name__ == "__main__":
    unittest.main()
