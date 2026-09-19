#!/usr/bin/env python3
"""Phase 2: browser-automation apply hook (Playwright, sync API).

This module powers the optional browser auto-fill mode of the Veto
MCP server. It opens an application's ``apply_url`` in headless Chromium,
extracts the visible form fields, fills them from a user ``profile`` dict,
attaches the resume file, and screenshots the result.

Safety contract (fill-only):
  * ``apply_via_browser`` NEVER submits. This module fills the form
    from the profile, uploads the resume, screenshots the completed
    form, and stops. There is no submit code path here: no click on a
    submit control, no programmatic form submission, no application
    POST, no approval-to-submit machinery. The submit click is always the
    user's own, in a browser they control. ``confirm=True`` and
    ``approval_id`` are accepted for signature compatibility and do
    nothing.
  * After filling, the handoff is: with ``headless=False`` the browser
    stays open on the completed form and the call WAITS until the user
    closes it — the user reviews every field and clicks submit
    themselves. Headless runs close the browser after the preview
    screenshot — the report carries the screenshot path and the apply
    URL so the user can open it and finish by hand. In both modes the
    run returns a fill report, never a submission receipt.
  * Screenshots of filled forms contain PII. Retention policy: at most
    ``SCREENSHOT_MAX_COUNT`` recent screenshots are kept, and any older
    than ``SCREENSHOT_MAX_AGE_DAYS`` are deleted. ``cleanup_screenshots``
    enforces this (it also runs best-effort at the start of every
    ``apply_via_browser`` run).
  * Consent banners are dismissed privacy-safely: reject/close buttons
    only — this module NEVER clicks "Accept" on the user's behalf.
    Every button-text read that authorizes a consent click runs in the
    CDP isolated world, never the page's forgeable main world — hostile
    page JS overriding ``innerText`` can otherwise forge "Reject all"
    over an "Accept all" button. Without an unforged channel no
    consent button is clicked at all.
  * Chromium is launched with ``--no-sandbox`` (m6, disclosed here, not
    hidden in the launch args): this tool commonly runs in
    containers/VMs (Docker, headless CI/dev VMs) where the kernel user
    namespaces the Chromium sandbox requires are unavailable — without
    the flag Chromium refuses to start at all. Trade-off: this weakens
    the browser's own process isolation, so a compromised renderer has
    fewer containment boundaries. Accepted because the browser only
    ever visits the user's chosen apply URL, runs with no stored
    credentials, and never types passwords or loads a saved login
    session; the sandbox would add little while breaking the
    tool in its normal environments.
  * No credentials are stored or used here. Saved login sessions are not
    supported: this module never types passwords and never loads a saved
    session.
  * Automated applying may violate a job board's Terms of Service and can
    trigger anti-bot measures (CAPTCHAs, IP throttles, account limits).
    Use at a human pace and keep a person in the loop.

Profile schema (all keys optional)::

    {
        "full_name": "Ada Lovelace",   # split into first/last when asked
        "first_name": "Ada",           # explicit overrides win over split
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "+1 555-0100",
        "location": "New York, NY",
        "linkedin_url": "https://www.linkedin.com/in/adalovelace",
        "website": "https://adalovelace.dev",
        "cover_letter": "Dear hiring team, ...",
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from providers import session_rescue
from providers._common import VETO_USER_AGENT

log = logging.getLogger("veto-mcp.browser")

BASE_DIR = Path(__file__).resolve().parent
SCREENSHOTS_DIR = BASE_DIR / "screenshots"  # gitignored: filled-form PII

#: Screenshot retention policy (m2): screenshots of filled application
#: forms contain PII (name, email, phone, address). Keep at most this
#: many recent screenshots; delete any older than this many days.
#: ``cleanup_screenshots`` enforces it (best-effort), and it runs at the
#: start of every ``apply_via_browser`` run.
SCREENSHOT_MAX_COUNT = 50
SCREENSHOT_MAX_AGE_DAYS = 30


def cleanup_screenshots(
    max_count: int = SCREENSHOT_MAX_COUNT,
    max_age_days: int = SCREENSHOT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Enforce the screenshot retention policy.

    Deletes ``apply-*.png`` files inside ``SCREENSHOTS_DIR`` that are
    older than ``max_age_days``, then trims the remainder to the
    ``max_count`` most recent. Only files matching ``apply-*.png`` are
    ever touched — nothing else in the directory is deleted.

    Best-effort: per-file failures are collected in the returned dict,
    never raised.

    Returns:
        ``{"deleted": [names], "kept": int, "errors": [messages]}``.
    """
    deleted: list[str] = []
    errors: list[str] = []
    try:
        files = [
            p for p in SCREENSHOTS_DIR.glob("apply-*.png") if p.is_file()
        ]
    except OSError as exc:
        return {"deleted": deleted, "kept": 0,
                "errors": [f"cannot list screenshots dir: {exc}"]}
    cutoff = time.time() - max_age_days * 86400
    survivors: list[tuple[float, Path]] = []
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if mtime < cutoff:
            try:
                path.unlink()
                deleted.append(path.name)
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
        else:
            survivors.append((mtime, path))
    survivors.sort(key=lambda item: item[0])  # oldest first
    while len(survivors) > max_count:
        _, path = survivors.pop(0)
        try:
            path.unlink()
            deleted.append(path.name)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return {"deleted": deleted, "kept": len(survivors), "errors": errors}

# Boards with a generic login page would be listed here with their URL.
# None remain: saved-login support was removed, so every entry is None.
# Greenhouse/Lever/Ashby are per-company (no generic login page).
BOARD_LOGIN_URLS: dict[str, str | None] = {
    "greenhouse": None,
    "lever": None,
    "ashby": None,
    "adzuna": None,
}

# ---------------------------------------------------------------------------
# Field detection
# ---------------------------------------------------------------------------

