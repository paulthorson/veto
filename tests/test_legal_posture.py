#!/usr/bin/env python3
"""Legal-posture regression contract (spec section 11, legal-hardening commit 12).

These tests encode the project's legal posture: the architecture changes made
by the legal-exposure-reduction program (commits 2-11) that keep Veto from
being the last actor before a third party receives something, and from
operating an authenticated session on a platform whose terms restrict
automated access.

REMOVING OR WEAKENING ONE OF THESE TESTS CHANGES THE PROJECT'S LEGAL
POSTURE. It does not merely change behavior. A failing test in this module
is a legal regression, not a feature regression, and must be treated as
blocking.

Each assertion re-derives the spec section 11 requirement against the current
tree: line numbers are never trusted, the assertions check live behavior and
source facts that must hold wherever the code moves. Where the spec's own
references have drifted, the re-derived target is named in a comment.

Spec section 11 required assertions, covered here:
 1. no module issues a submit POST or clicks a submit control on a
    third-party domain
 2. the User-Agent contains none of: Mozilla, Chrome, Safari, Gecko,
    WebKit, AppleWebKit, KHTML, Firefox, Version
 3. no module calls random.choice on a user-agent list
 4. confirmation refuses when stdin is not a TTY
 5. a hardcoded confirmation string does not satisfy the prompt
 6. robots.txt fetch or parse failure results in no request
 7. no code path constructs or reads a proxy configuration
 8. per-day and per-provider ceilings cannot be raised from config
 9. the servable HTTP default bind is 127.0.0.1 (the spec's uvicorn
    reference is stale: no uvicorn exists in the tree; the only bind
    surface is webui.serve())
10. tests/test_i05_packet.py still passes unchanged
"""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ats_apply  # noqa: E402
import browser_apply  # noqa: E402
import circuit_breaker  # noqa: E402
import compliance  # noqa: E402
import webui  # noqa: E402
from providers import _common as provider_common  # noqa: E402
from providers._common import VETO_USER_AGENT, make_job_id, robots_allows  # noqa: E402

# The final User-Agent string, asserted VERBATIM (spec section 14 item 4).
FINAL_USER_AGENT = "veto/0.1.0 (+https://github.com/paulthorson/veto)"

#: Tokens a browser-impersonating User-Agent would carry; none may appear
#: in Veto's User-Agent (spec section 11).
BANNED_UA_TOKENS = (
    "Mozilla",
    "Chrome",
    "Safari",
    "Gecko",
    "WebKit",
    "AppleWebKit",
    "KHTML",
    "Firefox",
    "Version",
)

_SKIP_DIRS = {
    ".venv",
    "__pycache__",
    ".git",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}


def _project_py_files(include_tests: bool = True) -> list[Path]:
    """Every tracked-project .py file, minus vendored/derived trees."""
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not include_tests and "tests" in path.parts:
            continue
        out.append(path)
    return sorted(out)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class TestNoSubmitPath(unittest.TestCase):
    """Spec section 11: no module issues a submit POST or clicks a submit
    control on a third-party domain."""

    #: Names of the deleted submit machinery. tests/ is excluded from the
    #: scan because posture tests quote these names to assert their absence.
    _DELETED_SUBMIT_NAMES = (
        "find_submit_button",
        "_submit_bound_form",
        "_find_submit_button_with_pattern",
        "_SUBMIT_BOUND_FORM_JS",
        "HTMLFormElement.prototype.submit",
    )

    def test_no_submit_machinery_in_production_code(self):
        for path in _project_py_files(include_tests=False):
            text = _read(path)
            for name in self._DELETED_SUBMIT_NAMES:
                self.assertNotIn(
                    name,
                    text,
                    f"{path.relative_to(REPO_ROOT)} still references "
                    f"deleted submit machinery {name!r}",
                )

    def test_no_bare_requests_post_call_sites(self):
        # Narrow check: greps for the literal ".post(" string in
        # production code. This catches requests-style ``session.post(`` /
        # ``requests.post(`` call sites but NOT urllib-style
        # ``method="POST"`` calls — it is a tripwire for resurrected
        # submit paths, not a general proof that no HTTP POST exists.
        # The ONLY ".post(" text in the tree is a string literal inside an
        # i11 policy-kit attack-sample fixture (not a real HTTP call). Any
        # other occurrence is a resurrected submit path.
        offenders: list[str] = []
        for path in _project_py_files(include_tests=False):
            if path.relative_to(REPO_ROOT).as_posix() == (
                "initiatives/i11/policy_kit/attacks.py"
            ):
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if ".post(" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(offenders, [], f"HTTP POST call sites found: {offenders}")

    def test_ats_apply_is_preview_only(self):
        result = ats_apply.apply_direct(
            "ashby",
            {"id": make_job_id("ashby", "acme:123")},
            {"full_name": "Ada Lovelace", "email": "ada@example.com"},
            confirm=True,
            dry_run=False,
        )
        self.assertEqual(result["mode"], "preview")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["network_calls"], 0)
        self.assertFalse(result["direct_apply_available"])

    def test_browser_apply_has_no_submit_locator(self):
        self.assertFalse(hasattr(browser_apply, "find_submit_button"))
        self.assertFalse(hasattr(browser_apply, "_submit_bound_form"))

    def test_consent_clicks_never_target_submit_or_accept(self):
        # browser_apply still clicks cookie-banner buttons, but ONLY
        # reject/dismiss ones (privacy-safe dismissal). Anything
        # accept-like or submit-like must classify as "leave it alone".
        kind = browser_apply._consent_button_kind
        for text in (
            "submit",
            "apply now",
            "send application",
            "accept",
            "accept all",
            "agree",
            "allow all",
            "ok",
        ):
            self.assertIsNone(
                kind(text), f"consent classifier must not click {text!r}"
            )
        for text in ("reject all", "reject", "decline", "necessary only"):
            self.assertEqual(kind(text), "reject", text)
        for text in ("close", "dismiss"):
            self.assertEqual(kind(text), "dismiss", text)


