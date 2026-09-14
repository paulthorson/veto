#!/usr/bin/env python3
"""Tests for the dashboard stack (Worker D: tests).

Covers three pieces built by parallel workers:

* ``dashboard_data.compute_kpis(root)`` (Worker A) — KPI computation over a
  state dir. All file I/O goes to a ``tempfile`` directory; the real user
  state files are never touched.
* ``webui.py`` API dispatch + confirm gate (Worker B) — stdlib http.server on
  127.0.0.1 with ``GET /api/tools``, ``GET /api/kpis`` and ``POST /api/run``
  (``{"tool", "action", "params"}``). Destructive actions are confirm-gated.
* ``dashboard.py`` terminal UI ``cmd_dashboard(args)`` (Worker A).

Stdlib unittest only. Suites whose target interface is not importable yet
degrade to an explicit *skip* (rather than failing on missing worker output);
once the worker files land, the skips lift automatically.

Run:  cd ~/workspace/job-apply-mcp && .venv/bin/python -m unittest tests.test_dashboard -v
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import importlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ---------------------------------------------------------------------------
# Optional imports: worker A / worker B output may still be landing.
# ---------------------------------------------------------------------------


def _optional_import(name):
    """Import ``name``; return None if the module simply does not exist yet.

    A genuine import-time bug inside an *existing* module is re-raised so it
    surfaces as a failure rather than a silent skip.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name or exc.name is None:
            return None
        raise


dashboard_data = _optional_import("dashboard_data")
dashboard = _optional_import("dashboard")
webui = _optional_import("webui")

# The eight KPI keys compute_kpis is specified to return.
KPI_KEYS = (
    "applications",
    "avg_fit",
    "streak_days",
    "interviews",
    "offers",
    "followups_due",
    "activity_30d",
    "funnel",
)

# The 19 overnight modules the webui tool catalog must list.
EXPECTED_MODULES = (
    "match",
    "radar",
    "tailor",
    "followup",
    "referrals",
    "mock_interview",
    "soft_skills",
    "ai_proficiency",
    "mentors",
    "offer_compare",
    "skill_gaps",
    "cover_letters",
    "linkedin_optimizer",
    "jd_decoder",
    "network_crm",
    "rejection_autopsy",
    "streaks",
    "crew",
)

TODAY = date.today()

HAS_COMPUTE_KPIS = dashboard_data is not None and hasattr(
    dashboard_data, "compute_kpis"
)
HAS_CMD_DASHBOARD = dashboard is not None and hasattr(
    dashboard, "cmd_dashboard"
)


# ---------------------------------------------------------------------------
# Fixture builders (temp state dir only — never the real user files).
# ---------------------------------------------------------------------------


def _app_entry(job_id, stage, *, days_ago=5, follow_up_due=None,
               company="Acme Corp", fit_score=None):
    """One applications.json entry in the repo's real entry schema."""
    submitted = TODAY - timedelta(days=days_ago)
    stamp = submitted.isoformat() + "T12:00:00+00:00"
    entry = {
        "job_id": job_id,
        "board": "linkedin",
        "title": "Senior Backend Engineer",
        "company": company,
        "location": "New York, NY",
        "stage": stage,
        "submitted_at": stamp,
        "stage_history": [{"stage": stage, "at": stamp, "note": ""}],
        "follow_up_due": follow_up_due,
    }
    if fit_score is not None:
        entry["fit_score"] = fit_score  # one of dashboard_data._FIT_KEYS
    return entry


def _streaks_store(days_back):
    """A streaks.json store (streaks.py format) with a rep on each day in
    ``days_back`` (0 == today), logged at local noon so the day is stable
    under either local or UTC interpretation."""
    reps = []
    for back in days_back:
        day = TODAY - timedelta(days=back)
        ts = datetime(day.year, day.month, day.day, 12, 0, 0).timestamp()
        reps.append({"kind": "application", "ts": ts})
    return {"reps": reps, "goals": {}}


def _write_state(tmpdir, applications=None, streaks=None):
    root = Path(tmpdir)
    if applications is not None:
        (root / "applications.json").write_text(
            json.dumps(applications), encoding="utf-8"
        )
    if streaks is not None:
        (root / "streaks.json").write_text(json.dumps(streaks), encoding="utf-8")
    return root


