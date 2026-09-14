"""Initiative 06 WS-E: optional voice input — privacy contract.

Text is the default modality. Voice is opt-in for existing exercises
only, local or connected behind an explicit privacy choice. Connected
mode must disclose the exact service, payload, and retention before
any transmission, and transcription fails closed until consent.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from initiatives.i06 import voice  # noqa: E402


def _fake_provider(audio):
    return "transcript of audio"


class VoiceCase(unittest.TestCase):
    def setUp(self):
        voice.reset()
        self.addCleanup(voice.reset)


class TestVoiceDefaults(VoiceCase):
    def test_text_is_default_modality(self):
        self.assertEqual(voice.INPUT_MODALITY_DEFAULT, "text")
        status = voice.describe_status()
        self.assertEqual(status["default_modality"], "text")
        self.assertEqual(status["mode"], "disabled")
        self.assertFalse(status["transcription_allowed"])

    def test_transcribe_fails_closed_by_default(self):
        with self.assertRaises(voice.VoiceBlocked):
            voice.transcribe("some audio")

    def test_no_pending_disclosure_by_default(self):
        self.assertIsNone(voice.get_pending_disclosure())


class TestLocalMode(VoiceCase):
    def test_enable_local_allows_transcription(self):
        status = voice.enable_local(_fake_provider)
        self.assertEqual(status["mode"], "local")
        self.assertTrue(status["transcription_allowed"])
        out = voice.transcribe("audio-bytes")
        self.assertEqual(out["transcript"], "transcript of audio")

    def test_local_disclosure_says_nothing_leaves_device(self):
        status = voice.enable_local(_fake_provider)
        d = status["disclosure"]
        self.assertIn("local", d["destination"].lower())
        self.assertIn("no network", d["retention"].lower())

    def test_local_requires_provider(self):
        with self.assertRaises(ValueError):
            voice.enable_local(None)  # type: ignore[arg-type]


class TestConnectedMode(VoiceCase):
    def test_propose_returns_full_disclosure(self):
        d = voice.propose_connected(
            "Acme STT (user's own endpoint)", _fake_provider,
            "Audio retained 30 days for abuse detection, then deleted; "
            "not used for training.")
        self.assertEqual(d["service_name"], "Acme STT (user's own endpoint)")
        self.assertIn("Audio", d["what_is_sent"])
        self.assertIn("30 days", d["retention"])
        self.assertIn("transmitted", d["destination"])

    def test_transcribe_blocked_until_consent(self):
        voice.propose_connected("Acme STT", _fake_provider,
                                "Retained 30 days.")
        with self.assertRaises(voice.VoiceBlocked):
            voice.transcribe("audio")

    def test_consent_requires_pending_disclosure(self):
        with self.assertRaises(voice.VoiceBlocked):
            voice.grant_consent()

    def test_consent_then_transcribe(self):
        voice.propose_connected("Acme STT", _fake_provider,
                                "Retained 30 days.")
        pending = voice.get_pending_disclosure()
        self.assertIsNotNone(pending)
        status = voice.grant_consent()
        self.assertTrue(status["transcription_allowed"])
        out = voice.transcribe("audio")
        self.assertEqual(out["service_name"], "Acme STT")

    def test_propose_requires_all_fields(self):
        with self.assertRaises(ValueError):
            voice.propose_connected("", _fake_provider, "retained")
        with self.assertRaises(ValueError):
            voice.propose_connected("Acme", None, "retained")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            voice.propose_connected("Acme", _fake_provider, "  ")

    def test_withdraw_consent_disables(self):
        voice.propose_connected("Acme STT", _fake_provider,
                                "Retained 30 days.")
        voice.grant_consent()
        voice.withdraw_consent()
        with self.assertRaises(voice.VoiceBlocked):
            voice.transcribe("audio")


if __name__ == "__main__":
    unittest.main()
