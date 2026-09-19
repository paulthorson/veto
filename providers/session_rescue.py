#!/usr/bin/env python3
"""Session rescue — Initiative 09, epic 5.

Pause browser-assisted fills on CAPTCHA, auth loss, or field ambiguity and
hand control to the user.

THE CORE RULE — CAPTCHA IS NEVER BYPASSED. There is deliberately no code
path in this module (or anywhere in Veto) that solves, clicks through, or
otherwise defeats a CAPTCHA. When one is detected, automation STOPS, a
screenshot is taken, and a handoff bundle is produced so the human can take
over in their own browser. The pause-and-hand-off IS the feature; any
bypass attempt is never made and would be a program-level incident.

Detection runs at three points in a fill:
  1. After navigation / page load (CAPTCHA, auth loss).
  2. After field extraction (field ambiguity).
  3. At the review step of multi-step flows, and immediately before the
     submit click in single-page flows (CAPTCHA, auth loss re-check).

The ``page`` argument is duck-typed: any object exposing ``query_selector``,
``url``, and ``content()`` works (Playwright pages and test fakes alike).

Internationalization (i18n) scope — honest limits:
  Text markers are English plus markers for the four most common
  non-English job-board languages: German, French, Spanish, Portuguese
  (CAPTCHA and auth-loss markers). Every other language is NOT covered:
  a CAPTCHA wall or login wall written only in, say, Dutch or Japanese
  will be missed by the text markers (CSS selectors still fire when the
  widget markup matches, whatever the language). This is a documented
  gap, not a silent one — if a wall in another language is missed, add
  its markers here and extend the i18n tests.

Decision log (Rules 3 and 4 — see the rule-citation note at the end):
  D1 — fail-CLOSED on detector failure (revised 2026-09-13; supersedes the
  earlier fail-open entry after a Rule-1 veto in blind QA review).
    Chose: ``check()`` fails CLOSED. Any exception in a detector, or in the
      handoff path after a hit, produces a ``RescueHandoff`` with reason
      "detector_failure" — a loud pause — instead of returning None.
      There are no internal failure swallows left inside the detectors:
      a dead page, a closed page, or a ``None`` page raises on
      ``query_selector`` / ``content()`` / ``url`` access, and that
      exception propagates to ``check()``'s handler. Failure classes
      that produce loud handoffs: any detector exception (including
      page-access failures), and any handoff-path exception (malformed
      detector output, unreadable ``page.url``). Nothing is handled
      silently — a None return means every detector ran clean and found
      nothing.
    Over: fail-open (log a warning, return None, keep filling).
    Trades away: a detector crash can no longer be ridden out — one broken
      detector pauses fills until it is fixed.
    To get: a broken safety net is loud, never invisible. A silent None is
      indistinguishable from "no CAPTCHA found", so automation would march
      on to submit and the user would believe an application went through
      a CAPTCHA wall.
    Because: Rule 1 — user-harm vetoes are absolute. Submitting through a
      possible CAPTCHA wall without the user knowing is exactly the harm
      this module exists to prevent; the pause IS the feature.
    product_goal: undetected-CAPTCHA submissions, down. (Rule 4: a metric
      and a direction, not "better quality".)
    scope_driven: false. (Rule 3: no scope pressure behind this call — it
      is a safety call, so nothing routes to the human gate.)
    Confidence: high.
    Unknown at decision time: which detector will break first against
      hostile page DOMs, and how often fail-closed pauses turn out to be
      false alarms.
  Rule-citation note: decision records are governed by Rule 3
  (``scope_driven``) and Rule 4 (``product_goal``). An earlier entry cited
  Rule 2, which was wrong — Rule 2 governs test strategies, not decision
  logs.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.session_rescue")

# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

#: CAPTCHA widget markers grouped by provider family. Kept as data so the
#: family-coverage policy test can assert every known family is covered —
#: each family must contribute at least one selector to CAPTCHA_SELECTORS,
#: and every selector in CAPTCHA_SELECTORS must belong to exactly one
#: family, so a reviewer can see exactly what "CAPTCHA detected" means.
CAPTCHA_FAMILIES: dict[str, tuple[str, ...]] = {
    "recaptcha": (
        'iframe[src*="recaptcha"]',  # Google reCAPTCHA
        'iframe[src*="recaptcha.net"]',
        ".g-recaptcha",
        "[data-sitekey]",  # reCAPTCHA / hCaptcha site key marker
    ),
    "hcaptcha": (
        'iframe[src*="hcaptcha.com"]',  # hCaptcha
        ".h-captcha",
    ),
    "turnstile": (
        'iframe[src*="challenges.cloudflare.com"]',  # Cloudflare Turnstile
        ".cf-turnstile",
    ),
    "arkose": (  # Arkose Labs / FunCaptcha
        'iframe[src*="arkoselabs.com"]',
        'iframe[src*="funcaptcha"]',
        "#FunCaptcha",
    ),
    "aws_waf": (  # AWS WAF CAPTCHA
        'iframe[src*="captcha-delivery.com"]',
        ".aws-waf-captcha",
    ),
    "datadome": (  # DataDome bot-protection challenge
        'iframe[src*="datadome"]',
    ),
    "perimeterx": (  # PerimeterX / HUMAN Enterprise challenge
        'iframe[src*="perimeterx"]',
        "#px-captcha",
    ),
    "geetest": (  # GeeTest slide / click CAPTCHA
        'iframe[src*="geetest"]',
        ".geetest_holder",
    ),
    "generic": (  # last-resort fallback for unknown providers
        'iframe[src*="captcha"]',
        "#captcha",
        ".captcha",
        'img[src*="captcha"]',
        'input[name*="captcha"]',
    ),
}

#: Flat tuple of every known CAPTCHA selector, built from the families so
#: the two can never drift apart.
CAPTCHA_SELECTORS: tuple[str, ...] = tuple(
    selector
    for selectors in CAPTCHA_FAMILIES.values()
    for selector in selectors
)

#: Text markers (lowercased) that indicate a CAPTCHA when selectors miss.
#: Matched with word boundaries (see _word_boundary_patterns), and covering
#: English plus German, French, Spanish, Portuguese. See the i18n note in
#: the module docstring for the honest limits of this list.
CAPTCHA_TEXT_MARKERS: tuple[str, ...] = (
    # English
    "verify you are human",
    "i'm not a robot",
    "i am not a robot",
    "complete the security check",
    "unusual traffic",
    "automated access",
    # German
    "ich bin kein roboter",
    "sie ein mensch sind",
    "sicherheitsüberprüfung",
    # French
    "je ne suis pas un robot",
    "êtes humain",
    "contrôle de sécurité",
    # Spanish
    "no soy un robot",
    "eres humano",
    "verificación de seguridad",
    # Portuguese
    "não sou um robô",
    "você é humano",
    "verificação de segurança",
)

#: URL fragments that indicate the session was bounced to a login wall.
#: Path markers ("/login") only match at a path boundary — "/login-help"
#: does NOT match — and subdomain markers ("login.") must not match
#: inside a longer hostname ("mylogin.example.com").
AUTH_LOSS_URL_MARKERS: tuple[str, ...] = (
    "/login",
    "/signin",
    "/sign-in",
    "login.",
    "authwall",
    "checkpoint",
)

#: Page-text markers (lowercased) for a lost/expired session. English plus
#: German, French, Spanish, Portuguese; same i18n limits as above.
AUTH_LOSS_TEXT_MARKERS: tuple[str, ...] = (
    # English
    "session expired",
    "please log in",
    "please sign in",
    "log in to continue",
    "sign in to continue",
    "your session has expired",
    # German
    "sitzung abgelaufen",
    "bitte melden sie sich an",
    "melden sie sich an, um fortzufahren",
    # French
    "session expirée",
    "veuillez vous connecter",
    "connectez-vous pour continuer",
    # Spanish
    "sesión expirada",
    "inicia sesión para continuar",
    "por favor inicia sesión",
    # Portuguese
    "sessão expirada",
    "faça login para continuar",
    "entre para continuar",
)


def _word_boundary_patterns(markers: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    """Compile markers to word-boundary regexes.

    Raw substring matching false-positives: "automated access" would fire
    on "semi-automated accesses", and "unusual traffic" on "unusual
    trafficker". Word boundaries keep each marker a whole phrase.
    """
    return tuple(re.compile(r"\b%s\b" % re.escape(m)) for m in markers)


_CAPTCHA_TEXT_PATTERNS = _word_boundary_patterns(CAPTCHA_TEXT_MARKERS)
_AUTH_LOSS_TEXT_PATTERNS = _word_boundary_patterns(AUTH_LOSS_TEXT_MARKERS)


def _page_text(page: Any) -> str:
    """Strip tags from ``page.content()`` and lowercase it.

    Fail-closed: a page-access failure (dead page, closed page, ``None``
    page) is NOT swallowed here — it propagates to ``check()``, which
    converts it into a loud "detector_failure" handoff. A swallowed
    failure would turn into an empty string and then a silent None,
    indistinguishable from "no wall found".
    """
    content = page.content()
    return re.sub(r"<[^>]+>", " ", str(content)).lower()


def detect_captcha(page: Any) -> dict[str, Any] | None:
    """Return evidence dict when a CAPTCHA is present, else None.

    Fail-closed: a ``query_selector`` failure is NOT swallowed — it
    propagates to ``check()``, which converts it into a loud
    "detector_failure" handoff. (An earlier revision skipped failed
    selectors with ``except Exception: continue``; a dead page then
    raised on every selector, was skipped every time, and ``check()``
    returned a silent None — the same Rule-1 defect class as the
    original veto.)
    """
    for selector in CAPTCHA_SELECTORS:
        if page.query_selector(selector):
            log.warning("Session rescue: CAPTCHA selector matched: %s", selector)
            return {"reason": "captcha", "evidence": f"selector:{selector}"}
    text = _page_text(page)
    for marker, pattern in zip(CAPTCHA_TEXT_MARKERS, _CAPTCHA_TEXT_PATTERNS):
        if pattern.search(text):
            log.warning("Session rescue: CAPTCHA text marker matched: %r", marker)
            return {"reason": "captcha", "evidence": f"text:{marker}"}
    return None


def _match_auth_url_marker(url: str) -> str | None:
    """Match a URL marker at a real boundary, or return None.

    "/login" matches "/login", "/login/", "/login?x" — but not
    "/login-help" or "/loginpage". "login." matches the subdomain in
    "login.example.com" — but not "mylogin.example.com". Bare word
    markers ("authwall", "checkpoint") only match outside longer words,
    so a job slug like "jobs/checkpoint-analyst-123" does NOT cause a
    needless pause, while "/checkpoint" still does.
    """
    for marker in AUTH_LOSS_URL_MARKERS:
        if marker.startswith("/"):
            if re.search(re.escape(marker) + r"(?![-\w])", url):
                return marker
        elif marker.endswith("."):
            if re.search(r"(?<![-\w])" + re.escape(marker), url):
                return marker
        elif re.search(r"(?<![-\w])" + re.escape(marker) + r"(?![-\w])", url):
            return marker
    return None


def detect_auth_loss(page: Any, board: str | None = None) -> dict[str, Any] | None:
    """Return evidence dict when the session hit a login wall, else None.

    Fail-closed: a ``page.url`` failure is NOT swallowed — it propagates
    to ``check()``, which converts it into a loud "detector_failure"
    handoff.
    """
    url = str(page.url or "").lower()
    marker = _match_auth_url_marker(url)
    if marker is not None:
        log.warning("Session rescue: auth-loss URL marker %r in %s", marker, url)
        return {"reason": "auth_loss", "evidence": f"url:{marker}"}
    text = _page_text(page)
    for marker, pattern in zip(AUTH_LOSS_TEXT_MARKERS, _AUTH_LOSS_TEXT_PATTERNS):
        if pattern.search(text):
            log.warning("Session rescue: auth-loss text marker %r", marker)
            return {"reason": "auth_loss", "evidence": f"text:{marker}"}
    return None


#: Input types whose duplicate labels are ambiguous (M3: text-like inputs
#: plus tel, textarea, select — any field the profile would fill).
_TEXT_LIKE_TYPES = ("text", "textbox", "email", "password", "tel", "textarea", "select", "")


def assess_field_ambiguity(fields: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return evidence dict when fields can't be mapped confidently.

    Three triggers:
    * A genuinely anonymous field — empty name AND no label, placeholder,
      or aria-label (hidden inputs excluded; they are never user-filled).
      Veto would be guessing what it is.
    * A visible field whose name is a synthesized numeric fallback
      (``field_N`` — digits only, so real names like ``field_of_study``
      do NOT false-positive) and which has no label. (The name is the
      fallback itself, so there is no "or name" qualifier here: with no
      label, the field is unidentified either way.)
    * Two or more text-like fields (text, textbox, email, password, tel,
      textarea, select) sharing the same normalized label — Veto can't
      tell which one the profile value belongs in.
    """
    if not fields:
        return None
    unlabeled: list[str] = []
    seen: dict[str, list[str]] = {}
    for i, f in enumerate(fields):
        name = str(f.get("name") or "")
        label = str(f.get("label") or "").strip()
        placeholder = str(f.get("placeholder") or "").strip()
        aria = str(f.get("aria_label") or f.get("aria-label") or "").strip()
        ftype = str(f.get("type") or "").lower()
        tag = f.get("selector") or name or f"<field #{i}>"
        if not (name or label or placeholder or aria):
            # Genuinely anonymous: no name, label, placeholder, or
            # aria-label at all. Hidden inputs are excluded — they are
            # never user-filled, so flagging them would false-positive.
            if ftype != "hidden":
                unlabeled.append(tag)
        elif re.fullmatch(r"field_\d+", name) and not label:
            unlabeled.append(tag)
        if ftype in _TEXT_LIKE_TYPES and label:
            key = re.sub(r"\s+", " ", label).lower()
            seen.setdefault(key, []).append(name or tag)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if unlabeled or dupes:
        parts = []
        if unlabeled:
            parts.append(f"{len(unlabeled)} unlabeled field(s): {unlabeled[:3]}")
        if dupes:
            parts.append(
                "duplicate labels: "
                + ", ".join(f"{k!r} x{len(v)}" for k, v in list(dupes.items())[:3])
            )
        return {"reason": "field_ambiguity", "evidence": "; ".join(parts)}
    return None