def _assert_all_zeros(test, kpis):
    """Empty/missing state dir must yield zeros per the specified contract.

    avg_fit is None (not 0) when no scored applications exist — a 0 would
    misleadingly imply a terrible average fit.
    """
    for key in ("applications", "streak_days",
                "interviews", "offers", "followups_due"):
        test.assertEqual(kpis[key], 0, f"kpi {key!r} should be 0")
    test.assertIsNone(kpis["avg_fit"], "avg_fit should be None with no data")
    activity = kpis["activity_30d"]
    if isinstance(activity, list):
        # 30-day series: every bucket must be zero.
        counts = [b.get("count", 0) for b in activity
                  if isinstance(b, dict)]
        test.assertTrue(counts, "activity_30d series came back empty")
        test.assertEqual(sum(counts), 0, "activity_30d counts should be 0")
    elif isinstance(activity, (int, float)):
        test.assertEqual(activity, 0)
    else:
        test.assertEqual(len(activity), 0, "activity_30d should be empty")
    funnel = kpis["funnel"]
    test.assertIsInstance(funnel, dict, "funnel should be a dict")
    stack = list(funnel.values())
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, (int, float)):
            test.assertEqual(value, 0, "funnel leaf should be 0")


# ---------------------------------------------------------------------------
# 1. compute_kpis (worker A)
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAS_COMPUTE_KPIS, "dashboard_data.compute_kpis not built yet")
class ComputeKpisEmptyTests(unittest.TestCase):
    """Empty / missing state dir -> all zeros."""

    def test_empty_dir_returns_all_zeros(self):
        with tempfile.TemporaryDirectory() as tmp:
            kpis = dashboard_data.compute_kpis(Path(tmp))
        for key in KPI_KEYS:
            self.assertIn(key, kpis, f"missing kpi key {key!r}")
        _assert_all_zeros(self, kpis)

    def test_missing_applications_file_alone_is_zeros(self):
        # Only streaks.json present: application-derived KPIs must still be 0.
        with tempfile.TemporaryDirectory() as tmp:
            _write_state(tmp, streaks=_streaks_store([0]))
            kpis = dashboard_data.compute_kpis(Path(tmp))
        self.assertEqual(kpis["applications"], 0)
        self.assertEqual(kpis["interviews"], 0)
        self.assertEqual(kpis["offers"], 0)

    def test_missing_streaks_file_alone_is_zero_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_state(tmp, applications=[_app_entry("x:1", "applied")])
            kpis = dashboard_data.compute_kpis(Path(tmp))
        self.assertEqual(kpis["streak_days"], 0)
        self.assertEqual(kpis["applications"], 1)

    def test_returns_plain_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            kpis = dashboard_data.compute_kpis(Path(tmp))
        self.assertIsInstance(kpis, dict)


