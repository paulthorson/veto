"""P0 craft sprint: design-token consolidation conformance.

The design-token source of truth is @veto/tokens, vendored at
``webui/vendor/veto-tokens.css`` (byte-identical to the monorepo dist; see
``scripts/sync-design-system.sh`` and ``DESIGN_SYSTEM_VERSIONS.json``).
The marketing site loads the vendored copy via a relative import
(``./vendor/tokens.css`` in ``site/src/main.tsx``) since legal-hardening
commit 11 removed the ``file:../../veto-design-system`` dependency.

These tests pin the WS-A consolidation (2026-09-13): the local stylesheets
``site/src/site.css``, ``webui/app.css`` and ``webui/guide.html`` must draw
colors, spacing-scale values, radii, shadows, borders and type from the
tokens and must not re-declare a private palette.

Exemptions (documented, deliberate):
  - ``#000`` / ``#fff``: pure black/white are system values, not palette
    colors (hard-shadow offsets, on-bright text, selection).
  - Translucent ``rgba()`` state tints (hover washes, glows): alpha blends
    with no token equivalent; the token-derived ones were normalized to
    ``--veto-dotgrid`` / ``--veto-scanline`` / ``--veto-shadow`` /
    ``--veto-overlay`` where a token exists.
  - Local semantic aliases (``--line`` in app.css -> ``--border-accent``;
    ``--dim``/``--line`` in guide.html -> ``--muted``/``--border``;
    ``--tone``/``--mono`` in site.css/app.css): listed in ``LOCAL_ALIASES``.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SITE_CSS = REPO / "site" / "src" / "site.css"
APP_CSS = REPO / "webui" / "app.css"
GUIDE = REPO / "webui" / "guide.html"
TOKENS_CSS = REPO / "webui" / "vendor" / "veto-tokens.css"

HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
VAR_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)")
ROOT_PROP_RE = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.M)

# Pure black/white: system values, not palette decisions.
EXEMPT_HEX = {"#000", "#fff"}

# Local semantic aliases: name -> canonical token they resolve to.
LOCAL_ALIASES = {
    "site/src/site.css": {"--tone"},
    "webui/app.css": {"--line", "--mono"},
    "webui/guide.html": {"--dim", "--line", "--green-dim"},
}


def _token_names() -> set[str]:
    names = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", TOKENS_CSS.read_text(), re.M))
    assert names, "vendored tokens.css defines no custom properties"
    return names


def test_token_vendoring_present():
    assert TOKENS_CSS.exists(), "vendored tokens.css missing"
    assert "--green-neon" in TOKENS_CSS.read_text()


def _strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_no_local_color_palette_redeclaration():
    """No file under review may declare its own color :root palette."""
    for rel, path, allowed in (("site/src/site.css", SITE_CSS, LOCAL_ALIASES["site/src/site.css"]),
                               ("webui/guide.html", GUIDE, LOCAL_ALIASES["webui/guide.html"])):
        text = _strip_css_comments(path.read_text())
        for m in ROOT_PROP_RE.finditer(text):
            assert m.group(1) in allowed, (
                f"{rel} declares unexpected local property {m.group(1)}"
            )


def test_no_raw_hex_colors():
    """No raw hex color literals outside the documented exemptions."""
    for rel, path in (("site/src/site.css", SITE_CSS),
                      ("webui/app.css", APP_CSS),
                      ("webui/guide.html", GUIDE)):
        text = path.read_text()
        bad = [h for h in HEX_RE.findall(text) if h not in EXEMPT_HEX]
        assert not bad, f"{rel} has non-exempt hex literals: {sorted(set(bad))}"


def test_all_var_references_resolve_to_tokens_or_aliases():
    tokens = _token_names()
    for rel, path in (("site/src/site.css", SITE_CSS),
                      ("webui/app.css", APP_CSS),
                      ("webui/guide.html", GUIDE)):
        text = _strip_css_comments(path.read_text())
        allowed = tokens | LOCAL_ALIASES[rel]
        unknown = sorted({v for v in VAR_RE.findall(text) if v not in allowed})
        assert not unknown, f"{rel} references unknown tokens: {unknown}"


def test_site_loads_tokens_before_local_css():
    main = (REPO / "site" / "src" / "main.tsx").read_text()
    # Legal-hardening commit 11: the file:../../veto-design-system dep is
    # gone; tokens load from the vendored relative import instead.
    tok = main.index("./vendor/tokens.css")
    local = main.index("./site.css")
    assert tok < local, "tokens must load before site.css so local overrides win"


def test_dashboard_tokens_before_app_css():
    index = (REPO / "webui" / "index.html").read_text()
    tok = index.index("vendor/veto-tokens.css")
    local = index.index("app.css")
    assert tok < local, "tokens must load before app.css"


def test_guide_loads_tokens():
    assert 'href="vendor/veto-tokens.css"' in GUIDE.read_text()


def test_terminal_theme_uses_design_system_source():
    """dashboard.py/veto_theme.py draw terminal color from the vendored theme.

    Every role declared in the vendored theme.json must resolve through the
    public veto_theme API (no hardcoded ANSI/hex in dashboard.py).
    """
    dash = (REPO / "dashboard.py").read_text()
    assert not HEX_RE.search(dash), "dashboard.py has raw hex color literals"
    assert "\\x1b[" not in dash, "dashboard.py emits raw ANSI escapes"
    import json
    import sys
    sys.path.insert(0, str(REPO))
    import veto_theme as vt
    theme = json.loads((REPO / "theme.json").read_text())
    for role in theme["roles"]:
        vt.paint(role, "x")
        vt.ansi256(role)
