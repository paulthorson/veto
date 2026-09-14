"""Initiative 08 — epic-level contribute.py tests.

Proves the hard contract at the contribute.py surface:
  - Epic 1: the EXACT bytes shown at preview are the bytes staged at
    submit (byte-for-byte, never re-serialized).
  - Epic 2: purpose is bound into the bundle, the hash, the consent
    record, and the receipt; non-persona purposes need a standing grant.
  - Epic 4: submit records append-only consent with purpose version +
    scrub ruleset version, and issues a local receipt.
  - Epic 5: quarantine lock is checked first; a preview/submit mismatch
    quarantines; a missing preview refuses without quarantining.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contribute  # noqa: E402
from initiatives.i08 import purposes, quarantine  # noqa: E402


def _store() -> dict:
    return {
        "sess_complete": {
            "session_id": "sess_complete",
            "company": "Initech",
            "role": "Senior Backend Engineer",
            "questions": [{"id": "q1", "kind": "behavioral",
                           "question": "Tell me about yourself."}],
            "answers": {"q1": {"answer": (
                "I led the billing rebuild and mentored engineers."),
                "overall": 82}},
            "complete": True,
            "created": "2026-09-01T00:00:00+00:00",
        },
    }


class EpicTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._store_file = root / "sessions.json"
        self._store_file.write_text(json.dumps(_store()), encoding="utf-8")
        self._contrib_dir = root / "contributions"
        patchers = [
            mock.patch.object(
                contribute, "_source_paths",
                return_value={"mock_interview": self._store_file,
                              "soft_skills": self._store_file,
                              "ai_proficiency": self._store_file}),
            mock.patch.object(contribute, "CONTRIB_DIR", self._contrib_dir),
            mock.patch.object(contribute, "OUTBOX_DIR",
                              self._contrib_dir / "outbox"),
            mock.patch.object(contribute, "CONSENT_PATH",
                              self._contrib_dir / "consent.json"),
            mock.patch.object(contribute, "SETTINGS_PATH",
                              self._contrib_dir / "settings.json"),
            mock.patch.object(contribute, "PREVIEWED_PATH",
                              self._contrib_dir / "previewed.json"),
            mock.patch.object(contribute, "PREVIEWS_DIR",
                              self._contrib_dir / "previews"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    # -- Epic 1: exact bytes ------------------------------------------------
    def test_staged_bytes_are_byte_exact_preview_bytes(self):
        preview = contribute.preview_contribution(["sess_complete"])
        self.assertIn("exact_bytes_hash", preview)
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertTrue(result["staged"])
        staged = Path(result["outbox"]).read_bytes()
        previewed = (self._contrib_dir / "previews"
                     / f"{preview['bundle_hash']}.json").read_bytes()
        self.assertEqual(staged, previewed,
                         "staged bytes must equal previewed bytes exactly")
        self.assertEqual(hashlib.sha256(staged).hexdigest(),
                         preview["exact_bytes_hash"])
        self.assertEqual(result["exact_bytes_hash"],
                         preview["exact_bytes_hash"])

    def test_preview_persists_exact_bytes(self):
        preview = contribute.preview_contribution(["sess_complete"])
        preview_file = (self._contrib_dir / "previews"
                        / f"{preview['bundle_hash']}.json")
        self.assertTrue(preview_file.exists())
        bundle = json.loads(preview_file.read_bytes().decode("utf-8"))
        self.assertEqual(bundle, preview["bundle"])

    def test_missing_preview_bytes_refuses_without_quarantine(self):
        # Preview record exists but the persisted bytes are gone
        # (deleted/corrupt): refuse, re-preview required, no quarantine.
        preview = contribute.preview_contribution(["sess_complete"])
        (self._contrib_dir / "previews"
         / f"{preview['bundle_hash']}.json").unlink()
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertIn("error", result)
        self.assertIn("previewed bytes are missing", result["error"])
        self.assertFalse(result.get("staged", False))
        self.assertFalse(quarantine.is_quarantined())

    # -- Epic 2: purpose binding --------------------------------------------
    def test_purpose_bound_into_bundle_hash_consent(self):
        preview = contribute.preview_contribution(
            ["sess_complete"], purpose="persona_improvements")
        self.assertEqual(preview["purpose"], "persona_improvements")
        self.assertEqual(preview["bundle"]["contribution_purpose"],
                         "persona_improvements")
        self.assertEqual(preview["bundle"]["purpose"], "Persona improvements")
        self.assertEqual(preview["purpose_version"],
                         preview["bundle"]["purpose_version"])
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(
                ["sess_complete"], confirmed=True,
                purpose="persona_improvements")
        self.assertTrue(result["staged"])
        consent = json.loads(
            (self._contrib_dir / "consent.json").read_text(encoding="utf-8"))
        self.assertEqual(len(consent), 1)
        record = consent[0]
        self.assertEqual(record["contribution_purpose"],
                         "persona_improvements")
        self.assertEqual(record["bundle_hash"], preview["bundle_hash"])
        self.assertIn("purpose_version", record)
        self.assertIn("scrub_ruleset", record)

    def test_unknown_purpose_rejected(self):
        result = contribute.preview_contribution(["sess_complete"],
                                                 purpose="marketing")
        self.assertIn("error", result)

    def test_research_submit_needs_standing_grant(self):
        contribute.preview_contribution(["sess_complete"],
                                        purpose="research")
        with mock.patch("builtins.print"):
            refused = contribute.submit_contribution(
                ["sess_complete"], confirmed=True, purpose="research")
        self.assertIn("error", refused)
        self.assertIn("not granted", refused["error"])
        purposes.grant_consent("research", Path(self._tmp.name))
        with mock.patch("builtins.print"):
            ok = contribute.submit_contribution(
                ["sess_complete"], confirmed=True, purpose="research")
        self.assertTrue(ok.get("staged"))

    # -- Epic 4: consent record + receipt ------------------------------------
    def test_receipt_issued_and_chained(self):
        contribute.preview_contribution(["sess_complete"])
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertIn("receipt_id", result)
        from initiatives.i08 import receipts as _receipts
        chain = _receipts.verify_chain(base_dir=self._contrib_dir)
        self.assertTrue(chain["valid"])

    # -- Epic 5: quarantine precedence ----------------------------------------
    def test_mismatch_with_existing_preview_refuses_without_quarantine(self):
        # A preview exists, but the session content changed afterwards:
        # the rebuilt bundle no longer matches any previewed hash. This
        # refuses (re-preview required) WITHOUT quarantining — by
        # construction, submit can only stage persisted preview bytes,
        # so a mismatch can never stage unreviewed bytes.
        contribute.preview_contribution(["sess_complete"])
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = \
            "Completely different answer text here."
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertIn("error", result)
        self.assertIn("not previewed", result["error"])
        self.assertFalse(result.get("staged", False))
        self.assertNotIn("QUARANTINED", result["error"])
        self.assertFalse(quarantine.is_quarantined())

    def test_quarantine_lock_blocks_submit_first(self):
        quarantine.raise_quarantine(trigger="test",
                                    base_dir=Path(self._tmp.name))
        result = contribute.submit_contribution(["sess_complete"],
                                                confirmed=True)
        self.assertIn("error", result)
        self.assertTrue(result.get("quarantine_locked"))
        # toggling settings cannot clear it
        contribute.set_contributions_enabled(True)
        result = contribute.submit_contribution(["sess_complete"],
                                                confirmed=True)
        self.assertTrue(result.get("quarantine_locked"))


if __name__ == "__main__":
    unittest.main()


class PurposeShapeTestCase(EpicTestCase):
    """Purpose binding: non-persona purposes NEVER carry content."""

    def test_research_bundle_is_aggregate_only(self):
        preview = contribute.preview_contribution(["sess_complete"],
                                                  purpose="research")
        bundle = preview["bundle"]
        self.assertEqual(bundle["bundle_kind"], "aggregate_counters")
        self.assertNotIn("sessions", bundle)
        self.assertNotIn("transcript", json.dumps(bundle))
        self.assertNotIn("scrub_summary", bundle)
        self.assertEqual(bundle["aggregate"]["session_count"], 1)
        self.assertIn("mock_interview",
                      bundle["aggregate"]["by_source"])

    def test_diagnostics_and_provider_health_are_aggregate_only(self):
        from initiatives.i08 import bundles
        for purpose in ("product_diagnostics", "provider_health"):
            preview = contribute.preview_contribution(["sess_complete"],
                                                      purpose=purpose)
            bundle = preview["bundle"]
            self.assertEqual(bundle["bundle_kind"], "aggregate_counters",
                             purpose)
            # Key-level check (definition prose may mention "transcripts").
            self.assertEqual(
                bundles._find_content_keys(bundle.get("aggregate")), [],
                purpose)
            # And the validator accepts it.
            bundles.validate_bundle(purpose, bundle)

    def test_persona_bundle_keeps_excerpts(self):
        preview = contribute.preview_contribution(["sess_complete"])
        bundle = preview["bundle"]
        self.assertEqual(bundle["bundle_kind"], "persona_excerpts")
        self.assertEqual(len(bundle["sessions"]), 1)
        self.assertTrue(bundle["sessions"][0]["transcript"])

    def test_validator_rejects_content_in_aggregate(self):
        from initiatives.i08 import bundles
        bad = {"bundle_kind": "aggregate_counters",
               "aggregate": {"transcript": [{"text": "hi"}]}}
        with self.assertRaises(ValueError):
            bundles.validate_bundle("research", bad)

    def test_research_submit_stages_aggregate_bytes(self):
        from initiatives.i08 import purposes
        purposes.grant_consent("research", Path(self._tmp.name))
        preview = contribute.preview_contribution(["sess_complete"],
                                                  purpose="research")
        with mock.patch("builtins.print"):
            ok = contribute.submit_contribution(
                ["sess_complete"], confirmed=True, purpose="research")
        self.assertTrue(ok.get("staged"))
        staged = json.loads(Path(ok["outbox"]).read_bytes()
                            .decode("utf-8"))
        self.assertEqual(staged["bundle_kind"], "aggregate_counters")
        self.assertNotIn("transcript", json.dumps(staged))
        self.assertEqual(
            ok["exact_bytes_hash"], preview["exact_bytes_hash"])

    def test_persona_needs_no_standing_grant(self):
        # Persona keeps per-bundle explicit confirmation instead.
        preview = contribute.preview_contribution(["sess_complete"])
        with mock.patch("builtins.print"):
            ok = contribute.submit_contribution(["sess_complete"],
                                                confirmed=True)
        self.assertTrue(ok.get("staged"))


class PreviewDisclosureTestCase(EpicTestCase):
    def test_preview_shows_removal_limits(self):
        for purpose in ("persona_improvements", "research"):
            preview = contribute.preview_contribution(["sess_complete"],
                                                      purpose=purpose)
            self.assertIn("removal_limits", preview)
            self.assertTrue(preview["removal_limits"])


class ExfiltrationRegressionTestCase(EpicTestCase):
    """Blind-review regressions (2026-09-13), end to end at the
    contribute.py surface.

    B1: the free-text rating note flowed RAW into staged bundles. It must
    now be scrubbed with ruleset v2, covered by the residual check, and
    caught by the audit's name/address shapes if scrubbing is bypassed.
    B2: a 32-char high-entropy token survived scrub+audit+residual
    byte-exact. It must now be redacted at the scrub layer.
    """

    PROBE_NOTE = ("Great persona! Reach me, Jane Alvarez, at "
                  "742 Oak Avenue, Portland for feedback")
    PROBE_TOKEN = "ab12cd34ef56gh78ij90kl12mn34op56"  # 32 chars

    def _store_with(self, **overrides) -> dict:
        store = _store()
        store["sess_complete"].update(overrides)
        return store

    def test_b1_rating_note_scrubbed_in_bundle(self):
        # The review's probe payload, rated onto the session.
        store = self._store_with(persona_rating={
            "stars": 5, "note": self.PROBE_NOTE,
            "rated_at": "2026-09-13T00:00:00+00:00"})
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        preview = contribute.preview_contribution(["sess_complete"])
        note = preview["bundle"]["sessions"][0]["rating_note"]
        self.assertNotIn("Jane Alvarez", note,
                         "B1: raw name reached the bundle")
        self.assertNotIn("742 Oak Avenue", note,
                         "B1: raw address reached the bundle")
        self.assertIn("[NAME]", note)
        self.assertIn("[ADDRESS]", note)

    def test_b1_rating_note_submit_stages_clean(self):
        store = self._store_with(persona_rating={
            "stars": 5, "note": self.PROBE_NOTE,
            "rated_at": "2026-09-13T00:00:00+00:00"})
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        contribute.preview_contribution(["sess_complete"])
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertTrue(result.get("staged"), result)
        self.assertFalse(result.get("quarantined", False))
        self.assertFalse(quarantine.is_quarantined())
        staged = Path(result["outbox"]).read_text(encoding="utf-8")
        self.assertNotIn("Jane Alvarez", staged)
        self.assertNotIn("742 Oak Avenue", staged)

    def test_b1_residual_check_covers_rating_note(self):
        # A scrub bypass on the note (raw PII in the staged field) must
        # trip the residual check, not slip past it.
        raw_texts = [self.PROBE_NOTE]
        scrubbed_texts = [self.PROBE_NOTE]  # as if scrubbing never ran
        findings = contribute._runtime_residual_check(raw_texts,
                                                      scrubbed_texts, ())
        self.assertTrue(findings, "residual check missed raw rating note")
        self.assertTrue(
            any(f["sample"].startswith("Jane Alvarez") or
                "742 Oak Avenue" in f["sample"] for f in findings))

    def test_b2_32char_token_redacted_end_to_end(self):
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = (
            f"The deploy key was {self.PROBE_TOKEN} in the config.")
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        preview = contribute.preview_contribution(["sess_complete"])
        turn_text = " ".join(
            t["text"] for t in preview["bundle"]["sessions"][0]["transcript"])
        self.assertNotIn(self.PROBE_TOKEN, turn_text,
                         "B2: 32-char token survived scrubbing")
        self.assertIn("[SECRET]", turn_text)
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertTrue(result.get("staged"), result)
        self.assertFalse(quarantine.is_quarantined())
        staged = Path(result["outbox"]).read_text(encoding="utf-8")
        self.assertNotIn(self.PROBE_TOKEN, staged)

    # -- B3: base64url tokens ------------------------------------------------
    # The scrubber's blob regex only knew the standard-base64 alphabet, so
    # 32-char base64url tokens split into sub-20-char runs at "-" / "_"
    # and survived scrub + audit + residual, staging verbatim into the
    # outbox. All fixtures are SYNTHETIC.
    B3_TOKEN_DASH = "aB3dE5fG7hI9jK1lM-nO4pQ6rS8tU0vW"      # 32 chars, '-'
    B3_TOKEN_UNDER = "aB3dE5fG7hI9jK1lM_nO4pQ6rS8tU0vW"     # 32 chars, '_'
    B3_TOKEN_MIXED = "aB3dE5fG7-hI9jK1lM_nO4pQ6rS8tU0vWxyz"  # 36 chars, both
    B3_JWT = (B3_TOKEN_DASH + "." + B3_TOKEN_UNDER + "."
              + "aB3dE5fG7-hI9jK1lM_nO4pQ6rS8tU0v")

    def test_b3_residual_pattern_is_independent_of_scrubber(self):
        # Guards the defense-in-depth fix: the residual check must keep
        # its own separately-maintained blob pattern in contribute.py, so
        # a future scrubber-regex regression is still caught downstream.
        # (CPython's re module caches identical literals, so object
        # identity is not the test — runtime behavior is.) Simulate the
        # regression by rebinding the scrubber's regex to one that
        # matches nothing: the residual check must still flag the token.
        from initiatives.i08 import scrub as _scrub
        old = _scrub._BASE64_BLOB_RE
        _scrub._BASE64_BLOB_RE = re.compile(r"a^")  # matches nothing
        try:
            raw = f"the key is {self.B3_TOKEN_DASH} ok"
            findings = contribute._runtime_residual_check([raw], [raw], ())
            self.assertTrue(
                findings,
                "B3: residual check silently inherited the scrubber "
                "regex regression")
        finally:
            _scrub._BASE64_BLOB_RE = old
        # And the residual pattern itself is the documented
        # base64/base64url blob shape.
        self.assertEqual(contribute._RESIDUAL_BLOB_RE.pattern,
                         r"\b[A-Za-z0-9+/=_-]{20,}={0,2}\b")

    def test_b3_residual_check_catches_base64url_tokens(self):
        for token in (self.B3_TOKEN_DASH, self.B3_TOKEN_UNDER,
                      self.B3_TOKEN_MIXED):
            raw = f"the key is {token} ok"
            findings = contribute._runtime_residual_check([raw], [raw], ())
            self.assertTrue(
                findings,
                f"B3: residual check missed base64url token {token!r}")
            self.assertTrue(
                any("residual_pii" == f["rule"] for f in findings))

    def test_b3_base64url_tokens_redacted_end_to_end(self):
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = (
            f"Deploy keys: {self.B3_TOKEN_DASH} and {self.B3_TOKEN_UNDER} "
            f"plus jwt {self.B3_JWT} in the config.")
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        preview = contribute.preview_contribution(["sess_complete"])
        turn_text = " ".join(
            t["text"] for t in preview["bundle"]["sessions"][0]["transcript"])
        for token in (self.B3_TOKEN_DASH, self.B3_TOKEN_UNDER,
                      self.B3_TOKEN_MIXED):
            self.assertNotIn(token, turn_text,
                             f"B3: base64url token survived scrubbing: {token!r}")
        for segment in self.B3_JWT.split("."):
            self.assertNotIn(segment, turn_text,
                             f"B3: JWT segment survived scrubbing: {segment!r}")
        self.assertIn("[SECRET]", turn_text)
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertTrue(result.get("staged"), result)
        self.assertFalse(result.get("quarantined", False))
        self.assertFalse(quarantine.is_quarantined())
        staged = Path(result["outbox"]).read_bytes()
        for token in (self.B3_TOKEN_DASH.encode(),
                      self.B3_TOKEN_UNDER.encode(),
                      self.B3_TOKEN_MIXED.encode(),
                      self.B3_JWT.encode()):
            self.assertNotIn(token, staged,
                             "B3: token verbatim in staged outbox bytes")


class ResidualDilutionBackstopTestCase(EpicTestCase):
    """B4 (2026-09-13): the residual layer is the critical backstop.

    Even if the scrubber AND the audit are defeated (as the whole-run
    entropy gate was, pre-fix), the residual check must flag a
    dilution-padded token and quarantine the payload. All fixtures are
    SYNTHETIC.
    """

    CORE = "ab12cd34ef56gh78ij90kl12mn34op56"  # 32 chars, ~4.6 bits/char
    PADDED_PREFIX_32 = "x" * 32 + CORE
    PADDED_SUFFIX_32 = CORE + "x" * 32
    PADDED_PREFIX_24 = "x" * 24 + CORE

    def test_sensitive_candidates_find_diluted_core(self):
        for padded in (self.PADDED_PREFIX_32, self.PADDED_SUFFIX_32,
                       self.PADDED_PREFIX_24):
            candidates = contribute._sensitive_candidates(padded)
            self.assertIn(
                self.CORE, candidates,
                f"B4: residual layer has no candidate for {padded[:16]}...")

    def test_runtime_residual_check_flags_scrub_miss(self):
        # As if scrubbing never ran: raw padded text in, raw padded
        # text out — the residual check must still fire.
        raw = f"the staging key was {self.PADDED_PREFIX_32} ok"
        findings = contribute._runtime_residual_check([raw], [raw])
        self.assertTrue(
            any(f["rule"] == "residual_pii" for f in findings),
            f"B4: residual check missed dilution-padded token: {findings}")

    def test_residual_ignores_dilution_guards(self):
        for word in ("internationalizations", "characteristically",
                     "responsibilities", "well-characteristically-speaking",
                     "very_long_snake_case_identifier_here"):
            text = f"the {word} of systems"
            self.assertEqual(contribute._sensitive_candidates(text), [],
                             f"B4: residual flagged ordinary word {word!r}")

    def test_submit_with_padded_token_stages_clean_bytes(self):
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = (
            f"The deploy key was {self.PADDED_PREFIX_32} in the config.")
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        preview = contribute.preview_contribution(["sess_complete"])
        turn_text = " ".join(
            t["text"] for t in preview["bundle"]["sessions"][0]["transcript"])
        self.assertNotIn(self.PADDED_PREFIX_32, turn_text)
        self.assertNotIn(self.CORE, turn_text)
        with mock.patch("builtins.print"):
            result = contribute.submit_contribution(["sess_complete"],
                                                    confirmed=True)
        self.assertTrue(result.get("staged"), result)
        self.assertFalse(result.get("quarantined", False))
        self.assertFalse(quarantine.is_quarantined())
        staged = Path(result["outbox"]).read_bytes()
        self.assertNotIn(self.PADDED_PREFIX_32.encode(), staged,
                         "B4: padded token verbatim in staged outbox bytes")
        self.assertNotIn(self.CORE.encode(), staged,
                         "B4: token core verbatim in staged outbox bytes")

    def test_scrub_and_audit_miss_still_quarantines_via_residual(self):
        # Faithful B4 replay: scrub and audit behave as they did
        # pre-fix (whole-run gate defeated by dilution padding), so the
        # padded token reaches the staged bundle verbatim. The residual
        # backstop must quarantine and lock sending.
        from initiatives.i08 import scrub as _scrub
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = (
            f"The deploy key was {self.PADDED_SUFFIX_32} in the config.")
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        passthrough = lambda text, known_companies=(): (text, [])  # noqa: E731
        with mock.patch.object(_scrub, "scrub_text_v2",
                               side_effect=passthrough), \
             mock.patch.object(_scrub, "audit_scrubbed_text",
                               return_value=[]):
            contribute.preview_contribution(["sess_complete"])
            with mock.patch("builtins.print"):
                result = contribute.submit_contribution(
                    ["sess_complete"], confirmed=True)
        self.assertTrue(result.get("quarantined"), result)
        self.assertEqual(result.get("check"), "residual_pii")
        self.assertTrue(quarantine.is_quarantined())
        # The lock is global: a second submit refuses without staging.
        with mock.patch("builtins.print"):
            again = contribute.submit_contribution(["sess_complete"],
                                                   confirmed=True)
        self.assertTrue(again.get("quarantine_locked"))
        self.assertFalse(again.get("staged", False))


class ResidualGateIndependenceB5TestCase(EpicTestCase):
    """B5 (2026-09-13): the residual layer's decision is independent of
    the shared ``_high_entropy_cores`` gate.

    Two reviewer-demonstrated dilution shapes staged byte-exact with
    zero quarantine through the real preview -> submit flow because all
    three layers shared the one gate: sub-threshold chunk dilution
    (D_chunk5: 5-char ``aaaaa`` chunks interleaved every 8 core chars —
    under the 6-char repeat-split threshold, every 20-char window < 4.0
    bits/char) and core bisection (D_bisect: 16-char halves around 32
    x's — repeat-split segments filtered by the 20-char minimum, no
    20-char window gating). The residual now applies its own
    de-pad/de-period assessment (``contribute._residual_blob_views``)
    and must fire even when the shared gate returns []. All fixtures
    are SYNTHETIC.
    """

    CORE = "ab12cd34ef56gh78ij90kl12mn34op56"  # 32 chars, ~4.6 bits/char
    # NOTE: written as literals, not a comprehension over CORE — class
    # bodies cannot see their own names inside a generator expression.
    D_CHUNK5 = "aaaaa".join(
        ["ab12cd34", "ef56gh78", "ij90kl12", "mn34op56"])
    D_BISECT = CORE[:16] + "x" * 32 + CORE[16:]
    # Periodic multi-char pad: the second (de-period) mechanism must
    # recover the core independently of the shared gate.
    D_PERIODIC = CORE[:16] + "an" * 16 + CORE[16:]

    DILUTION_GUARDS = (
        "internationalizations",
        "characteristically",
        "responsibilities",
        "well-characteristically-speaking",
        "very_long_snake_case_identifier_here",
    )

    def test_b5_shapes_defeat_shared_gate(self):
        # Fixture sanity: the shared gate really is defeated by both
        # demonstrated shapes (this is the precondition the fix closes).
        from initiatives.i08 import scrub as _scrub
        for diluted in (self.D_CHUNK5, self.D_BISECT):
            self.assertEqual(_scrub._high_entropy_cores(diluted), [],
                             "fixture sanity: shared gate must return [] "
                             f"for {diluted[:24]}...")

    def test_residual_views_recover_cores_independently(self):
        for diluted in (self.D_CHUNK5, self.D_BISECT, self.D_PERIODIC):
            views = contribute._residual_blob_views(diluted)
            self.assertIn(
                self.CORE, views,
                f"B5: residual assessment did not recover the core from "
                f"{diluted[:24]}...")
            candidates = contribute._sensitive_candidates(
                f"the key is {diluted} ok")
            self.assertIn(
                diluted, candidates,
                "B5: whole diluted run must be a residual candidate when "
                "any view gates (it is what a scrub miss leaves verbatim)")

    def test_residual_fires_when_shared_gate_returns_empty(self):
        # Constitutional property: even with the shared gate patched to
        # total defeat, the residual backstop must still flag every B5
        # shape — one defeated gate can no longer silence all layers.
        from initiatives.i08 import scrub as _scrub
        with mock.patch.object(_scrub, "_high_entropy_cores",
                               return_value=[]):
            for diluted in (self.D_CHUNK5, self.D_BISECT, self.D_PERIODIC):
                raw = f"the key is {diluted} ok"
                findings = contribute._runtime_residual_check(
                    [raw], [raw], ())
                self.assertTrue(
                    any(f["rule"] == "residual_pii" for f in findings),
                    f"B5: residual check depends on the shared gate for "
                    f"{diluted[:24]}...")

    def test_b5_residual_ignores_dilution_guards(self):
        # Residual-path over-scrub sanity: ordinary long words must not
        # quarantine — neither as candidates nor as findings when the
        # text passes through unscathed.
        for word in self.DILUTION_GUARDS:
            text = f"the {word} of systems"
            self.assertEqual(contribute._sensitive_candidates(text), [],
                             f"B5: residual flagged ordinary word {word!r}")
            self.assertEqual(
                contribute._runtime_residual_check([text], [text], ()), [],
                f"B5: residual quarantined ordinary word {word!r}")

    def _submit_with_answer(self, answer):
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = answer
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        contribute.preview_contribution(["sess_complete"])
        with mock.patch("builtins.print"):
            return contribute.submit_contribution(["sess_complete"],
                                                  confirmed=True)

    def _assert_quarantined_via_residual(self, result, diluted):
        self.assertFalse(result.get("staged"), result)
        self.assertTrue(result.get("quarantined"), result)
        self.assertEqual(result.get("check"), "residual_pii", result)
        self.assertTrue(quarantine.is_quarantined())
        # The lock is global: a second submit refuses without staging.
        with mock.patch("builtins.print"):
            again = contribute.submit_contribution(["sess_complete"],
                                                   confirmed=True)
        self.assertTrue(again.get("quarantine_locked"))
        self.assertFalse(again.get("staged", False))

    def test_d_chunk5_quarantines_end_to_end(self):
        # Exact B5 demonstration, real flow, no mocks: scrub and audit
        # genuinely miss the sub-threshold chunk dilution, so the
        # residual backstop must quarantine and lock sending.
        result = self._submit_with_answer(
            f"The deploy key was {self.D_CHUNK5} in the config.")
        self._assert_quarantined_via_residual(result, self.D_CHUNK5)

    def test_d_bisect32x_quarantines_end_to_end(self):
        # Exact B5 demonstration, real flow, no mocks: scrub and audit
        # genuinely miss the bisected core, so the residual backstop
        # must quarantine and lock sending.
        result = self._submit_with_answer(
            f"The deploy key was {self.D_BISECT} in the config.")
        self._assert_quarantined_via_residual(result, self.D_BISECT)

    def test_b5_scrub_and_audit_miss_still_quarantines_via_residual(self):
        # Faithful backstop replay: scrub and audit behave as fully
        # defeated (passthrough), so the diluted token reaches the
        # staged bundle verbatim. The residual's independent assessment
        # must quarantine and lock sending.
        from initiatives.i08 import scrub as _scrub
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = (
            f"The deploy key was {self.D_CHUNK5} in the config.")
        self._store_file.write_text(json.dumps(store), encoding="utf-8")
        passthrough = lambda text, known_companies=(): (text, [])  # noqa: E731
        with mock.patch.object(_scrub, "scrub_text_v2",
                               side_effect=passthrough), \
             mock.patch.object(_scrub, "audit_scrubbed_text",
                               return_value=[]):
            contribute.preview_contribution(["sess_complete"])
            with mock.patch("builtins.print"):
                result = contribute.submit_contribution(
                    ["sess_complete"], confirmed=True)
        self._assert_quarantined_via_residual(result, self.D_CHUNK5)


def _r4_interleave(core: str, pad: str, every: int) -> str:
    """Interleave ``pad`` after every ``every`` core chars (no trailing pad)."""
    chunks = [core[i:i + every] for i in range(0, len(core), every)]
    return "".join(
        ch + (pad if j < len(chunks) - 1 else "")
        for j, ch in enumerate(chunks))


_R4_CORE = "ab12cd34ef56gh78ij90kl12mn34op56"  # 32 chars, ~4.6 bits/char

#: Round-4 demonstrated vulnerable shapes: dense sub-3-char-run,
#: non-adjacent interleaving. Every shape stages BYTE-EXACT with zero
#: quarantine pre-fix (whole-run entropy 3.28-3.99, shared gate returns
#: [], de-pad needs 3+ runs, de-period needs adjacent repeats).
_R4_VULNERABLE = {
    # Reviewer's exact demonstrated string ("aa" every 4 core chars).
    "aa_every4": "ab12aacd34aaef56aagh78aaij90aakl12aamn34aaop56",
    "aa_every2": _r4_interleave(_R4_CORE, "aa", 2),
    "aa_every3": _r4_interleave(_R4_CORE, "aa", 3),
    "a_every2": _r4_interleave(_R4_CORE, "a", 2),
    "ab_every2": _r4_interleave(_R4_CORE, "ab", 2),
    "zz_every2": _r4_interleave(_R4_CORE, "zz", 2),
    "zz_every3": _r4_interleave(_R4_CORE, "zz", 3),
}

#: New dense-interleaving probes invented for the round-4 fix: wider
#: pads, alternating pads, sparser pads, 64-char cores, mixed densities.
_R4_NEW_PROBES = {
    "pad3_every2": _r4_interleave(_R4_CORE, "qxz", 2),
    "pad3_every3": _r4_interleave(_R4_CORE, "qxz", 3),
    "alt_ab_cd_every2": "".join(
        ch + ("ab" if j % 2 == 0 else "cd")
        for j, ch in enumerate(
            [_R4_CORE[i:i + 2] for i in range(0, 32, 2)])),
    "pad_every5th": _r4_interleave(_R4_CORE, "aa", 5),
    "core64_aa_every2": _r4_interleave(_R4_CORE * 2, "aa", 2),
    "core64_ab_every2": _r4_interleave(_R4_CORE * 2, "ab", 2),
    "core64_zz_every3": _r4_interleave(_R4_CORE * 2, "zz", 3),
    "mixed_density": (_r4_interleave(_R4_CORE[:16], "aa", 2)
                      + _r4_interleave(_R4_CORE[16:], "aa", 6)),
    "pad5_every2": _r4_interleave(_R4_CORE, "qxzjv", 2),
    "wxyz_every2": _r4_interleave(_R4_CORE, "wxyz", 2),
}


class ResidualDenseInterleavingR4TestCase(EpicTestCase):
    """Round 4 (2026-09-13): dense sub-3-char-run, non-adjacent
    interleaving defeats the scrub gate, the de-pad transform, and the
    de-period transform simultaneously.

    The residual's third independent transform — frequency dominance
    (``contribute._residual_dominance_views``) — closes the hole: a pad
    dense enough to drag the whole run under the entropy bar must
    dominate the character distribution, so stripping the dominant
    characters recovers the core for re-gating. All fixtures are
    SYNTHETIC.
    """

    DILUTION_GUARDS = (
        "internationalizations",
        "characteristically",
        "responsibilities",
        "well-characteristically-speaking",
        "very_long_snake_case_identifier_here",
    )

    def test_r4_shapes_defeat_shared_gate(self):
        # Fixture sanity: the shared gate really is defeated by every
        # demonstrated shape (this is the precondition the fix closes).
        from initiatives.i08 import scrub as _scrub
        for name, diluted in _R4_VULNERABLE.items():
            self.assertEqual(_scrub._high_entropy_cores(diluted), [],
                             "fixture sanity: shared gate must return [] "
                             f"for {name}")

    def test_r4_shapes_are_credential_recoverable(self):
        # Documents why these shapes are dangerous: the attacker
        # recovers the credential trivially by removing the pad.
        self.assertEqual(
            _R4_VULNERABLE["aa_every4"].replace("aa", ""), _R4_CORE)
        self.assertEqual(
            _R4_VULNERABLE["aa_every2"].replace("aa", ""), _R4_CORE)
        self.assertEqual(
            _R4_VULNERABLE["zz_every3"].replace("zz", ""), _R4_CORE)
        # Single-char pad: removing every pad char also removes the
        # core's own lone "a" — still a trivial positional recovery.
        self.assertEqual(
            _R4_VULNERABLE["a_every2"].replace("a", ""),
            _R4_CORE.replace("a", ""))

    def test_r4_dominance_views_gate_independently(self):
        # The new view recovers a gating core from every vulnerable
        # shape, and the whole diluted run is emitted as the residual
        # candidate (it is what a scrub miss leaves verbatim).
        for name, diluted in _R4_VULNERABLE.items():
            stripped = contribute._residual_dominance_views(diluted)
            self.assertTrue(stripped,
                            f"R4: no dominance view for {name}")
            for view in stripped:
                if (len(view) >= contribute._RESIDUAL_MIN_LEN
                        and contribute._residual_shannon_entropy(view)
                        >= contribute._RESIDUAL_MIN_BITS):
                    break
            else:
                self.fail(f"R4: dominance views did not re-gate {name}")
            candidates = contribute._sensitive_candidates(
                f"the key is {diluted} ok")
            self.assertIn(
                diluted, candidates,
                f"R4: whole diluted run must be a residual candidate "
                f"for {name}")

    def test_r4_dominance_view_survives_defeated_transforms(self):
        # Layered independence, round 4: even with the shared gate AND
        # both earlier residual transforms defeated, the dominance view
        # must still gate every vulnerable shape.
        from initiatives.i08 import scrub as _scrub
        dead = re.compile(r"a^")  # matches nothing
        old_depad, old_tandem = (contribute._RESIDUAL_DEPAD_RE,
                                 contribute._RESIDUAL_TANDEM_RE)
        contribute._RESIDUAL_DEPAD_RE = dead
        contribute._RESIDUAL_TANDEM_RE = dead
        try:
            with mock.patch.object(_scrub, "_high_entropy_cores",
                                   return_value=[]):
                for name, diluted in _R4_VULNERABLE.items():
                    views = contribute._residual_blob_views(diluted)
                    self.assertTrue(
                        views,
                        f"R4: residual depends on a defeated transform "
                        f"for {name}")
                    raw = f"the key is {diluted} ok"
                    findings = contribute._runtime_residual_check(
                        [raw], [raw], ())
                    self.assertTrue(
                        any(f["rule"] == "residual_pii" for f in findings),
                        f"R4: residual check missed {name} with all "
                        f"other transforms defeated")
        finally:
            contribute._RESIDUAL_DEPAD_RE = old_depad
            contribute._RESIDUAL_TANDEM_RE = old_tandem

    def test_r4_residual_ignores_dilution_guards(self):
        # Fail-closed must not over-scrub: ordinary long words stage
        # verbatim with zero residual findings — through both scrub and
        # residual paths, and even with the shared gate defeated.
        from initiatives.i08 import scrub as _scrub
        for word in self.DILUTION_GUARDS:
            text = f"the {word} of systems"
            self.assertEqual(contribute._sensitive_candidates(text), [],
                             f"R4: residual flagged ordinary word {word!r}")
            self.assertEqual(
                contribute._runtime_residual_check([text], [text], ()), [],
                f"R4: residual quarantined ordinary word {word!r}")
            scrubbed, _ = _scrub.scrub_text_v2(text)
            self.assertEqual(scrubbed, text,
                             f"R4: scrub altered ordinary word {word!r}")
            with mock.patch.object(_scrub, "_high_entropy_cores",
                                   return_value=[]):
                self.assertEqual(
                    contribute._runtime_residual_check([text], [text], ()),
                    [],
                    f"R4: residual quarantined {word!r} with gate defeated")

    def _submit_fresh(self, answer, tag):
        # Fresh isolated store per shape: quarantine is global, so each
        # shape gets its own directory (mirrors EpicTestCase.setUp).
        # Returns (result, patched-context-manager): the caller must
        # keep the context entered while asserting quarantine state,
        # because quarantine resolves its state dir from the patched
        # contribute.CONTRIB_DIR.
        import contextlib
        root = Path(self._tmp.name) / tag
        root.mkdir(parents=True, exist_ok=True)
        store_file = root / "sessions.json"
        store = _store()
        store["sess_complete"]["answers"]["q1"]["answer"] = answer
        store_file.write_text(json.dumps(store), encoding="utf-8")
        contrib_dir = root / "contributions"
        ctx = contextlib.ExitStack()
        ctx.enter_context(mock.patch.object(
            contribute, "_source_paths",
            return_value={"mock_interview": store_file,
                          "soft_skills": store_file,
                          "ai_proficiency": store_file}))
        ctx.enter_context(mock.patch.object(contribute, "CONTRIB_DIR",
                                            contrib_dir))
        ctx.enter_context(mock.patch.object(contribute, "OUTBOX_DIR",
                                            contrib_dir / "outbox"))
        ctx.enter_context(mock.patch.object(contribute, "CONSENT_PATH",
                                            contrib_dir / "consent.json"))
        ctx.enter_context(mock.patch.object(contribute, "SETTINGS_PATH",
                                            contrib_dir / "settings.json"))
        ctx.enter_context(mock.patch.object(contribute, "PREVIEWED_PATH",
                                            contrib_dir / "previewed.json"))
        ctx.enter_context(mock.patch.object(contribute, "PREVIEWS_DIR",
                                            contrib_dir / "previews"))
        ctx.enter_context(mock.patch("builtins.print"))
        contribute.preview_contribution(["sess_complete"])
        result = contribute.submit_contribution(["sess_complete"],
                                                confirmed=True)
        return result, ctx

    def _assert_quarantined_via_residual(self, result, name):
        self.assertFalse(result.get("staged"), f"R4: {name} staged")
        self.assertTrue(result.get("quarantined"), f"R4: {name} not "
                        "quarantined")
        self.assertEqual(result.get("check"), "residual_pii", result)
        self.assertTrue(quarantine.is_quarantined())
        with mock.patch("builtins.print"):
            again = contribute.submit_contribution(["sess_complete"],
                                                   confirmed=True)
        self.assertTrue(again.get("quarantine_locked"))
        self.assertFalse(again.get("staged", False))

    def test_r4_vulnerable_shapes_quarantine_end_to_end(self):
        # Exact round-4 demonstration, real flow, no mocks of the
        # scrub/audit/residual layers: every vulnerable shape must
        # quarantine (fail-closed) and lock sending.
        for name, diluted in _R4_VULNERABLE.items():
            result, ctx = self._submit_fresh(
                f"The deploy key was {diluted} in the config.",
                f"r4_{name}")
            with ctx:
                self._assert_quarantined_via_residual(result, name)

    def test_r4_new_dense_probes_never_stage_verbatim(self):
        # Invented dense-interleaving probes: wider pads, alternating
        # pads, sparser pads, 64-char cores, mixed densities. Each must
        # scrub to [SECRET] or quarantine via the residual — never
        # stage with the diluted run verbatim in the outbox.
        from initiatives.i08 import scrub as _scrub
        for name, diluted in _R4_NEW_PROBES.items():
            token = diluted
            scrubbed, _ = _scrub.scrub_text_v2(f"key {token} ok")
            views = contribute._residual_blob_views(token)
            self.assertTrue("[SECRET]" in scrubbed or views,
                            f"R4: new probe {name} evades every layer")
            result, ctx = self._submit_fresh(
                f"The deploy key was {token} in the config.",
                f"r4probe_{name}")
            with ctx:
                if result.get("quarantined"):
                    self.assertEqual(result.get("check"), "residual_pii",
                                     result)
                else:
                    self.assertTrue(result.get("staged"), result)
                    staged = Path(result["outbox"]).read_bytes()
                    self.assertNotIn(token.encode(), staged,
                                     f"R4: new probe {name} staged verbatim")
                    self.assertIn(b"[SECRET]", staged,
                                  f"R4: new probe {name} staged without "
                                  f"redaction")

    def test_r4_no_catastrophic_backtracking(self):
        # The dominance view adds no regexes; the existing residual
        # regexes must stay linear on pathological inputs, and the
        # full candidate extraction must stay well under its budget.
        import time
        t0 = time.perf_counter()
        contribute._RESIDUAL_DEPAD_RE.sub("", "a" * 7000 + "b" * 7000)
        contribute._RESIDUAL_TANDEM_RE.sub("", "ab" * 7000)
        self.assertLess(time.perf_counter() - t0, 1.0,
                        "R4: residual regexes too slow on 14KB input")
        big = ("lorem ipsum dolor sit amet " * 200 + " "
               + _R4_VULNERABLE["aa_every3"] + " " + ("x" * 30 + " ") * 100)
        t0 = time.perf_counter()
        contribute._sensitive_candidates(big)
        self.assertLess(time.perf_counter() - t0, 2.0,
                        "R4: _sensitive_candidates too slow on 14KB input")