class TestHonestUserAgent(unittest.TestCase):
    """Spec section 11: the User-Agent is one honest constant, verbatim,
    with none of the banned browser-impersonation tokens."""

    def test_ua_string_is_verbatim(self):
        self.assertEqual(VETO_USER_AGENT, FINAL_USER_AGENT)

    def test_ua_contains_no_banned_tokens(self):
        for token in BANNED_UA_TOKENS:
            self.assertNotIn(
                token,
                VETO_USER_AGENT,
                f"User-Agent must not contain {token!r}",
            )
            self.assertNotIn(
                token.lower(),
                VETO_USER_AGENT.lower(),
                f"User-Agent must not contain {token!r} (case-insensitive)",
            )

    def test_ua_defined_once_and_imported(self):
        # Spec section 2.2: one constant, defined in exactly one module
        # (providers._common) and imported everywhere. server.py keeps a
        # fallback literal ONLY for when the providers package itself is
        # unimportable; it must match the canonical value.
        import server

        self.assertIs(server.VETO_USER_AGENT, VETO_USER_AGENT)
        import briefs

        self.assertIs(briefs.VETO_USER_AGENT, VETO_USER_AGENT)

    def test_every_ua_header_uses_the_constant(self):
        for path in _project_py_files(include_tests=False):
            for i, line in enumerate(_read(path).splitlines(), 1):
                if '"User-Agent"' in line:
                    self.assertIn(
                        "VETO_USER_AGENT",
                        line,
                        f"{path.relative_to(REPO_ROOT)}:{i} sets a "
                        "User-Agent header without the honest constant",
                    )

    def test_no_ua_list_variable_in_code(self):
        # The rotated-UA list variable must not be reintroduced. (Prose in
        # planning docs/ledger entries describing the deletion is allowed;
        # code assignments are not.)
        offenders = []
        for path in _project_py_files():
            if path.name == "test_legal_posture.py":
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if re.match(r"\s*USER_AGENTS\s*=", line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(offenders, [], f"UA list reintroduced: {offenders}")

    def test_no_rotation_comment_in_code(self):
        comment = "Rotated to look like ordinary browser traffic"
        offenders = []
        for path in _project_py_files():
            if path.name == "test_legal_posture.py":
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if comment in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(offenders, [], f"rotation comment present: {offenders}")


class TestNoRandomChoiceOnUAList(unittest.TestCase):
    """Spec section 11: no module calls random.choice on a user-agent list."""

    def test_no_random_choice_anywhere(self):
        # Re-derived: there is no UA list left to choose from, and no
        # random.choice call remains anywhere in project code (the only
        # randomness in request hygiene is random.uniform jitter in
        # polite_delay, which is kept per spec section 2).
        offenders = []
        for path in _project_py_files():
            if path.name == "test_legal_posture.py":
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if "random.choice" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(offenders, [], f"random.choice found: {offenders}")


class TestCircuitBreakers(unittest.TestCase):
    """Spec section 11: confirmation refuses on non-TTY; a hardcoded
    confirmation string does not satisfy the prompt (spec section 6)."""

    def test_non_tty_refuses_and_exits_nonzero(self):
        with mock.patch.object(
            circuit_breaker._registry, "_stdin_is_tty", return_value=False
        ):
            with self.assertRaises(SystemExit) as ctx:
                circuit_breaker.require_action_confirmation(
                    summary="Apply to Acme Corp as Engineer",
                    expected_value="Acme Corp",
                )
        self.assertEqual(
            ctx.exception.code, 2, "non-TTY refusal must exit nonzero"
        )

    def test_hardcoded_confirmation_string_fails(self):
        # The typed answer is compared ONLY against the per-application
        # varying value; fixed strings can never satisfy the prompt.
        for fixed in ("yes", "y", "ok", "send", "confirm"):
            with mock.patch.object(
                circuit_breaker._registry, "_stdin_is_tty", return_value=True
            ), mock.patch.object(
                circuit_breaker._registry, "_approval_input", return_value=fixed
            ):
                result = circuit_breaker.require_action_confirmation(
                    summary="Apply to Acme Corp as Engineer",
                    expected_value="Acme Corp",
                )
            self.assertFalse(
                result.get("ok"), f"hardcoded {fixed!r} must not approve"
            )
            self.assertEqual(result.get("error"), "approval_declined")

    def test_empty_expected_value_fails_closed(self):
        # Without a per-action varying value the prompt must not degrade
        # to a fixed string.
        with mock.patch.object(
            circuit_breaker._registry, "_stdin_is_tty", return_value=True
        ):
            result = circuit_breaker.require_action_confirmation(
                summary="Apply to Acme Corp as Engineer",
                expected_value="",
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "missing_expected_value")

    def test_no_environment_override_seam(self):
        # Spec section 6.3: no env var may override the TTY gate.
        source = inspect.getsource(circuit_breaker)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)
        registry_source = inspect.getsource(circuit_breaker._registry)
        self.assertNotIn("VETO_SKIP_CONFIRM", registry_source)
        self.assertNotIn("VETO_AGENT_MODE", registry_source)


