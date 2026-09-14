#!/usr/bin/env python3
"""Radar charts for fit-score breakdowns.

Takes a ``match.score_job`` result (which carries ``components`` with raw
points per axis) and renders a 5-axis radar chart as standalone SVG, plus
a multi-job comparison overlay and a self-contained HTML page.

Stdlib only. Colors follow the Veto brand: dark background, amber
primary, cyan/green/violet for comparisons.
"""

from __future__ import annotations

import html
import logging
import math
from typing import Any

log = logging.getLogger("job-apply-mcp.radar")

#: Axis order (clockwise from the top) and their max raw points.
AXES: list[str] = ["skills", "seniority", "salary", "location", "recency"]
_AXIS_MAX: dict[str, float] = {
    "skills": 50.0,
    "seniority": 15.0,
    "salary": 15.0,
    "location": 15.0,
    "recency": 5.0,
}

BG = "#0d0d14"
GRID = "#2a2a3a"
LABEL = "#9a9ab0"
MUTED = "#5b5b6e"
AMBER = "#ffb000"
VETO_RED = "#ff4d5e"
COMPARE_COLORS = [AMBER, "#41e6ff", "#3ddc84", "#b48cff"]
FONT = "ui-monospace, 'Cascadia Mono', Menlo, Consolas, monospace"


def normalize_components(components: dict[str, Any]) -> dict[str, float]:
    """Raw component points -> 0-100 per axis (missing axes score 0)."""
    out: dict[str, float] = {}
    for axis in AXES:
        raw = components.get(axis, 0) if isinstance(components, dict) else 0
        try:
            raw_f = float(raw)
        except (TypeError, ValueError):
            raw_f = 0.0
        pct = raw_f / _AXIS_MAX[axis] * 100.0
        out[axis] = max(0.0, min(100.0, pct))
    return out


def _polar(cx: float, cy: float, r: float, angle: float) -> tuple[float, float]:
    return (cx + r * math.cos(angle), cy + r * math.sin(angle))


def _axis_angles(n: int) -> list[float]:
    return [-math.pi / 2 + i * 2 * math.pi / n for i in range(n)]


