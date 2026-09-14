"""P0 craft sprint: motion contract conformance.

Web contract (roadmap Initiative 00): ``prefers-reduced-motion`` kills all
animation; the kill-switch value comes from the ``--motion-reduced`` token.
Terminal contract: ``VETO_REDUCED_MOTION=1`` (or the generic
``REDUCED_MOTION=1``) must render all terminal microinteraction inert; the
terminal is fully static today, so the contract also pins "no animation
primitives" in dashboard.py.

Motion values on the web must come from the ``--motion-*`` tokens
(``--motion-press``, ``--motion-blink*``, ``--motion-bar``,
``--motion-progress``) rather than raw timing literals.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SITE_CSS = REPO / "site" / "src" / "site.css"
APP_CSS = REPO / "webui" / "app.css"
GUIDE = REPO / "webui" / "guide.html"
TOKENS_CSS = REPO / "webui" / "vendor" / "veto-tokens.css"
VETO_WEB_CSS = REPO / "webui" / "vendor" / "veto-web.css"

MOTION_TOKEN = re.compile(r"--motion-[a-z-]+")


def _declarations(text: str, prop: str) -> list[str]:
    """All `prop: <value>;` declaration values outside comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.findall(rf"(?<![a-z-]){prop}\s*:\s*([^;{{}}]+);", text)


def _motion_token_names() -> set[str]:
    return set(MOTION_TOKEN.findall(TOKENS_CSS.read_text()))


def test_motion_tokens_exist():
    names = _motion_token_names()
    for needed in ("--motion-press", "--motion-blink", "--motion-blink-fast",
                   "--motion-blink-caret", "--motion-bar", "--motion-progress",
                   "--motion-reduced"):
        assert needed in names, f"token {needed} missing from tokens.css"


# Declarations that deliberately keep local timing (documented here, not tokens):
# - site.css .feature-card hover-lift also transitions border-color; no
#   --motion-* token covers that property combination, so the raw steps(2)
#   timing is retained next to the tokenized siblings.
LOCAL_MOTION_OK = {
    "site/src/site.css": {
        "transform 0.08s steps(2), border-color 0.08s steps(2) !important",
    },
    # Delight entrance: no --motion-* token covers keyframed entrances; the
    # chunky steps(6) value is documented here and inert under reduced motion.
    "webui/app.css": {"delight-in 0.24s steps(6)"},
    "webui/guide.html": {"delight-in 0.24s steps(6)"},
}


def test_web_motion_uses_tokens():
    """animation/transition declarations resolve to --motion-* tokens."""
    for rel, path in (("site/src/site.css", SITE_CSS), ("webui/app.css", APP_CSS)):
        text = path.read_text()
        allowed = LOCAL_MOTION_OK.get(rel, set())
        for prop in ("animation", "transition"):
            for value in _declarations(text, prop):
                v = value.strip()
                if v == "none" or v in allowed:
                    continue
                tokens = MOTION_TOKEN.findall(v)
                assert tokens, (
                    f"{rel}: {prop}: {v!r} uses raw timing, not a --motion-* token"
                )


def _has_kill_switch(text: str) -> bool:
    if "@media (prefers-reduced-motion: reduce)" not in text:
        return False
    block = text.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    return "var(--motion-reduced)" in block or "animation: none" in block


def test_reduced_motion_kill_switch_present():
    """Every web P0 surface carries a prefers-reduced-motion kill switch.

    Surfaces are stylesheet stacks, not single files: the dashboard is
    app.css over vendor/veto-web.css; the marketing site is site.css over
    @veto/web; the setup guide is standalone.
    """
    ds_web_dist = Path(
        "/home/hatch/workspace/veto-design-system/packages/web/dist/veto-web.css"
    )
    surfaces = {
        "dashboard": [APP_CSS, VETO_WEB_CSS],
        "marketing site": [SITE_CSS, ds_web_dist],
        "setup guide": [GUIDE],
    }
    for name, files in surfaces.items():
        texts = []
        for f in files:
            assert f.exists(), f"surface {name}: missing {f}"
            texts.append(f.read_text())
        assert any(_has_kill_switch(t) for t in texts), (
            f"surface {name} has no prefers-reduced-motion kill switch"
        )


def test_no_focus_outline_kills():
    """Web contract: no stylesheet may kill the visible focus indicator.

    ``outline: none`` on :focus rules defeats the shared :focus-visible
    ring (higher specificity than the global rule). Border-color changes
    may accompany focus, but the outline must survive.
    """
    for rel, path in (("site/src/site.css", SITE_CSS),
                      ("webui/app.css", APP_CSS),
                      ("webui/guide.html", GUIDE)):
        text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
        for m in re.finditer(r"([^{}]*:focus[^{}]*)\{([^}]*)\}", text):
            if "focus-visible" in m.group(1):
                continue
            declarations = m.group(2).replace(" ", "").replace("\n", "")
            assert "outline:none" not in declarations, (
                f"{rel}: `{m.group(1).strip()}` kills the focus outline"
            )


def test_terminal_reduced_motion_flag():
    sys.path.insert(0, str(REPO))
    import importlib
    import os
    import veto_theme as vt
    importlib.reload(vt)
    for var in ("VETO_REDUCED_MOTION", "REDUCED_MOTION"):
        os.environ.pop(var, None)
    importlib.reload(vt)
    assert vt.reduced_motion() is False
    os.environ["VETO_REDUCED_MOTION"] = "1"
    importlib.reload(vt)
    assert vt.reduced_motion() is True
    del os.environ["VETO_REDUCED_MOTION"]
    os.environ["REDUCED_MOTION"] = "1"
    importlib.reload(vt)
    assert vt.reduced_motion() is True
    del os.environ["REDUCED_MOTION"]
    importlib.reload(vt)


def test_terminal_has_no_animation_primitives():
    """dashboard.py must stay fully static: no spinners, no \r redraws."""
    src = (REPO / "dashboard.py").read_text()
    assert "\\r" not in src, "dashboard.py uses carriage-return redraws"
    for glyph in ("◐", "◑", "◒", "◓", "⠋", "⣾", "⣽", "⣻", "⢿"):
        assert glyph not in src, f"dashboard.py contains spinner glyph {glyph!r}"
    assert "curses" not in src
