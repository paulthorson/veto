"""P0 craft sprint: context-gated delight (WS-G).

Roadmap Initiative 00 gate: delight ships ONLY after a user-initiated
success state. PROHIBITED on rejection, veto, error, blocked, and empty
states. Always dismissible. Fully inert under reduced motion.

The gate is enforced structurally: webui/delight.js exposes exactly one
entry point (success()), never auto-shows, and degrades to a static toast
under prefers-reduced-motion. Error/veto/rejection paths use the existing
#banner / role="alert" patterns and must never call this module.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DELIGHT_JS = REPO / "webui" / "delight.js"
GUIDE = REPO / "webui" / "guide.html"
APP_JS = REPO / "webui" / "app.js"
APP_CSS = REPO / "webui" / "app.css"
INDEX_HTML = REPO / "webui" / "index.html"

PROHIBITED_COPY = re.compile(r"confetti|celebrat|🎉|🎊", re.I)


def _src() -> str:
    return DELIGHT_JS.read_text()


def test_delight_module_exists_and_is_wired():
    assert DELIGHT_JS.exists()
    html = INDEX_HTML.read_text()
    assert 'src="delight.js"' in html
    assert html.index('src="delight.js"') < html.index('src="app.js"')


def test_only_success_entry_point():
    """The module exposes success() and nothing else — no error/veto API."""
    src = _src()
    assert "VetoDelight = { success: success }" in src
    for forbidden in (".error", ".veto", ".reject", ".empty", ".blocked"):
        assert f"VetoDelight.{forbidden[1:]}" not in src, (
            f"delight.js must not expose a {forbidden} entry point"
        )


def test_never_auto_shows():
    """No DOM observation, no timers, no load-time auto-show."""
    src = _src()
    assert "DOMContentLoaded" not in src
    assert "MutationObserver" not in src
    assert "setInterval" not in src
    assert "setTimeout" not in src


def test_always_dismissible():
    src = _src()
    assert 'aria-label", "Dismiss"' in src or 'aria-label' in src
    assert "Escape" in src  # keyboard dismissal
    assert "removeEventListener" in src  # cleans up after itself


def test_inert_under_reduced_motion():
    src = _src()
    assert "prefers-reduced-motion" in src
    assert "matchMedia" in src
    assert "delight-static" in src
    css = APP_CSS.read_text()
    assert ".delight-toast.delight-static" in css


def test_polite_announcement_not_assertive():
    """Success feedback uses role=status (polite), never role=alert."""
    assert 'role", "status"' in _src()


def test_guide_calls_delight_only_on_user_success():
    """guide.html: the ONLY VetoDelight.success call is the all-steps-done
    branch — a user-initiated success. No error/empty path may call it."""
    html = GUIDE.read_text()
    calls = [m.start() for m in re.finditer(r"VetoDelight\.success", html)]
    assert len(calls) == 1, f"expected exactly one delight call site, found {len(calls)}"
    context = html[max(0, calls[0] - 600):calls[0]]
    assert "done.length === total" in context, (
        "delight call site must be gated on all-steps-done"
    )


def test_app_js_never_calls_delight():
    """Dashboard error/veto/rejection paths must not touch the delight module."""
    assert "VetoDelight" not in APP_JS.read_text()


def test_no_celebration_copy_on_prohibited_surfaces():
    """No confetti/celebration language anywhere in the shipped surfaces."""
    for rel, path in (("webui/delight.js", DELIGHT_JS),
                      ("webui/app.js", APP_JS),
                      ("webui/guide.html", GUIDE),
                      ("dashboard.py", REPO / "dashboard.py")):
        assert not PROHIBITED_COPY.search(path.read_text()), (
            f"{rel} contains celebration copy"
        )


def test_delight_toast_meets_touch_target():
    """.delight-close is a 44px touch target (phone contract)."""
    css = APP_CSS.read_text()
    m = re.search(r"\.delight-close\s*\{([^}]*)\}", css)
    assert m, ".delight-close styles missing"
    block = m.group(1)
    assert "width: 44px" in block and "height: 44px" in block