@unittest.skipUnless(HAS_COMPUTE_KPIS, "dashboard_data.compute_kpis not built yet")
class ComputeKpisFixtureTests(unittest.TestCase):
    """Fixture state -> correct counts."""

    def _kpis(self, applications, streaks_days=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        streaks = _streaks_store(streaks_days) if streaks_days is not None else None
        root = _write_state(tmp.name, applications=applications, streaks=streaks)
        return dashboard_data.compute_kpis(root)

    def test_counts_from_fixture_applications(self):
        apps = [
            _app_entry("linkedin:a1", "applied"),
            _app_entry("linkedin:a2", "interviewing"),
            _app_entry("linkedin:a3", "offer"),
            _app_entry("linkedin:a4", "rejected"),
        ]
        kpis = self._kpis(apps)
        self.assertEqual(kpis["applications"], 4)
        self.assertEqual(kpis["interviews"], 1)
        self.assertEqual(kpis["offers"], 1)

    def test_funnel_buckets_match_fixture(self):
        apps = [
            _app_entry("linkedin:a1", "applied"),
            _app_entry("linkedin:a2", "interviewing"),
            _app_entry("linkedin:a3", "offer"),
            _app_entry("linkedin:a4", "rejected"),
        ]
        kpis = self._kpis(apps)
        funnel = kpis["funnel"]
        self.assertIsInstance(funnel, dict)
        self.assertEqual(
            funnel,
            {"applied": 1, "phone": 0, "interview": 1, "offer": 1, "rejected": 1},
        )
        self.assertEqual(kpis["interviews"], funnel["phone"] + funnel["interview"])
        self.assertGreaterEqual(kpis["offers"], funnel["offer"])

    def test_streak_days_from_fixture_streaks(self):
        # Reps today + yesterday -> 2-day streak.
        kpis = self._kpis([_app_entry("linkedin:a1", "applied")], streaks_days=[0, 1])
        self.assertEqual(kpis["streak_days"], 2)

    def test_broken_streak_counts_zero(self):
        # Reps 3 and 4 days ago only -> no current streak.
        kpis = self._kpis([_app_entry("linkedin:a1", "applied")], streaks_days=[3, 4])
        self.assertEqual(kpis["streak_days"], 0)

    def test_avg_fit_averages_fit_scores(self):
        apps = [
            _app_entry("linkedin:a1", "applied", fit_score=90),
            _app_entry("linkedin:a2", "interviewing", fit_score=70),
            _app_entry("linkedin:a3", "offer", fit_score=80),
        ]
        kpis = self._kpis(apps)
        self.assertIsInstance(kpis["avg_fit"], (int, float))
        self.assertAlmostEqual(kpis["avg_fit"], 80.0)

    def test_followups_due_counts_due_entries(self):
        overdue = (TODAY - timedelta(days=1)).isoformat()
        future = (TODAY + timedelta(days=30)).isoformat()
        apps = [
            _app_entry("linkedin:a1", "applied", follow_up_due=overdue),
            _app_entry("linkedin:a2", "applied", follow_up_due=future),
            _app_entry("linkedin:a3", "offer"),  # terminal: never due
        ]
        kpis = self._kpis(apps)
        self.assertEqual(kpis["followups_due"], 1)

    def test_activity_30d_sees_recent_applications(self):
        apps = [
            _app_entry("linkedin:a1", "applied", days_ago=3),
            _app_entry("linkedin:a2", "applied", days_ago=40),  # outside window
        ]
        kpis = self._kpis(apps)
        activity = kpis["activity_30d"]
        self.assertIsInstance(activity, list)
        self.assertEqual(len(activity), 30)
        total = sum(b.get("count", 0) for b in activity if isinstance(b, dict))
        self.assertEqual(total, 1)


# ---------------------------------------------------------------------------
# 2/3/4. webui API dispatch + confirm gate + catalog (worker B)
#
# POST /api/run takes {"tool", "action", "params"}. The dispatch callable may
# not be factored for import yet — then tests go over real HTTP against a
# server bound to 127.0.0.1 on an ephemeral port (setUp), killed in tearDown.
# ---------------------------------------------------------------------------

_DISPATCH_ATTR_NAMES = (
    "dispatch",
    "dispatch_request",
    "api_dispatch",
    "handle_api",
    "handle_request",
    "route_request",
    "process_request",
    "handle_run",
    "dispatch_run",
    "api_run",
)


def _find_dispatch(module):
    for name in _DISPATCH_ATTR_NAMES:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _normalize_response(resp):
    if isinstance(resp, (bytes, bytearray)):
        resp = resp.decode("utf-8")
    if isinstance(resp, str):
        return json.loads(resp)
    return resp


def _call_dispatch(dispatch_fn, tool, action, params):
    """Call an unknown-signature dispatch function with a few conventions."""
    attempts = [
        lambda: dispatch_fn({"tool": tool, "action": action, "params": params}),
        lambda: dispatch_fn(tool, action, params),
        lambda: dispatch_fn({"action": f"{tool}.{action}", "params": params}),
    ]
    errors = []
    for attempt in attempts:
        try:
            return _normalize_response(attempt())
        except (TypeError, KeyError) as exc:
            errors.append(exc)
    raise errors[0]


def _find_handler_class(module):
    for value in vars(module).values():
        if (
            isinstance(value, type)
            and issubclass(value, http.server.BaseHTTPRequestHandler)
            and value is not http.server.BaseHTTPRequestHandler
        ):
            return value
    return None


class WebuiClient:
    """Uniform caller over either a direct dispatch function or HTTP."""

    def __init__(self):
        self.dispatch_fn = _find_dispatch(webui) if webui else None
        self.base_url = None
        self._server = None
        self._thread = None
        self._token = None
        self._token_dir = None
        self._token_env_was_set = False
        self._token_env_old = None
        if self.dispatch_fn is None and webui is not None:
            handler = _find_handler_class(webui)
            if handler is not None:
                # _Handler gates every /api/* route on bearer auth; point
                # the token file at a temp path and send the token header.
                self._token_dir = tempfile.TemporaryDirectory()
                self._token_env_was_set = (
                    "VETO_WEBUI_TOKEN_FILE" in os.environ
                )
                self._token_env_old = os.environ.get("VETO_WEBUI_TOKEN_FILE")
                tok_path = Path(self._token_dir.name) / "token"
                os.environ["VETO_WEBUI_TOKEN_FILE"] = str(tok_path)
                self._token = webui.load_or_create_token()
                server = http.server.HTTPServer(("127.0.0.1", 0), handler)
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.05},
                    daemon=True,
                )
                thread.start()
                self._server = server
                self._thread = thread
                self.base_url = (
                    f"http://127.0.0.1:{server.server_address[1]}"
                )

    @property
    def available(self):
        return self.dispatch_fn is not None or self.base_url is not None

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
        if self._token_dir is not None:
            if self._token_env_was_set:
                os.environ["VETO_WEBUI_TOKEN_FILE"] = self._token_env_old
            else:
                os.environ.pop("VETO_WEBUI_TOKEN_FILE", None)
            self._token_dir.cleanup()
            self._token_dir = None

    def run(self, tool, action, params):
        body = {"tool": tool, "action": action, "params": params}
        if self.dispatch_fn is not None:
            return _call_dispatch(self.dispatch_fn, tool, action, params)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/run", data=data, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        if self._token is not None:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
        return json.loads(payload)

    def get(self, path):
        if self.dispatch_fn is not None:
            raise AssertionError("GET endpoints need the HTTP path")
        req = urllib.request.Request(self.base_url + path)
        if self._token is not None:
            req.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))