# (profile_key, regex patterns matched against name/id/placeholder/label)
_FIELD_PATTERNS: list[tuple[str, list[str]]] = [
    ("email", [r"\be-?mail\b"]),
    ("phone", [r"\bphone\b", r"\btel\b", r"\bmobile\b", r"\bcell\b"]),
    ("first_name", [r"first[\s_-]?name", r"\bgiven\b", r"\bfname\b"]),
    ("last_name", [r"last[\s_-]?name", r"\bsurname\b", r"\bfamily\b", r"\blname\b"]),
    ("full_name", [r"full[\s_-]?name", r"\byour name\b", r"applicant[\s_-]?name"]),
    ("location", [r"\blocation\b", r"\bcity\b", r"\baddress\b"]),
    ("linkedin_url", [r"linkedin"]),
    ("website", [r"\bwebsite\b", r"\bportfolio\b", r"(?<!linked)\burl\b"]),
    ("cover_letter", [r"cover[\s_-]?letter"]),
]

_SKIP_TYPES = {"hidden", "submit", "button", "image", "reset"}


def _field_haystack(field: dict[str, Any]) -> str:
    return " ".join(
        str(field.get(k, "") or "")
        for k in ("name", "id", "placeholder", "label", "aria")
    ).lower()


def _match_profile_key(field: dict[str, Any]) -> str | None:
    haystack = _field_haystack(field)
    for key, patterns in _FIELD_PATTERNS:
        for pat in patterns:
            if re.search(pat, haystack):
                return key
    return None


def extract_form_fields(page: Any) -> list[dict[str, Any]]:
    """Return visible form fields as {name, label, type, selector}.

    Args:
        page: A Playwright ``Page`` (or any object exposing ``evaluate``).

    Only visible, non-hidden inputs/textareas/selects are returned. Each
    entry carries a best-effort CSS selector for human review.
    """
    raw = page.evaluate(
        """() => {
      const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
      return [...document.querySelectorAll('input, textarea, select')].map((el, i) => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        let label = '';
        if (el.id) {
          const lab = document.querySelector('label[for="' + esc(el.id) + '"]');
          if (lab) label = (lab.innerText || '').trim().slice(0, 160);
        }
        if (!label) {
          const wrap = el.closest('label');
          if (wrap) label = (wrap.innerText || '').trim().slice(0, 160);
        }
        return {
          index: i,
          tag: el.tagName.toLowerCase(),
          type: (el.type || '').toLowerCase(),
          name: el.name || '',
          id: el.id || '',
          placeholder: el.placeholder || '',
          label,
          aria: el.getAttribute('aria-label') || '',
          visible: !!(rect.width && rect.height)
            && style.visibility !== 'hidden' && style.display !== 'none',
        };
      });
    }"""
    )
    fields: list[dict[str, Any]] = []
    for f in raw:
        if not f.get("visible"):
            continue
        if f.get("type") in _SKIP_TYPES:
            continue
        selector = _selector_for(f)
        fields.append(
            {
                "name": f.get("name") or f.get("id") or f.get("placeholder") or f.get("label") or f"field_{f['index']}",
                "label": f.get("label") or f.get("placeholder") or f.get("aria") or f.get("name"),
                "type": f.get("type") or f.get("tag"),
                "selector": selector,
                "_index": f["index"],
                "_tag": f.get("tag"),
                "_input_type": f.get("type"),
            }
        )
    return fields


def _selector_for(f: dict[str, Any]) -> str:
    """Best-effort CSS selector for a detected field (human review only)."""
    if f.get("id"):
        return f"#{f['id']}"
    if f.get("name"):
        return f"{f.get('tag', 'input')}[name=\"{f['name']}\"]"
    return f"{f.get('tag', 'input')}:nth-of-type({f['index'] + 1})"


# ---------------------------------------------------------------------------
# Form filling
# ---------------------------------------------------------------------------


