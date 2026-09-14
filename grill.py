#!/usr/bin/env python3
"""Per-application grilling for the job-apply MCP server.

The wizard's ``--grill`` mode interrogates the user once at profile
setup. This module grills per application: before an application
proceeds it generates sharp, targeted questions from the gaps between a
job description and the saved profile, asks them over the user's
preferred messaging channel, collects answers, and hands the Q&A to the
tailoring step (``tailor.py``) so resumes/cover letters use real answers
instead of invented facts.

Channel reality (verified 2026-09-10):

* **WhatsApp** is the only proactive channel, via the Muse WhatsApp side
  chat (a 1:1 user<->Muse conversation). iMessage cannot be auto-sent
  (paired iPhones only expose ``message.draft``, which still requires
  the user to hit send). Discord has no integration. SMS needs a paired
  Android with send capability.
* A channel message can only be sent **from that channel's own side
  chat**. From the main chat, proactive reach requires a scheduled task
  created *inside* the WhatsApp side chat.

So this module is channel-agnostic and never assumes it can push. It
formats the outbound message; the caller (running in the WhatsApp side
chat) delivers it. ``outbound_for_channel()`` makes the routing explicit.

Preferences (in ``preferences.json``):

* ``grill_channel`` — ``"chat"`` (default), ``"whatsapp"``, ``"gmail"``
  (ask in the current chat), or ``"off"``.
* ``grill_on_apply`` — bool, default True. When False, apply flows skip
  grilling entirely.

The ``"gmail"`` channel sends the questions as an email to the user's own
address (from the saved profile) and ingests numbered answers from
replies; see ``parse_reply_body`` / ``ingest_reply`` and docs/grill.md.

Session state lives in ``grill_sessions.json`` next to this module, keyed
by job_id. NOTE: that file can contain application answers; add
``grill_sessions.json`` to ``.gitignore``. This module deliberately does
not edit .gitignore itself.

Honesty rules (fail closed on facts):

* Questions are deterministic and derived from the job text + profile.
* A question never asserts a profile fact that isn't there — quantify
  questions cite only that a skill is listed, gap questions say "I don't
  see it in your profile", never "you have no experience with X".
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.grill")

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = BASE_DIR / "grill_sessions.json"

# Connect link for the WhatsApp side chat (user must link once).
WHATSAPP_CONNECT_URL = "https://agent.meta.ai/connect/channel?service=whatsapp"


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def get_grill_prefs() -> dict[str, Any]:
    """Read grilling preferences from preferences.json with safe defaults.

    Uses the repo's prefs helper for the file path when available, then
    reads the raw JSON so the grill-specific keys aren't dropped.
    """
    prefs_file = BASE_DIR / "preferences.json"
    try:
        from prefs import PREFS_FILE as _PREFS_FILE  # noqa: N811

        prefs_file = Path(_PREFS_FILE)
    except ImportError:
        pass
    data: dict[str, Any] = {}
    try:
        data = json.loads(prefs_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.debug("Could not read %s: %s", prefs_file, exc)
    channel = str(data.get("grill_channel", "chat")).strip().lower()
    if channel not in ("whatsapp", "gmail", "chat", "off"):
        channel = "chat"
    return {
        "grill_channel": channel,
        "grill_on_apply": bool(data.get("grill_on_apply", True)),
    }


# ---------------------------------------------------------------------------
# Job-description keyword extraction (simple heuristics, deterministic)
# ---------------------------------------------------------------------------

#: Curated skill keywords matched against JD text. Curated rather than
#: NER'd so extraction is deterministic and testable.
SKILL_KEYWORDS = [
    # languages
    "python", "go", "golang", "javascript", "typescript", "java", "c++",
    "c#", "rust", "ruby", "php", "swift", "kotlin", "scala", "r",
    # frontend
    "react", "angular", "vue", "next.js", "html", "css",
    # data / ml
    "sql", "postgresql", "postgres", "mysql", "mongodb", "redis",
    "elasticsearch", "kafka", "spark", "airflow", "dbt",
    "machine learning", "deep learning", "nlp", "llm",
    "tensorflow", "pytorch", "scikit-learn", "pandas", "numpy",
    "data pipeline", "etl", "data modeling", "a/b testing",
    # infra / devops
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform",
    "ci/cd", "jenkins", "github actions", "linux", "bash",
    # backend / web
    "rest", "graphql", "grpc", "microservices", "django", "flask",
    "node.js", "fastapi", "spring",
    # product / misc
    "agile", "scrum", "figma", "salesforce", "excel", "tableau",
    "power bi", "jira", "confluence", "seo", "content marketing",
]

_MUST_HAVE_RE = re.compile(
    r"\b(must[\s-]?have|required?|requirements?|essential|minimum|you have|"
    r"you'll have|you will have)\b",
    re.IGNORECASE,
)

_SECTION_RE = re.compile(
    r"(?im)^\s*(requirements?|qualifications?|what you('ll| will) (bring|need)|"
    r"what we('re| are) looking for|responsibilities|nice[\s-]?to[\s-]?have|"
    r"preferred|bonus|about the role)\s*:?\s*$"
)

_PHRASE_PATTERNS = [
    re.compile(
        r"\b(?:experience|expertise|proficiency|proficient|knowledge|"
        r"familiarity|background|skilled)\s+(?:with|in|of|using)\s+"
        r"([A-Za-z][A-Za-z0-9+#.\-/ ]{2,40}?)(?=[,.;:()!?]|\s+and\s|\s+or\s|$)",
        re.IGNORECASE,
    ),
]


def _jd_text(job: dict[str, Any]) -> str:
    parts = []
    for key in ("description", "requirements", "responsibilities", "snippet"):
        val = job.get(key, "")
        if isinstance(val, list):
            parts.extend(str(v) for v in val)
        elif val:
            parts.append(str(val))
    return "\n".join(parts)


def _requirement_sections(text: str) -> tuple[list[str], list[str]]:
    """Split JD text into (must_have_lines, other_lines).

    Lines under a requirements/qualifications-style header count as
    must-have; lines under nice-to-have/preferred/bonus headers do not.
    """
    must: list[str] = []
    other: list[str] = []
    in_req = False
    for line in text.splitlines():
        header = _SECTION_RE.search(line)
        if header:
            kind = header.group(1).lower()
            in_req = "nice" not in kind and "preferred" not in kind and "bonus" not in kind
            continue
        (must if in_req else other).append(line)
    return must, other


def extract_jd_keywords(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract ``{keyword, must_have}`` pairs from the job description.

    Deterministic: curated skill list + "experience with X" phrases,
    must-have determined by requirement sections or must-have wording.
    """
    text = _jd_text(job)
    if not text.strip():
        return []
    must_lines, other_lines = _requirement_sections(text)
    must_blob = "\n".join(must_lines)
    found: dict[str, bool] = {}

    def note(kw: str, blob: str, must_have: bool) -> None:
        key = kw.strip().lower()
        if len(key) < 2:
            return
        prev = found.get(key)
        # must-have wins over a plain sighting.
        if prev is None or (must_have and not prev):
            found[key] = must_have

    for kw in SKILL_KEYWORDS:
        pattern = re.compile(r"(?<![\w+#.])" + re.escape(kw) + r"(?![\w+#.])",
                             re.IGNORECASE)
        in_must = bool(pattern.search(must_blob))
        in_other = bool(pattern.search("\n".join(other_lines)))
        if in_must or in_other:
            note(kw, "", in_must or bool(
                _MUST_HAVE_RE.search(
                    next((ln for ln in text.splitlines()
                          if pattern.search(ln)), ""))))
    for rx in _PHRASE_PATTERNS:
        for match in rx.finditer(text):
            phrase = match.group(1).strip().rstrip(".,;:")
            # Skip phrases that are just a curated keyword restated.
            note(phrase, "", bool(_MUST_HAVE_RE.search(
                text[max(0, match.start() - 80):match.start() + 40])))
    # Order: must-have first, then alphabetical for determinism.
    return [
        {"keyword": k, "must_have": v}
        for k, v in sorted(found.items(), key=lambda kv: (not kv[1], kv[0]))
    ]