class TestRequestHygiene(unittest.TestCase):
    """Spec section 11: robots.txt failure means no request; no proxy
    configuration code path (spec section 7)."""

    def setUp(self):
        provider_common._robots_cache.clear()
        self.addCleanup(provider_common._robots_cache.clear)

    def test_robots_fetch_failure_is_disallow(self):
        def _boom(*args, **kwargs):
            raise ConnectionError("simulated network failure")

        with mock.patch.object(provider_common, "make_client") as make_client:
            make_client.return_value.__enter__.side_effect = _boom
            self.assertFalse(
                robots_allows("test", "https://example.com/jobs/1")
            )

    def test_robots_parse_failure_is_disallow(self):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.text = "this is not a parseable robots file \x00\x01"
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = resp
        with mock.patch.object(
            provider_common, "make_client", return_value=client
        ), mock.patch(
            "urllib.robotparser.RobotFileParser.parse",
            side_effect=ValueError("simulated parse failure"),
        ):
            self.assertFalse(
                robots_allows("test", "https://example.com/jobs/1")
            )

    def test_robots_failure_means_no_request(self):
        # Fail-CLOSED end to end: when robots.txt cannot be fetched, the
        # bring-your-own-listing fetch path must not issue the target
        # request at all.
        import byol

        requested: list[str] = []

        class _TrackingClient:
            def get(self, url, **kwargs):
                requested.append(url)
                raise AssertionError(
                    f"target URL must never be requested: {url}"
                )

            def close(self):
                pass

        def _boom(*args, **kwargs):
            raise ConnectionError("robots.txt unreachable")

        with mock.patch.object(
            provider_common, "make_client"
        ) as make_client, mock.patch.object(byol, "polite_delay"):
            make_client.return_value.__enter__.side_effect = _boom
            result = byol.fetch_url("https://example.com/jobs/1")
        self.assertIn("error", result)
        self.assertIn("robots", result["error"].lower())
        self.assertEqual(requested, [])

    def test_no_proxy_configuration_code_path(self):
        # Spec section 7.5: no code path constructs or reads a proxy
        # configuration. sanitize_proxy_env (providers/_common.py) only
        # PRUNES malformed no_proxy entries and never sets or selects a
        # proxy — it is asserted separately below.
        patterns = (
            "proxies=",
            "proxy=",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "proxy_url",
            "proxy-server",
            "trust_env",
        )
        offenders = []
        for path in _project_py_files(include_tests=False):
            for i, line in enumerate(_read(path).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in patterns:
                    if pattern in line and "no_proxy" not in line.lower():
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{i}: {pattern}"
                        )
        self.assertEqual(
            offenders, [], f"proxy configuration code paths: {offenders}"
        )

    def test_sanitize_proxy_env_never_sets_or_selects_proxy(self):
        source = inspect.getsource(provider_common.sanitize_proxy_env)
        self.assertIn("never sets or selects a proxy", source)
        # It only prunes entries; it never assigns a proxy value.
        self.assertNotIn("proxies", source)


