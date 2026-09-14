#!/usr/bin/env python3
"""Local file ingestion (Initiative 04, Epic 6).

Extract text from PDF and DOCX files (plus plain text) **locally** —
nothing is uploaded anywhere. Extraction always stops at an explicit
preview gate before any analysis runs.

``extract_file(path)`` returns::

    {"kind": "pdf"|"docx"|"text",
     "text": <extracted text>,
     "chars": int, "words": int,
     "pages": int | None, "paragraphs": int | None,
     "preview": <first ~600 chars>,
     "warnings": [...]}

Preview contract (frozen): before analysis runs, the surface must show
(1) character/word/page-or-paragraph counts, (2) the first ~600
characters of extracted text, and (3) a "this is what will be analyzed
— continue / paste text instead / cancel" confirmation. The UI layers
implement the confirmation (see integration_notes.md);
:func:`preview_report` renders the preview block and
:func:`confirm_preview_cli` provides the terminal confirmation as a
tri-state decision ("continue" | "paste" | "cancel"); "paste" starts the
stdin paste flow (:func:`read_pasted_text_cli`), which is itself
preview-gated. :func:`run_ingest_cli` wires the whole sequence for the
``veto ingest`` command; ``python -m initiatives.i04.file_ingest <path>
[--yes]`` runs it standalone.

Honesty rules:
* Scanned/image PDFs (no text layer) are **declined** with a
  plain-language message + "paste the text instead" escape hatch — no
  OCR (heavy native dependency, poor accuracy; discovery-backlog item).
* ``pypdf`` / ``python-docx`` are required dependencies for this
  module; when missing, extraction fails with an install hint instead
  of a traceback.
* Files over ``MAX_BYTES`` (20 MB) are refused before parsing.

Decision record (genuine-options entries)
----------------------------------------
D1: no OCR for scanned/image PDFs.
- Chose: decline with a plain-language message + "paste the text
  instead" escape hatch; OCR stays a discovery-backlog item.
- Over: (a) bundling Tesseract — trades easy installs and a small
  footprint for scanned-PDF coverage; (b) a cloud OCR API — trades the
  local-only privacy promise for extraction accuracy.
- Because: OCR is a heavy native dependency with poor accuracy on
  resumes, and uploading pages to a cloud API would break this module's
  core honesty rule ("nothing is uploaded anywhere"). Unknown at
  decision time: what share of real user PDFs are scanned — if it turns
  out to be most of them, revisit this.
- convenience_driven: false — the deciding constraint is the local-only
  privacy promise, not team convenience (declining OCR is *less*
  convenient for users; the escape hatch is the mitigation).

D2: tri-state terminal gate ("continue" | "paste" | "cancel") with a
real paste flow.
- Chose: the gate returns a tri-state decision; "paste" reads
  replacement text from stdin and re-runs the preview gate on it; the
  gate fails closed (any non-yes, including EOF, cancels).
- Over: (a) a boolean y/N gate — trades the promised escape hatch for
  less code, but the prompt advertised "paste" and delivered a cancel:
  a lying prompt; (b) a --yes-only flow — trades the frozen preview
  contract for speed.
- Because: the frozen contract demands continue / paste-text-instead /
  cancel; a gate that offers "paste" must honor it.
- convenience_driven: false — the tri-state gate is more code than a
  boolean; chosen because the contract requires it.

Genuine options considered (trades X for Y):

| Option | Trades away | To get |
|---|---|---|
| Tesseract OCR in-process | Simple installs, small footprint | Scanned-PDF text without leaving the machine |
| Cloud OCR API | The local-only privacy guarantee | Best extraction accuracy |
| Decline + paste hatch (chosen, D1) | Scanned-PDF coverage | Privacy kept, zero new deps, honest failure |
| Boolean y/N gate | The "paste text instead" escape hatch | Less code |
| Tri-state gate + paste flow (chosen, D2) | A little code simplicity | An honest prompt and a working escape hatch |
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

#: Refuse to parse files larger than this. This bounds the raw file
#: bytes read from disk — NOT the extracted text size (a small .docx
#: unzips to much more text), so it is an approximate, not a hard,
#: memory-safety cap on output.
MAX_BYTES = 20 * 1024 * 1024

#: Preview shows the first N characters of extracted text.
PREVIEW_CHARS = 600

#: Tri-state outcomes of the preview confirmation gate. The gate never
#: returns a bare bool: "paste" must stay reachable, and "cancel" must
#: stay distinct from it.
GATE_CONTINUE = "continue"  #: analyze the extracted text as previewed
GATE_PASTE = "paste"  #: start the "paste text instead" stdin flow
GATE_CANCEL = "cancel"  #: nothing was analyzed (fails closed)

_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}


class IngestError(ValueError):
    """The file could not be ingested (with a human-readable reason)."""


def _need_pypdf() -> Any:
    try:
        import pypdf  # type: ignore[import]

        return pypdf
    except ImportError as exc:
        raise IngestError(
            "PDF extraction needs the 'pypdf' package "
            "(pip install pypdf); paste the text instead for now"
        ) from exc


def _need_docx() -> Any:
    try:
        import docx  # type: ignore[import]

        return docx
    except ImportError as exc:
        raise IngestError(
            "DOCX extraction needs the 'python-docx' package "
            "(pip install python-docx); paste the text instead for now"
        ) from exc


def _extract_pdf(path: Path) -> dict[str, Any]:
    pypdf = _need_pypdf()
    warnings: list[str] = []
    try:
        # The reader holds the file stream open; the context manager
        # closes it on the way out, even when parsing fails midway.
        with pypdf.PdfReader(str(path)) as reader:
            pages = len(reader.pages)
            texts: list[str] = []
            empty_pages = 0
            for page in reader.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                if not text.strip():
                    empty_pages += 1
                texts.append(text)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"could not read PDF {path.name}: {exc}") from exc
    full = "\n".join(texts)
    if pages > 0 and empty_pages == pages:
        warnings.append(
            "This PDF has no extractable text layer — it looks like a "
            "scanned/image PDF. Veto does not do OCR; paste the text "
            "instead (or export the PDF as text from your reader)."
        )
    elif empty_pages:
        warnings.append(
            f"{empty_pages} of {pages} page(s) had no extractable text "
            "(images or scanned content); only the text layer was used."
        )
    return {
        "kind": "pdf",
        "text": full,
        "chars": len(full),
        "words": len(full.split()),
        "pages": pages,
        "paragraphs": None,
        "preview": full[:PREVIEW_CHARS],
        "warnings": warnings,
    }


def _extract_docx(path: Path) -> dict[str, Any]:
    docx = _need_docx()
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise IngestError(f"could not read DOCX {path.name}: {exc}") from exc
    paras = [p.text for p in document.paragraphs]
    # Tables carry real resume content (skills matrices); include them.
    for table in document.tables:
        for row in table.rows:
            paras.append(" | ".join(cell.text for cell in row.cells))
    full = "\n".join(paras)
    return {
        "kind": "docx",
        "text": full,
        "chars": len(full),
        "words": len(full.split()),
        "pages": None,
        "paragraphs": len(document.paragraphs),
        "preview": full[:PREVIEW_CHARS],
        "warnings": [],
    }


def _extract_text(path: Path) -> dict[str, Any]:
    try:
        # utf-8-sig strips a leading UTF-8 BOM so \ufeff never pollutes
        # the preview; invalid bytes still become U+FFFD via replace.
        full = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise IngestError(f"could not read {path.name}: {exc}") from exc
    return {
        "kind": "text",
        "text": full,
        "chars": len(full),
        "words": len(full.split()),
        "pages": None,
        "paragraphs": None,
        "preview": full[:PREVIEW_CHARS],
        "warnings": [],
    }


def extract_file(path: str | Path) -> dict[str, Any]:
    """Extract text from a PDF, DOCX, or plain-text file.

    Raises :class:`IngestError` with a human-readable reason when the
    file is missing, too large, unreadable, or of an unsupported type.
    Never returns partial text silently: warnings describe exactly
    what was skipped.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise IngestError(f"no such file: {path}")
    if file_path.stat().st_size > MAX_BYTES:
        raise IngestError(
            f"{file_path.name} is larger than {MAX_BYTES // (1024 * 1024)} MB; "
            "split it or paste the text instead"
        )
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(file_path)
    if suffix == ".docx":
        return _extract_docx(file_path)
    if suffix in _TEXT_SUFFIXES:
        return _extract_text(file_path)
    raise IngestError(
        f"unsupported file type {suffix or '(none)'}: Veto ingests "
        ".pdf, .docx, and .txt/.md — paste the text instead for anything else"
    )