# ---------------------------------------------------------------------------
# Profile evidence
# ---------------------------------------------------------------------------


def _profile_evidence(profile: dict[str, Any]) -> dict[str, str]:
    """Map normalized evidence key -> text where a keyword matched.

    Keys: the matched keyword; values: the profile text it was found in
    (used to check whether numbers/quantification already exist).
    """
    blobs: dict[str, str] = {}
    simple_lists = []
    for key in ("skills", "certifications", "languages"):
        val = profile.get(key)
        if isinstance(val, list):
            simple_lists.extend(str(v) for v in val)
    skill_years = profile.get("skill_years")
    if isinstance(skill_years, dict):
        simple_lists.extend(str(k) for k in skill_years)
    blobs["__lists__"] = "\n".join(simple_lists)

    exp_texts = []
    for entry in profile.get("experience", []) or []:
        if not isinstance(entry, dict):
            continue
        bits = []
        for key in ("title", "company", "description", "summary"):
            if entry.get(key):
                bits.append(str(entry[key]))
        for key in ("bullets", "achievements", "highlights"):
            val = entry.get(key)
            if isinstance(val, list):
                bits.extend(str(v) for v in val)
        exp_texts.append("\n".join(bits))
    blobs["__experience__"] = "\n".join(exp_texts)

    misc = []
    for key in ("summary", "achievements", "career_highlights"):
        val = profile.get(key)
        if isinstance(val, list):
            misc.extend(str(v) for v in val)
        elif val:
            misc.append(str(val))
    blobs["__misc__"] = "\n".join(misc)
    return blobs


