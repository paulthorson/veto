#!/usr/bin/env python3
"""Tests for Initiative 09 epic 5: session rescue.

All browser interaction is faked; no Playwright, no network. Asserts the
pause-and-hand-off behavior and — critically — that CAPTCHA is never
bypassed: this module contains no solve/click-through path, and every
detector outcome is a pause, never a bypass.
"""

import inspect
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

from providers import session_rescue
from providers.session_rescue import (
    RescueHandoff,
    assess_field_ambiguity,
    check,
    detect_auth_loss,
    detect_captcha,
)


class FakePage:
    """Duck-typed stand-in for a Playwright page."""

    def __init__(self, url="https://example.com/apply", html="",
                 selectors=None):
        self.url = url
        self._html = html
        self._selectors = set(selectors or ())

    def query_selector(self, selector):
        return object() if selector in self._selectors else None

    def content(self):
        return self._html


def _field(name, label="", ftype="text"):
    return {"name": name, "label": label, "type": ftype,
            "selector": f"input[name='{name}']"}


class CaptchaDetectionTests(unittest.TestCase):
    def test_recaptcha_iframe_detected(self):
        page = FakePage(selectors={'iframe[src*="recaptcha"]'})
        hit = detect_captcha(page)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["reason"], "captcha")

    def test_turnstile_detected(self):
        page = FakePage(selectors={".cf-turnstile"})
        self.assertIsNotNone(detect_captcha(page))

    def test_text_marker_detected(self):
        page = FakePage(html="<div>Please verify you are human</div>")
        hit = detect_captcha(page)
        self.assertIsNotNone(hit)
        self.assertIn("verify you are human", hit["evidence"])

    def test_clean_page_has_no_captcha(self):
        page = FakePage(html="<form><input name='q'></form>")
        self.assertIsNone(detect_captcha(page))


class AuthLossDetectionTests(unittest.TestCase):
    def test_login_redirect_detected(self):
        page = FakePage(url="https://www.linkedin.com/login?from=jobs")
        hit = detect_auth_loss(page, board="linkedin")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["reason"], "auth_loss")

    def test_session_expired_text_detected(self):
        page = FakePage(html="<p>Your session has expired</p>")
        self.assertIsNotNone(detect_auth_loss(page))

    def test_clean_page_has_no_auth_loss(self):
        page = FakePage(url="https://boards.greenhouse.io/acme/jobs/1",
                        html="<form></form>")
        self.assertIsNone(detect_auth_loss(page))


class FieldAmbiguityTests(unittest.TestCase):
    def test_unlabeled_synthetic_field_triggers(self):
        fields = [_field("field_3")]  # synthesized name, no label
        hit = assess_field_ambiguity(fields)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["reason"], "field_ambiguity")

    def test_realistic_field_prefix_name_does_not_trigger(self):
        # 'field_of_study' is a real application field name, not a
        # synthesized field_N fallback — no numeric suffix, no trigger.
        fields = [_field("field_of_study", ftype="text")]
        self.assertIsNone(assess_field_ambiguity(fields))

    def test_synthetic_field_with_label_does_not_trigger(self):
        # A labeled field is never "unlabeled", even with a numeric name.
        fields = [_field("field_7", label="Phone", ftype="text")]
        self.assertIsNone(assess_field_ambiguity(fields))

    def test_duplicate_labels_trigger(self):
        fields = [_field("a", label="Name"), _field("b", label="Name")]
        hit = assess_field_ambiguity(fields)
        self.assertIsNotNone(hit)
        self.assertIn("duplicate", hit["evidence"])

    def test_duplicate_labels_trigger_for_email_and_password(self):
        # Duplicate-label detection covers all text-like input types,
        # including email and password fields.
        fields = [
            _field("email_1", label="Email", ftype="email"),
            _field("email_2", label="Email", ftype="email"),
        ]
        hit = assess_field_ambiguity(fields)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["reason"], "field_ambiguity")

        fields = [
            _field("pw1", label="Password", ftype="password"),
            _field("pw2", label="Password", ftype="password"),
        ]
        hit = assess_field_ambiguity(fields)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["reason"], "field_ambiguity")

    def test_clean_fields_pass(self):
        fields = [
            _field("first_name", label="First name"),
            _field("email", label="Email", ftype="email"),
        ]
        self.assertIsNone(assess_field_ambiguity(fields))

    def test_empty_fields_pass(self):
        self.assertIsNone(assess_field_ambiguity([]))