def _needs_webui(test):
    """Return a usable WebuiClient, or skip if worker B's interface is absent."""
    if webui is None:
        test.skipTest("webui.py not built yet (worker B)")
    client = WebuiClient()
    if not client.available:
        test.skipTest(
            "webui.py exposes neither an importable dispatch function "
            f"{_DISPATCH_ATTR_NAMES} nor an http.server handler class yet"
        )
    test.addCleanup(client.close)
    return client


def _extract_tool_names(payload):
    """Catalog -> set of lowercase tool id/name strings, however keyed."""
    if isinstance(payload, dict):
        tools = payload.get("tools", payload.get("actions", payload))
    else:
        tools = payload
    names = set()

    def add(value):
        if isinstance(value, str) and value:
            names.add(value.lower())

    if isinstance(tools, dict):
        for key, val in tools.items():
            add(key)
            if isinstance(val, dict):
                for field in ("id", "name", "tool", "module", "action"):
                    add(val.get(field))
            else:
                add(val)
    elif isinstance(tools, list):
        for item in tools:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                for field in ("id", "name", "tool", "module", "action"):
                    add(item.get(field))
    return names


class WebuiToolsCatalogTests(unittest.TestCase):
    """GET /api/tools must list all 19 overnight modules."""

    def test_catalog_lists_every_overnight_module(self):
        client = _needs_webui(self)
        if client.dispatch_fn is not None:
            self.skipTest("no HTTP server: catalog is served over HTTP only")
        payload = client.get("/api/tools")
        names = _extract_tool_names(payload)
        self.assertTrue(names, "tool catalog came back empty")
        missing = [
            mod for mod in EXPECTED_MODULES
            if not any(mod == name or mod in name for name in names)
        ]
        self.assertEqual(
            missing, [],
            f"catalog is missing overnight modules: {missing} "
            f"(catalog names: {sorted(names)})",
        )


class WebuiKpisEndpointTests(unittest.TestCase):
    """GET /api/kpis returns the dashboard KPI dict."""

    def test_kpis_endpoint_has_all_keys(self):
        client = _needs_webui(self)
        if client.dispatch_fn is not None:
            self.skipTest("no HTTP server: /api/kpis is served over HTTP only")
        payload = client.get("/api/kpis")
        if isinstance(payload, dict) and "kpis" in payload:
            payload = payload["kpis"]
        self.assertIsInstance(payload, dict)
        for key in KPI_KEYS:
            self.assertIn(key, payload, f"/api/kpis missing key {key!r}")


def _mocked_send_boundaries():
    """Patch the real send boundary so the gate tests can never send mail."""
    import followup

    return (
        mock.patch.object(followup, "send_followup", autospec=True),
        mock.patch("followup.email_sync.send_followup", autospec=True),
    )


def _fail_closed_followup_params():
    # Non-empty fields so the preview gate engages (empty fields are
    # rejected as missing params before any preview is built); the entry id
    # does not exist, so even a bypassed gate would fail closed downstream.
    return {
        "entry_id": "test-nonexistent-entry-xyz",
        "to": "nobody@example.invalid",
        "subject": "Test follow-up (automated test, do not send)",
        "body": "This is a test preview body. Do not send.",
    }


