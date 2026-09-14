#!/usr/bin/env python3
"""Initiative 06 — optional voice input: privacy contract layer.

Text is the default input modality everywhere in the interview lab.
Voice input is strictly opt-in and works ONLY for existing exercises
(practice answers), behind an explicit privacy choice.

This module is the policy boundary for voice. It holds:

* the modality default ("text"),
* the configured voice mode: "disabled" | "local" | "connected",
* the two-step consent flow for connected mode,
* the disclosure contract: before ANY audio is transmitted, the user
  must be shown the exact configured service name, what is sent, and
  the retention implications.

Fail-closed design:
* Default state is "disabled": transcribe() always refuses.
* "local" mode requires the user to supply their own transcription
  provider (a callable). Nothing in this module reads microphones,
  files, or devices; the provider receives only what the caller
  passes, and the disclosure for local mode states that no audio
  leaves this machine (per the provider the user chose).
* "connected" mode additionally requires a service name and a
  retention statement, and transcription is refused until the user
  has seen the full disclosure AND granted explicit consent via
  grant_consent(). Consent cannot be granted without a pending
  disclosure.

Stdlib only, deterministic, no network. There is deliberately NO
network code here: actual speech services are user-configured and
passed in as callables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: Default input modality across all lab exercises.
INPUT_MODALITY_DEFAULT = "text"

MODES = ("disabled", "local", "connected")


class VoiceBlocked(Exception):
    """Raised when voice transcription is attempted without a valid
    configuration and (for connected mode) explicit consent."""


@dataclass
class Disclosure:
    """What the user must see BEFORE any audio is transmitted."""
    service_name: str
    what_is_sent: str
    retention: str
    destination: str

    def as_dict(self) -> dict[str, str]:
        return {
            "service_name": self.service_name,
            "what_is_sent": self.what_is_sent,
            "retention": self.retention,
            "destination": self.destination,
        }


@dataclass
class _State:
    mode: str = "disabled"
    provider: Callable[[Any], str] | None = None
    disclosure: Disclosure | None = None
    consent_granted: bool = False


_state = _State()


def describe_status() -> dict[str, Any]:
    """Current voice configuration and whether transcription is allowed."""
    allowed = _state.mode == "local" and _state.provider is not None
    allowed = allowed or (
        _state.mode == "connected"
        and _state.provider is not None
        and _state.consent_granted
    )
    reason = ""
    if not allowed:
        if _state.mode == "disabled":
            reason = ("Voice input is off. Text is the default modality. "
                      "Enable local or connected voice explicitly to use it.")
        elif _state.provider is None:
            reason = ("No transcription provider configured. Voice input "
                      "requires a user-configured speech service.")
        elif _state.mode == "connected" and not _state.consent_granted:
            reason = ("Connected voice needs your explicit consent after "
                      "you review the disclosure (service, payload, "
                      "retention). See get_pending_disclosure().")
    return {
        "default_modality": INPUT_MODALITY_DEFAULT,
        "mode": _state.mode,
        "transcription_allowed": allowed,
        "consent_granted": _state.consent_granted,
        "disclosure": (_state.disclosure.as_dict()
                       if _state.disclosure else None),
        "reason": reason,
    }


def reset() -> None:
    """Return voice to the default disabled state (tests + opt-out)."""
    _state.mode = "disabled"
    _state.provider = None
    _state.disclosure = None
    _state.consent_granted = False


def enable_local(provider: Callable[[Any], str]) -> dict[str, Any]:
    """Enable local voice processing with a user-supplied provider.

    The provider callable receives the audio payload the caller passes
    and returns transcript text. Audio never leaves this machine —
    that is the disclosure contract for local mode (the provider is
    the user's own configured service, and this module performs no
    network I/O at all).
    """
    if not callable(provider):
        raise ValueError("Local voice needs a user-configured "
                         "transcription provider callable.")
    _state.mode = "local"
    _state.provider = provider
    _state.disclosure = Disclosure(
        service_name="user-configured local provider",
        what_is_sent="Audio you choose to transcribe, processed by the "
                     "provider callable you supplied.",
        retention="Audio stays on this machine. This module performs no "
                  "network I/O; retention is governed entirely by the "
                  "provider you configured.",
        destination="local only — nothing is transmitted off-device by "
                    "this module.",
    )
    _state.consent_granted = False
    return describe_status()


def propose_connected(service_name: str,
                      provider: Callable[[Any], str],
                      retention: str) -> dict[str, str]:
    """Propose connected voice mode. Returns the disclosure the user
    MUST review before anything is transmitted.

    All three arguments are required: the exact configured service
    name, the transcription provider callable, and a retention
    statement describing what the service does with the audio.
    Transcription stays blocked until grant_consent() is called.
    """
    if not service_name or not service_name.strip():
        raise ValueError("Connected voice requires the exact configured "
                         "service name (e.g. the STT endpoint you set up).")
    if not callable(provider):
        raise ValueError("Connected voice requires a user-configured "
                         "transcription provider callable.")
    if not retention or not retention.strip():
        raise ValueError("Connected voice requires a retention statement "
                         "describing what the service does with audio.")
    _state.mode = "connected"
    _state.provider = provider
    _state.consent_granted = False
    _state.disclosure = Disclosure(
        service_name=service_name.strip(),
        what_is_sent="Audio you choose to transcribe, plus the minimum "
                     "request metadata the service needs (format, "
                     "language hint). No other lab data is sent.",
        retention=retention.strip(),
        destination=f"transmitted to {service_name.strip()} over the "
                    f"network by your configured provider.",
    )
    return _state.disclosure.as_dict()


def get_pending_disclosure() -> dict[str, str] | None:
    """The disclosure awaiting consent, if any."""
    if _state.mode == "connected" and not _state.consent_granted:
        return _state.disclosure.as_dict() if _state.disclosure else None
    return None


def grant_consent() -> dict[str, Any]:
    """Record the user's explicit consent to the pending disclosure.

    Raises if there is no pending connected-mode disclosure — consent
    cannot be granted (or assumed) without one.
    """
    if _state.mode != "connected" or _state.disclosure is None:
        raise VoiceBlocked("No pending connected-voice disclosure to "
                           "consent to. Call propose_connected() first and "
                           "review the disclosure.")
    _state.consent_granted = True
    return describe_status()


def withdraw_consent() -> dict[str, Any]:
    """Revoke consent (and disable voice entirely)."""
    reset()
    return describe_status()


def transcribe(audio: Any) -> dict[str, Any]:
    """Transcribe audio through the configured provider.

    FAILS CLOSED: raises VoiceBlocked unless the mode is "local" with
    a provider, or "connected" with a provider AND explicit consent to
    the reviewed disclosure. Never transmits without consent.
    """
    status = describe_status()
    if not status["transcription_allowed"]:
        raise VoiceBlocked(status["reason"])
    assert _state.provider is not None  # guaranteed by status
    text = _state.provider(audio)
    return {
        "transcript": text,
        "mode": _state.mode,
        "service_name": (_state.disclosure.service_name
                         if _state.disclosure else ""),
    }