class _FilledForm(dict):
    """``field_name -> value`` filled at fill time, plus identity sidecars.

    A plain ``dict[str, str]`` in every other respect (JSON-serializable,
    ``.items()``, equality with plain dicts), so existing consumers
    (the result dict) and test doubles are unaffected.
    For ``<select>`` fields the dict value is the profile value used as
    the match needle; the matched option's label and VALUE ATTRIBUTE
    (the actual transmitted bytes) live in ``select_bindings``.

    Sidecars:

    * ``indices``: ``field_name ->`` fill-time positional index in
      ``querySelectorAll('input, textarea, select')`` order — the same
      order ``_field_locator`` fills by.
    * ``select_bindings``: ``field_name -> {"label", "value"}`` — the
      matched option's visible label and VALUE ATTRIBUTE (the actual
      transmitted bytes), captured in the same evaluate that matched
      the option.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.indices: dict[str, int] = {}
        self.select_bindings: dict[str, dict[str, str]] = {}


def _select_pair_json(label: str, value: str) -> str:
    """Canonical encoding of a select's bound (label, value-attribute) pair.

    Exact string equality on these canonical encodings compares the
    transmitted bytes AND the visible label. No containment, no
    sanitization, no hashing anywhere in the comparison.
    """
    return json.dumps(
        {"label": label, "value": value}, sort_keys=True, ensure_ascii=False
    )


def _split_name(profile: dict[str, Any]) -> tuple[str, str]:
    first = (profile.get("first_name") or "").strip()
    last = (profile.get("last_name") or "").strip()
    if first or last:
        return first, last
    full = (profile.get("full_name") or "").strip()
    parts = full.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return full, ""


def _field_locator(page: Any, field: dict[str, Any]) -> Any:
    """Locate a field positionally (robust against tricky selectors)."""
    return page.locator("input, textarea, select").nth(field["_index"])


def fill_application(
    page: Any, profile: dict[str, Any], resume_path: str | None
) -> dict[str, str]:
    """Fill detected fields from ``profile``; upload ``resume_path``.

    Returns a ``_FilledForm`` (a ``dict`` of ``field_name -> value``
    actually filled, plus fill-time element identity sidecars).
    Fields with no matching profile data are skipped. File inputs
    receive the resume when it exists on disk. Never touches submit
    buttons.
    """
    filled = _FilledForm()
    first_name, last_name = _split_name(profile)
    values: dict[str, str] = {
        "email": str(profile.get("email", "") or ""),
        "phone": str(profile.get("phone", "") or ""),
        "first_name": first_name,
        "last_name": last_name,
        "full_name": str(profile.get("full_name", "") or ""),
        "location": str(profile.get("location", "") or ""),
        "linkedin_url": str(profile.get("linkedin_url", "") or ""),
        "website": str(profile.get("website", "") or ""),
        "cover_letter": str(profile.get("cover_letter", "") or ""),
    }

    resume = Path(os.path.expanduser(resume_path)).expanduser() if resume_path else None
    resume_ok = bool(resume and resume.is_file())

    def _record(name: str, value: str, field: dict[str, Any]) -> None:
        filled[name] = value
        # The fill-time positional index is recorded alongside the
        # value (readback keys are page-controlled and cannot be
        # trusted).
        filled.indices[name] = field["_index"]

    for field in extract_form_fields(page):
        input_type = field["_input_type"]
        tag = field["_tag"]
        try:
            if input_type == "file":
                if resume_ok:
                    _field_locator(page, field).set_input_files(str(resume))
                    _record(field["name"], f"<resume: {resume.name}>", field)
                continue
            if tag == "select":
                key = _match_profile_key(field)
                value = values.get(key or "", "")
                matched = (
                    _select_matching_option(page, field, value) if value else None
                )
                if matched is not None:
                    _record(field["name"], value, field)
                    # S1: bind the matched option's transmitted bytes
                    # (value attribute) and visible label — captured in
                    # the same evaluate that matched the option. Unknown
                    # on old test doubles (bare-int shape): fall back to
                    # the profile value, which is exactly what an
                    # <option> without a value attribute transmits (the
                    # value IDL defaults to the text).
                    filled.select_bindings[field["name"]] = {
                        "label": (
                            matched["label"]
                            if matched["label"] is not None
                            else value
                        ),
                        "value": (
                            matched["value"]
                            if matched["value"] is not None
                            else value
                        ),
                    }
                continue
            if input_type in {"checkbox", "radio"}:
                continue  # Phase 2: leave attestations/EEO to the human

            key = _match_profile_key(field)
            if not key:
                continue
            # A lone "name" field gets the full name; split fields get parts.
            value = values.get(key, "")
            if key == "full_name" and not value and (first_name or last_name):
                value = f"{first_name} {last_name}".strip()
            if not value:
                continue
            _field_locator(page, field).fill(value)
            _record(field["name"], value, field)
        except Exception as exc:  # one bad field must not abort the run
            log.debug("Skipping field %r: %s", field.get("name"), exc)
    return filled


#: JS for _select_matching_option: find the option whose visible label
#: contains the needle and return its index, label, AND value attribute
#: in a single evaluate — so the fill-time binding captures exactly what
#: was matched, with no TOCTOU between "which option" and "what it
#: transmits".
_SELECT_MATCH_JS = """([fieldIndex, needle]) => {
  const el = [...document.querySelectorAll('input, textarea, select')][fieldIndex];
  if (!el || el.tagName.toLowerCase() !== 'select') return null;
  const n = needle.toLowerCase();
  const opts = [...el.options];
  const i = opts.findIndex(o => (o.text || '').toLowerCase().includes(n));
  if (i < 0) return null;
  const opt = opts[i];
  // The value attribute is the transmitted bytes (S1); the label is what
  // the user saw matched. Both are bound at fill time.
  return {
    index: i,
    label: (opt.text || '').trim(),
    value: opt.value == null ? '' : String(opt.value),
  };
}"""


def _select_matching_option(
    page: Any, field: dict[str, Any], value: str
) -> dict[str, Any] | None:
    """Select the dropdown option whose label contains ``value``.

    Returns ``{"index", "label", "value"}`` for the matched option, or
    None when nothing matched. ``value`` is the option's VALUE ATTRIBUTE
    — the actual transmitted bytes (S1, round 5): the fill-time binding
    captures THAT, never the profile value used as the match needle.
    ``label`` is the visible option text the user saw matched.

    Compatibility: old test doubles return a bare option index (int)
    from this evaluate; that shape is tolerated (label/value reported as
    None so the caller can fall back) — real pages always return the
    dict.

    MAJOR 2 (round 6): the match runs through the unforged readback
    channel (CDP isolated world on real pages), not the page's main
    world — a forged fill-time match would otherwise become a forged
    baseline.
    """
    matched = _evaluate_unforged(page, _SELECT_MATCH_JS, [field["_index"], value])
    if matched is _READBACK_UNAVAILABLE:
        return None
    index: int | None = None
    label: str | None = None
    opt_value: str | None = None
    if isinstance(matched, dict):
        raw_index = matched.get("index")
        index = raw_index if isinstance(raw_index, int) else None
        raw_label = matched.get("label")
        label = raw_label if isinstance(raw_label, str) else None
        raw_value = matched.get("value")
        opt_value = raw_value if isinstance(raw_value, str) else None
    elif isinstance(matched, int) and not isinstance(matched, bool):
        # Legacy test-double shape: bare option index, label/value unknown.
        index = matched
    if index is None or index < 0:
        return None
    _field_locator(page, field).select_option(index=index)
    return {"index": index, "label": label, "value": opt_value}


# ---------------------------------------------------------------------------
# Consent banners, screenshots
# ---------------------------------------------------------------------------

#: Button text that REJECTS optional cookies/tracking (privacy-safe), or
#: merely dismisses the banner without choosing. Anything accept-like
#: ("Accept all", "Got it", "Allow all", ...) is deliberately absent:
#: this function must NEVER opt the user into tracking.
_REJECT_TEXT_RE = re.compile(
    r"^(reject( all)?|decline( all)?|deny( all)?|no thanks|"
    r"necessary only|essentials? only|essential only)$",
    re.IGNORECASE,
)
_DISMISS_TEXT_RE = re.compile(r"^(close|dismiss)$", re.IGNORECASE)

#: Selectors whose ONLY action is rejecting optional cookies/tracking.
#: (The old accept selectors were removed deliberately — see above.)
_CONSENT_REJECT_SELECTORS = [
    "#onetrust-reject-all-handler",  # OneTrust "Reject All"
    "button#reject-cookies",  # generic reject-cookie buttons
    "#reject-cookies",
]

#: JS to enumerate visible buttons (index/text/aria) for Python-side
#: classification — the accept/reject decision lives in Python so it is
#: unit-testable, not buried in page JS.
#: R7-C: runs in the CDP isolated world via ``_evaluate_unforged``,
#: NEVER the page's main world — hostile page JS overriding
#: ``innerText`` could otherwise forge "Reject all" over an "Accept
#: all" button, defeating the click-time check and steering the tool
#: into an opt-in click the logs would misreport as a privacy-safe
#: reject.
_CONSENT_BUTTONS_JS = """() => {
  // veto:consent-enumerate
  return [...document.querySelectorAll('button')].map((b, i) => {
    const r = b.getBoundingClientRect();
    return {
      index: i,
      text: (b.innerText || b.textContent || '').trim().slice(0, 80),
      aria: b.getAttribute('aria-label') || '',
      visible: !!(r.width && r.height),
    };
  });
}"""

_CONSENT_CLICK_JS = """([i, expectedText]) => {
  // veto:consent-click
  // N4 (round 4): re-verify the button's text INSIDE the click
  // evaluate — the DOM may have mutated between enumeration and this
  // click (racing page JS), shifting the positional index onto an
  // accept-like button. Only click when the text still matches the
  // reject/dismiss-classified text seen at enumeration time; anything
  // else (missing button, changed text) is left alone (fail closed)
  // and reported to the caller.
  // R7-C: runs in the CDP isolated world — the page's main world can
  // forge innerText, so a main-world re-verification would be theater.
  const b = [...document.querySelectorAll('button')][i];
  if (!b) return 'missing';
  const txt = (b.innerText || b.textContent || '').trim().slice(0, 80);
  if (txt !== expectedText) return 'text-changed';
  b.click();
  return 'clicked';
}"""

#: R7-C: the known-selector path reads the button's text AND clicks it
#: through the isolated world too. The probe returns text/aria for
#: Python-side classification; the click JS atomically re-verifies the
#: text before clicking (N4), so a forged main-world ``innerText`` can
#: neither misclassify the button nor race the click.
_CONSENT_ID_PROBE_JS = """([sel]) => {
  // veto:consent-id-probe
  const el = document.querySelector(sel);
  if (!el) return {found: false, visible: false, text: '', aria: ''};
  const r = el.getBoundingClientRect();
  return {
    found: true,
    visible: !!(r.width && r.height),
    text: ((el.innerText || el.textContent) || '').trim().slice(0, 80),
    aria: el.getAttribute('aria-label') || '',
  };
}"""

_CONSENT_ID_CLICK_JS = """([sel, expectedText]) => {
  // veto:consent-id-click
  // Atomic verify+click (N4): only clicks when the button's text
  // STILL classifies as the expected reject/dismiss text — a DOM race
  // between the probe and the click cannot shift the click onto an
  // accept-like button.
  const el = document.querySelector(sel);
  if (!el) return 'missing';
  const r = el.getBoundingClientRect();
  if (!(r.width && r.height)) return 'hidden';
  const txt = ((el.innerText || el.textContent) || '').trim().slice(0, 80);
  if (txt !== expectedText) return 'text-changed';
  el.click();
  return 'clicked';
}"""


class _ConsentReadbackDead(Exception):
    """The unforged consent-readback channel is unavailable: no consent
    button may be clicked on forgeable readback. The run continues
    without banner dismissal (fail closed on the clicks, not the run)."""


def _consent_button_kind(text: str, aria_label: str = "") -> str | None:
    """Classify a visible button for privacy-safe consent handling.

    Returns ``"reject"`` (rejects optional cookies/tracking),
    ``"dismiss"`` (closes the banner without opting in), or ``None``
    (leave it alone). Notably, anything accept-like returns ``None`` —
    this function NEVER opts the user into tracking.
    """
    t = (text or "").strip().lower()
    aria = (aria_label or "").strip().lower()
    if _REJECT_TEXT_RE.match(t):
        return "reject"
    if _DISMISS_TEXT_RE.match(t):
        return "dismiss"
    # A bare "×" is only a dismiss when its accessible name says so.
    if t in {"\u00d7", "x"} and re.search(r"close|dismiss", aria):
        return "dismiss"
    return None


def dismiss_consent_banners(page: Any) -> int:
    """Privacy-safe consent-banner dismissal. Returns buttons clicked.

    Clicks REJECT / dismiss buttons only — NEVER "Accept". Consent
    banners exist to obtain the user's agreement to tracking; a tool
    that auto-accepts on the user's behalf would opt them into tracking
    they never agreed to.

    TOCTOU defense (N4): the generic-button path enumerates buttons by
    positional index, but re-reads each button's text INSIDE the click
    evaluate — a button whose text changed between enumeration and
    click (racing DOM mutation) is left alone, fail closed. The
    known-selector path verifies and clicks atomically in a single
    evaluate for the same reason.

    Trust boundary (R7-C): EVERY text read that authorizes a click —
    the known-selector probe, the generic enumeration, and both
    click-time re-verifications — runs in the CDP isolated world via
    ``_evaluate_unforged``, never through the page's main world. Under
    the round-6 threat model hostile page JS can override ``innerText``,
    so a main-world readback could forge "Reject all" over an "Accept
    all" button: the forged read would both defeat the N4 check and
    steer the tool into an opt-in click the logs would misreport as a
    privacy-safe reject. When the isolated world cannot be established
    (non-Chromium page, broken CDP channel, or a page object with no
    JS bridge at all), NO consent button is clicked — the run continues
    without banner dismissal (fail closed on the clicks, not on the
    run). The accept/reject CLASSIFICATION itself stays in Python
    (``_consent_button_kind``) so it remains unit-testable; only the
    DOM reads are isolated.
    """

    def _unforged(js: str, arg: Any) -> Any:
        """One isolated-world consent readback.

        Returns the readback value — which may itself be None on a
        transport or JS failure, in which case the caller skips that
        button (fail closed). Raises ``_ConsentReadbackDead`` when the
        unforged CHANNEL itself is unavailable: the caller then skips
        ALL consent clicking, never degrading to the forgeable main
        world.
        """
        try:
            res = _evaluate_unforged(page, js, arg)
        except RuntimeError as exc:
            # Real page but no CDP channel (non-Chromium) or a broken
            # isolated world: button text cannot be verified unforgably.
            raise _ConsentReadbackDead(str(exc)) from exc
        if res is _READBACK_UNAVAILABLE:
            # No JS bridge on the page object at all (incomplete test
            # double only — real Playwright pages always expose it).
            raise _ConsentReadbackDead("page object has no JS bridge")
        return res

    clicked = 0
    try:
        for sel in _CONSENT_REJECT_SELECTORS:
            # m1: verify the button's TEXT before clicking an
            # ID-selected button — never trust the selector alone. A
            # hostile page could repurpose a known ID (e.g. render
            # "Accept all" behind #onetrust-reject-all-handler). R7-C:
            # the text read AND the click both run in the isolated
            # world — a forged main-world innerText can neither
            # misclassify the button nor race the click. Click only
            # when the visible text classifies as reject/dismiss via
            # _consent_button_kind; anything else — including text we
            # failed to read — is left alone (fail closed) and logged
            # loudly.
            try:
                probe = _unforged(_CONSENT_ID_PROBE_JS, sel)
                if not isinstance(probe, dict) or not probe.get("found"):
                    continue
                if not probe.get("visible"):
                    continue
                text = probe.get("text", "") or ""
                aria = probe.get("aria", "") or ""
                if _consent_button_kind(text, aria) is None:
                    log.warning(
                        "consent: NOT clicking %r: its text %r does not "
                        "classify as reject/dismiss (fail closed)",
                        sel, text,
                    )
                    continue
                # N4: verify+click atomically in one isolated evaluate
                # — a DOM race between the probe and the click cannot
                # shift the click onto an accept-like button.
                outcome = _unforged(_CONSENT_ID_CLICK_JS, [sel, text])
                if outcome != "clicked":
                    log.warning(
                        "consent: NOT clicking %r: click-time "
                        "re-verification reported %r (fail closed)",
                        sel, outcome,
                    )
                    continue
                clicked += 1
            except _ConsentReadbackDead:
                raise
            except Exception:
                continue
        buttons = _unforged(_CONSENT_BUTTONS_JS, None) or []
        for button in buttons:
            if clicked >= 3:
                break
            try:
                if not button.get("visible"):
                    continue
                button_text = button.get("text", "")
                if _consent_button_kind(
                    button_text, button.get("aria", "")
                ) is None:
                    continue
                # N4: the index was captured at enumeration time; racing
                # DOM mutation could have shifted it onto an accept-like
                # button by click time. The click JS re-reads the
                # button's text at click time — in the isolated world
                # (R7-C), so page JS cannot forge it — and only clicks
                # when it still matches the classified text; a mismatch
                # is left alone (fail closed) and logged loudly.
                outcome = _unforged(
                    _CONSENT_CLICK_JS, [button.get("index", 0), button_text]
                )
                if outcome != "clicked":
                    log.warning(
                        "consent: NOT clicking button index %r: click-time "
                        "re-verification reported %r (the DOM changed "
                        "between enumeration and click — fail closed)",
                        button.get("index", 0), outcome,
                    )
                    continue
                clicked += 1
            except _ConsentReadbackDead:
                raise
            except Exception:
                continue
    except _ConsentReadbackDead as exc:
        # The unforged channel is unavailable: clicking any consent
        # button would mean trusting the forgeable main world. Loud,
        # then continue the run WITHOUT banner dismissal.
        log.warning(
            "consent: unforged readback unavailable (%s) — skipping "
            "consent-banner dismissal entirely (fail closed)",
            exc,
        )
    return clicked


def _precreate_owner_only(path: Path) -> bool:
    """Create ``path`` owner-only (0o600) BEFORE any secret bytes land.

    NIT 7 (round 6): screenshots and session files used to be written
    at umask permissions and chmodded to 0o600 afterwards — the
    PII/secret bytes briefly existed world-readable between the write
    and the chmod. Pre-creating with ``O_CREAT|O_TRUNC|O_WRONLY`` at
    0o600 (plus an explicit ``fchmod``, so a PRE-EXISTING file with a
    wider mode is tightened too) means the bytes never exist at wider
    permissions — Playwright then overwrites the existing inode,
    preserving the mode. Callers keep their post-write chmod as
    defense-in-depth. Returns False (with a loud log) when the file
    could not be secured — callers must treat that as fatal and not
    write secrets to the path.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        log.error(
            "Could not pre-create owner-only file %s: %s — refusing to "
            "write PII/secrets to an unsecurable path.",
            path, exc,
        )
        return False