def _keyword_in_text(keyword: str, text: str) -> bool:
    """Word-boundary match for short tokens, substring for longer ones."""
    if len(keyword) <= 3:
        return bool(re.search(r"(?<![\w+#.])" + re.escape(keyword) + r"(?![\w+#.])",
                              text, re.IGNORECASE))
    return keyword.lower() in text.lower()


def profile_has_keyword(profile: dict[str, Any], keyword: str) -> str | None:
    """Return the evidence text for the first profile blob containing the
    keyword, or None. Never raises."""
    for blob in _profile_evidence(profile).values():
        if blob and _keyword_in_text(keyword, blob):
            return blob
    return None


_HAS_NUMBER_RE = re.compile(r"\d")


def _evidence_is_quantified(evidence: str, keyword: str) -> bool:
    """True if the evidence text near the keyword contains any number."""
    # Look at the lines mentioning the keyword; a number anywhere in
    # those lines counts as quantified.
    for line in evidence.splitlines():
        if _keyword_in_text(keyword, line) and _HAS_NUMBER_RE.search(line):
            return True
    return False


# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

MAX_QUESTIONS_DEFAULT = 5

_LOGISTICS_SIGNALS = [
    # (jd pattern, profile key meaning "already covered", question template)
    (re.compile(r"\bhybrid\b", re.I), "remote_preference",
     "The posting mentions a hybrid schedule — does that work for you, "
     "and what's your commuting range?"),
    (re.compile(r"\bon[\s-]?site\b", re.I), "remote_preference",
     "The posting is on-site — is that workable for you?"),
    (re.compile(r"\btravel\b.{0,20}\d+\s*%|\d+\s*%\s*travel", re.I), None,
     "The posting mentions travel — how much travel are you open to?"),
    (re.compile(r"\bclearance\b", re.I), "security_clearance",
     "The posting mentions a security clearance — do you hold one, "
     "or have you held one?"),
    (re.compile(r"\brelocat", re.I), "willing_to_relocate",
     "The posting mentions relocation — are you open to relocating?"),
    (re.compile(r"\bsponsor", re.I), "needs_sponsorship",
     "The posting mentions sponsorship — do you need visa sponsorship?"),
]


def _logistics_questions(job: dict[str, Any],
                         profile: dict[str, Any]) -> list[dict[str, Any]]:
    text = _jd_text(job)
    out = []
    for pattern, covered_key, template in _LOGISTICS_SIGNALS:
        if not pattern.search(text):
            continue
        if covered_key and profile.get(covered_key):
            continue
        out.append({"kind": "logistics", "topic": pattern.pattern,
                    "question": template})
    # Location mismatch: JD names a place, profile location doesn't cover it.
    jd_location = str(job.get("location") or "")
    profile_location = str(profile.get("location") or "")
    if (jd_location and profile_location
            and not _keyword_in_text(profile_location.split(",")[0].strip(),
                                     jd_location)
            and "remote" not in jd_location.lower()
            and not profile.get("willing_to_relocate")):
        out.append({
            "kind": "logistics",
            "topic": "location",
            "question": (
                f"The posting is listed in {jd_location}. Your profile says "
                f"{profile_location} — is that location workable for you?"
            ),
        })
    return out


