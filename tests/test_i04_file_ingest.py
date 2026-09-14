#!/usr/bin/env python3
"""Tests for ``initiatives.i04.file_ingest`` (Epic 6).

Fixtures are generated in-test (synthetic content only); no real
resumes or files are used. Nothing here is real.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from initiatives.i04 import file_ingest  # noqa: E402

#: SYNTHETIC FIXTURE — fake resume content, generated into real files
#: in-test. Nothing here is real.
SYNTHETIC_TEXT = (
    "Jane Synthetic\n"
    "Senior Software Engineer\n"
    "- 6 years Python building data pipelines; reduced ETL runtime 40%\n"
)


def _make_pdf(path: Path, text: str) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    page = DictionaryObject()
    page.update(
        {
            NameObject("/Type"): NameObject("/Page"),
            NameObject("/MediaBox"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(612), NumberObject(792)]
            ),
        }
    )
    # Minimal text-showing content stream.
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
    font = DictionaryObject()
    font.update(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject()
    resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): font})
    page[NameObject("/Resources")] = resources
    page_ref = writer._add_object(page)
    content_ref = writer._add_object(content)
    page[NameObject("/Contents")] = content_ref
    writer._add_object(page_ref)
    # Build the page tree manually.
    pages = DictionaryObject()
    pages.update(
        {
            NameObject("/Type"): NameObject("/Pages"),
            NameObject("/Kids"): ArrayObject([page_ref]),
            NameObject("/Count"): NumberObject(1),
        }
    )
    writer._root_object[NameObject("/Pages")] = writer._add_object(pages)
    page[NameObject("/Parent")] = writer._root_object["/Pages"]
    with open(path, "wb") as fh:
        writer.write(fh)


def _make_blank_pdf(path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as fh:
        writer.write(fh)


def _make_docx(path: Path, text: str) -> None:
    import docx

    document = docx.Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    document.save(str(path))


class FileIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self._tmp_dir())
        self.tmp.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _tmp_dir() -> str:
        import tempfile

        return tempfile.mkdtemp(prefix="i04-ingest-")

    def test_pdf_extraction(self) -> None:
        pdf = self.tmp / "resume.pdf"
        _make_pdf(pdf, "Jane Synthetic 6 years Python")
        result = file_ingest.extract_file(pdf)
        self.assertEqual(result["kind"], "pdf")
        self.assertEqual(result["pages"], 1)
        self.assertIn("Jane Synthetic", result["text"])
        self.assertIn("6 years Python", result["text"])
        self.assertTrue(result["chars"] > 0)
        self.assertTrue(result["words"] > 0)
        self.assertLessEqual(len(result["preview"]), 600)

    def test_scanned_pdf_declined_with_warning(self) -> None:
        pdf = self.tmp / "scanned.pdf"
        _make_blank_pdf(pdf)
        result = file_ingest.extract_file(pdf)
        self.assertEqual(result["text"].strip(), "")
        self.assertTrue(
            any("no extractable text layer" in w for w in result["warnings"])
        )

    def test_docx_extraction(self) -> None:
        docx_path = self.tmp / "resume.docx"
        _make_docx(docx_path, SYNTHETIC_TEXT)
        result = file_ingest.extract_file(docx_path)
        self.assertEqual(result["kind"], "docx")
        self.assertIn("Jane Synthetic", result["text"])
        self.assertIn("6 years Python", result["text"])
        self.assertIsNotNone(result["paragraphs"])

    def test_txt_passthrough(self) -> None:
        txt = self.tmp / "resume.txt"
        txt.write_text(SYNTHETIC_TEXT, encoding="utf-8")
        result = file_ingest.extract_file(txt)
        self.assertEqual(result["kind"], "text")
        self.assertEqual(result["text"], SYNTHETIC_TEXT)

    def test_missing_file(self) -> None:
        with self.assertRaises(file_ingest.IngestError):
            file_ingest.extract_file(self.tmp / "nope.pdf")

    def test_unsupported_type(self) -> None:
        weird = self.tmp / "resume.png"
        weird.write_bytes(b"\x89PNG\r\n\x1a\n")
        with self.assertRaises(file_ingest.IngestError) as ctx:
            file_ingest.extract_file(weird)
        self.assertIn("unsupported file type", str(ctx.exception))

    def test_preview_report_contract(self) -> None:
        txt = self.tmp / "resume.txt"
        txt.write_text(SYNTHETIC_TEXT * 50, encoding="utf-8")
        result = file_ingest.extract_file(txt)
        report = file_ingest.preview_report(result)
        # Contract: counts + first ~600 chars + the confirmation question.
        self.assertIn("Characters", report)
        self.assertIn("Words", report)
        self.assertIn("first 600 characters", report)
        self.assertIn("Continue with this text", report)
        self.assertIn("paste text instead", report)
        self.assertLessEqual(len(result["preview"]), 600)

    def test_size_cap(self) -> None:
        big = self.tmp / "big.txt"
        big.write_bytes(b"x" * (file_ingest.MAX_BYTES + 1))
        with self.assertRaises(file_ingest.IngestError) as ctx:
            file_ingest.extract_file(big)
        self.assertIn("larger than", str(ctx.exception))

    def test_pdf_reader_closed_after_extraction(self) -> None:
        import pypdf

        pdf = self.tmp / "resume.pdf"
        _make_pdf(pdf, "Jane Synthetic")
        closed: list[bool] = []
        real_reader = pypdf.PdfReader

        class TrackingReader(real_reader):  # type: ignore[valid-type,misc]
            def close(self) -> None:
                closed.append(True)
                super().close()

        with mock.patch.object(pypdf, "PdfReader", TrackingReader):
            file_ingest.extract_file(pdf)
        self.assertTrue(closed, "PdfReader was not closed after extraction")

    def test_bom_stripped_from_text(self) -> None:
        bom = self.tmp / "bom.txt"
        bom.write_bytes(b"\xef\xbb\xbfJane Synthetic")
        result = file_ingest.extract_file(bom)
        self.assertFalse(result["text"].startswith("\ufeff"))
        self.assertTrue(result["text"].startswith("Jane Synthetic"))
        self.assertFalse(result["preview"].startswith("\ufeff"))

    def test_preview_report_rejects_bad_shape(self) -> None:
        with self.assertRaises(file_ingest.IngestError) as ctx:
            file_ingest.preview_report({"kind": "text"})  # type: ignore[dict-item]
        # The error must name the missing keys, not just raise KeyError.
        self.assertIn("'preview'", str(ctx.exception))
        self.assertIn("'chars'", str(ctx.exception))

    def test_preview_report_rejects_non_dict(self) -> None:
        with self.assertRaises(file_ingest.IngestError) as ctx:
            file_ingest.preview_report("not a dict")  # type: ignore[arg-type]
        self.assertIn("extraction dict", str(ctx.exception))


#: Minimal extraction-shaped dict for gate tests (synthetic).
_MINIMAL_EXTRACTION = {
    "kind": "text",
    "text": "hello world",
    "chars": 11,
    "words": 2,
    "pages": None,
    "paragraphs": None,
    "preview": "hello world",
    "warnings": [],
}


class PreviewGateTest(unittest.TestCase):
    """Tri-state terminal gate: continue | paste | cancel."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="i04-gate-"))

    def _gate(self, answer: str | None) -> str:
        """Run the gate with one scripted answer (None = EOF)."""
        if answer is None:
            patched = mock.patch(
                "builtins.input", side_effect=EOFError
            )
        else:
            patched = mock.patch("builtins.input", return_value=answer)
        with patched, contextlib.redirect_stdout(io.StringIO()):
            return file_ingest.confirm_preview_cli(_MINIMAL_EXTRACTION)

    def test_gate_yes_continues(self) -> None:
        self.assertEqual(self._gate("y"), file_ingest.GATE_CONTINUE)
        self.assertEqual(self._gate("yes"), file_ingest.GATE_CONTINUE)
        self.assertEqual(self._gate("  Y  "), file_ingest.GATE_CONTINUE)

    def test_gate_paste_returns_paste(self) -> None:
        # The escape hatch must be reachable — never collapse to decline.
        self.assertEqual(self._gate("paste"), file_ingest.GATE_PASTE)
        self.assertEqual(self._gate("Paste"), file_ingest.GATE_PASTE)

    def test_gate_anything_else_cancels(self) -> None:
        self.assertEqual(self._gate("n"), file_ingest.GATE_CANCEL)
        self.assertEqual(self._gate("no"), file_ingest.GATE_CANCEL)
        self.assertEqual(self._gate(""), file_ingest.GATE_CANCEL)
        self.assertEqual(self._gate("banana"), file_ingest.GATE_CANCEL)

    def test_gate_eof_cancels(self) -> None:
        self.assertEqual(self._gate(None), file_ingest.GATE_CANCEL)

    def test_cancel_flow_prints_cancellation(self) -> None:
        txt = self.tmp / "resume.txt"
        txt.write_text("original text", encoding="utf-8")
        out = io.StringIO()
        with mock.patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(out):
            rc = file_ingest.run_ingest_cli(txt)
        self.assertEqual(rc, 0)
        self.assertIn("Cancelled — nothing was analyzed.", out.getvalue())

    def test_paste_flow_replaces_text_and_regates(self) -> None:
        txt = self.tmp / "resume.txt"
        txt.write_text("original text", encoding="utf-8")
        answers = iter(["paste", "y"])
        out = io.StringIO()
        with mock.patch(
            "builtins.input", side_effect=lambda *a: next(answers)
        ), mock.patch("sys.stdin") as stdin, contextlib.redirect_stdout(out):
            stdin.read.return_value = "pasted replacement text"
            rc = file_ingest.run_ingest_cli(txt)
        self.assertEqual(rc, 0)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        # The text that is finally "analyzed" is the pasted one.
        self.assertEqual(lines[-1], "pasted replacement text")
        # The pasted text went through its own preview gate (counts shown
        # twice: once for the file, once for the paste).
        self.assertGreater(out.getvalue().count("=== Extracted text preview ==="), 1)

    def test_paste_flow_empty_paste_cancels(self) -> None:
        txt = self.tmp / "resume.txt"
        txt.write_text("original text", encoding="utf-8")
        out = io.StringIO()
        with mock.patch("builtins.input", return_value="paste"), \
                mock.patch("sys.stdin") as stdin, \
                contextlib.redirect_stdout(out):
            stdin.read.return_value = "   \n"
            rc = file_ingest.run_ingest_cli(txt)
        self.assertEqual(rc, 0)
        self.assertIn("no text was pasted", out.getvalue())

    def test_continue_flow_prints_extracted_text(self) -> None:
        txt = self.tmp / "resume.txt"
        txt.write_text("original text", encoding="utf-8")
        out = io.StringIO()
        with mock.patch("builtins.input", return_value="y"), \
                contextlib.redirect_stdout(out):
            rc = file_ingest.run_ingest_cli(txt)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().splitlines()[-1], "original text")

    def test_assume_yes_skips_gate(self) -> None:
        txt = self.tmp / "resume.txt"
        txt.write_text("original text", encoding="utf-8")
        out = io.StringIO()
        with mock.patch(
            "builtins.input", side_effect=AssertionError("gate must not prompt")
        ), contextlib.redirect_stdout(out):
            rc = file_ingest.run_ingest_cli(txt, assume_yes=True)
        self.assertEqual(rc, 0)
        self.assertIn("original text", out.getvalue())


if __name__ == "__main__":
    unittest.main()