class TestCeilings(unittest.TestCase):
    """Spec section 11: per-day and per-provider ceilings cannot be raised
    from config (spec section 6.5)."""

    def test_ceiling_values(self):
        self.assertEqual(compliance.DEFAULT_DAILY_APPLY_CAP, 5)
        self.assertEqual(
            compliance.DEFAULT_SEARCH_BUDGET,
            {"official": 1000, "scraping": 40, "user": 1000},
        )

    def test_ceilings_are_code_literals(self):
        # Raising a ceiling must require editing source: the assignments
        # must be literals, not reads from config/env/argv.
        tree = ast.parse(_read(REPO_ROOT / "compliance.py"))
        found: dict[str, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in (
                        "DEFAULT_DAILY_APPLY_CAP",
                        "DEFAULT_SEARCH_BUDGET",
                    ):
                        found[target.id] = node.value
        self.assertEqual(
            set(found),
            {"DEFAULT_DAILY_APPLY_CAP", "DEFAULT_SEARCH_BUDGET"},
        )
        self.assertIsInstance(found["DEFAULT_DAILY_APPLY_CAP"], ast.Constant)
        self.assertIsInstance(found["DEFAULT_SEARCH_BUDGET"], ast.Dict)

    def test_ceilings_not_read_from_config_or_env(self):
        source = _read(REPO_ROOT / "compliance.py")
        for seam in ("os.environ", "getenv", "sys.argv", "argparse",
                     "configparser", "tomllib", "yaml.safe_load"):
            self.assertNotIn(
                seam, source, f"compliance.py must not read config via {seam}"
            )

    def test_ceilings_defined_in_exactly_one_module(self):
        offenders = []
        for path in _project_py_files(include_tests=False):
            if path.name == "compliance.py":
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if re.match(
                    r"\s*DEFAULT_(DAILY_APPLY_CAP|SEARCH_BUDGET)\s*=", line
                ):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(offenders, [], f"ceiling redefined: {offenders}")


class TestBindDefault(unittest.TestCase):
    """Spec section 11: the servable HTTP default bind is 127.0.0.1.

    The spec's ``server.py`` uvicorn reference is stale (re-derived in
    commit 1): no uvicorn exists anywhere in the tree and ``server.py``
    is stdio-only. The only socket-bind surface is ``webui.serve()``,
    so the assertion targets it.
    """

    def test_serve_default_bind_is_localhost(self):
        sig = inspect.signature(webui.serve)
        self.assertIn("host", sig.parameters)
        self.assertEqual(sig.parameters["host"].default, "127.0.0.1")

    def test_no_uvicorn_in_tree(self):
        offenders = []
        for path in _project_py_files(include_tests=False):
            for i, line in enumerate(_read(path).splitlines(), 1):
                if "uvicorn" in line.lower():
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(offenders, [], f"uvicorn references: {offenders}")


class TestI05PacketContract(unittest.TestCase):
    """Spec section 11: tests/test_i05_packet.py keeps passing unchanged.

    Note on the spec's ``tests/test_i05_packet.py:220`` reference: line
    numbers drifted. The no-submit contract for the packet path is now
    ``test_module_has_no_submit_capability`` (currently at line 245);
    this test runs the whole module unchanged and asserts it is green.
    """

    def test_i05_packet_suite_passes(self):
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_i05_packet.py", "-q"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"tests/test_i05_packet.py must pass unchanged.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