def generate_questions(job: dict[str, Any], profile: dict[str, Any],
                       max_questions: int = MAX_QUESTIONS_DEFAULT
                       ) -> list[dict[str, Any]]:
    """Generate up to ``max_questions`` grilling questions for one job.

    Kinds: ``quantify`` (skill listed without numbers), ``gap``
    (required keyword with no profile match — always phrased as an open
    question, never as an accusation), ``logistics`` (location/travel/
    clearance/etc. uncovered by the profile), ``motivation`` (one
    why-this-role question for the cover letter).

    Ranked: must-have gap/quantify first, then the rest, logistics next,
    motivation last. Never asserts a fact that isn't in the profile or
    the job text.
    """
    company = str(job.get("company") or "this company").strip()
    title = str(job.get("title") or "this role").strip()

    ranked: list[tuple[int, dict[str, Any]]] = []
    seen_topics: set[str] = set()

    for item in extract_jd_keywords(job):
        kw = item["keyword"]
        norm = kw.lower()
        if norm in seen_topics:
            continue
        seen_topics.add(norm)
        evidence = profile_has_keyword(profile, kw)
        priority = 0 if item["must_have"] else 2
        if evidence is None:
            ranked.append((priority, {
                "kind": "gap",
                "topic": kw,
                "question": (
                    f"The posting lists {kw} as a requirement, and I don't "
                    f"see it in your profile. Any experience with it? "
                    f"(Totally fine to say none — that tells me it's a gap "
                    f"to address honestly.)"
                ),
            }))
        elif not _evidence_is_quantified(evidence, kw):
            ranked.append((priority, {
                "kind": "quantify",
                "topic": kw,
                "question": (
                    f"You list {kw} but without numbers. What's the largest "
                    f"scale you've used it at — users, requests/day, team "
                    f"size, revenue impact, whatever fits?"
                ),
            }))

    for lq in _logistics_questions(job, profile):
        if lq["topic"] not in seen_topics:
            seen_topics.add(lq["topic"])
            # Deal-breaker class: above nice-to-have gaps, below must-haves.
            ranked.append((1, lq))

    ranked.sort(key=lambda pair: pair[0])
    # Reserve the last slot for the motivation question so the cover-letter
    # hook is never cut by the cap.
    body = [q for _, q in ranked[:max(0, max_questions - 1)]]
    motivation = {
        "kind": "motivation",
        "topic": "motivation",
        "question": (
            f"Why this {title} role at {company}, in one or two sentences? "
            f"(This feeds your cover letter — be specific, not generic.)"
        ),
    }
    questions = []
    for i, q in enumerate(body + [motivation], start=1):
        questions.append({"id": f"q{i}", **q})
    return questions


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


def _load_sessions() -> dict[str, Any]:
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def get_grill_session(job_id: str) -> dict[str, Any] | None:
    """Return the raw session dict for ``job_id``, or None when there is
    none."""
    return _load_sessions().get(job_id)