def radar_svg(
    result: dict[str, Any],
    title: str | None = None,
    size: int = 400,
    color: str = AMBER,
) -> str:
    """Render one fit-score result as a radar-chart SVG string."""
    norm = normalize_components(result.get("components", {}))
    score = result.get("score", 0)
    veto = bool(result.get("veto"))
    title = title or _result_title(result)

    cx = cy = size / 2
    radius = size * 0.36
    angles = _axis_angles(len(AXES))
    parts: list[str] = []

    def esc(s: Any) -> str:
        return html.escape(str(s), quote=True)

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" font-family="{FONT}">'
    )
    parts.append(f'<rect width="{size}" height="{size}" fill="{BG}"/>')

    # Grid rings (pentagons at 25/50/75/100).
    for frac, ring_label in ((0.25, "25"), (0.5, "50"), (0.75, "75"), (1.0, "100")):
        pts = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in
            (_polar(cx, cy, radius * frac, a) for a in angles)
        )
        parts.append(
            f'<polygon points="{pts}" fill="none" stroke="{GRID}" stroke-width="1"/>'
        )
        lx, ly = _polar(cx, cy, radius * frac, angles[0])
        parts.append(
            f'<text x="{lx:.1f}" y="{ly - 4:.1f}" fill="{MUTED}" font-size="9" '
            f'text-anchor="middle">{ring_label}</text>'
        )

    # Axes + labels.
    for axis, angle in zip(AXES, angles):
        ex, ey = _polar(cx, cy, radius, angle)
        parts.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        lx, ly = _polar(cx, cy, radius + 26, angle)
        anchor = "middle"
        if abs(math.cos(angle)) > 0.3:
            anchor = "start" if math.cos(angle) > 0 else "end"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" fill="{LABEL}" font-size="12" '
            f'text-anchor="{anchor}">{esc(axis)} {norm[axis]:.0f}</text>'
        )

    # Data polygon.
    data_pts = [
        _polar(cx, cy, radius * norm[axis] / 100.0, angle)
        for axis, angle in zip(AXES, angles)
    ]
    pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in data_pts)
    parts.append(
        f'<polygon points="{pts_str}" fill="{color}" fill-opacity="0.22" '
        f'stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>'
    )
    for x, y in data_pts:
        parts.append(
            f'<rect x="{x - 3:.1f}" y="{y - 3:.1f}" width="6" height="6" '
            f'fill="{color}"/>'
        )

    # Center score.
    score_color = VETO_RED if veto else color
    parts.append(
        f'<text x="{cx:.1f}" y="{cy - 2:.1f}" fill="{score_color}" font-size="34" '
        f'font-weight="bold" text-anchor="middle">{int(score)}</text>'
    )
    parts.append(
        f'<text x="{cx:.1f}" y="{cy + 16:.1f}" fill="{MUTED}" font-size="10" '
        f'text-anchor="middle">FIT SCORE</text>'
    )
    if veto:
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + 32:.1f}" fill="{VETO_RED}" font-size="11" '
            f'text-anchor="middle">VETOED</text>'
        )

    # Title.
    parts.append(
        f'<text x="{cx:.1f}" y="26" fill="{LABEL}" font-size="14" '
        f'text-anchor="middle">{esc(title)}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _result_title(result: dict[str, Any]) -> str:
    title = str(result.get("title") or "job")
    company = str(result.get("company") or "").strip()
    return f"{title} @ {company}" if company else title


def compare_svg(
    labeled: list[tuple[str, dict[str, Any]]],
    title: str = "Fit comparison",
    size: int = 440,
) -> str:
    """Overlay up to 4 fit-score results on one radar chart."""
    labeled = labeled[:4]
    cx = cy = size / 2
    radius = size * 0.34
    angles = _axis_angles(len(AXES))
    parts: list[str] = []

    def esc(s: Any) -> str:
        return html.escape(str(s), quote=True)

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" font-family="{FONT}">'
    )
    parts.append(f'<rect width="{size}" height="{size}" fill="{BG}"/>')
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in
            (_polar(cx, cy, radius * frac, a) for a in angles)
        )
        parts.append(
            f'<polygon points="{pts}" fill="none" stroke="{GRID}" stroke-width="1"/>'
        )
    for axis, angle in zip(AXES, angles):
        ex, ey = _polar(cx, cy, radius, angle)
        parts.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        lx, ly = _polar(cx, cy, radius + 24, angle)
        anchor = "middle"
        if abs(math.cos(angle)) > 0.3:
            anchor = "start" if math.cos(angle) > 0 else "end"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" fill="{LABEL}" font-size="11" '
            f'text-anchor="{anchor}">{esc(axis)}</text>'
        )

    for i, (label, result) in enumerate(labeled):
        color = COMPARE_COLORS[i % len(COMPARE_COLORS)]
        norm = normalize_components(result.get("components", {}))
        data_pts = [
            _polar(cx, cy, radius * norm[axis] / 100.0, angle)
            for axis, angle in zip(AXES, angles)
        ]
        pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in data_pts)
        parts.append(
            f'<polygon points="{pts_str}" fill="{color}" fill-opacity="0.14" '
            f'stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
        )

    # Legend.
    lx0, ly0 = 16, size - 16 - 18 * len(labeled)
    for i, (label, result) in enumerate(labeled):
        color = COMPARE_COLORS[i % len(COMPARE_COLORS)]
        y = ly0 + i * 18
        parts.append(
            f'<rect x="{lx0}" y="{y - 9}" width="10" height="10" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{lx0 + 16}" y="{y}" fill="{LABEL}" font-size="11">'
            f'{esc(label)} ({int(result.get("score", 0))})</text>'
        )

    parts.append(
        f'<text x="{cx:.1f}" y="24" fill="{LABEL}" font-size="14" '
        f'text-anchor="middle">{esc(title)}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def radar_html(
    labeled: list[tuple[str, dict[str, Any]]],
    title: str = "Veto fit radar",
) -> str:
    """Self-contained HTML page: comparison chart + per-job reasons."""
    if len(labeled) == 1:
        label, result = labeled[0]
        chart = radar_svg(result, title=label, size=420)
    else:
        chart = compare_svg(labeled, title=title, size=460)

    sections: list[str] = []
    for i, (label, result) in enumerate(labeled):
        color = COMPARE_COLORS[i % len(COMPARE_COLORS)]
        reasons = "".join(
            f"<li>{html.escape(str(r))}</li>"
            for r in (result.get("reasons") or [])[:8]
        )
        veto_badge = (
            ' <span style="color:#ff4d5e">[VETOED]</span>'
            if result.get("veto") else ""
        )
        sections.append(
            f'<div class="job"><h2><span class="sw" style="background:{color}">'
            f"</span>{html.escape(label)}{veto_badge} "
            f'<span class="score">{int(result.get("score", 0))}/100</span></h2>'
            f"<ul>{reasons}</ul></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{background:#0d0d14;color:#c9c9d8;font-family:{FONT};margin:0;padding:32px}}
h1{{color:#ffb000;font-size:20px}}
.job{{border:1px solid #2a2a3a;margin:16px 0;padding:12px 16px;max-width:720px}}
.job h2{{font-size:14px;margin:0 0 8px}}
.sw{{display:inline-block;width:10px;height:10px;margin-right:8px}}
.score{{color:#ffb000;float:right}}
ul{{margin:0;padding-left:20px;font-size:13px;line-height:1.7}}
.wrap{{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}}
</style></head><body>
<h1>&gt; {html.escape(title)}</h1>
<div class="wrap"><div>{chart}</div><div>{"".join(sections)}</div></div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Plugin wiring (MCP + CLI)
# ---------------------------------------------------------------------------

def register_tools(mcp):  # noqa: ANN001, ANN202 - duck-typed FastMCP
    """Register the radar-chart tools on a FastMCP instance."""
    _core_svg = globals()["radar_svg"]
    _core_html = globals()["radar_html"]

    @mcp.tool()
    def radar_chart(  # noqa: F811 - intentional tool wrapper
        result: dict, title: str = "", size: int = 400
    ) -> str:
        """Render one fit-score result as a radar-chart SVG string.

        Five axes: skills, seniority, salary, location, recency.
        Vetoed results render in red. Feed it a ``match.score_job``
        result (it carries the per-axis ``components``).

        Args:
            result: scored posting dict with ``components``.
            title: chart title (defaults to the posting title).
            size: SVG width/height in px.
        """
        return _core_svg(result, title=title or None, size=size)

    @mcp.tool()
    def radar_compare_page(  # noqa: F811 - intentional tool wrapper
        entries: list, title: str = "Veto fit radar"
    ) -> str:
        """Self-contained HTML page overlaying up to 4 fit-score results.

        Args:
            entries: list of {"label": str, "result": scored posting dict}.
            title: page title.
        """
        labeled = [
            (str(e.get("label", f"job {i + 1}")), e.get("result", {}))
            for i, e in enumerate(entries)
        ]
        return _core_html(labeled, title=title)


def _cli_radar(args: Any) -> int:
    """``veto radar`` implementation."""
    import json

    if args.compare_file:
        raw = json.load(open(args.compare_file, encoding="utf-8"))
        labeled = [
            (str(e.get("label", f"job {i + 1}")), e.get("result", {}))
            for i, e in enumerate(raw)
        ]
        out = radar_html(labeled, title=args.title)
    elif args.result_file:
        result = json.load(open(args.result_file, encoding="utf-8"))
        out = (
            radar_svg(result, title=args.title or None, size=args.size)
            if not args.html
            else radar_html([(args.title or "job", result)], title=args.title)
        )
    else:
        print("error: one of --result-file or --compare-file is required", flush=True)
        return 2
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"wrote {args.out}")
    else:
        print(out)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the ``radar`` subcommand; return {command: handler}."""
    parser = subparsers.add_parser(
        "radar", help="Render fit-score radar charts (SVG or HTML)."
    )
    parser.add_argument(
        "--result-file", default="",
        help="JSON file with one scored posting (from `veto match --json`).",
    )
    parser.add_argument(
        "--compare-file", default="",
        help='JSON list of {"label", "result"} for a comparison overlay (max 4).',
    )
    parser.add_argument("--title", default="Veto fit radar", help="Chart/page title.")
    parser.add_argument("--size", type=int, default=400, help="SVG size in px.")
    parser.add_argument(
        "--html", action="store_true",
        help="Emit a self-contained HTML page instead of bare SVG.",
    )
    parser.add_argument(
        "--out", default="", help="Write output to a file instead of stdout."
    )
    return {"radar": _cli_radar}
