"""Unit tests for browser_apply form logic using a fake page (no browser).

The FakePage/FakeLocator below emulate the Playwright surface that
browser_apply.py touches: evaluate(), locator(), screenshot().
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import browser_apply  # noqa: E402


# ---------------------------------------------------------------------------
# Fake Playwright surface
# ---------------------------------------------------------------------------


def _raw_field(index, *, tag="input", type="text", name="", id="",
               placeholder="", label="", visible=True):
    return {
        "index": index, "tag": tag, "type": type, "name": name, "id": id,
        "placeholder": placeholder, "label": label, "aria": "",
        "visible": visible,
    }


class FakeElement:
    """Emulates a Playwright Locator bound to one fake field."""

    def __init__(self, page, index):
        self._page = page
        self._index = index

    # -- element actions -------------------------------------------------
    def fill(self, value):
        field = self._page.fields[self._index]
        self._page.filled[field["key"]] = value

    def set_input_files(self, path):
        field = self._page.fields[self._index]
        assert Path(path).is_file(), f"resume missing: {path}"
        self._page.uploaded[field["key"]] = path

    def select_option(self, index=None, **kwargs):
        field = self._page.fields[self._index]
        self._page.selected[field["key"]] = field["options"][index]

    def click(self, timeout=None):
        self._page.submitted = True

    # -- introspection ----------------------------------------------------
    def is_visible(self):
        return True

    def count(self):
        return 1


class FakeLocator:
    def __init__(self, page, selector, *, count=0, single_index=None):
        self._page = page
        self._selector = selector
        self._count = count
        self._single_index = single_index

    def nth(self, i):
        return FakeElement(self._page, i)

    @property
    def first(self):
        return FakeElement(self._page, self._single_index or 0)

    def count(self):
        return self._count

    def is_visible(self):
        return True

    def click(self, timeout=None):
        self._page.submitted = True


class FakePage:
    """Emulates the Playwright Page methods browser_apply uses."""

    def __init__(self, fields, *, has_submit=True):
        # fields: list of dicts with keys: key, tag, type, name, id,
        # placeholder, label, visible, options (for selects)
        self.fields = fields
        self.filled = {}
        self.uploaded = {}
        self.selected = {}
        self.submitted = False
        self.url = "file:///tmp/apply_form.html"
        self._has_submit = has_submit

    # -- evaluate dispatch -------------------------------------------------
    def evaluate(self, js, arg=None):
        if "getBoundingClientRect" in js:
            return [
                _raw_field(
                    i, tag=f["tag"], type=f["type"], name=f.get("name", ""),
                    id=f.get("id", ""), placeholder=f.get("placeholder", ""),
                    label=f.get("label", ""), visible=f["visible"],
                )
                for i, f in enumerate(self.fields)
            ]
        if "findIndex" in js:  # _select_matching_option
            field_index, needle = arg
            options = self.fields[field_index].get("options", [])
            needle = needle.lower()
            for i, opt in enumerate(options):
                if needle in opt.lower():
                    return i
            return -1
        if "querySelectorAll('button')" in js:  # consent banners
            return 0
        return None

    def locator(self, selector):
        if selector == "input, textarea, select":
            return FakeLocator(self, selector)
        if self._has_submit and selector == "button[type='submit']":
            return FakeLocator(self, selector, count=1, single_index=0)
        return FakeLocator(self, selector, count=0)

    def screenshot(self, path=None, full_page=False):
        Path(path).write_bytes(b"fake-png")
        return b"fake-png"


def _standard_fields():
    return [
        {"key": "first_name", "tag": "input", "type": "text",
         "name": "first_name", "id": "", "placeholder": "", "label": "First name",
         "visible": True},
        {"key": "last_name", "tag": "input", "type": "text",
         "name": "last_name", "id": "", "placeholder": "", "label": "Last name",
         "visible": True},
        {"key": "email", "tag": "input", "type": "email",
         "name": "email", "id": "", "placeholder": "you@example.com",
         "label": "", "visible": True},
        {"key": "phone", "tag": "input", "type": "tel",
         "name": "phone", "id": "", "placeholder": "", "label": "",
         "visible": True},
        {"key": "location", "tag": "select", "type": "",
         "name": "location", "id": "", "placeholder": "", "label": "Location",
         "visible": True, "options": ["Choose…", "New York, NY", "Remote"]},
        {"key": "linkedin", "tag": "input", "type": "url",
         "name": "linkedin", "id": "", "placeholder": "", "label": "",
         "visible": True},
        {"key": "resume", "tag": "input", "type": "file",
         "name": "resume", "id": "", "placeholder": "", "label": "",
         "visible": True},
        {"key": "cover_letter", "tag": "textarea", "type": "",
         "name": "cover_letter", "id": "", "placeholder": "", "label": "",
         "visible": True},
        {"key": "eeo", "tag": "input", "type": "checkbox",
         "name": "eeo_consent", "id": "", "placeholder": "", "label": "",
         "visible": True},
        # must be excluded from detection:
        {"key": "csrf", "tag": "input", "type": "hidden",
         "name": "csrf_token", "id": "", "placeholder": "", "label": "",
         "visible": True},
        {"key": "submit", "tag": "input", "type": "submit",
         "name": "submit", "id": "", "placeholder": "", "label": "",
         "visible": True},
        {"key": "ghost", "tag": "input", "type": "text",
         "name": "ghost", "id": "", "placeholder": "", "label": "",
         "visible": False},
    ]


PROFILE = {
    "full_name": "Ada Lovelace",
    "email": "ada@example.com",
    "phone": "+1 555-0100",
    "location": "New York, NY",
    "linkedin_url": "https://www.linkedin.com/in/adalovelace",
    "website": "https://adalovelace.dev",
    "cover_letter": "Dear hiring team, ...",
}


class TestFieldDetection(unittest.TestCase):
    def test_hidden_submit_invisible_excluded(self):
        page = FakePage(_standard_fields())
        fields = browser_apply.extract_form_fields(page)
        names = [f["name"] for f in fields]
        self.assertNotIn("csrf_token", names)   # type=hidden skipped
        self.assertNotIn("submit", names)       # type=submit skipped
        self.assertNotIn("ghost", names)        # invisible skipped
        for expected in ("first_name", "last_name", "email", "phone",
                         "location", "linkedin", "resume", "cover_letter",
                         "eeo_consent"):
            self.assertIn(expected, names)

    def test_field_shape(self):
        page = FakePage(_standard_fields())
        for f in browser_apply.extract_form_fields(page):
            for key in ("name", "label", "type", "selector"):
                self.assertIn(key, f)


class TestNameSplitting(unittest.TestCase):
    def test_full_name_split(self):
        first, last = browser_apply._split_name({"full_name": "Ada Lovelace"})
        self.assertEqual((first, last), ("Ada", "Lovelace"))

    def test_explicit_names_win(self):
        first, last = browser_apply._split_name({
            "full_name": "Ada Lovelace",
            "first_name": "Augusta", "last_name": "King",
        })
        self.assertEqual((first, last), ("Augusta", "King"))

    def test_single_word(self):
        first, last = browser_apply._split_name({"full_name": "Madonna"})
        self.assertEqual((first, last), ("Madonna", ""))

    def test_empty(self):
        self.assertEqual(browser_apply._split_name({}), ("", ""))


class TestFillApplication(unittest.TestCase):
    def test_all_fields_mapped(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(b"%PDF-1.4 fake")
            resume = fh.name
        page = FakePage(_standard_fields())
        filled = browser_apply.fill_application(page, PROFILE, resume)
        self.assertEqual(filled["first_name"], "Ada")
        self.assertEqual(filled["last_name"], "Lovelace")
        self.assertEqual(filled["email"], "ada@example.com")
        self.assertEqual(filled["phone"], "+1 555-0100")
        self.assertEqual(filled["location"], "New York, NY")
        self.assertEqual(filled["linkedin"],
                         "https://www.linkedin.com/in/adalovelace")
        self.assertIn("resume", filled)
        self.assertEqual(filled["cover_letter"], "Dear hiring team, ...")
        Path(resume).unlink()

    def test_checkbox_skipped(self):
        page = FakePage(_standard_fields())
        filled = browser_apply.fill_application(page, PROFILE, None)
        self.assertNotIn("eeo_consent", filled)
        # checkbox element must never be touched
        self.assertNotIn("eeo_consent", page.filled)

    def test_resume_only_when_file_exists(self):
        page = FakePage(_standard_fields())
        filled = browser_apply.fill_application(
            page, PROFILE, "/tmp/does-not-exist-resume.pdf")
        self.assertNotIn("resume", filled)
        self.assertEqual(page.uploaded, {})

    def test_empty_profile_does_not_crash(self):
        page = FakePage(_standard_fields())
        filled = browser_apply.fill_application(page, {}, None)
        self.assertEqual(filled, {})

    def test_partial_profile_fills_only_matches(self):
        page = FakePage(_standard_fields())
        filled = browser_apply.fill_application(
            page, {"email": "x@y.z"}, None)
        self.assertEqual(filled, {"email": "x@y.z"})


class TestSubmitDetection(unittest.TestCase):
    def test_no_submit_button_locator_exists(self):
        # Legal-hardening commit 6 deleted the submit-button locator
        # (find_submit_button): Veto never locates a submit control,
        # so the locator must not exist at all.
        self.assertFalse(hasattr(browser_apply, "find_submit_button"))

    def test_consent_banners_best_effort(self):
        page = FakePage(_standard_fields())
        self.assertEqual(browser_apply.dismiss_consent_banners(page), 0)


if __name__ == "__main__":
    unittest.main()