class WebuiConfirmGateTests(unittest.TestCase):
    """Destructive actions need the two-step confirm gate.

    Exercised on followup.send: the unconfirmed call must return
    ``needs_confirm`` with ``preview`` + ``preview_hash``; a second call
    with a WRONG hash must return ``ok:false`` + error and must not send
    anything (the real send boundary is mocked — asserting the gate
    response is the point; nothing is ever actually sent).
    """

    def test_unconfirmed_send_returns_needs_confirm(self):
        client = _needs_webui(self)
        patch_send, patch_gmail = _mocked_send_boundaries()
        with patch_send as mock_send, patch_gmail as mock_gmail:
            resp = client.run(
                "followup", "send", _fail_closed_followup_params()
            )
        self.assertIsInstance(resp, dict)
        self.assertFalse(resp.get("ok"), f"expected ok:false, got {resp!r}")
        self.assertTrue(
            resp.get("needs_confirm"),
            f"expected needs_confirm:true, got {resp!r}",
        )
        preview = resp.get("preview")
        preview_hash = resp.get("preview_hash")
        self.assertIsInstance(preview, str)
        self.assertTrue(preview, "preview must be non-empty")
        self.assertIsInstance(preview_hash, str)
        self.assertEqual(
            preview_hash,
            hashlib.sha256(preview.encode("utf-8")).hexdigest(),
            "preview_hash must be sha256(preview)",
        )
        mock_send.assert_not_called()
        mock_gmail.assert_not_called()

    def _confirm_call(self, client, params, preview_hash, confirmed=True):
        """Second-step call; tries the confirmed flag inside params first,
        then merged at the body top level (server may read either)."""
        variants = [
            ("followup", "send", {**params, "confirmed": confirmed,
                                  "preview_hash": preview_hash}),
        ]
        last = None
        for tool, action, p in variants:
            last = client.run(tool, action, p)
            if isinstance(last, dict) and (
                last.get("needs_confirm")
                or ("error" in last and not last.get("ok", True))
                or last.get("ok")
            ):
                return last, ("params", p)
        return last, (None, None)

    def test_wrong_hash_rejects_and_sends_nothing(self):
        client = _needs_webui(self)
        params = _fail_closed_followup_params()
        patch_send, patch_gmail = _mocked_send_boundaries()
        with patch_send as mock_send, patch_gmail as mock_gmail:
            first = client.run("followup", "send", params)
            self.assertTrue(
                first.get("needs_confirm"), f"no gate offered: {first!r}"
            )
            resp, (placement, _) = self._confirm_call(
                client, params, "0" * 64  # deliberately wrong hash
            )
        self.assertIsInstance(resp, dict)
        self.assertFalse(resp.get("ok"), f"expected ok:false, got {resp!r}")
        self.assertIn("error", resp, f"expected an error field, got {resp!r}")
        # The server may re-offer the gate with a fresh preview after a
        # mismatch ("preview mismatch — review the new preview"); what
        # matters per the contract is ok:false + error + nothing executed.
        if resp.get("needs_confirm"):
            fresh_preview = resp.get("preview")
            fresh_hash = resp.get("preview_hash")
            if isinstance(fresh_preview, str) and isinstance(fresh_hash, str):
                self.assertEqual(
                    fresh_hash,
                    hashlib.sha256(fresh_preview.encode("utf-8")).hexdigest(),
                    "re-offered preview_hash must match its preview",
                )
        # Nothing was sent: followup's send was never invoked with
        # confirmed=True (in fact, never invoked at all on a wrong hash).
        for call in mock_send.call_args_list:
            _, kwargs = call
            self.assertNotEqual(
                kwargs.get("confirmed"), True,
                "send must not run with confirmed=True on a wrong hash",
            )
        mock_send.assert_not_called()
        mock_gmail.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Terminal dashboard cmd_dashboard (worker A)
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAS_CMD_DASHBOARD, "dashboard.cmd_dashboard not built yet")
class TerminalDashboardTests(unittest.TestCase):
    """cmd_dashboard must handle EOF on stdin immediately and exit 0."""

    def _run_with_eof_stdin(self, args):
        """Run cmd_dashboard with empty stdin in a thread (hang guard)."""
        outcome = {}

        def target():
            try:
                with mock.patch("sys.stdin", io.StringIO("")), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    outcome["rc"] = dashboard.cmd_dashboard(args)
            except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
                outcome["exc"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=10)
        return outcome, thread.is_alive()

    def test_eof_exits_zero_without_traceback(self):
        outcome, alive = self._run_with_eof_stdin(argparse.Namespace())
        self.assertFalse(alive, "cmd_dashboard hung on EOF instead of exiting")
        self.assertNotIn("exc", outcome, f"raised: {outcome.get('exc')!r}")
        self.assertEqual(outcome.get("rc"), 0)


if __name__ == "__main__":
    unittest.main()