class CheckPriorityTests(unittest.TestCase):
    def test_captcha_beats_auth_loss(self):
        page = FakePage(
            url="https://example.com/login",
            selectors={".g-recaptcha"},
        )
        handoff = check(page, board="x")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.reason, "captcha")

    def test_handoff_carries_guidance_and_token(self):
        page = FakePage(selectors={"#captcha"})
        handoff = check(page, board="greenhouse",
                        screenshot_path="/tmp/shot.png")
        self.assertIsInstance(handoff, RescueHandoff)
        self.assertTrue(handoff.resume_token)
        self.assertTrue(handoff.what_user_should_do)
        self.assertIn("never solve", handoff.what_user_should_do.lower())
        bundle = handoff.to_dict()
        self.assertEqual(bundle["reason"], "captcha")
        self.assertEqual(bundle["board"], "greenhouse")

    def test_detector_failure_fails_closed_with_loud_handoff(self):
        # Legal-hardening: a crashed detector must FAIL CLOSED with a
        # loud RescueHandoff (reason="detector_failure"), never a silent
        # None that downstream would misread as "no CAPTCHA found".
        class Broken:
            url = "https://example.com"
            def query_selector(self, selector):
                raise RuntimeError("boom")
            def content(self):
                raise RuntimeError("boom")
        handoff = check(Broken())
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.reason, "detector_failure")
        self.assertIn("RuntimeError", handoff.evidence)


class NeverBypassTests(unittest.TestCase):
    def test_no_bypass_function_exists(self):
        names = [n.lower() for n, _ in inspect.getmembers(session_rescue)]
        for forbidden in ("bypass", "solve_captcha", "defeat", "click_through"):
            self.assertFalse(
                any(forbidden in n for n in names),
                f"forbidden bypass-adjacent member: {forbidden}",
            )

    def test_source_contains_no_bypass_implementation(self):
        import pathlib
        src = pathlib.Path(session_rescue.__file__).read_text()
        # "bypass" may only appear in refusal/policy prose, never as code.
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if "bypass" in stripped.lower() and not stripped.startswith(("#", '"""', '"')):
                # Allow only the refusal_note string and docstring prose.
                self.assertTrue(
                    'NEVER' in line or 'never' in line.lower(),
                    f"line {i}: suspicious bypass reference: {line.strip()[:80]}",
                )

    def test_refusal_note_states_policy(self):
        note = session_rescue.refusal_note()
        self.assertIn("never", note.lower())
        self.assertIn("handed to you", note.lower())


class DetectedAtTests(unittest.TestCase):
    def test_detected_at_is_timezone_aware_utc(self):
        page = FakePage(selectors={"#captcha"})
        handoff = check(page, board="greenhouse")
        stamp = datetime.fromisoformat(handoff.detected_at)
        self.assertIsNotNone(stamp.tzinfo, "detected_at must be tz-aware")
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        self.assertIn("+00:00", handoff.detected_at)


def _install_fake_playwright(page):
    """Inject a fake ``playwright.sync_api`` module into sys.modules."""
    entered = mock.MagicMock()
    entered.__enter__.return_value = entered
    entered.__exit__.return_value = False
    fake_browser = mock.MagicMock()
    fake_context = mock.MagicMock()
    fake_context.new_page.return_value = page
    fake_browser.new_context.return_value = fake_context
    entered.chromium.launch.return_value = fake_browser
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: entered
    pw = types.ModuleType("playwright")
    pw.sync_api = sync_api
    old = {n: sys.modules.get(n) for n in ("playwright", "playwright.sync_api")}
    sys.modules["playwright"] = pw
    sys.modules["playwright.sync_api"] = sync_api
    return old, fake_browser


class _PlaywrightFakedTest(unittest.TestCase):
    """Base: fake playwright + stubbed browser_apply seams, restored on tearDown."""

    def setUp(self):
        import browser_apply

        self.browser_apply = browser_apply
        self._patches = []
        self._pw_old = None

    def _start(self, target, new=mock.DEFAULT, **kwargs):
        patcher = mock.patch(target, new=new, **kwargs)
        self._patches.append(patcher)
        return patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        if self._pw_old is not None:
            for name, old in self._pw_old.items():
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old
            self._pw_old = None


class _FakeSubmitButton:
    def __init__(self, page):
        self.page = page
        self.clicked = False

    def click(self, timeout=None):
        self.clicked = True
        if self.page.captcha_active:
            raise AssertionError(
                "submit.click() ran despite a post-fill CAPTCHA — the "
                "pre-submit rescue check failed"
            )


if __name__ == "__main__":
    unittest.main()