#: Keys every extraction dict must carry for the preview gate to render.
#: ``pages``/``paragraphs``/``warnings`` are optional and default to None/[].
_PREVIEW_REQUIRED_KEYS = ("kind", "chars", "words", "preview")


def preview_report(extraction: dict[str, Any]) -> str:
    """Render the explicit preview block for the confirmation gate.

    Raises :class:`IngestError` naming the missing keys when the dict
    does not have the extraction shape — hand-built dicts fail with a
    clear message instead of a bare KeyError.
    """
    if not isinstance(extraction, dict):
        raise IngestError(
            "preview_report needs an extraction dict like the one "
            "extract_file() returns; got "
            f"{type(extraction).__name__}"
        )
    missing = [k for k in _PREVIEW_REQUIRED_KEYS if k not in extraction]
    if missing:
        raise IngestError(
            "preview_report is missing required "
            f"{'key' if len(missing) == 1 else 'keys'}: "
            + ", ".join(repr(k) for k in missing)
            + " — pass the dict returned by extract_file()"
        )
    lines = [
        "=== Extracted text preview ===",
        f"Source kind : {extraction['kind']}",
        f"Characters  : {extraction['chars']:,}",
        f"Words       : {extraction['words']:,}",
    ]
    if extraction.get("pages") is not None:
        lines.append(f"Pages       : {extraction['pages']}")
    if extraction.get("paragraphs") is not None:
        lines.append(f"Paragraphs  : {extraction['paragraphs']}")
    for warning in extraction.get("warnings", []):
        lines.append(f"WARNING     : {warning}")
    lines.append("--- first 600 characters (what will be analyzed) ---")
    lines.append(extraction["preview"] or "(no text extracted)")
    lines.append("--- end preview ---")
    lines.append(
        "This is what will be analyzed. Continue with this text, "
        "paste text instead, or cancel?"
    )
    return "\n".join(lines)


