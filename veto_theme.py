"""veto_theme — canonical terminal theme for Veto.

Loads ``theme.json`` from the package directory and exposes the primitives
the terminal surfaces (``dashboard.py``, ``cli.py``, ``wizard.py``) use to
adopt color safely. The theme load is fail-soft: a missing, corrupt, or structurally invalid
``theme.json`` emits a warning on stderr and the module falls back to a
minimal embedded theme, so a cosmetic theming failure can never break
import for a consumer.

- ``supports_color()`` — False when ``NO_COLOR`` is set (and non-empty, per
  https://no-color.org), when ``TERM=dumb``, when ``CLICOLOR=0``, or when
  stdout is not a TTY.
- ``reduced_motion()`` — True when ``VETO_REDUCED_MOTION=1`` (or the generic
  ``REDUCED_MOTION=1``).
- ``paint(role, text)`` / ``style(text, roles=[...])`` — wrap text in ANSI
  SGR sequences from theme.json when color is supported; degrade gracefully
  otherwise (color roles drop off; ``bold`` falls back to the existing
  ALL-CAPS heading convention). Both raise ``KeyError`` on unknown
  role/style names, in every mode.
- ``divider(label)`` / ``banner(text)`` / ``kpi_line(pairs)`` — the 60-col
  em-dash section divider (overlong labels truncated to 60 columns), the
  ``══`` banner, and the KPI header line.
- ``status_mark(status)`` — glyph convention: ``✓`` / ``✕`` / ``⚠`` / ``•``;
  ``None`` and unknown statuses return ``?``.

Stdlib only — no third-party terminal libraries.
"""

import io
import json
import os
import sys

# theme.json lives in the package root: packages/terminal/theme.json.
# This module lives in:             packages/terminal/python/veto_theme.py
# (the vendored flat-layout copy patches this to look next to the module —
# see scripts/sync-design-system.sh; the packaging contract is documented
# in SPEC.md, "Packaging".)
_THEME_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.json")

#: Minimal embedded theme used when theme.json cannot be loaded at import
#: time. Covers exactly the roles/functions the module needs to keep
#: working: display, success, danger, muted, border, plus the text styles,
#: glyphs, layout width, placeholders, and status marks.
_FALLBACK_THEME = {
    "version": "fallback",
    "layout": {"divider_width": 60},
    "glyphs": {
        "divider_rule": "—",
        "banner_rule": "══",
        "unknown": "?",
    },
    "placeholders": {"missing": "—"},
    "status_marks": {"ok": "✓", "fail": "✕", "warn": "⚠", "info": "•"},
    "text_styles": {
        "bold": {"ansi": 1, "no_color": "ALL-CAPS"},
        "dim": {"ansi": 2, "no_color": "plain"},
        "underline": {"ansi": 4, "no_color": "plain"},
    },
    "roles": {
        "display": {"ansi256": 49, "ansi16": "bright_green",
                    "hex": "#00ff9c", "no_color": "",
                    "note": "fallback: brand display green"},
        "success": {"ansi256": 40, "ansi16": "green",
                    "hex": "#39d353", "no_color": "✓",
                    "note": "fallback: success green"},
        "danger": {"ansi256": 197, "ansi16": "bright_red",
                   "hex": "#ff3b5c", "no_color": "✕",
                   "note": "fallback: danger red"},
        "muted": {"ansi256": 102, "ansi16": "bright_black",
                  "hex": "#7d8590", "no_color": "",
                  "note": "fallback: muted gray"},
        "border": {"ansi256": 34, "ansi16": "green",
                   "hex": "#2ea043", "no_color": "—",
                   "note": "fallback: border green"},
    },
}


def _load_theme():
    """Load theme.json, falling back to the embedded theme on failure."""
    try:
        with open(_THEME_PATH, encoding="utf-8") as _f:
            theme = json.load(_f)
        _validate_theme(theme)
        return theme
    except (OSError, ValueError) as exc:
        # OSError covers missing/unreadable files; ValueError covers
        # corrupt JSON (JSONDecodeError) and schema-invalid content
        # (raised by _validate_theme below). A cosmetic theme must never
        # break import for consumers like dashboard.py.
        print(
            "veto_theme: warning: could not load {} ({!r}); "
            "using built-in fallback theme".format(_THEME_PATH, exc),
            file=sys.stderr,
        )
        return _FALLBACK_THEME


#: Top-level keys every theme.json must provide. A file that parses as
#: valid JSON but lacks these (e.g. ``{}`` or a top-level list) is routed
#: to the fallback theme rather than breaking import at module scope.
_REQUIRED_THEME_KEYS = (
    "roles",
    "text_styles",
    "status_marks",
    "glyphs",
    "layout",
    "placeholders",
)


def _validate_theme(theme):
    """Raise ValueError when parsed theme.json is structurally invalid."""
    if not isinstance(theme, dict):
        raise ValueError(
            "theme.json must be a JSON object, got {}".format(type(theme).__name__)
        )
    missing = [k for k in _REQUIRED_THEME_KEYS if k not in theme]
    if missing:
        raise ValueError(
            "theme.json missing required keys: {}".format(", ".join(missing))
        )


THEME = _load_theme()

#: True when the fallback theme is in use (theme.json missing/corrupt).
USING_FALLBACK_THEME = THEME.get("version") == "fallback"

ROLES = THEME["roles"]
TEXT_STYLES = THEME["text_styles"]
STATUS_MARKS = THEME["status_marks"]