def _take_screenshot(page: Any, suffix: str = "preview") -> Path | None:
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = SCREENSHOTS_DIR / f"apply-{stamp}-{uuid.uuid4().hex[:8]}-{suffix}.png"
        # NIT 7 (round 6): owner-only FROM CREATION — the file exists
        # at 0o600 before page.screenshot() writes a single byte.
        if not _precreate_owner_only(path):
            return None
        page.screenshot(path=str(path), full_page=False)
        # Screenshots of filled forms contain PII (name/email/phone).
        # They are not encrypted (see docs/i09/decisions/
        # communication-connectors.md D2 for why not, and the residual
        # risk routed to Paul) — but they are at least restricted to the
        # owner, and NIT 7 (round 6) the file was pre-created 0o600
        # BEFORE the screenshot bytes landed. This post-write chmod is
        # defense-in-depth (e.g. if the writer ever recreated the
        # inode). N3 (round 4): a chmod failure is an ERROR, never a
        # warning-only event: the
        # file is DELETED (fallback chmod 0) rather than left on disk at
        # umask permissions, and the failure is reported loudly. Returns
        # None so callers never treat the shot as usable.
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            try:
                os.unlink(path)
                removal_note = "the captured file was DELETED"
            except OSError as unlink_exc:
                try:
                    os.chmod(path, 0)
                    removal_note = (
                        f"deletion failed ({unlink_exc}); the file was "
                        "chmodded to 0 (no permissions for anyone) as a "
                        "last resort — remove it manually"
                    )
                except OSError as chmod0_exc:
                    removal_note = (
                        "CRITICAL: the file could neither be deleted "
                        f"({unlink_exc}) nor chmodded to 0 ({chmod0_exc}) "
                        "— it may still be readable; remove it manually "
                        "IMMEDIATELY"
                    )
            log.error(
                "Screenshot hardening FAILED (%s): %s. A hardening step "
                "that did not land is an error, never a warning.",
                exc, removal_note,
            )
            return None
        return path
    except Exception as exc:
        log.warning("Screenshot failed: %s", exc)
        return None


