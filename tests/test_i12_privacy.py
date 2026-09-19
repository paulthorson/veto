#!/usr/bin/env python3
"""Initiative 12 — privacy scanner unit tests (WS2 kickback fixes).

Proves the scanner's guarantees directly: value-side detection
(email/phone/marker/long text/quotation/person name), forbidden field
names, the newsletter_opt_in false-positive fix (C8), and the
observability signal (O3).
"""

import unittest

from initiatives.i12 import privacy
from initiatives.i12.privacy import (
    ContentDetected,
    assert_clean,
    scan_field,
    scan_payload,
)


class ValueDetectionTest(unittest.TestCase):
    def test_email_fires(self):
        findings = scan_payload({"role": "ping me at jane.doe@example.com"})
        self.assertTrue(any("email" in f for f in findings), findings)

    def test_phone_fires(self):
        findings = scan_payload({"company": "call 415-555-0132 anytime"})
        self.assertTrue(any("phone" in f for f in findings), findings)

    def test_marker_fires(self):
        findings = scan_payload(
            {"evidence_summary": "Work Experience: Senior Engineer, Initech"})
        self.assertTrue(any("marker" in f for f in findings), findings)

    def test_long_text_fires(self):
        findings = scan_payload({"note": "lorem ipsum " * 30})  # 360 chars
        self.assertTrue(any("long free text" in f for f in findings), findings)

    def test_long_text_without_spaces_is_not_content(self):
        # A long token (hash, id) with no spaces is not prose.
        self.assertEqual(scan_payload({"session_id": "x" * 300}), [])

    def test_short_clean_prose_passes(self):
        self.assertEqual(
            scan_payload({"evidence_summary": "Python and systems match"}), [])

    def test_quoted_span_fires(self):
        # C5: a long quoted span reads as a verbatim quotation of source
        # prose, even with no marker words and under 280 chars.
        quote = ('Manager wrote "she consistently delivered complex platform '
                 'migrations ahead of schedule every single quarter" truly')
        findings = scan_payload({"evidence_summary": quote})
        self.assertTrue(any("quotation" in f for f in findings), findings)

    def test_short_quote_does_not_fire(self):
        # A short quoted phrase is a legitimate paraphrase device.
        self.assertEqual(
            scan_payload({"evidence_summary": 'Uses "event-driven" patterns'}),
            [])

    def test_short_unquoted_prose_is_accepted_residual_risk(self):
        # C5 documented boundary: short, unquoted, marker-free prose cannot
        # be distinguished from a legitimate paraphrase. The scanner does
        # NOT claim to catch it — this test pins the honest boundary.
        prose = "Built the billing pipeline and improved its reliability"
        self.assertEqual(scan_payload({"evidence_summary": prose}), [])


class FieldNameTest(unittest.TestCase):
    def test_forbidden_field_name_fires(self):
        findings = scan_payload({"resume_text": "anything"})
        self.assertTrue(any("forbidden pattern" in f for f in findings), findings)

    def test_cover_letter_field_name_fires(self):
        findings = scan_payload({"cover_letter": "anything"})
        self.assertTrue(any("forbidden pattern" in f for f in findings), findings)

    def test_newsletter_opt_in_does_not_fire(self):
        # C8: the old bare "letter" pattern false-positived here.
        self.assertEqual(scan_field("newsletter_opt_in", True), [])

    def test_safe_field_names_skip_name_check_but_not_value_check(self):
        self.assertEqual(scan_field("role", "Backend Engineer"), [])
        self.assertTrue(scan_field("role", "jane.doe@example.com"))