#: Valid 16-color names (xterm/ANSI palette) accepted by theme.json.
ANSI16_NAMES = frozenset(
    [
        "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
        "bright_black", "bright_red", "bright_green", "bright_yellow",
        "bright_blue", "bright_magenta", "bright_cyan", "bright_white",
    ]
)


def supports_color() -> bool:
    """True when the terminal may be colored.

    Honors ``NO_COLOR`` (set and non-empty disables color), ``TERM=dumb``
    (dumb terminals cannot render SGR), and ``CLICOLOR=0`` (the CLICOLOR
    convention for disabling color); otherwise requires a TTY on stdout —
    piped output degrades to the glyph/text conventions.
    """
    no_color = os.environ.get("NO_COLOR")
    if no_color is not None and no_color != "":
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("CLICOLOR") == "0":
        return False
    stdout = sys.stdout
    isatty = getattr(stdout, "isatty", None)
    try:
        return bool(isatty and isatty())
    except (OSError, ValueError, io.UnsupportedOperation):
        return False


def reduced_motion() -> bool:
    """True when the user asked for reduced motion.

    Honors ``VETO_REDUCED_MOTION=1`` (Veto-specific, takes precedence) and
    the generic ``REDUCED_MOTION=1``. Today the terminal is fully static, so
    this documents the static-render contract: when True, no spinners,
    progress bars, or live-refresh may ever be introduced.
    """
    return os.environ.get("VETO_REDUCED_MOTION") == "1" or os.environ.get(
        "REDUCED_MOTION"
    ) == "1"


def _fg(role: str) -> str:
    """ANSI SGR prefix for a color role's 256-color fallback."""
    spec = ROLES[role]  # KeyError -> programmer error; let it surface
    return "\x1b[38;5;{}m".format(spec["ansi256"])


def _style_prefix(name: str) -> str:
    """ANSI SGR prefix for a text style (bold/dim/underline)."""
    return "\x1b[{}m".format(TEXT_STYLES[name]["ansi"])


def ansi256(role: str) -> int:
    """The ANSI-256 color code for a theme role."""
    return ROLES[role]["ansi256"]


def ansi16(role: str) -> str:
    """The 16-color name for a theme role."""
    return ROLES[role]["ansi16"]


def paint(role: str, text: str) -> str:
    """Paint ``text`` in the color for ``role``.

    With color support: ``\\x1b[38;5;<code>m`` + text + ``\\x1b[39m``
    (default-foreground reset, so background survives).
    Without: returns ``text`` unchanged — the glyph/text fallback is the
    caller's glyph (e.g. ``✓``), which theme.json documents per role.

    Raises ``KeyError`` for an unknown role, in every mode (colored or
    degraded) — the lookup is validated before the color check.
    """
    if role not in ROLES:
        raise KeyError("unknown role: {!r}".format(role))
    if not supports_color():
        return text
    return _fg(role) + text + "\x1b[39m"


def style(text: str, roles=None) -> str:
    """Apply color roles and/or text styles to ``text``.

    ``roles`` may mix color roles (``success``, ``warning``, ``danger``,
    ...) and text styles (``bold``, ``dim``, ``underline``). Order of
    ``roles`` is preserved inside the prefix.

    Degradation without color: color roles are dropped; ``bold`` falls back
    to the existing ALL-CAPS heading convention; ``dim``/``underline`` fall
    back to plain text. An empty ``roles`` list returns ``text`` unchanged
    (no bare reset sequence is emitted).
    """
    roles = list(roles or [])
    for r in roles:
        if r not in ROLES and r not in TEXT_STYLES:
            raise KeyError("unknown role/style: {!r}".format(r))
    if not supports_color():
        if "bold" in roles:
            return text.upper()
        return text
    prefix = "".join(
        _style_prefix(r) if r in TEXT_STYLES else _fg(r) for r in roles
    )
    if not prefix:
        return text
    return prefix + text + "\x1b[0m"


def divider(label: str = "") -> str:
    """60-column em-dash section divider (dashboard.py:199-201 convention).

    Example: ``divider("SCORE A JOB")`` -> ``— SCORE A JOB ———…`` (60 cols).
    Labels longer than ``divider_width - 3`` are truncated so the composed
    line never exceeds the 60-column contract. The blank line above the
    divider is the caller's responsibility (dashboard.py prints it).
    """
    width = THEME["layout"]["divider_width"]
    glyph = THEME["glyphs"]["divider_rule"]
    if label:
        label = label[: width - 3]
    return ("{} {} ".format(glyph, label) if label else glyph + " ").ljust(
        width, glyph
    )


def banner(text: str) -> str:
    """Double-rule banner: ``  ══ TEXT ══`` (dashboard.py:1303)."""
    rule = THEME["glyphs"]["banner_rule"]
    return "  {} {} {}".format(rule, text.upper(), rule)


def kpi_line(pairs) -> str:
    """KPI header line: 2-space indent, items joined with ``  |  ``.

    ``pairs`` is an iterable of ``(label, value)``; ``None`` values render
    the canonical missing placeholder ``—`` (theme.json ``placeholders``).
    """
    missing = THEME["placeholders"]["missing"]
    items = [
        "{}: {}".format(label, missing if value is None else value)
        for label, value in pairs
    ]
    return "  " + "  |  ".join(items)


def status_mark(status: str) -> str:
    """Glyph for a status word: ``✓`` / ``✕`` / ``⚠`` / ``•``.

    Unknown statuses — and ``None`` — return ``?`` (the unknown-value
    placeholder).
    """
    if status is None:
        return THEME["glyphs"]["unknown"]
    return STATUS_MARKS.get(status.strip().lower(), THEME["glyphs"]["unknown"])
