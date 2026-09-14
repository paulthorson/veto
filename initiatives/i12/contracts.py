#!/usr/bin/env python3
"""Initiative 12 — upstream contracts (contract-first parallel build).

Initiative 12's public tools depend on two upstream surfaces that other
initiatives own:

* **Initiative 04 — native fit decoder interface.** Mirrors the existing
  deterministic decoder API (``jd_decoder.decode_jd`` / ``jd_verdict``), which
  is the reference implementation until 04 ships a richer interface.
* **Initiative 10 — packaging/install interface.** Not yet implemented.
  This module pins the expected interface (installer entry points) so the
  public growth surface can build its "install path" against a stable
  contract now and wire to the real implementation when it lands.

Adapters resolve dynamically: if the real module is importable with the
pinned signature, it is used; otherwise the contract record documents the
unavailable capability. Public tools must degrade honestly ("install path
not yet available") rather than fake the capability.

Contract versions are pinned here and in tests; any signature drift fails
the contract tests loudly instead of silently breaking growth surfaces.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any

#: Pinned contract revisions. Bump only when the upstream initiative ships
#: the interface and this package's adapters are updated + re-tested.
DECODER_CONTRACT_VERSION = "1.0"   # mirrors jd_decoder API (pre-Initiative 04)
PACKAGING_CONTRACT_VERSION = "1.0"  # expectation for Initiative 10
MATCH_CONTRACT_VERSION = "1.0"     # mirrors match.score_job (Initiative 04 scorer)

#: Sentinel a packaging module must expose to be trusted. Bare module names
#: are not identity: the PyPI `installer` package (wheel installer) exposes
#: install() and would otherwise be mistaken for Veto's installer.
VETO_PACKAGING_SENTINEL = "VETO_PACKAGING_CONTRACT"

#: Keys :func:`match.score_job` really returns. Pinned 2026-09-13 against
#: match.py: {"score", "reasons", "matched", "missing", "veto",
#: "veto_reason", "components"}. There is no "factors", "vetoed", or
#: "veto_reasons" key — readers must use these names or fail loudly.
MATCH_EXPECTED_KEYS: tuple[str, ...] = (
    "score",
    "reasons",
    "matched",
    "missing",
    "veto",
    "veto_reason",
    "components",
)


class MatchContractViolation(RuntimeError):
    """The scorer's return shape drifted from the pinned contract.

    Raised instead of degrading to silent empty output: a contract
    violation must be loud, never an empty explanation presented
    as complete.
    """


def check_match_result(result: Any) -> dict[str, Any]:
    """Validate a :func:`match.score_job` result against the pinned contract.

    Returns the result dict unchanged when every expected key is present;
    raises :class:`MatchContractViolation` otherwise. Callers convert this
    into an honest unavailable envelope — never into defaulted empties.
    """
    if not isinstance(result, dict):
        raise MatchContractViolation(
            f"score_job returned {type(result).__name__}, not a dict "
            f"(contract {MATCH_CONTRACT_VERSION})"
        )
    missing = [k for k in MATCH_EXPECTED_KEYS if k not in result]
    if missing:
        raise MatchContractViolation(
            f"score_job result missing keys {missing} "
            f"(contract {MATCH_CONTRACT_VERSION}); refusing to render "
            "a partial explanation as complete"
        )
    return result


@dataclass(frozen=True)
class CapabilityStatus:
    """Resolution state of one contracted capability."""

    name: str
    contract_version: str
    available: bool
    provider: str
    note: str = ""


def decoder_capabilities() -> list[CapabilityStatus]:
    """Resolve the Initiative 04 decoder contract against the runtime."""
    caps: list[CapabilityStatus] = []
    try:
        mod = importlib.import_module("jd_decoder")
    except ImportError as exc:  # pragma: no cover - importable in this repo
        # Consistent shape with the success path: one record per contracted
        # function, all marked unavailable, so callers can iterate blindly.
        return [
            CapabilityStatus(
                name=fname,
                contract_version=DECODER_CONTRACT_VERSION,
                available=False,
                provider="jd_decoder",
                note=f"import failed: {exc}",
            )
            for fname in ("decode_jd", "jd_verdict")
        ]
    for fname in ("decode_jd", "jd_verdict"):
        fn = getattr(mod, fname, None)
        available = callable(fn)
        note = ""
        if available:
            try:
                params = list(inspect.signature(fn).parameters)
                # Only the "text" convention is accepted: jd_demo calls
                # decode_jd(text) unconditionally, so an "args"-style
                # (argparse-Namespace) decoder would crash inside the
                # decoder. Anything else is signature drift: the
                # capability is marked unavailable rather than called blind.
                if not params or params[0] != "text":
                    note = f"signature drift: {params}"
                    available = False
            except (TypeError, ValueError):
                pass
        caps.append(
            CapabilityStatus(
                name=fname,
                contract_version=DECODER_CONTRACT_VERSION,
                available=available,
                provider="jd_decoder",
                note=note,
            )
        )
    return caps


#: Expected Initiative 10 packaging interface. Each entry is
#: (function name, short description). Adapters raise ``PackagingUnavailable``
#: until Initiative 10 ships a module satisfying this contract.
PACKAGING_EXPECTED_API: tuple[tuple[str, str], ...] = (
    ("install", "One-command install: env checks, dependency setup, guided onboarding"),
    ("uninstall", "Clean uninstall path with data-deletion confirmation"),
    ("migrate", "Versioned local-data migration with forward/rollback"),
    ("backup", "Encrypted export with integrity check"),
    ("restore", "Selective restore with checksum verification"),
    ("check_environment", "Pre-install environment checks returning a report"),
)


class PackagingUnavailable(RuntimeError):
    """Raised when a packaging/install capability is requested before
    Initiative 10 ships. Never faked; callers must degrade honestly."""


def packaging_capabilities() -> list[CapabilityStatus]:
    """Resolve the Initiative 10 packaging contract against the runtime.

    Today this always reports unavailable — which is the honest answer.
    When Initiative 10 lands, it must expose the ``VETO_PACKAGING_SENTINEL``
    attribute (equal to ``PACKAGING_CONTRACT_VERSION``); bare module names
    are never trusted, because unrelated packages (e.g. PyPI's ``installer``)
    expose same-named callables that must not be mistaken for Veto's.
    """
    candidates = ("packaging", "installer", "veto_install")
    for modname in candidates:
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        if getattr(mod, VETO_PACKAGING_SENTINEL, None) != PACKAGING_CONTRACT_VERSION:
            # Importable but not Veto's packaging surface: do not trust it.
            # (The classic trap is PyPI's `installer` wheel-installer lib.)
            continue
        resolved: list[CapabilityStatus] = []
        for fname, _desc in PACKAGING_EXPECTED_API:
            fn = getattr(mod, fname, None)
            resolved.append(
                CapabilityStatus(
                    name=fname,
                    contract_version=PACKAGING_CONTRACT_VERSION,
                    available=callable(fn),
                    provider=modname,
                )
            )
        return resolved
    return [
        CapabilityStatus(
            name=fname,
            contract_version=PACKAGING_CONTRACT_VERSION,
            available=False,
            provider="initiative-10 (not yet shipped)",
            note="Install path contract pinned; implementation pending Initiative 10.",
        )
        for fname, _ in PACKAGING_EXPECTED_API
    ]


def require_packaging_capability(name: str) -> Any:
    """Return the real packaging callable, or raise PackagingUnavailable."""
    for cap in packaging_capabilities():
        if cap.name == name:
            if not cap.available:
                raise PackagingUnavailable(
                    f"Packaging capability '{name}' is not yet available: "
                    f"{cap.note or cap.provider}. Initiative 10 has not shipped. "
                    "Degrade honestly — do not fake an install path."
                )
            mod = importlib.import_module(cap.provider)
            return getattr(mod, name)
    raise PackagingUnavailable(f"Unknown packaging capability '{name}'.")


def contract_report() -> dict[str, Any]:
    """Machine-readable contract status for dashboards and tests."""
    return {
        "decoder_contract_version": DECODER_CONTRACT_VERSION,
        "packaging_contract_version": PACKAGING_CONTRACT_VERSION,
        "match_contract_version": MATCH_CONTRACT_VERSION,
        "decoder": [c.__dict__ for c in decoder_capabilities()],
        "packaging": [c.__dict__ for c in packaging_capabilities()],
    }


def install_path_status() -> dict[str, Any]:
    """Honest public-facing install-path status for growth surfaces.

    Returns a dict the web UI / CLI can render directly: available or not,
    and what to show instead. Never fabricates an installer.
    """
    caps = packaging_capabilities()
    available = all(c.available for c in caps)
    return {
        "available": available,
        "contract_version": PACKAGING_CONTRACT_VERSION,
        "message": (
            "One-command install is available."
            if available
            else (
                "The one-command installer is not released yet. "
                "Current install path: clone the repository and follow the "
                "README quickstart. Run `python -m initiatives.i12 changelog` "
                "to see when packaging ships."
            )
        ),
        "capabilities": [c.__dict__ for c in caps],
    }