# Unforged readback (CDP isolated world)
# ---------------------------------------------------------------------------

#: Sentinel: the page object has no JS bridge at all. Returned by
#: ``_evaluate_unforged`` when ``page.evaluate`` is missing — which only
#: happens with incomplete test doubles. Real Playwright pages always
#: expose ``evaluate``, and page JS (a hostile page) cannot remove
#: the Python-side method, so "unavailable" never means "the page hid
#: from us" — it means there is no real browser behind this object.
_READBACK_UNAVAILABLE = object()

#: Attribute used to cache the per-page CDP isolated-world context id.
_ISOLATED_WORLD_ATTR = "_veto_isolated_world_id"


def _cdp_isolated_world_id(page: Any) -> int | None:
    """Create (once per page) and return a CDP isolated-world context id.

    MAJOR 2 (round 6): the isolated world shares the page's DOM but runs
    with PRISTINE JS globals and prototypes — the page's own scripts
    cannot override ``document.querySelectorAll`` or
    ``HTMLInputElement.prototype.value`` inside it, so snapshot JS run
    there reports the true DOM even against hostile page JS.

    Returns the world id. Returns None only when the page exposes no
    CDP session factory at all — callers MUST treat None as fatal for
    production security decisions (see ``_evaluate_unforged``): there
    is deliberately NO main-world fallback, because trusting the
    page's own JS realm would let hostile page scripts forge the
    readback. (``_evaluate_unforged`` refuses with RuntimeError when
    the isolated context cannot be established.)
    """
    cached = getattr(page, _ISOLATED_WORLD_ATTR, None)
    if isinstance(cached, int) and not isinstance(cached, bool):
        return cached
    new_cdp_session = getattr(
        getattr(page, "context", None), "new_cdp_session", None
    )
    if not callable(new_cdp_session):
        return None
    try:
        session = new_cdp_session(page)
        try:
            tree = session.send("Page.getFrameTree")
            frame_id = tree["frameTree"]["frame"]["id"]
            world = session.send(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": "veto-readback"},
            )
            world_id = int(world["executionContextId"])
        finally:
            try:
                session.detach()
            except Exception:
                pass
    except Exception as exc:
        log.warning(
            "Unforged readback: could not create CDP isolated world: %s", exc
        )
        return None
    try:
        setattr(page, _ISOLATED_WORLD_ATTR, world_id)
    except Exception:
        pass
    return world_id