class PersonNameTest(unittest.TestCase):
    def test_bare_name_in_name_field_fires(self):
        # C2: {"name": "Alex Rivera"} used to pass clean.
        findings = scan_payload({"name": "Alex Rivera"})
        self.assertTrue(any("person's name" in f for f in findings), findings)

    def test_single_token_name_field_value_passes(self):
        # Factor names ("skills") are single tokens — never person names.
        self.assertEqual(scan_payload({"name": "skills"}), [])

    def test_lowercase_label_passes(self):
        self.assertEqual(scan_payload({"name": "backend skills"}), [])

    def test_name_token_fields_fire(self):
        # F2: the person-name value check applies to any field whose name
        # contains a "name" token (split on _ and camelCase boundaries;
        # a token merely containing "name" counts, so "username" fires).
        for field in ("candidate_name", "full_name", "display_name",
                      "contact_name", "username", "userName", "fullName"):
            findings = scan_payload({field: "Alex Rivera"})
            self.assertTrue(any("person's name" in f for f in findings),
                            (field, findings))

    def test_candidate_name_fails_closed(self):
        with self.assertRaises(ContentDetected):
            assert_clean({"candidate_name": "Alex Rivera"}, "f2-probe")

    def test_product_name_exempt_from_name_check_only(self):
        # F2 scoping choice: "product_name" is a pinned SAFE_FIELD_NAMES
        # metadata label, so the person-name *value* check skips it ...
        self.assertEqual(
            scan_payload({"product_name": "Cloud Architecture"}), [])
        # ... but every other signal still applies to its values.
        findings = scan_payload(
            {"product_name": "ping jane.doe@example.com"})
        self.assertTrue(any("email" in f for f in findings), findings)

    def test_non_name_token_field_is_documented_residual(self):
        # "hiring_manager" has no "name" token — accepted residual
        # false-negative risk, stated in D2.
        self.assertEqual(scan_payload({"hiring_manager": "Alex Rivera"}), [])


class NonStringKeyTest(unittest.TestCase):
    """F3: scan_field used to call pat.search(123) -> TypeError on
    non-string dict keys. Keys are coerced with str() before the
    forbidden-name check, and a non-string key is itself a finding —
    fail closed with the audit signal recording it, never TypeError."""

    def test_int_key_is_a_finding(self):
        findings = scan_payload({123: "clean value"})
        self.assertTrue(any("not a string" in f for f in findings), findings)

    def test_int_key_fails_closed_with_audit_signal(self):
        before = privacy.detection_count
        with self.assertRaises(ContentDetected):
            assert_clean({123: "clean value"}, "nonstring-key-probe")
        self.assertEqual(privacy.detection_count, before + 1)

    def test_nested_non_string_key_fails_closed(self):
        with self.assertRaises(ContentDetected):
            assert_clean({"a": {456: "x"}}, "nested-key-probe")

    def test_no_typeerror_on_non_string_key(self):
        try:
            scan_payload({123: "x"})
        except TypeError:
            self.fail("scan_payload raised TypeError on a non-string key")


class CurlyQuoteTest(unittest.TestCase):
    """F4: the quotation heuristic was ASCII-only — curly quotes
    (U+201C/U+201D, U+2018/U+2019) around a 50-char span passed. They are
    matched now; the thresholds remain estimates, not guarantees."""

    def test_curly_double_quotes_fire(self):
        quote = ("Manager wrote “she consistently delivered complex platform "
                 "migrations ahead of schedule every single quarter” truly")
        findings = scan_payload({"evidence_summary": quote})
        self.assertTrue(any("quotation" in f for f in findings), findings)

    def test_curly_single_quotes_fire(self):
        quote = ("‘she consistently delivered complex platform migrations "
                 "ahead of schedule every single quarter’ indeed")
        findings = scan_payload({"evidence_summary": quote})
        self.assertTrue(any("quotation" in f for f in findings), findings)

    def test_short_curly_quote_does_not_fire(self):
        self.assertEqual(
            scan_payload(
                {"evidence_summary": "Uses “event-driven” patterns"}), [])