def _save_sessions(sessions: dict[str, Any]) -> None:
    SESSIONS_FILE.write_text(
        json.dumps(sessions, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _build_gmail_body(job: dict[str, Any],
                      questions: list[dict[str, Any]]) -> str:
    title = str(job.get("title") or "this role")
    company = str(job.get("company") or "this company")
    lines = [
        f"Hi — quick grill before I tailor your application for the "
        f"{title} role at {company}.",
        "",
    ]
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q['question']}")
    lines += [
        "",
        "Reply with numbered answers, e.g.:",
        "1. ...",
        "2. ...",
        "",
        "Skip any question by replying \"skip\" for its number. "
        "Nothing is submitted yet — your answers sharpen the resume "
        "and cover letter.",
    ]
    return "\n".join(lines)


def _grill_subject(job: dict[str, Any], session_id: str,
                   n_questions: int) -> str:
    title = str(job.get("title") or "this role")
    company = str(job.get("company") or "this company")
    return (f"Grill: {title} at {company} — {n_questions} questions before "
            f"I tailor your application [grill:{session_id}]")


# ---------------------------------------------------------------------------
# Reply ingestion (gmail channel)
# ---------------------------------------------------------------------------

_ANSWER_START_RE = re.compile(
    r"^\s*(?:Q\s*)?(\d{1,2})\s*[.):\-–—]\s*(.*\S)\s*$"
)

# Inline markers for single-line replies like "1. Lots 2. 15GB 3. 2GB".
# Only trusted as a fallback (see parse_reply_body): the markers must form
# a strictly ascending 1..n sequence, which rules out stray decimals like
# the "2." in "2.5 years of experience".
_INLINE_MARKER_RE = re.compile(r"(?<!\d)(\d{1,2})[.):]\s*")


def _parse_inline_markers(text: str) -> dict[str, str]:
    """Best-effort split of a single-line numbered reply.

    Returns ``{question_id: answer}`` or ``{}`` when the text does not
    contain a clean ascending numbered sequence.
    """
    matches = list(_INLINE_MARKER_RE.finditer(text))
    nums = [int(m.group(1)) for m in matches]
    if (
        len(matches) < 2
        or nums[0] != 1
        or any(b != a + 1 for a, b in zip(nums, nums[1:]))
    ):
        return {}
    answers: dict[str, str] = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        answers[f"q{nums[i]}"] = text[start:end].strip()
    return {qid: ans for qid, ans in answers.items() if ans}
_QUOTE_CUTOFF_RES = [
    re.compile(r"^On .+wrote:\s*$"),                       # Gmail quote header
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.I),
    re.compile(r"^_{2,}\s*$"),                             # Outlook separator
    re.compile(r"^From: .+Sent: .+", re.I),
]
_SIGNATURE_RE = re.compile(r"^--\s*$")  # standard signature delimiter
_SIGNOFF_RE = re.compile(
    r"^(thanks!?|thank you!?|best,?|cheers,?|regards,?|sincerely,?)\s*$",
    re.IGNORECASE,
)


def _strip_quoted(body: str) -> str:
    """Cut quoted history and signatures from a reply body."""
    lines = []
    for line in (body or "").splitlines():
        if _SIGNATURE_RE.match(line):
            break
        if any(rx.match(line) for rx in _QUOTE_CUTOFF_RES):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    # Drop trailing sign-off closings ("Thanks!", "Best,") — they belong to
    # the email, not to the last numbered answer.
    while lines and _SIGNOFF_RE.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines)


def parse_reply_body(body_text: str) -> dict[str, str]:
    """Extract numbered answers from a reply body.

    Accepts ``"1. ..."``, ``"1) ..."``, ``"1: ..."``, ``"Q2: ..."`` etc.;
    freeform text before the first numbered answer is ignored and
    continuation lines are folded into the current answer. Quoted
    history (``>`` lines, "On ... wrote:") and signatures are stripped.

    Returns ``{question_id: answer}`` with ids like ``"q1"``.
    """
    answers: dict[str, str] = {}
    current: str | None = None
    for line in _strip_quoted(body_text).splitlines():
        match = _ANSWER_START_RE.match(line)
        if match:
            num = int(match.group(1))
            if 1 <= num <= 20:
                current = f"q{num}"
                answers[current] = match.group(2).strip()
            continue
        if current is not None:
            stripped = line.strip()
            if stripped:
                answers[current] += "\n" + stripped
    answers = {qid: ans for qid, ans in answers.items() if ans}
    if len(answers) < 2:
        # Single-line reply ("1. Lots 2. 15GB ..."): the line parser above
        # swallows everything into q1. Fall back to inline markers.
        inline = _parse_inline_markers(_strip_quoted(body_text))
        if inline:
            answers.update(inline)
    return answers


def ingest_reply(job_id: str, body_text: str) -> dict[str, Any]:
    """Parse a reply body and record every matching answer.

    Returns ``{"recorded": {qid: answer}, "unparsed": [qid, ...],
    "complete", "answered", "total"}``. ``unparsed`` holds numbered
    answers with no matching open question. Raises ValueError for an
    unknown session.
    """
    sessions = _load_sessions()
    session = sessions.get(job_id)
    if session is None:
        raise ValueError(f"No grilling session for job_id {job_id!r}")
    valid = {q["id"] for q in session["questions"]}
    parsed = parse_reply_body(body_text)
    recorded: dict[str, str] = {}
    unparsed: list[str] = []
    for qid, answer in parsed.items():
        if qid not in valid:
            unparsed.append(qid)
            continue
        session["answers"][qid] = answer.strip()
        recorded[qid] = session["answers"][qid]
    answered = sum(1 for q in session["questions"]
                   if session["answers"].get(q["id"]))
    session["complete"] = answered == len(session["questions"])
    _save_sessions(sessions)
    return {
        "recorded": recorded,
        "unparsed": unparsed,
        "complete": session["complete"],
        "answered": answered,
        "total": len(session["questions"]),
    }
