#!/usr/bin/env python3
"""Resume builder for the job-apply MCP server.

Usage::

    python3 resume_builder.py [profile_path]

``profile_path`` defaults to ``profiles/profile.json``. Always emits
``resume.md`` and ``resume.html`` next to the profile. A PDF is produced
whenever possible: via WeasyPrint if installed, else via ReportLab if
installed; otherwise a note is printed explaining how to get PDF support.

Run interactively or non-interactively — this script never prompts.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE = BASE_DIR / "profiles" / "profile.json"

# ---------------------------------------------------------------------------
# Loading & helpers
# ---------------------------------------------------------------------------


def load_profile(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"Profile not found: {path}\nRun `python3 wizard.py` first.")
    except json.JSONDecodeError as exc:
        sys.exit(f"Profile is not valid JSON: {exc}")


def esc(text: str) -> str:
    return html.escape(text or "")


def dates(exp_or_edu: dict) -> str:
    start = (exp_or_edu.get("start") or "").strip()
    end = (exp_or_edu.get("end") or "").strip() or "Present"
    if start and end != "Present":
        return f"{start} – {end}"
    if start:
        return f"{start} – Present"
    return "Present" if end == "Present" else end


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def build_markdown(profile: dict) -> str:
    lines: list[str] = []
    lines.append(f"# {profile.get('full_name', '')}")
    contact = [profile.get("email", ""), profile.get("phone", ""),
               profile.get("location", "")]
    links = [l for l in (profile.get("linkedin_url", ""),
                        profile.get("website", "")) if l]
    lines.append(" | ".join([c for c in contact if c]))
    if links:
        lines.append(" | ".join(links))
    lines.append("")

    if profile.get("summary"):
        lines.append("## Summary")
        lines.append(profile["summary"])
        lines.append("")

    if profile.get("experience"):
        lines.append("## Experience")
        for exp in profile["experience"]:
            header = exp.get("title", "")
            if exp.get("company"):
                header += f" — {exp['company']}"
            lines.append(f"### {header}")
            meta = dates(exp)
            if exp.get("location"):
                meta += f" · {exp['location']}"
            lines.append(f"*{meta}*")
            if exp.get("description"):
                lines.append("")
                lines.append(exp["description"])
            lines.append("")

    if profile.get("education"):
        lines.append("## Education")
        for edu in profile["education"]:
            creds = ", ".join(
                p for p in (edu.get("degree", ""), edu.get("field", "")) if p
            )
            lines.append(f"### {edu.get('school', '')}")
            line = f"*{creds}*" if creds else ""
            dr = dates(edu)
            if dr and dr != "Present":
                line = f"{line} ({dr})" if line else dr
            if line:
                lines.append(line)
            lines.append("")

    if profile.get("skills"):
        lines.append("## Skills")
        lines.append(", ".join(profile["skills"]))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# HTML (single page, inline CSS)
# ---------------------------------------------------------------------------

_CSS = """\
body{font-family:Georgia,'Times New Roman',serif;color:#222;max-width:760px;
margin:32px auto;padding:0 24px;line-height:1.5}
header{text-align:center;border-bottom:2px solid #333;padding-bottom:12px;
margin-bottom:20px}
h1{margin:0;font-size:2em}
.contact{margin-top:6px;font-size:.95em;color:#555}
h2{font-size:1.15em;text-transform:uppercase;letter-spacing:.06em;
border-bottom:1px solid #ccc;padding-bottom:4px;margin-top:28px}
h3{margin-bottom:2px}
.meta{color:#666;font-style:italic;font-size:.92em;margin:0 0 6px}
p{margin:6px 0}
ul{margin:6px 0;padding-left:20px}
"""


def build_html(profile: dict) -> str:
    parts: list[str] = [
        "<!DOCTYPE html>", "<html lang='en'>", "<head>",
        "<meta charset='utf-8'>",
        f"<title>Resume — {esc(profile.get('full_name', ''))}</title>",
        f"<style>{_CSS}</style>", "</head>", "<body>",
        "<header>",
        f"<h1>{esc(profile.get('full_name', ''))}</h1>",
    ]
    contact = " · ".join(
        c for c in (profile.get("email", ""), profile.get("phone", ""),
                    profile.get("location", "")) if c
    )
    if contact:
        parts.append(f"<div class='contact'>{esc(contact)}</div>")
    links = []
    if profile.get("linkedin_url"):
        links.append(
            f"<a href='{esc(profile['linkedin_url'])}'>LinkedIn</a>"
        )
    if profile.get("website"):
        links.append(f"<a href='{esc(profile['website'])}'>Website</a>")
    if links:
        parts.append(f"<div class='contact'>{' · '.join(links)}</div>")
    parts.append("</header>")

    if profile.get("summary"):
        parts += ["<h2>Summary</h2>", f"<p>{esc(profile['summary'])}</p>"]

    if profile.get("experience"):
        parts.append("<h2>Experience</h2>")
        for exp in profile["experience"]:
            header = esc(exp.get("title", ""))
            if exp.get("company"):
                header += f" — {esc(exp['company'])}"
            parts.append(f"<h3>{header}</h3>")
            meta = esc(dates(exp))
            if exp.get("location"):
                meta += f" · {esc(exp['location'])}"
            parts.append(f"<p class='meta'>{meta}</p>")
            if exp.get("description"):
                desc = "".join(
                    f"<li>{esc(line)}</li>"
                    for line in exp["description"].splitlines()
                    if line.strip()
                )
                parts.append(f"<ul>{desc}</ul>" if desc
                            else f"<p>{esc(exp['description'])}</p>")

    if profile.get("education"):
        parts.append("<h2>Education</h2>")
        for edu in profile["education"]:
            parts.append(f"<h3>{esc(edu.get('school', ''))}</h3>")
            creds = ", ".join(
                p for p in (edu.get("degree", ""), edu.get("field", "")) if p
            )
            line = esc(creds)
            dr = dates(edu)
            if dr and dr != "Present":
                line = f"{line} ({esc(dr)})" if line else esc(dr)
            if line:
                parts.append(f"<p class='meta'>{line}</p>")

    if profile.get("skills"):
        parts.append("<h2>Skills</h2>")
        parts.append(f"<p>{esc(', '.join(profile['skills']))}</p>")

    parts += ["</body>", "</html>"]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# PDF backends (checked at runtime; never fatal)
# ---------------------------------------------------------------------------


def write_pdf_weasyprint(html_text: str, out: Path) -> bool:
    try:
        import weasyprint  # type: ignore
    except ImportError:
        return False
    try:
        weasyprint.HTML(string=html_text).write_pdf(str(out))
        return True
    except Exception as exc:  # e.g. missing pango/cairo system deps
        print(f"  WeasyPrint failed ({exc}); trying next backend.", file=sys.stderr)
        return False


def write_pdf_reportlab(profile: dict, out: Path) -> bool:
    try:
        from reportlab.lib.pagesizes import letter  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet  # type: ignore
        from reportlab.platypus import (  # type: ignore
            Paragraph, SimpleDocTemplate, Spacer,
        )
    except ImportError:
        return False
    try:
        styles = getSampleStyleSheet()
        story = []
        add = lambda el: story.append(el)
        add(Paragraph(esc(profile.get("full_name", "")), styles["Title"]))
        contact = " | ".join(
            c for c in (profile.get("email", ""), profile.get("phone", ""),
                        profile.get("location", "")) if c
        )
        if contact:
            add(Paragraph(esc(contact), styles["Normal"]))
        add(Spacer(1, 12))
        for section, key in (("Summary", None), ("Experience", "experience"),
                             ("Education", "education"), ("Skills", None)):
            if key == "experience" and profile.get("experience"):
                add(Paragraph("Experience", styles["Heading2"]))
                for exp in profile["experience"]:
                    hdr = exp.get("title", "")
                    if exp.get("company"):
                        hdr += f" — {exp['company']}"
                    add(Paragraph(esc(hdr), styles["Heading3"]))
                    if exp.get("description"):
                        add(Paragraph(esc(exp["description"]),
                                     styles["Normal"]))
            elif key == "education" and profile.get("education"):
                add(Paragraph("Education", styles["Heading2"]))
                for edu in profile["education"]:
                    creds = ", ".join(
                        p for p in (edu.get("degree", ""),
                                    edu.get("field", "")) if p
                    )
                    add(Paragraph(
                        esc(f"{edu.get('school', '')} — {creds}".rstrip(" —")),
                        styles["Heading3"]))
            elif key is None:
                if section == "Summary" and profile.get("summary"):
                    add(Paragraph("Summary", styles["Heading2"]))
                    add(Paragraph(esc(profile["summary"]), styles["Normal"]))
                if section == "Skills" and profile.get("skills"):
                    add(Paragraph("Skills", styles["Heading2"]))
                    add(Paragraph(esc(", ".join(profile["skills"])),
                                 styles["Normal"]))
            add(Spacer(1, 6))
        SimpleDocTemplate(str(out), pagesize=letter).build(story)
        return True
    except Exception as exc:
        print(f"  ReportLab failed ({exc}).", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    profile_path = Path(argv[1]).expanduser() if len(argv) > 1 else DEFAULT_PROFILE
    profile = load_profile(profile_path)
    out_dir = profile_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    md_path = out_dir / "resume.md"
    html_path = out_dir / "resume.html"
    pdf_path = out_dir / "resume.pdf"

    html_text = build_html(profile)
    md_path.write_text(build_markdown(profile), encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    print(f"Wrote {md_path}")
    print(f"Wrote {html_path}")

    pdf_ok = write_pdf_weasyprint(html_text, pdf_path)
    backend = "WeasyPrint" if pdf_ok else None
    if not pdf_ok:
        pdf_ok = write_pdf_reportlab(profile, pdf_path)
        backend = "ReportLab" if pdf_ok else None
    if pdf_ok:
        print(f"Wrote {pdf_path} (via {backend})")
    else:
        print("PDF skipped: neither weasyprint nor reportlab is installed.")
        print("  For PDF output, run:  pip install weasyprint")
        print("  (WeasyPrint also needs system libraries: pango, cairo.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