def confirm_preview_cli(extraction: dict[str, Any]) -> str:
    """Terminal confirmation gate: print preview, ask to continue.

    Returns one of "continue" | "paste" | "cancel" (see GATE_*). Only an
    explicit "y"/"yes" continues; "paste" starts the paste-instead flow;
    anything else — including EOF — cancels, so the gate fails closed.
    """
    print(preview_report(extraction))
    try:
        answer = input("Analyze this text? [y/N/paste] ").strip().lower()
    except EOFError:
        return GATE_CANCEL
    if answer in ("y", "yes"):
        return GATE_CONTINUE
    if answer == "paste":
        return GATE_PASTE
    return GATE_CANCEL


def read_pasted_text_cli() -> str:
    """Read replacement text from stdin (the gate's "paste" branch).

    The user pastes, then signals end-of-input; returns the pasted text
    verbatim (possibly empty — the caller decides what that means).
    """
    print(
        "Paste the text, then signal end-of-input "
        "(Ctrl-D on macOS/Linux, Ctrl-Z then Enter on Windows):"
    )
    try:
        return sys.stdin.read()
    except KeyboardInterrupt:
        print()
        return ""


def _pasted_extraction(text: str) -> dict[str, Any]:
    """Build an extraction-shaped dict for user-pasted text (kind "text")."""
    return {
        "kind": "text",
        "text": text,
        "chars": len(text),
        "words": len(text.split()),
        "pages": None,
        "paragraphs": None,
        "preview": text[:PREVIEW_CHARS],
        "warnings": [],
    }


def run_ingest_cli(path: str | Path, *, assume_yes: bool = False) -> int:
    """CLI handler: extract -> preview gate -> print text; returns exit status.

    Wires the tri-state gate: "continue" prints the extracted text,
    "paste" starts the stdin paste flow (the pasted text is itself
    preview-gated, per the frozen contract — the loop allows re-pasting),
    "cancel" prints the cancellation message. The gate fails closed:
    anything but an explicit yes ends with nothing analyzed.
    """
    try:
        extraction = extract_file(path)
    except IngestError as exc:
        print(f"Error: {exc}")
        return 1
    if not assume_yes:
        while True:
            decision = confirm_preview_cli(extraction)
            if decision == GATE_PASTE:
                pasted = read_pasted_text_cli()
                if not pasted.strip():
                    print("Cancelled — no text was pasted; nothing was analyzed.")
                    return 0
                extraction = _pasted_extraction(pasted)
                continue
            if decision == GATE_CANCEL:
                print("Cancelled — nothing was analyzed.")
                return 0
            break
    print(extraction["text"])
    return 0


def main(argv: list[str] | None = None) -> int:
    """``python -m initiatives.i04.file_ingest <path> [--yes]``."""
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Extract text from a PDF/DOCX/TXT file with an explicit "
            "preview gate before analysis."
        )
    )
    parser.add_argument("path", help="file to ingest")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the preview confirmation gate",
    )
    args = parser.parse_args(argv)
    return run_ingest_cli(args.path, assume_yes=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