def _build_outbound_message(job: dict[str, Any],
                            questions: list[dict[str, Any]]) -> str:
    title = str(job.get("title") or "this role")
    company = str(job.get("company") or "this company")
    board = str(job.get("board") or "")
    header = f"Quick grill before we apply — {title} at {company}"
    if board:
        header += f" (via {board})"
    lines = [header + ":", ""]
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q['question']}")
    lines += [
        "",
        "Reply with the question number and your answer, e.g. \"1: ...\".",
        "Skip any question by replying \"skip\". These answers sharpen "
        "your resume and cover letter — nothing is submitted yet.",
    ]
    return "\n".join(lines)


def start_grill(job_id: str, job: dict[str, Any],
                profile: dict[str, Any]) -> dict[str, Any]:
    """Start (or restart) a grilling session for a job.

    Returns ``{session_id, questions, outbound_message}``. The outbound
    message is ready-to-send text; the caller delivers it over the
    user's preferred channel (see ``outbound_for_channel``).
    """
    questions = generate_questions(job, profile)
    sessions = _load_sessions()
    session = {
        "session_id": uuid.uuid4().hex[:12],
        "job_id": job_id,
        "title": str(job.get("title") or ""),
        "company": str(job.get("company") or ""),
        "email": str(profile.get("email") or ""),
        "questions": questions,
        "answers": {},
        "complete": False,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    sessions[job_id] = session
    _save_sessions(sessions)
    return {
        "session_id": session["session_id"],
        "questions": questions,
        "outbound_message": _build_outbound_message(job, questions),
    }


def record_answer(job_id: str, question_id: str, answer: str) -> dict[str, Any]:
    """Record one answer. Raises ValueError for unknown session/question."""
    sessions = _load_sessions()
    session = sessions.get(job_id)
    if session is None:
        raise ValueError(f"No grilling session for job_id {job_id!r}")
    qids = {q["id"] for q in session["questions"]}
    if question_id not in qids:
        raise ValueError(
            f"Unknown question {question_id!r}; valid: {sorted(qids)}")
    session["answers"][question_id] = str(answer or "").strip()
    answered = sum(1 for q in session["questions"]
                   if session["answers"].get(q["id"]))
    session["complete"] = answered == len(session["questions"])
    _save_sessions(sessions)
    return {
        "recorded": True,
        "complete": session["complete"],
        "answered": answered,
        "total": len(session["questions"]),
    }


def grill_status(job_id: str) -> dict[str, Any]:
    """Return ``{complete, answered, total, qa_pairs}`` for a session."""
    session = _load_sessions().get(job_id)
    if session is None:
        return {"complete": False, "answered": 0, "total": 0,
                "qa_pairs": [], "error": "no grilling session"}
    answers = session.get("answers", {})
    answered_qs = [q for q in session["questions"] if answers.get(q["id"])]
    return {
        "complete": bool(session.get("complete")),
        "answered": len(answered_qs),
        "total": len(session["questions"]),
        "qa_pairs": [(q["question"], answers[q["id"]]) for q in answered_qs],
    }


def get_qa_pairs(job_id: str) -> list[tuple[str, str]]:
    """Answered (question, answer) pairs, in question order.

    This is the handoff to the tailoring step: real answers the user
    gave, so ``tailor.py`` never has to invent them.
    """
    return grill_status(job_id)["qa_pairs"]


def cancel_grill(job_id: str) -> bool:
    """Delete a grilling session. Returns True when one existed."""
    sessions = _load_sessions()
    if job_id not in sessions:
        return False
    del sessions[job_id]
    _save_sessions(sessions)
    return True


# ---------------------------------------------------------------------------
# Channel routing
# ---------------------------------------------------------------------------


def outbound_for_channel(job_id: str,
                         channel: str | None = None) -> dict[str, Any]:
    """Build the deliverable for a session over the given channel.

    Returns a dict with ``deliverable_in`` telling the caller where the
    message must be handed off — ``"whatsapp_side_chat"``,
    ``"current_chat"``, ``"gmail"`` (with ``to``/``subject``/``body``),
    or ``"none"`` (grilling off / no session). This module never sends
    anything itself.
    """
    prefs = get_grill_prefs()
    channel = (channel or prefs["grill_channel"]).strip().lower()
    session = _load_sessions().get(job_id)
    message = ""
    if session is not None:
        job = {"title": session.get("title", ""),
               "company": session.get("company", "")}
        message = _build_outbound_message(job, session["questions"])

    if channel == "off" or session is None:
        return {"deliverable_in": "none", "message": "",
                "note": "Grilling is off or no session exists."}
    if channel == "chat":
        return {"deliverable_in": "current_chat", "message": message,
                "note": "Ask these in the current chat; record answers "
                        "with grill_answer."}
    if channel == "gmail":
        job = {"title": session.get("title", ""),
               "company": session.get("company", "")}
        to = session.get("email") or ""
        if not to:
            # Fall back to the saved profile's address.
            try:
                import server as _server  # lazy: avoids circular import

                to = str(_server._load_saved_profile().get("email") or "")
            except Exception:
                to = ""
        return {
            "deliverable_in": "gmail",
            "to": to,
            "subject": _grill_subject(
                job, session["session_id"], len(session["questions"])),
            "body": _build_gmail_body(job, session["questions"]),
            "note": (
                "Send this email only with the user's explicit go-ahead on "
                "the exact text (standing approval covers later grill "
                "emails). Replies are matched by the [grill:<session_id>] "
                "subject tag; ingest them with grill_ingest_reply."
            ),
        }
    # whatsapp (default): only sendable from the WhatsApp side chat.
    return {
        "deliverable_in": "whatsapp_side_chat",
        "message": message,
        "note": (
            "WhatsApp is the only proactive channel. This message can only "
            "be sent from the WhatsApp side chat (link once: "
            f"{WHATSAPP_CONNECT_URL}). From the main chat, proactive "
            "grilling needs a scheduled task created *inside* the WhatsApp "
            "side chat that calls grill_status/grill_answer here."
        ),
    }


# ---------------------------------------------------------------------------
# job_id convenience (lazy server import to avoid circulars)
# ---------------------------------------------------------------------------


def grill_start_for_job_id(job_id: str,
                           fetch_details: Any = None) -> dict[str, Any]:
    """Start a grilling session from a search_jobs job id.

    ``fetch_details`` is an injectable ``(job_id) -> job dict`` (tests);
    when None, ``server.get_job_details`` and ``server._load_saved_profile``
    are imported lazily.
    """
    if fetch_details is None:
        import server as _server  # lazy: avoids circular import

        profile = _server._load_saved_profile()
        job = _server.get_job_details(job_id)
    else:
        from server import _load_saved_profile as _load  # type: ignore

        profile = _load()
        job = fetch_details(job_id)
    if not isinstance(job, dict) or job.get("error"):
        raise ValueError(f"Could not load job {job_id!r}: "
                         f"{job.get('error') if isinstance(job, dict) else job}")
    job.setdefault("job_id", job_id)
    return start_grill(job_id, job, profile)


# ---------------------------------------------------------------------------
# MCP / CLI registration
# ---------------------------------------------------------------------------


def _print_result(result: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def cmd_grill_start(args: Any) -> int:
    """CLI handler for `grill-start`."""
    try:
        result = grill_start_for_job_id(args.job_id)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    prefs = get_grill_prefs()
    channel = getattr(args, "channel", None) or prefs["grill_channel"]
    routed = outbound_for_channel(args.job_id, channel)
    result["channel"] = channel
    result["deliverable_in"] = routed["deliverable_in"]
    result["note"] = routed["note"]
    _print_result(result, getattr(args, "json", False))
    return 0


def cmd_grill_answer(args: Any) -> int:
    """CLI handler for `grill-answer`."""
    try:
        result = record_answer(args.job_id, args.question_id, args.answer)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    _print_result(result, getattr(args, "json", False))
    return 0


def cmd_grill_status(args: Any) -> int:
    """CLI handler for `grill-status`."""
    _print_result(grill_status(args.job_id), getattr(args, "json", False))
    return 0


def cmd_grill_ingest_reply(args: Any) -> int:
    """CLI handler for `grill-ingest-reply`."""
    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    try:
        result = ingest_reply(args.job_id, body)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    _print_result(result, getattr(args, "json", False))
    return 0


def register_tools(mcp: Any) -> None:
    """Register the grill MCP tools on an MCP server instance."""
    _impl_start = globals()["grill_start_for_job_id"]
    _impl_answer = globals()["record_answer"]
    _impl_status = globals()["grill_status"]
    _impl_ingest = globals()["ingest_reply"]

    @mcp.tool()
    def grill_start(job_id: str) -> dict:
        """Start per-application grilling for a job from search_jobs.

        Generates sharp questions from the gaps between the job
        description and the saved profile. Returns the questions plus a
        ready-to-send message; the caller delivers it over the user's
        preferred channel (WhatsApp side chat by default).

        Args:
            job_id: The job id returned by search_jobs.
        """
        try:
            result = _impl_start(job_id)
        except ValueError as exc:
            return {"error": str(exc)}
        routed = outbound_for_channel(job_id)
        result["deliverable_in"] = routed["deliverable_in"]
        result["note"] = routed["note"]
        return result

    @mcp.tool()
    def grill_answer(job_id: str, question_id: str, answer: str) -> dict:
        """Record one grilling answer for a job.

        Args:
            job_id: The job id the session was started for.
            question_id: e.g. "q1".
            answer: The user's answer (or "skip").
        """
        try:
            return _impl_answer(job_id, question_id, answer)
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def grill_status(job_id: str) -> dict:
        """Check grilling progress for a job.

        Args:
            job_id: The job id the session was started for.

        Returns:
            Dict with complete, answered, total, qa_pairs.
        """
        return _impl_status(job_id)

    @mcp.tool()
    def grill_ingest_reply(job_id: str, body_text: str) -> dict:
        """Ingest a gmail reply to a grilling email.

        Parses numbered answers ("1. ...", "Q2: ...") from the reply body
        and records each one. The reply is matched to its session by the
        [grill:<session_id>] subject tag; pass the body of the matching
        message.

        Args:
            job_id: The job id the session was started for.
            body_text: The reply email's plain-text body.
        """
        try:
            return _impl_ingest(job_id, body_text)
        except ValueError as exc:
            return {"error": str(exc)}


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `grill-start` / `grill-answer` / `grill-status` /
    `grill-ingest-reply` subcommands.

    Returns a {command: handler} mapping the caller merges into its own
    dispatch table (cli.py-style).
    """
    p_start = subparsers.add_parser(
        "grill-start", help="Start per-application grilling for a job id.")
    p_start.add_argument("job_id", help="Job id from the search output.")
    p_start.add_argument(
        "--channel",
        choices=["whatsapp", "gmail", "chat", "off"],
        default=None,
        help="Override the grill_channel preference for this run.",
    )
    p_start.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    p_answer = subparsers.add_parser(
        "grill-answer", help="Record one grilling answer.")
    p_answer.add_argument("job_id", help="Job id the session was started for.")
    p_answer.add_argument("question_id", help='Question id, e.g. "q1".')
    p_answer.add_argument("answer", help='The answer (or "skip").')
    p_answer.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    p_status = subparsers.add_parser(
        "grill-status", help="Check grilling progress for a job id.")
    p_status.add_argument("job_id", help="Job id the session was started for.")
    p_status.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    p_ingest = subparsers.add_parser(
        "grill-ingest-reply",
        help="Ingest a gmail reply body into a grilling session.")
    p_ingest.add_argument("job_id",
                          help="Job id the session was started for.")
    p_ingest.add_argument(
        "--body", default="",
        help="Reply body text (or use --body-file).")
    p_ingest.add_argument(
        "--body-file", default=None,
        help="Path to a file containing the reply body text.")
    p_ingest.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    return {
        "grill-start": cmd_grill_start,
        "grill-answer": cmd_grill_answer,
        "grill-status": cmd_grill_status,
        "grill-ingest-reply": cmd_grill_ingest_reply,
    }