def _evaluate_unforged(page: Any, js: str, arg: Any = None) -> Any:
    """Evaluate ``js`` where the page's scripts cannot forge the result.

    MAJOR 2 (round 6): on real Chromium pages this runs ``js`` via
    ``Runtime.callFunctionOn`` inside the CDP isolated world (see
    ``_cdp_isolated_world_id``) — a JS realm the page's own scripts
    cannot touch — never through the page's main-world
    ``page.evaluate``. That closes the round-6 threat: hostile page JS
    overriding ``document.querySelectorAll`` /
    ``HTMLInputElement.prototype.value`` to report approved values
    while the live DOM is tampered.

    FAIL CLOSED — ``page.evaluate`` is NEVER consulted for a production
    security decision, so there is deliberately NO main-world fallback.
    The three page kinds are distinguished explicitly:

    - No ``page.context`` at all: an INCOMPLETE TEST DOUBLE. Real
      Playwright pages ALWAYS expose ``.context`` (a BrowserContext),
      and page JS cannot remove the Python-side attribute — so a page
      without one is not driving a real browser and has no real TOCTOU
      target. Returns ``_READBACK_UNAVAILABLE`` with a loud warning
      (never silent); callers take their legacy "unavailable" path.
    - ``page.context`` present but no ``context.new_cdp_session``: a
      REAL non-Chromium page (CDP is Chromium-only). Raises
      RuntimeError — refusing to run the readback rather than trust the
      page's own JS realm, which hostile page scripts can forge.
    - ``context.new_cdp_session`` present: the isolated world is
      established; if THAT fails, raises RuntimeError (same refusal).

    The RuntimeError propagates to ``apply_via_browser``, which catches
    it and records ``result["error"]``. A mid-flight
    CDP transport failure on an already-established world returns None
    (fail closed upstream) — that is a transport error, not a trust
    decision.
    """
    context = getattr(page, "context", None)
    cdp_factory = getattr(context, "new_cdp_session", None)
    if cdp_factory is None:
        if context is None:
            # Incomplete test double: no browser context, so no real
            # browser and no real TOCTOU target. Warn loudly (never
            # silent) and signal unavailability — failing closed here
            # would only break test doubles.
            log.warning(
                "Unforged readback unavailable: page object has no "
                "browser context and no CDP channel. Expected only for "
                "incomplete test doubles; real Playwright pages always "
                "expose .context."
            )
            return _READBACK_UNAVAILABLE
        # Real page (a browser context exists) but no CDP session
        # channel: a non-Chromium browser — CDP is Chromium-only. Fail
        # closed: the page's own JS realm is untrusted (hostile page
        # scripts can forge querySelectorAll / el.value), so there is
        # no main-world fallback to degrade to.
        raise RuntimeError(
            "cannot establish an isolated (unforged) readback context: "
            "this page exposes no CDP session channel (non-Chromium "
            "browsers cannot provide one). Refusing the readback rather "
            "than trust the page's own JS realm, which hostile page "
            "scripts can forge."
        )
    try:
        world_id = _cdp_isolated_world_id(page)
    except Exception as exc:
        raise RuntimeError(
            "could not establish an isolated (unforged) readback "
            f"context: {exc}. Refusing the readback rather than trust the "
            "page's own JS realm."
        ) from exc
    if world_id is None:
        raise RuntimeError(
            "could not establish an isolated (unforged) readback "
            "context (the CDP session factory is present but the "
            "isolated world could not be created). Refusing the readback "
            "rather than trust the page's own JS realm."
        )
    try:
        session = cdp_factory(page)
        try:
            res = session.send(
                "Runtime.callFunctionOn",
                {
                    "contextId": world_id,
                    "functionDeclaration": js,
                    "arguments": [] if arg is None else [{"value": arg}],
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
        finally:
            try:
                session.detach()
            except Exception:
                pass
    except Exception as exc:
        log.warning(
            "Unforged readback: isolated-world evaluate failed: %s", exc
        )
        return None
    if res.get("exceptionDetails"):
        log.warning(
            "Unforged readback: isolated-world JS threw: %s",
            res["exceptionDetails"],
        )
        return None
    return res.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _block_until_browser_closed(browser: Any) -> None:
    """Block until the user closes the browser window (headed handoff).

    Returning from inside the Playwright context would tear the browser
    down, so the headed fill-only flow waits here instead: the user
    reviews the completed form and clicks submit themselves, then
    closes the window, and only then does the tool return its fill
    report. Polls ``browser.is_connected()``; never raises.
    """
    try:
        while True:
            try:
                if not browser.is_connected():
                    return
            except Exception:
                return  # connection object unusable -> treat as closed
            time.sleep(0.5)
    except KeyboardInterrupt:
        return


def apply_via_browser(
    apply_url: str,
    profile: dict[str, Any] | None = None,
    resume_path: str | None = None,
    headless: bool = True,
    confirm: bool = False,
    approval_id: str | None = None,
    timeout_ms: int = 60000,
    board: str | None = None,
    browser_kind: str = "chromium",
) -> dict[str, Any]:
    """Fill a job application form in a real browser. NEVER submits.

    Fill-only by architecture: this tool opens the ``apply_url``,
    extracts the visible form fields, fills them from the ``profile``
    dict, uploads the resume, screenshots the completed form, and
    stops. There is no code path here that clicks a submit control,
    performs a programmatic form submission, issues an application
    POST, or mints an approval to submit. The submit click is always the user's own, in
    a browser they control.

    Handoff: with ``headless=False`` the browser stays open on the
    completed form and the call waits until the user closes it — the
    user reviews every field, clicks submit themselves, and closes the
    window when done. Headless runs close the browser after the preview
    screenshot; the report carries the screenshot path and the apply
    URL so the user can open the form in their own browser and finish
    by hand.

    Args:
        apply_url: Direct URL of the application form.
        profile: Dict of applicant details (see module docstring).
        resume_path: Local resume file to upload (skipped if missing).
        headless: Run the browser headless (default True). Set False
            to keep the filled form open in a visible window for
            manual review and submit.
        confirm: DEPRECATED and does nothing. Kept only so existing
            callers do not raise TypeError.
        approval_id: DEPRECATED and does nothing. No approval
            machinery remains; nothing can be authorized.
        timeout_ms: Navigation timeout.
        board: Optional board key (e.g. "greenhouse"). The browser runs
            unauthenticated; saved login sessions are not supported and
            are never loaded.
        browser_kind: Playwright browser to launch: "chromium" (default)
            or "firefox". The chosen browser must be installed
            (``python -m playwright install chromium firefox``).

    Returns:
        Fill-report dict with keys: ok, fields_filled, fields_detected,
        screenshot, final_url, browser_open, handoff,
        screenshot_cleanup, error. ``ok`` is True when the form was
        filled and screenshotted without error — it means the FILL
        succeeded, never that anything was submitted. ``browser_open``
        is False in every returned report: headed runs wait for the
        user to close the browser before returning, so ``handoff``
        describes what just happened, not a still-open window.
        ``paused``/``rescue`` appear on CAPTCHA/auth pauses.
        Never raises on page/automation failures — errors are returned
        in the dict.
    """
    profile = dict(profile or {})
    if confirm:
        log.warning(
            "browser_apply.apply_via_browser: confirm= is deprecated and "
            "does nothing. This tool only fills forms; it never submits."
        )
    if approval_id is not None:
        log.warning(
            "browser_apply.apply_via_browser: approval_id= is deprecated "
            "and does nothing. No approval machinery remains."
        )
    result: dict[str, Any] = {
        "ok": False,
        "fields_filled": {},
        "fields_detected": 0,
        "screenshot": None,
        "final_url": apply_url,
        # Fill-only handoff: True only when the browser was deliberately
        # left open on the completed form for the user (headed mode).
        "browser_open": False,
        "handoff": None,
        # S2: the screenshot-retention report, filled in below. Per-file
        # deletion failures are surfaced here (not discarded).
        "screenshot_cleanup": {"deleted": [], "kept": 0, "errors": []},
        "error": None,
    }
    # Enforce the screenshot retention policy (PII) before creating new
    # ones. S2 (round 5): cleanup is best-effort — it never fails the
    # run — but its failures are NOT silent anymore. Per-file deletion
    # errors are ERROR-logged (these files contain filled-form PII, said
    # explicitly) and surfaced in result["screenshot_cleanup"], so the
    # D2 ops_goal ("screenshot files older than the retention cap —
    # zero") and the "fail loud" standing constraint are actually
    # enforced when deletion fails.
    try:
        screenshot_cleanup = cleanup_screenshots()
    except Exception as exc:  # pragma: no cover - defensive
        log.error(
            "Screenshot retention cleanup CRASHED: %s. PII-bearing "
            "screenshot files may remain on disk past the retention cap "
            "— investigate and remove them manually.",
            exc,
        )
        screenshot_cleanup = {
            "deleted": [],
            "kept": 0,
            "errors": [f"screenshot retention cleanup crashed: {exc}"],
        }
    else:
        if screenshot_cleanup.get("errors"):
            log.error(
                "Screenshot retention cleanup could not delete %d "
                "PII-bearing screenshot file(s): %s. They remain on disk "
                "past the retention cap — remove them manually.",
                len(screenshot_cleanup["errors"]),
                "; ".join(screenshot_cleanup["errors"]),
            )
    result["screenshot_cleanup"] = screenshot_cleanup
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["error"] = (
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        )
        return result

    # Saved login sessions were removed: no code path may load a saved
    # Playwright storage_state. If pre-existing sessions/*.json files are
    # still on disk, refuse to run and tell the user to delete them by
    # hand — they may hold live credentials, so this tool will not
    # delete them itself.
    _stale_sessions_dir = BASE_DIR / "sessions"
    _stale = (
        sorted(_stale_sessions_dir.glob("*.json"))
        if _stale_sessions_dir.is_dir()
        else []
    )
    if _stale:
        _names = ", ".join(p.name for p in _stale)
        result["error"] = (
            "Saved login sessions are no longer supported and are never "
            "loaded. Found stale session file(s) in "
            f"{_stale_sessions_dir}: {_names}. Delete them by hand "
            "(they may contain live credentials, so this tool will not "
            "delete them for you), then re-run."
        )
        log.error(
            "Refusing to run with stale saved login sessions present: %s",
            _names,
        )
        return result

    browser = None
    try:
        with sync_playwright() as p:
            launcher = getattr(p, browser_kind, None) or p.chromium
            launch_args = ["--no-sandbox"]
            browser = launcher.launch(headless=headless, args=launch_args)
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1366, "height": 900},
                "user_agent": VETO_USER_AGENT,
            }
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            try:
                page.goto(apply_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                result["error"] = f"Navigation to apply_url failed: {exc}"
                return result

            dismiss_consent_banners(page)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # some pages never settle; continue anyway

            # --- Session rescue (Initiative 09, epic 5): CAPTCHA / auth loss
            # check right after page load. CAPTCHA is NEVER bypassed — the
            # pause-and-hand-off IS the feature. In headed mode the
            # browser stays open so the user can clear the challenge by
            # hand.
            rescue = session_rescue.check(page, board=board)
            if rescue is not None:
                shot = _take_screenshot(page, f"rescue-{rescue.reason}")
                rescue.screenshot_path = str(shot) if shot else None
                result["paused"] = True
                result["rescue"] = rescue.to_dict()
                result["final_url"] = page.url
                result["error"] = (
                    f"Paused: {rescue.reason} detected — control handed to "
                    f"the user. {rescue.what_user_should_do}"
                )
                result["ok"] = False
                if not headless:
                    # The browser is genuinely open here (we are still
                    # inside the Playwright context). Wait for the user to
                    # clear the challenge by hand and close the window.
                    result["browser_open"] = True
                    result["handoff"] = (
                        f"Paused ({rescue.reason}): the browser is open. "
                        "Clear the challenge by hand, then close the "
                        "browser window — this call waits until you do."
                    )
                    _block_until_browser_closed(browser)
                    result["browser_open"] = False
                    result["handoff"] = (
                        f"Paused ({rescue.reason}): you closed the "
                        "browser. Re-run the tool after clearing the "
                        "challenge by hand if you want to retry the fill."
                    )
                return result

            fields = extract_form_fields(page)
            result["fields_detected"] = len(fields)

            # --- Session rescue: field-ambiguity check before filling.
            rescue = session_rescue.check(page, board=board, fields=fields)
            if rescue is not None:
                shot = _take_screenshot(page, f"rescue-{rescue.reason}")
                rescue.screenshot_path = str(shot) if shot else None
                result["paused"] = True
                result["rescue"] = rescue.to_dict()
                result["final_url"] = page.url
                result["error"] = (
                    f"Paused: {rescue.reason} — control handed to the user. "
                    f"{rescue.what_user_should_do}"
                )
                result["ok"] = False
                if not headless:
                    result["browser_open"] = True
                    result["handoff"] = (
                        f"Paused ({rescue.reason}): the browser is open. "
                        "Resolve it by hand, then close the browser "
                        "window — this call waits until you do."
                    )
                    _block_until_browser_closed(browser)
                    result["browser_open"] = False
                    result["handoff"] = (
                        f"Paused ({rescue.reason}): you closed the "
                        "browser. Re-run the tool to retry the fill once "
                        "the issue is resolved by hand."
                    )
                return result

            result["fields_filled"] = fill_application(page, profile, resume_path)
            # Gentle human-like pause before the screenshot.
            time.sleep(1.0)

            shot = _take_screenshot(page, "preview")
            result["screenshot"] = str(shot) if shot else None
            result["final_url"] = page.url
            result["ok"] = True

            if not headless:
                # Fill-only handoff: the browser is OPEN on the completed
                # form. Block here until the user closes it — they review
                # every field and click submit themselves. Only after the
                # window is closed does the tool return its fill report,
                # so the report never claims a browser is open that isn't.
                result["browser_open"] = True
                result["handoff"] = (
                    "The browser is open on the completed form. Review "
                    "every field for accuracy, then click submit yourself. "
                    "This call waits until you close the browser window."
                )
                log.info(
                    "browser_apply: form filled (%d fields); waiting for "
                    "the user to review, submit by hand, and close the "
                    "browser.",
                    len(result["fields_filled"]),
                )
                _block_until_browser_closed(browser)
                result["browser_open"] = False
                result["handoff"] = (
                    "You closed the browser. The form was filled "
                    f"({len(result['fields_filled'])} fields) and "
                    f"screenshotted at {result['screenshot']} before "
                    "close. This report describes the fill only — it does "
                    "not confirm any submission."
                )
                return result

            result["handoff"] = (
                "Headless run: the browser was closed after the preview "
                f"screenshot ({result['screenshot']}). Open "
                f"{result['final_url']} in your own browser to review the "
                "form and submit it yourself — or re-run with "
                "headless=False to keep the filled form open."
            )
    except Exception as exc:  # never let the tool crash
        result["error"] = f"Browser automation failed: {exc}"
    finally:
        # Headed runs only return after the user closed the browser, so
        # by here the browser is either already closed or a headless
        # instance to tear down. Playwright context teardown follows on
        # `with` exit.
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
    return result