class ObservabilityTest(unittest.TestCase):
    def setUp(self):
        privacy.set_detection_hook(None)
        self._before = privacy.detection_count

    def tearDown(self):
        privacy.set_detection_hook(None)

    def test_counter_increments_on_detection(self):
        with self.assertRaises(ContentDetected):
            assert_clean({"resume_text": "x"}, "test")
        self.assertEqual(privacy.detection_count, self._before + 1)

    def test_counter_untouched_on_clean(self):
        assert_clean({"role": "Backend Engineer"}, "test")
        self.assertEqual(privacy.detection_count, self._before)

    def test_hook_called_with_what_and_findings(self):
        seen = []
        privacy.set_detection_hook(lambda what, findings: seen.append((what, findings)))
        with self.assertRaises(ContentDetected):
            assert_clean({"email": "a@b.com"}, "hook-probe")
        self.assertEqual(len(seen), 1)
        what, findings = seen[0]
        self.assertEqual(what, "hook-probe")
        self.assertTrue(findings)

    def test_failing_hook_does_not_break_fail_closed(self):
        def bad_hook(what, findings):
            raise RuntimeError("hook exploded")
        privacy.set_detection_hook(bad_hook)
        with self.assertRaises(ContentDetected):
            assert_clean({"email": "a@b.com"}, "hook-failure-probe")
        # Counter still recorded despite the hook blowing up.
        self.assertEqual(privacy.detection_count, self._before + 1)


class DepthCutoffTest(unittest.TestCase):
    def test_deeply_nested_content_still_found_within_cutoff(self):
        nested = {"l1": {"l2": {"l3": {"email": "a@b.com"}}}}
        findings = scan_payload(nested)
        self.assertTrue(any("forbidden pattern" in f for f in findings), findings)


def _deep_hostile(levels=8, leaf_key="email", leaf_value="bob@example.com"):
    """A hand-built hostile dict: the leaf buried `levels` deep."""
    d = {leaf_key: leaf_value}
    for i in range(levels):
        d = {f"l{i}": d}
    return d


class FullDepthScanTest(unittest.TestCase):
    """F1: the old depth-6 cutoff silently returned [] past the cutoff, so
    a hand-built dict nested 7+ deep published its content through
    render_markdown. There is no cutoff anymore: the scan walks to full
    depth, iteratively, with a cycle guard. Nothing is silently passed."""

    def test_depth_8_hostile_dict_fails_closed(self):
        before = privacy.detection_count
        with self.assertRaises(ContentDetected) as ctx:
            assert_clean({"payload": _deep_hostile(8)}, "depth-probe")
        # Findings never carry offending values — the email must not
        # appear in the exception either.
        self.assertNotIn("bob@example.com", str(ctx.exception))
        # ... and the audit signal recorded the detection.
        self.assertEqual(privacy.detection_count, before + 1)

    def test_depth_20_hostile_dict_fails_closed(self):
        # Well past any recursion limit a recursive scanner could use.
        with self.assertRaises(ContentDetected):
            assert_clean(_deep_hostile(20), "depth-20-probe")

    def test_deep_clean_dict_passes(self):
        # Full-depth scanning must not false-positive on deep clean input.
        d = {"ok": "fine"}
        for i in range(30):
            d = {f"l{i}": d}
        self.assertEqual(scan_payload(d), [])

    def test_cyclic_dict_terminates_clean(self):
        d = {"note": "clean label"}
        d["self"] = d  # self-referential: must not hang the scanner
        self.assertEqual(scan_payload(d), [])

    def test_cyclic_dict_with_content_fails_closed(self):
        d: dict = {}
        d["self"] = d
        d["note"] = "reach bob@example.com"
        with self.assertRaises(ContentDetected):
            assert_clean(d, "cycle-probe")

    def test_shared_substructure_still_detected(self):
        shared = {"email": "bob@example.com"}
        findings = scan_payload({"a": shared, "b": shared})
        self.assertTrue(any("email" in f for f in findings), findings)


if __name__ == "__main__":
    unittest.main()