# ---------------------------------------------------------------------------
# Handoff bundle
# ---------------------------------------------------------------------------


def _screenshots_dir() -> str:
    """Where the caller saves rescue screenshots.

    Mirrors ``browser_apply.SCREENSHOTS_DIR`` without importing it (that
    module imports this one, so the import would be circular).
    """
    return str(Path(__file__).resolve().parent.parent / "screenshots")


def _guidance_for(
    reason: str,
    *,
    screenshot_path: str | None,
    page_url: str,
    board: str | None,
) -> str:
    """Plain-language, actionable handoff text for non-technical users.

    Every reason's guidance states (1) what happened, (2) where the
    screenshot was saved — the concrete directory, or the exact file when
    it is known — and (3) the exact re-run command. No bare code
    identifiers: this text is read by humans, not developers.
    """
    board_bit = f" on {board}" if board else ""
    page_bit = f"\n\nThe page this happened on: {page_url}" if page_url else ""
    if screenshot_path:
        shot_bit = f"A screenshot of the page was saved at:\n    {screenshot_path}"
    else:
        shot_bit = (
            "A screenshot of the page is saved in the screenshots folder "
            "inside the Veto project folder:\n"
            f"    {_screenshots_dir()}\n"
            "The file name starts with 'apply-' and mentions the pause reason."
        )
    rerun_bit = (
        "When you are done with the step above, re-run the application with "
        "this exact command (run it from the Veto project folder):\n"
        "    python cli.py apply <job-id> --confirm\n"
        "Use the same job id you applied to."
    )
    happened = {
        "captcha": (
            f"Veto found a 'prove you're human' puzzle (a CAPTCHA){board_bit}. "
            "Veto will never solve it for you — automation paused instead, "
            "and nothing was submitted."
        ),
        "auth_loss": (
            f"The job site{board_bit} sent Veto to a login page, which means "
            "the saved login is gone or expired. Veto paused instead of "
            "typing your password anywhere, and nothing was submitted."
        ),
        "field_ambiguity": (
            f"Veto couldn't tell which form field is which{board_bit} — some "
            "fields have no label at all, or two fields share the same label. "
            "It stopped rather than guess and put your details in the wrong "
            "box. Nothing was submitted."
        ),
        "detector_failure": (
            "Something went wrong inside Veto's own safety check — the check "
            "itself crashed before it could finish. Veto paused loudly "
            "rather than continue blind. Nothing was submitted on this run."
        ),
    }.get(
        reason,
        "Veto paused automation and handed control to you. Nothing was submitted.",
    )
    do = {
        "captcha": (
            "Open the page in your own web browser, solve the puzzle "
            "yourself, then re-run."
        ),
        "auth_loss": (
            "Sign in to the job site yourself in your own web browser, "
            "then re-run."
        ),
        "field_ambiguity": (
            "Look at the screenshot and the written list of fields "
            "included in this handoff — if you can't use the screenshot "
            "(for example with a screen reader), use the written list "
            "instead — then fill those fields by hand in your own browser."
        ),
        "detector_failure": (
            "Check in your own browser whether anything was actually sent. "
            "If not, finish the application by hand or re-run. If this "
            "keeps happening, the safety check itself needs a fix — "
            "please report it."
        ),
    }.get(
        reason,
        "Review the screenshot, finish the step in your own browser, then continue.",
    )
    return (
        f"{happened}\n\n{shot_bit}{page_bit}\n\n"
        f"What to do next: {do}\n\n{rerun_bit}"
    )


@dataclass
class RescueHandoff:
    """What the user receives when automation pauses.

    The ``resume_token`` is a short correlation key (``uuid4``-derived),
    not a credential and not a resumption mechanism. Its intended
    consumer is the human-facing surface that displays the handoff —
    today the result dict's ``"rescue"`` bundle read by the server's
    caller and any dashboard/alert UI built on top: the token lets a
    human (or support tooling) match a screenshot file, a log line, and
    the paused-attempt record to each other when the user re-runs the
    fill. It is deliberately NOT consumed by any automated code path:
    no component resumes automation on the strength of this token, and
    any resume always goes through the normal confirm-gated path with
    the human in control. Do not invent consumers for it — if a future
    integration starts treating it as a machine-resume key, that
    integration must go through adversarial review first.
    """

    reason: str  # "captcha" | "auth_loss" | "field_ambiguity" | "detector_failure"
    detected_at: str
    page_url: str
    board: str | None
    evidence: str
    screenshot_path: str | None
    fields_summary: list[str] = field(default_factory=list)
    resume_token: str = ""
    what_user_should_do: str = ""

    def __post_init__(self) -> None:
        if not self.resume_token:
            self.resume_token = uuid.uuid4().hex[:12]
        if not self.what_user_should_do:
            self.what_user_should_do = _guidance_for(
                self.reason,
                screenshot_path=self.screenshot_path,
                page_url=self.page_url,
                board=self.board,
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _handoff_from_hit(
    page: Any,
    hit: dict[str, Any],
    *,
    board: str | None,
    fields: list[dict[str, Any]] | None,
    screenshot_path: str | None,
) -> RescueHandoff:
    """Build the handoff for a detector hit. May raise (KeyError on a
    malformed hit, or a page-access failure on ``page.url``) — the caller
    treats that as a handoff-path failure and pauses loudly anyway. The
    ``page.url`` read is deliberately unguarded: a dead page here must
    raise into ``check()``'s fail-closed handler, never degrade to an
    empty URL silently."""
    page_url = str(page.url or "")
    return RescueHandoff(
        reason=hit["reason"],
        detected_at=datetime.now(timezone.utc).isoformat(),
        page_url=page_url,
        board=board,
        evidence=hit["evidence"],
        screenshot_path=screenshot_path,
        fields_summary=[
            f"{f.get('label') or f.get('name')} [{f.get('type')}]"
            for f in (fields or [])[:12]
        ],
    )


def _detector_failure_handoff(exc: BaseException, *, board: str | None) -> RescueHandoff:
    """Loud pause for a crashed detector or handoff path.

    Built from hardcoded values only — it never touches the page or the
    driver, so this constructor path cannot depend on whatever just broke.
    """
    return RescueHandoff(
        reason="detector_failure",
        detected_at=datetime.now(timezone.utc).isoformat(),
        page_url="",
        board=board,
        evidence=f"detector failure — verify manually ({type(exc).__name__}: {exc})",
        screenshot_path=None,
        fields_summary=[],
        what_user_should_do=_guidance_for(
            "detector_failure", screenshot_path=None, page_url="", board=board
        ),
    )


def check(
    page: Any,
    *,
    board: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    screenshot_path: str | None = None,
) -> RescueHandoff | None:
    """Run the rescue detectors in priority order — FAIL-CLOSED.

    Priority: CAPTCHA > auth loss > field ambiguity. Returns a
    ``RescueHandoff`` on the first trigger, else None.

    FAIL-CLOSED (D1, revised 2026-09-13): any exception in a detector, or
    in the handoff path after a hit, produces a loud ``RescueHandoff``
    with reason "detector_failure" — never a silent None. The detectors
    contain no internal failure swallows: a dead page, a closed page, or
    a ``None`` page raises on page access (``query_selector``,
    ``content()``, ``url``), and that exception propagates here. A None
    return therefore means every detector ran clean and found nothing; it
    is never the answer to "the safety check crashed". The pause IS the
    feature, and a broken safety net must be loud, not invisible.
    """
    try:
        hit = detect_captcha(page)
        if hit is None:
            hit = detect_auth_loss(page, board)
        if hit is None and fields is not None:
            hit = assess_field_ambiguity(fields)
        if hit is None:
            return None
        try:
            return _handoff_from_hit(
                page, hit, board=board, fields=fields,
                screenshot_path=screenshot_path,
            )
        except Exception as handoff_exc:
            # Handoff-path failure (e.g. malformed detector output):
            # still pause loudly, never silently.
            log.error(
                "Session rescue: handoff construction failed — pausing loudly: %s",
                handoff_exc,
            )
            return _detector_failure_handoff(handoff_exc, board=board)
    except Exception as exc:
        # Detector failure: FAIL CLOSED. Log at error, then hand the user
        # a loud pause they can act on.
        log.error(
            "Session rescue: detector failure — pausing loudly instead of "
            "returning None: %s",
            exc,
        )
        try:
            return _detector_failure_handoff(exc, board=board)
        except Exception:
            # Absolute last resort: hardcoded values only, so the pause is
            # still visible even if the failure-handoff builder breaks.
            return RescueHandoff(
                reason="detector_failure",
                detected_at=datetime.now(timezone.utc).isoformat(),
                page_url="",
                board=None,
                evidence="detector failure — verify manually "
                "(handoff construction also failed)",
                screenshot_path=None,
                what_user_should_do=(
                    "Something went wrong inside Veto's own safety check — "
                    "the check itself crashed before it could finish. Veto "
                    "paused loudly rather than continue blind. Nothing was "
                    "submitted on this run.\n\n"
                    "What to do next: check in your own browser whether "
                    "anything was actually sent. If not, finish the "
                    "application by hand."
                ),
            )


def refusal_note() -> str:
    """The policy, in one place, for UI copy and audits."""
    return (
        "Veto never bypasses CAPTCHAs. When one is detected, automation "
        "pauses, a screenshot is saved, and control is handed to you. "
        "This pause-and-hand-off IS the feature."
    )
