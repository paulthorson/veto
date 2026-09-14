"""Veto Web UI backend — stdlib-only HTTP API server.

Serves the static frontend in ``webui/`` (built by the frontend worker)
plus a JSON API over the real job-apply modules:

  GET  /api/tools        -> tool/action/param catalog
  GET  /api/kpis         -> dashboard_data.compute_kpis(repo_root)
  POST /api/run          -> {"tool","action","params"} dispatch

Destructive actions (follow-up send, application record) are
confirm-gated: the first call returns a preview + preview_hash,
and a second call with {"confirmed": true, "preview_hash": ...} only
proceeds when the recomputed hash matches.

Every /api/* route requires ``Authorization: Bearer <token>`` (see the
Token auth section); static files stay open.

Exposes ``cmd_serve(args)`` for cli.py wiring. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html as _html  # noqa: F401  (kept for potential template use)
import json
import math
import mimetypes
import os
import secrets
import socket
import sys
import threading
import traceback
import webbrowser
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

WEBUI_DIR = BASE_DIR / "webui"
DEFAULT_PORT = 8765
MAX_BODY = 5 * 1024 * 1024  # 5 MB
_LIST_CAP = 500
_DEPTH_CAP = 12


# ---------------------------------------------------------------------------
# JSON sanitization
# ---------------------------------------------------------------------------

def _clean(obj, _depth=0):
    """Convert arbitrary module output into JSON-serializable data."""
    if _depth > _DEPTH_CAP:
        return "<max-depth>"
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (date, datetime, time)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _clean(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v, _depth + 1) for v in obj[:_LIST_CAP]]
    if isinstance(obj, (set, frozenset)):
        return [_clean(v, _depth + 1)
                for v in sorted(obj, key=repr)[:_LIST_CAP]]
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8", "replace")
        except Exception:
            return repr(obj)
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return repr(obj)


def _require(params, *names):
    missing = [n for n in names if params.get(n) in (None, "")]
    if missing:
        raise ValueError("missing required param(s): " + ", ".join(missing))


def _profile(params):
    """Profile dict for actions: explicit param or the saved default profile."""
    p = params.get("profile")
    if isinstance(p, dict) and p:
        return p
    try:
        from profiles import load_profile
        prof = load_profile("")
        return prof if isinstance(prof, dict) else {}
    except Exception:
        return {}


def _preview_payload(preview: str):
    digest = hashlib.sha256(preview.encode("utf-8")).hexdigest()
    return {
        "ok": False,
        "needs_confirm": True,
        "preview": preview,
        "preview_hash": digest,
    }


class _GateError(Exception):
    """A preview/validation failure from a confirm-gated action (user-facing)."""

# ---------------------------------------------------------------------------
# Action handlers — each calls the REAL module function. No reimplementation.
# ---------------------------------------------------------------------------

# ---- FIND: match --------------------------------------------------------

def _h_match_score(params):
    import match
    _require(params, "job")
    return match.score_job(params["job"], _profile(params), params.get("preferences"))


def _h_match_rank(params):
    import match
    _require(params, "jobs")
    jobs = list(params["jobs"])[:50]
    return match.rank_jobs(jobs, _profile(params), params.get("preferences"))


# ---- FIND: radar --------------------------------------------------------

def _h_radar_svg(params):
    import radar
    _require(params, "score")
    return {"svg": radar.radar_svg(
        params["score"],
        title=params.get("title"),
        size=int(params.get("size") or 400),
        color=params.get("color") or "#ffb000",
    )}


def _h_radar_compare(params):
    import radar
    _require(params, "items")
    labeled = []
    for item in params["items"][:20]:
        labeled.append((item.get("name", "?"), item.get("components", {})))
    return {"svg": radar.compare_svg(labeled, title=params.get("title") or "Fit comparison")}


def _h_radar_html(params):
    import radar
    _require(params, "items")
    labeled = []
    for item in params["items"][:20]:
        labeled.append((item.get("name", "?"), item.get("components", {})))
    return {"html": radar.radar_html(labeled, title=params.get("title") or "Veto fit radar")}


# ---- FIND: jd_decoder -----------------------------------------------------

def _h_jd_decode(params):
    import jd_decoder
    _require(params, "text")
    return jd_decoder.decode_jd(params["text"])


def _h_jd_verdict(params):
    import jd_decoder
    _require(params, "text")
    return jd_decoder.jd_verdict(params["text"])


# ---- FIND: byol (bring-your-own-listing, the primary listing flow) --------

def _h_byol_add(params):
    import byol
    return byol.add_listing(
        text=params.get("text") or "",
        url=params.get("url") or "",
        html_file=params.get("html_file") or "",
        title=params.get("title") or "",
        company=params.get("company") or "",
        location=params.get("location") or "",
    )


def _h_byol_list(params):
    import byol
    return {"listings": byol.list_listings()}


# ---- FIND: referrals ------------------------------------------------------

def _h_referrals_find(params):
    import referrals
    return referrals.find_referrals_data(
        company=params.get("company") or "",
        limit=int(params.get("limit") or 10),
    )


def _h_referrals_rank(params):
    import referrals
    _require(params, "connections", "targets")
    return referrals.rank_referrals(params["connections"], params["targets"],
                                    _profile(params))


def _h_referrals_outreach(params):
    import referrals
    _require(params, "contact")
    return referrals.draft_outreach(params["contact"], params.get("job"),
                                    _profile(params),
                                    kind=params.get("kind") or "warm")


def _h_referrals_targets(_params):
    import referrals
    return {"companies": referrals.target_companies()}


# ---- FIND: watch ------------------------------------------------------------

_WATCHES_PATH = BASE_DIR / "watches.json"


def _h_watch_list(_params):
    import watch
    watches = watch.load_watches(_WATCHES_PATH)
    return {"watches": [
        {"name": name, "query": w.get("query"), "location": w.get("location"),
         "board": w.get("board"), "initialized": w.get("initialized"),
         "last_checked_at": w.get("last_checked_at")}
        for name, w in watches.items()
    ]}


def _h_watch_add(params):
    import watch
    _require(params, "name", "query")
    watches = watch.load_watches(_WATCHES_PATH)
    filters = params.get("filters") if isinstance(params.get("filters"), dict) else None
    if params.get("remote_only") and filters is None:
        filters = {"remote_only": True}
    saved = watch.add_watch(
        watches, params["name"], params["query"],
        params.get("location") or "",
        params.get("board") or "all",
        filters,
    )
    watch.save_watches(_WATCHES_PATH, watches)
    return {"watch": {k: saved.get(k) for k in
                      ("name", "query", "location", "board", "filters")}}


def _h_watch_check(params):
    import watch
    try:
        import server as _server
    except ImportError as exc:
        return {"ok": False,
                "error": f"search engine unavailable: {exc}"}
    watches = watch.load_watches(_WATCHES_PATH)
    name = params.get("name")
    if name:
        if name not in watches:
            return {"ok": False, "error": f"no watch named {name!r}"}
        targets = {name: watches[name]}
    else:
        targets = watches
    results = watch.check_all(targets, _server.search_jobs)
    watch.save_watches(_WATCHES_PATH, watches)
    return {"results": {
        n: {"new_jobs": res.get("new_jobs") or [],
            "total_seen": res.get("total_seen", 0),
            "error": res.get("error")}
        for n, res in results.items()
    }}


def _h_watch_remove(params):
    import watch
    _require(params, "name")
    watches = watch.load_watches(_WATCHES_PATH)
    removed = watch.remove_watch(watches, params["name"])
    if removed:
        watch.save_watches(_WATCHES_PATH, watches)
    return {"removed": removed}


# ---- TAILOR: tailor -------------------------------------------------------

def _h_tailor_resume(params):
    import tailor
    _require(params, "job")
    return tailor.tailor_resume(_profile(params), params["job"])


def _h_tailor_diff(params):
    import tailor
    _require(params, "job")
    return tailor.tailor_with_diff(_profile(params), params["job"])


def _h_tailor_keywords(params):
    import tailor
    _require(params, "job")
    return tailor.extract_job_keywords(params["job"], _profile(params))


def _h_tailor_variants(_params):
    import tailor
    return {"variants": tailor.list_approved_variants()}


def _h_tailor_variant(params):
    import tailor
    _require(params, "job_id")
    return {"variant": tailor.load_approved_variant(params["job_id"])}


# ---- TAILOR: cover_letters --------------------------------------------------

def _h_cover_draft(params):
    import cover_letters
    _require(params, "job")
    return cover_letters.draft_cover_letter(_profile(params), params["job"])


def _h_cover_honesty(params):
    import cover_letters
    _require(params, "letter")
    return {"flags": cover_letters.honesty_scan(params["letter"], _profile(params),
                                                params.get("job"))}


def _h_cover_approve(params):
    import cover_letters
    _require(params, "job_id", "letter")
    return cover_letters.approve_letter(params["job_id"], params["letter"])


def _h_cover_list(_params):
    import cover_letters
    return {"letters": cover_letters.list_letters()}


# ---- TAILOR: linkedin_optimizer ----------------------------------------------

def _h_linkedin_audit(params):
    import linkedin_optimizer
    return linkedin_optimizer.audit_profile(_profile(params), params.get("target_role"))


def _h_linkedin_headline(params):
    import linkedin_optimizer
    return linkedin_optimizer.rewrite_headline(_profile(params), params.get("target_role"))


def _h_linkedin_about(params):
    import linkedin_optimizer
    return linkedin_optimizer.rewrite_about(_profile(params))


def _h_linkedin_keyword_gap(params):
    import linkedin_optimizer
    return linkedin_optimizer.keyword_gap(_profile(params), params.get("target_role"))

# ---- APPLY: grill ------------------------------------------------------------

def _h_grill_session(params):
    import grill
    _require(params, "job_id")
    return {"session": grill.get_grill_session(params["job_id"])}


def _h_grill_start(params):
    import grill
    _require(params, "job_id", "job")
    job = params["job"]
    if not isinstance(job, dict):
        raise ValueError("job must be an object")
    return grill.start_grill(params["job_id"], job, _profile(params))


def _h_grill_answer(params):
    import grill
    _require(params, "job_id", "question_id", "answer")
    return grill.record_answer(params["job_id"], params["question_id"],
                               params["answer"])


def _h_grill_status(params):
    import grill
    _require(params, "job_id")
    return grill.grill_status(params["job_id"])


def _h_grill_qa_pairs(params):
    import grill
    _require(params, "job_id")
    pairs = grill.get_qa_pairs(params["job_id"])
    return {"qa_pairs": [{"question": q, "answer": a} for q, a in pairs]}


def _h_grill_cancel(params):
    import grill
    _require(params, "job_id")
    return {"cancelled": grill.cancel_grill(params["job_id"])}


def _h_grill_keywords(params):
    import grill
    _require(params, "job")
    job = params["job"]
    if not isinstance(job, dict):
        raise ValueError("job must be an object")
    return {"keywords": grill.extract_jd_keywords(job)}


# ---- TRAIN: mock_interview --------------------------------------------------

def _h_mock_start(params):
    import mock_interview
    _require(params, "company", "role")
    return mock_interview.start_mock_interview(
        params["company"], params["role"],
        profile=_profile(params) or {},
        channel=params.get("channel") or "chat",
        job_id=params.get("job_id"),
    )


def _h_mock_answer(params):
    import mock_interview
    _require(params, "session_id", "answer")
    return mock_interview.answer_mock_question(params["session_id"], params["answer"])


def _h_mock_summary(params):
    import mock_interview
    _require(params, "session_id")
    return mock_interview.mock_summary(params["session_id"])


# ---- TRAIN: soft_skills -----------------------------------------------------

def _h_soft_assess(params):
    import soft_skills
    _require(params, "area", "answers")
    return soft_skills.assess(params["area"], params["answers"])


def _h_soft_drill(params):
    import soft_skills
    _require(params, "area")
    return soft_skills.drill(params["area"], _profile(params),
                             mode=params.get("mode") or "coach",
                             difficulty=params.get("difficulty") or "firm")


def _h_soft_review(params):
    import soft_skills
    _require(params, "session_id", "answer")
    return soft_skills.review_drill(params["session_id"], params["answer"])


def _h_soft_negotiate(params):
    import soft_skills
    _require(params, "current_offer", "target")
    return soft_skills.negotiation_sim(float(params["current_offer"]),
                                      float(params["target"]),
                                      _profile(params),
                                      difficulty=params.get("difficulty") or "firm")


# ---- TRAIN: ai_proficiency ----------------------------------------------------

def _h_ai_tracks(_params):
    import ai_proficiency
    return {"tracks": ai_proficiency.list_tracks()}


def _h_ai_plan(params):
    import ai_proficiency
    _require(params, "track")
    return ai_proficiency.plan(params["track"], params.get("level"))


def _h_ai_lesson(params):
    import ai_proficiency
    _require(params, "track", "lesson_id")
    return ai_proficiency.get_lesson(params["track"], params["lesson_id"])


def _h_ai_exercise(params):
    import ai_proficiency
    _require(params, "track", "lesson_id")
    return ai_proficiency.exercise(params["track"], params["lesson_id"])


def _h_ai_submit(params):
    import ai_proficiency
    _require(params, "session_id", "response")
    return ai_proficiency.submit_exercise(params["session_id"], params["response"])


def _h_ai_progress(_params):
    import ai_proficiency
    return ai_proficiency.progress()


def _h_ai_diagnose(params):
    import ai_proficiency
    _require(params, "track", "answers")
    return ai_proficiency.diagnose(params["track"], params["answers"])


# ---- TRAIN: mentors -----------------------------------------------------------

def _h_mentors_matchmake(params):
    import mentors
    _require(params, "answers")
    return mentors.matchmake(params["answers"])


def _h_mentors_outreach(params):
    import mentors
    _require(params, "mentor_id", "mentee_name", "mentee_goal")
    return mentors.connection_draft(params["mentor_id"], params["mentee_name"],
                                    params["mentee_goal"])


def _h_mentors_prep(params):
    import mentors
    _require(params, "topic")
    return mentors.prep_for_topic(params["topic"])


def _h_mentors_suggest(_params):
    import mentors
    return mentors.suggest_mentors_from_training()


def _h_mentors_opt_in(params):
    import mentors
    _require(params, "profile")
    return mentors.mentor_opt_in(params["profile"])


def _h_mentors_opt_out(params):
    import mentors
    return mentors.mentor_opt_out(params.get("mentor_id"))


def _h_mentors_card(params):
    import mentors
    return mentors.my_mentor_card(params.get("updates"))


def _h_mentors_record_outreach(params):
    import mentors
    _require(params, "mentor_id")
    return mentors.record_outreach(params["mentor_id"])


# ---- TRAIN: skill_gaps --------------------------------------------------------

def _h_gaps_analyze(params):
    import skill_gaps
    return skill_gaps.analyze_gaps(profile=params.get("profile") or _profile(params),
                                  jobs=params.get("jobs"))


def _h_gaps_plan(_params):
    import skill_gaps
    return skill_gaps.gap_plan()


def _h_gaps_mark(params):
    import skill_gaps
    _require(params, "skill", "status")
    return skill_gaps.mark_gap_progress(params["skill"], params["status"],
                                       params.get("evidence"))

# ---- TRAIN: briefs ------------------------------------------------------------

def _h_briefs_company(params):
    import briefs
    _require(params, "company")
    return briefs.company_brief(params["company"])


def _h_briefs_prep(params):
    import briefs
    _require(params, "job_id")
    return briefs.prep_interview(params["job_id"], profile=_profile(params))


# ---- WIN: followup ------------------------------------------------------------

def _h_followup_draft(params):
    import followup
    _require(params, "entry")
    return followup.draft_followup(params["entry"], _profile(params))


def _h_followup_drafts(params):
    import followup
    return {"drafts": followup.followups_with_drafts(_profile(params))}


def _followup_preview(params):
    _require(params, "entry_id", "to", "subject", "body")
    return "\n".join([
        "FOLLOW-UP EMAIL — review before confirming",
        f"Entry:   {params['entry_id']}",
        f"To:      {params['to']}",
        f"Subject: {params['subject']}",
        "",
        params["body"],
    ])


def _followup_send_confirmed(params):
    import followup
    return followup.send_followup(params["entry_id"], params["to"],
                                  params["subject"], params["body"],
                                  confirmed=True,
                                  approval_id=params.get("approval_id"))


# ---- WIN: network_crm -----------------------------------------------------------

def _h_crm_add(params):
    import network_crm
    _require(params, "name", "company")
    return network_crm.add_contact(
        params["name"], params["company"],
        role=params.get("role") or "", industry=params.get("industry") or "",
        linkedin_url=params.get("linkedin_url") or "",
        source=params.get("source") or "", notes=params.get("notes") or "",
    )


def _h_crm_list(_params):
    import network_crm
    return {"contacts": network_crm.list_contacts()}


def _h_crm_log(params):
    import network_crm
    _require(params, "contact_id", "kind", "summary")
    return network_crm.log_interaction(params["contact_id"], params["kind"],
                                      params["summary"], params.get("due"))


def _h_crm_nudges(_params):
    import network_crm
    return {"nudges": network_crm.nudge_list()}


def _h_crm_warm_path(params):
    import network_crm
    _require(params, "company")
    return {"path": network_crm.warm_path_to(params["company"])}


# ---- WIN: offer_compare -----------------------------------------------------------

def _h_offer_add(params):
    import offer_compare
    _require(params, "name", "base")
    return offer_compare.add_offer(
        params["name"], float(params["base"]),
        bonus=float(params.get("bonus") or 0),
        equity_total=float(params.get("equity_total") or 0),
        equity_years=float(params.get("equity_years") or 4),
        benefits_value=float(params.get("benefits_value") or 0),
        pto_days=float(params.get("pto_days") or 15),
        remote=params.get("remote") or "onsite",
        location=params.get("location") or "",
        growth_score=float(params.get("growth_score") or 5),
        notes=params.get("notes") or "",
    )


def _h_offer_list(_params):
    import offer_compare
    return offer_compare.list_offers()


def _h_offer_report(params):
    import offer_compare
    _require(params, "offer_id")
    return offer_compare.offer_report(params["offer_id"])


def _h_offer_compare(params):
    import offer_compare
    return offer_compare.compare_offers(params.get("weights"))


def _h_offer_weights(_params):
    import offer_compare
    return {"weights": offer_compare.get_weights()}


def _h_offer_set_weights(params):
    import offer_compare
    _require(params, "overrides")
    return offer_compare.set_weights(params["overrides"])


# ---- WIN: rejection_autopsy -------------------------------------------------------

def _h_autopsy_record(params):
    import rejection_autopsy
    _require(params, "job_id", "outcome", "stage")
    return rejection_autopsy.record_outcome(
        params["job_id"], params["outcome"], params["stage"],
        notes=params.get("notes") or "", applied_at=params.get("applied_at"),
    )


def _h_autopsy_run(_params):
    import rejection_autopsy
    return rejection_autopsy.autopsy()


def _h_autopsy_diagnose(_params):
    import rejection_autopsy
    return rejection_autopsy.diagnose()


# ---- WIN: streaks -------------------------------------------------------------------

def _h_streaks_log(params):
    import streaks
    _require(params, "kind")
    return streaks.log_rep(params["kind"])


def _h_streaks_view(_params):
    import streaks
    return streaks.streaks()


def _h_streaks_set_goal(params):
    import streaks
    _require(params, "kind", "daily_target")
    return streaks.set_goal(params["kind"], int(params["daily_target"]))


def _h_streaks_goals(_params):
    import streaks
    return streaks.goals()


def _h_streaks_today(_params):
    import streaks
    return streaks.today()


def _h_streaks_weekly(_params):
    import streaks
    return streaks.weekly_recap()


def _h_streaks_card(_params):
    import streaks
    return streaks.share_card()

# ---- WIN: email_sync -----------------------------------------------------------

def _h_email_scan(params):
    import email_sync
    days = int(params.get("days") or 14)
    # Proposals only: the dashboard flow applies nothing; applying stage
    # updates goes through email_sync's own confirm flow.
    return email_sync.scan_recruiter_emails(days=days, apply_updates=False)


def _h_email_draft(params):
    import email_sync
    _require(params, "application_id")
    return email_sync.draft_followup(params["application_id"],
                                     params.get("kind") or "check_in")


# ---- GOVERN: crew -------------------------------------------------------------------

def _h_crew_run(params):
    import crew
    _require(params, "persona_id", "task")
    return crew.run_agent(params["persona_id"], params["task"], params.get("context"))


def _h_crew_plan(params):
    import crew
    _require(params, "goal")
    return {"plan": crew.ceo_plan(params["goal"])}


def _h_crew_status(_params):
    import crew
    return crew.crew_status()


def _h_crew_red_team(params):
    import crew
    _require(params, "text")
    return crew.red_team_scan(params["text"])


def _h_crew_compliance(params):
    import crew
    _require(params, "task")
    return crew.compliance_screen(params["task"])


# ---- CORE: search -------------------------------------------------------------------

def _h_search_query(params):
    import server
    _require(params, "query", "location")
    return {"jobs": server.search_jobs(
        params["query"], params["location"],
        board=params.get("board") or "all",
        limit=int(params.get("limit") or 10),
        remote_only=bool(params.get("remote_only") or False),
        salary_min=int(params.get("salary_min") or 0),
        seniority=params.get("seniority") or "",
    )}


def _h_search_details(params):
    import server
    _require(params, "job_id")
    return server.get_job_details(params["job_id"])


def _h_search_boards(_params):
    import server
    return {"boards": server.list_boards()}


def _h_search_track(_params):
    import server
    return {"applications": server.track_applications()}


def _apply_preview_canonical(params):
    """Canonical preview for the apply record gate: a dry run."""
    _require(params, "job_id", "resume_path")
    import server
    preview = server.apply_to_job(
        params["job_id"], params["resume_path"],
        cover_letter=params.get("cover_letter") or "",
        answers=params.get("answers"),
        confirm=False,
        profile=_profile(params) if params.get("profile") else None,
        headless=bool(params.get("headless", True)),
    )
    canonical = json.dumps(preview, sort_keys=True, default=str)
    if isinstance(preview, dict) and preview.get("status") == "error":
        raise _GateError(str(preview.get("error") or "could not preview application"))
    job = (preview.get("preview") or {}) if isinstance(preview, dict) else {}
    text = "\n".join([
        "APPLICATION — review before confirming",
        f"Job:      {job.get('title', '?')} @ {job.get('company', '?')}",
        f"Location: {job.get('location', '?')}",
        f"Board:    {job.get('board', '?')}",
        f"Resume:   {params['resume_path']}",
        "",
        "Confirming records the application locally and returns the",
        "apply_url for manual completion in your browser.",
    ])
    return canonical, text


def _apply_record_confirmed(params):
    import server
    return server.apply_to_job(
        params["job_id"], params["resume_path"],
        cover_letter=params.get("cover_letter") or "",
        answers=params.get("answers"),
        confirm=True,
        profile=_profile(params) if params.get("profile") else None,
        headless=bool(params.get("headless", True)),
    )


# ---- CORE: apply_queue ----------------------------------------------------------------

def _h_queue_list(_params):
    import apply_queue
    return {"queue": apply_queue.list_queue()}


def _h_queue_remove(params):
    import apply_queue
    _require(params, "job_id")
    return {"removed": apply_queue.remove_from_queue(params["job_id"])}


def _queue_preview_canonical(params):
    _require(params, "job_id")
    return json.dumps(
        {"job_id": params["job_id"], "scheduled_for": params.get("scheduled_for")},
        sort_keys=True,
    ), "\n".join([
        "QUEUE APPLICATION — review before confirming",
        f"Job ID:       {params['job_id']}",
        f"Scheduled for:{params.get('scheduled_for') or 'as soon as possible'}",
        "",
        "Confirming adds this job to the local apply queue.",
    ])


def _queue_add_confirmed(params):
    import apply_queue
    return apply_queue.add_to_queue(params["job_id"],
                                   scheduled_for=params.get("scheduled_for"))


# ---- CORE: wizard -----------------------------------------------------------------------

def _h_wizard_blank(_params):
    import wizard
    return wizard.blank_profile()


def _h_wizard_merge(params):
    import wizard
    _require(params, "base", "linkedin")
    return wizard.merge_profile(params["base"], params["linkedin"])


# ---- CORE: doctor -------------------------------------------------------------------------

def _h_doctor_run(_params):
    import doctor
    return doctor.run_doctor()


def _h_doctor_profile(_params):
    import doctor
    return doctor.check_profile()


# ---- CORE: analytics ------------------------------------------------------------------------

def _h_analytics_report(params):
    import analytics
    return analytics.generate_report(stale_days=int(params.get("stale_days") or 14))


def _h_analytics_funnel(params):
    import analytics
    _require(params, "applications")
    return analytics.funnel(params["applications"])


def _h_analytics_response_rate(params):
    import analytics
    _require(params, "applications")
    return analytics.response_rate_by_board(params["applications"])


def _h_analytics_stale(params):
    import analytics
    _require(params, "applications")
    return {"stale": analytics.stale_applications(
        params["applications"], days=int(params.get("days") or 14))}


# ---------------------------------------------------------------------------
# Confirm-gated actions
# ---------------------------------------------------------------------------
# Each entry: preview builder -> (canonical_string_for_hashing, human_preview_text)
# plus the confirmed runner. The gate recomputes the canonical string and
# requires the supplied preview_hash to match before calling the module
# with confirmed=True.

def _followup_preview_pair(params):
    text = _followup_preview(params)
    return text, text  # canonical string is the exact preview text


GATED = {
    ("followup", "send"): {
        "preview": _followup_preview_pair,
        "run": _followup_send_confirmed,
    },
    ("search", "record_apply"): {
        "preview": _apply_preview_canonical,
        "run": _apply_record_confirmed,
    },
    ("apply_queue", "add"): {
        "preview": _queue_preview_canonical,
        "run": _queue_add_confirmed,
    },
}

# ---------------------------------------------------------------------------
# Tool catalog for GET /api/tools
# ---------------------------------------------------------------------------
# Param schema entries: {name, type, required, default, help}.
# Types: string | integer | number | boolean | object | array.

def _p(name, type, required=False, default=None, help=""):
    p = {"name": name, "type": type, "required": bool(required), "help": help}
    if default is not None:
        p["default"] = default
    return p


def _profile_param(help="Profile object; defaults to the saved profile when omitted."):
    return _p("profile", "object", False, help=help)


def _a(action_id, description, handler, params):
    return {"id": action_id, "description": description,
            "handler": handler, "params": params}


TOOLS = [
    # ---------------- FIND ----------------
    {"id": "match", "group": "FIND", "name": "Job fit scoring",
     "description": "Deterministic 0-100 fit scores for jobs against your profile, with reasons, matched/missing skills, and veto flags.",
     "actions": [
         _a("score", "Score one job against your profile.",
            _h_match_score, [_p("job", "object", True, help="Job dict (title, company, description, requirements, location, salary)."),
                                        _profile_param(),
                                        _p("preferences", "object", False, help="Preferences dict (locations, salary_min, remote).")]),
         _a("rank", "Rank a list of jobs by fit (capped at 50).",
            _h_match_rank, [_p("jobs", "array", True, help="List of job dicts."),
                                       _profile_param(), _p("preferences", "object", False)]),
     ]},
    {"id": "radar", "group": "FIND", "name": "Fit radar charts",
     "description": "SVG / HTML radar visualizations of a score_job component breakdown.",
     "actions": [
         _a("svg", "Radar SVG for one match.score_job result.",
            _h_radar_svg, [_p("score", "object", True, help="Output of match.score."),
                                      _p("title", "string"), _p("size", "integer", default=400),
                                      _p("color", "string", default="#ffb000")]),
         _a("compare", "Overlay radar SVG comparing up to 20 scored results.",
            _h_radar_compare, [_p("items", "array", True, help="[{name, components}] list."),
                                          _p("title", "string", default="Fit comparison")]),
         _a("html", "Standalone HTML radar comparison page.",
            _h_radar_html, [_p("items", "array", True, help="[{name, components}] list."),
                                       _p("title", "string", default="Veto fit radar")]),
     ]},
    {"id": "jd_decoder", "group": "FIND", "name": "JD decoder",
     "description": "Reads a job description and flags red/green flags, vagueness, and overwork signals.",
     "actions": [
         _a("decode", "Full structured decode of a job description.",
            _h_jd_decode, [_p("text", "string", True, help="Job description text.")]),
         _a("verdict", "Short verdict: apply / caution / skip with reasons.",
            _h_jd_verdict, [_p("text", "string", True, help="Job description text.")]),
     ]},
    {"id": "byol", "group": "FIND", "name": "Bring your own listing",
     "description": "The primary listing flow: add a posting from pasted text, a URL (fetched once, parsed locally), or a saved HTML file (no network). Returns a job id for the rest of the pipeline.",
     "actions": [
         _a("add", "Parse and save a listing; returns the standard job dict with its id.",
            _h_byol_add, [_p("text", "string", help="Pasted job-description text."),
                          _p("url", "string", help="Posting URL: fetched once, parsed locally."),
                          _p("html_file", "string", help="Saved HTML file path: parsed locally, zero network."),
                          _p("title", "string", help="Override the auto-detected title."),
                          _p("company", "string", help="Override the auto-detected company."),
                          _p("location", "string", help="Override the auto-detected location.")]),
         _a("list", "List your saved listings (newest first).",
            _h_byol_list, []),
     ]},
    {"id": "referrals", "group": "FIND", "name": "Referral finder",
     "description": "Ranks your connections as referral prospects for target companies and drafts outreach.",
     "actions": [
         _a("find", "Find referral prospects for a company from your connections.",
            _h_referrals_find, [_p("company", "string", help="Company name filter."),
                                               _p("limit", "integer", default=10)]),
         _a("rank", "Rank connections against target companies.",
            _h_referrals_rank, [_p("connections", "array", True, help="Connection dicts."),
                                              _p("targets", "array", True, help="Company names."),
                                              _profile_param()]),
         _a("outreach", "Draft a referral request message for a connection.",
            _h_referrals_outreach, [_p("contact", "object", True),
                                                   _p("job", "object"),
                                                   _profile_param(),
                                                   _p("kind", "string", default="warm", help="Outreach tone (warm only).")]),
         _a("targets", "Target companies derived from watches + applications.",
            _h_referrals_targets, []),
     ]},
    {"id": "watch", "group": "FIND", "name": "Job watches",
     "description": "Named saved searches that diff each check against previously seen postings and report only what is new.",
     "actions": [
         _a("list", "List saved watches with their last check.",
            _h_watch_list, []),
         _a("add", "Create (or replace) a named watch. The first check records a baseline and alerts on nothing.",
            _h_watch_add, [_p("name", "string", True), _p("query", "string", True),
                                       _p("location", "string"), _p("board", "string", default="all"),
                                       _p("remote_only", "boolean", default=False),
                                       _p("filters", "object", help="Extra watch filters (e.g. {\"limit\": 20}).")]),
         _a("check", "Check one watch, or all watches when name is omitted, for new postings.",
            _h_watch_check, [_p("name", "string", help="Watch name; omit to check every watch.")]),
         _a("remove", "Delete a watch.",
            _h_watch_remove, [_p("name", "string", True)]),
     ]},
    # ---------------- TAILOR ----------------
    {"id": "tailor", "group": "TAILOR", "name": "Resume tailor",
     "description": "Rewrites resume bullets around a job's keywords; keeps a library of approved variants.",
     "actions": [
         _a("resume", "Tailored resume markdown for a job.",
            _h_tailor_resume, [_profile_param(), _p("job", "object", True)]),
         _a("diff", "Tailored resume plus a diff against the base resume.",
            _h_tailor_diff, [_profile_param(), _p("job", "object", True)]),
         _a("keywords", "Keywords extracted from a job, split by matched/missing.",
            _h_tailor_keywords, [_profile_param(), _p("job", "object", True)]),
         _a("variants", "List saved approved resume variants.",
            _h_tailor_variants, []),
         _a("variant", "Load one approved resume variant.",
            _h_tailor_variant, [_p("job_id", "string", True)]),
     ]},
    {"id": "cover_letters", "group": "TAILOR", "name": "Cover letters",
     "description": "Drafts letters from profile evidence only; the honesty scan flags invented claims.",
     "actions": [
         _a("draft", "Draft a cover letter from profile evidence.",
            _h_cover_draft, [_profile_param(), _p("job", "object", True)]),
         _a("honesty_scan", "Flag claims in a letter that are not backed by the profile.",
            _h_cover_honesty, [_p("letter", "string", True), _profile_param(),
                                          _p("job", "object")]),
         _a("approve", "Save an approved letter for a job.",
            _h_cover_approve, [_p("job_id", "string", True), _p("letter", "string", True)]),
         _a("letters", "List approved cover letters.",
            _h_cover_list, []),
     ]},
    {"id": "linkedin_optimizer", "group": "TAILOR", "name": "LinkedIn optimizer",
     "description": "Scores headline, about, bullets, and skills; suggests rewrites for a target role.",
     "actions": [
         _a("audit", "Score every profile section with fix suggestions.",
            _h_linkedin_audit, [_profile_param(), _p("target_role", "string")]),
         _a("headline", "Rewrite the headline for a target role.",
            _h_linkedin_headline, [_profile_param(), _p("target_role", "string")]),
         _a("about", "Rewrite the about section.",
            _h_linkedin_about, [_profile_param()]),
         _a("keyword_gap", "Keywords missing from the profile for a target role.",
            _h_linkedin_keyword_gap, [_profile_param(), _p("target_role", "string")]),
     ]},
    # ---------------- APPLY ----------------
    {"id": "grill", "group": "APPLY", "name": "Application grill",
     "description": "Asks the questions the tailor step needs answered, one job at a time; the application stays grill-pending until every question has an answer.",
     "actions": [
         _a("session", "Raw saved session for a job, or null when there is none.",
            _h_grill_session, [_p("job_id", "string", True)]),
         _a("start", "Start (or restart) a grill session from a job and profile; returns the questions plus a ready-to-send outbound message.",
            _h_grill_start, [_p("job_id", "string", True),
                                         _p("job", "object", True, help="Job dict (title, company, description)."),
                                         _profile_param()]),
         _a("answer", "Record an answer to one grill question.",
            _h_grill_answer, [_p("job_id", "string", True), _p("question_id", "string", True),
                                          _p("answer", "string", True)]),
         _a("status", "Completion status (complete / answered / total) plus answered Q/A pairs.",
            _h_grill_status, [_p("job_id", "string", True)]),
         _a("qa_pairs", "Answered (question, answer) pairs in question order; the handoff so the tailor step never invents facts.",
            _h_grill_qa_pairs, [_p("job_id", "string", True)]),
         _a("cancel", "Delete a grill session.",
            _h_grill_cancel, [_p("job_id", "string", True)]),
         _a("keywords", "JD keywords with profile evidence for one job.",
            _h_grill_keywords, [_p("job", "object", True, help="Job dict (title, company, description).")]),
     ]},
    # ---------------- TRAIN ----------------
    {"id": "mock_interview", "group": "TRAIN", "name": "Mock interview",
     "description": "Deterministic 5-question mock interviews with STAR-scored answers. Chat channel is non-interactive.",
     "actions": [
         _a("start", "Start a session and get the first question.",
            _h_mock_start, [_p("company", "string", True), _p("role", "string", True),
                                       _profile_param(),
                                       _p("channel", "string", default="chat", help="'chat' or 'whatsapp'."),
                                       _p("job_id", "string", help="Reuse questions from an interview brief.")]),
         _a("answer", "Submit an answer; get scored feedback and the next question.",
            _h_mock_answer, [_p("session_id", "string", True), _p("answer", "string", True)]),
         _a("summary", "Final scorecard and tips for a session.",
            _h_mock_summary, [_p("session_id", "string", True)]),
     ]},
    {"id": "soft_skills", "group": "TRAIN", "name": "Soft-skills drills",
     "description": "Scenario assessments, coaching drills, and a salary-negotiation simulator.",
     "actions": [
         _a("assess", "Score answers for a skill area.",
            _h_soft_assess, [_p("area", "string", True), _p("answers", "array", True)]),
         _a("drill", "Start a coaching drill scenario.",
            _h_soft_drill, [_p("area", "string", True), _profile_param(),
                                       _p("mode", "string", default="coach"),
                                       _p("difficulty", "string", default="firm")]),
         _a("review", "Get coached feedback on a drill answer.",
            _h_soft_review, [_p("session_id", "string", True), _p("answer", "string", True)]),
         _a("negotiate", "Run a salary-negotiation simulation.",
            _h_soft_negotiate, [_p("current_offer", "number", True), _p("target", "number", True),
                                              _profile_param(), _p("difficulty", "string", default="firm")]),
     ]},
    {"id": "ai_proficiency", "group": "TRAIN", "name": "AI proficiency",
     "description": "Lesson tracks, graded exercises, diagnostics, and progress tracking.",
     "actions": [
         _a("tracks", "List available lesson tracks.", _h_ai_tracks, []),
         _a("plan", "Learning plan for a track and level.",
            _h_ai_plan, [_p("track", "string", True), _p("level", "string")]),
         _a("lesson", "Fetch a lesson's content.",
            _h_ai_lesson, [_p("track", "string", True), _p("lesson_id", "string", True)]),
         _a("exercise", "Start a graded exercise (returns a session_id).",
            _h_ai_exercise, [_p("track", "string", True), _p("lesson_id", "string", True)]),
         _a("submit", "Submit an exercise response for grading.",
            _h_ai_submit, [_p("session_id", "string", True), _p("response", "string", True)]),
         _a("progress", "Overall progress and streaks.", _h_ai_progress, []),
         _a("diagnose", "Diagnose a level from quiz answers.",
            _h_ai_diagnose, [_p("track", "string", True), _p("answers", "array", True)]),
     ]},
    {"id": "mentors", "group": "TRAIN", "name": "Mentor matchmaking",
     "description": "Opt in as a mentor, get matched, draft outreach, prep for topics.",
     "actions": [
         _a("matchmake", "Match mentors to mentee answers.",
            _h_mentors_matchmake, [_p("answers", "object", True, help="Goal, role, topics, availability answers.")]),
         _a("outreach", "Draft a connection request to a mentor.",
            _h_mentors_outreach, [_p("mentor_id", "string", True),
                                                 _p("mentee_name", "string", True),
                                                 _p("mentee_goal", "string", True)]),
         _a("prep", "Prep notes for a mentorship topic.",
            _h_mentors_prep, [_p("topic", "string", True)]),
         _a("suggest", "Suggest mentors based on your training weak areas.",
            _h_mentors_suggest, []),
         _a("opt_in", "Publish your own mentor card.",
            _h_mentors_opt_in, [_p("profile", "object", True, help="Mentor profile card fields.")]),
         _a("opt_out", "Remove your mentor card.",
            _h_mentors_opt_out, [_p("mentor_id", "string")]),
         _a("card", "View or update your mentor card.",
            _h_mentors_card, [_p("updates", "object")]),
         _a("record_outreach", "Record that you reached out to a mentor.",
            _h_mentors_record_outreach, [_p("mentor_id", "string", True)]),
     ]},
    {"id": "skill_gaps", "group": "TRAIN", "name": "Skill-gap analyzer",
     "description": "Compares profile skills against target jobs and builds a learning plan.",
     "actions": [
         _a("analyze", "Find gaps between your profile and target jobs.",
            _h_gaps_analyze, [_profile_param(), _p("jobs", "array", help="Job dicts; defaults to watched targets.")]),
         _a("plan", "Learning plan for the recorded gaps.", _h_gaps_plan, []),
         _a("mark_progress", "Update progress on a gap skill.",
            _h_gaps_mark, [_p("skill", "string", True), _p("status", "string", True),
                                      _p("evidence", "string")]),
     ]},
    {"id": "briefs", "group": "TRAIN", "name": "Company briefs & interview prep",
     "description": "Public-web company research and full interview-prep docs. Fields are marked unverified when the fetch returns nothing; nothing is invented.",
     "actions": [
         _a("company_brief", "Research brief for a company: what they do, funding, headcount hints, recent news, interview-process hints.",
            _h_briefs_company, [_p("company", "string", True)]),
         _a("interview_prep", "Interview-prep doc for a job id: role summary, key requirements, likely questions, STAR stories from the profile, questions to ask, salary talking points.",
            _h_briefs_prep, [_p("job_id", "string", True), _profile_param()]),
     ]},
    # ---------------- WIN ----------------
    {"id": "followup", "group": "WIN", "name": "Follow-up emails",
     "description": "Drafts follow-up emails from application history. Sending requires a genuine interactive user approval at the send boundary: the user is shown the exact draft at a terminal and types the per-send varying value (by default the recipient address exactly as shown); a fixed string like 'send' FAILS. A confirmed=true flag alone never authorizes a send.",
     "actions": [
         _a("draft", "Draft a follow-up for one application entry.",
            _h_followup_draft, [_p("entry", "object", True, help="Application entry dict."),
                                               _profile_param()]),
         _a("drafts", "All due follow-ups with drafts.",
            _h_followup_drafts, [_profile_param()]),
         _a("send", "Send a follow-up email. INTERACTIVE-APPROVAL GATED: the exact draft must have been approved by the user at a terminal send prompt — they must type the per-send varying value shown with the draft (by default the recipient address exactly as shown); a hardcoded 'send'/'yes' fails. Without that approval the send is refused. confirmed=true alone authorizes nothing.",
            _h_followup_draft,  # placeholder; dispatch uses GATED entry
            [_p("entry_id", "string", True), _p("to", "string", True),
             _p("subject", "string", True), _p("body", "string", True),
             _p("confirmed", "boolean"),
             _p("approval_id", "string", False,
                help="Approval record from an interactive approval of this exact draft.")]),
     ]},
    {"id": "network_crm", "group": "WIN", "name": "Network CRM",
     "description": "Local contact list with interaction logging, nudge lists, and warm paths to companies.",
     "actions": [
         _a("add", "Add a contact.",
            _h_crm_add, [_p("name", "string", True), _p("company", "string", True),
                                    _p("role", "string"), _p("industry", "string"),
                                    _p("linkedin_url", "string"), _p("source", "string"),
                                    _p("notes", "string")]),
         _a("list", "List all contacts.", _h_crm_list, []),
         _a("log", "Log an interaction with a contact.",
            _h_crm_log, [_p("contact_id", "string", True), _p("kind", "string", True),
                                    _p("summary", "string", True), _p("due", "string", help="ISO date for the next nudge.")]),
         _a("nudges", "Contacts with overdue follow-ups.", _h_crm_nudges, []),
         _a("warm_path", "Find the warmest path to someone at a company.",
            _h_crm_warm_path, [_p("company", "string", True)]),
     ]},
    {"id": "offer_compare", "group": "WIN", "name": "Offer comparison",
     "description": "4-year total-comp math, weighted offer ranking, and per-offer reports.",
     "actions": [
         _a("add", "Record an offer.",
            _h_offer_add, [_p("name", "string", True), _p("base", "number", True),
                                      _p("bonus", "number", default=0), _p("equity_total", "number", default=0),
                                      _p("equity_years", "number", default=4),
                                      _p("benefits_value", "number", default=0),
                                      _p("pto_days", "number", default=15),
                                      _p("remote", "string", default="onsite"),
                                      _p("location", "string"), _p("growth_score", "number", default=5),
                                      _p("notes", "string")]),
         _a("list", "List all recorded offers.", _h_offer_list, []),
         _a("report", "Full report for one offer.",
            _h_offer_report, [_p("offer_id", "string", True)]),
         _a("compare", "Rank offers by weighted score.",
            _h_offer_compare, [_p("weights", "object", help="Factor -> weight overrides.")]),
         _a("weights", "Current comparison weights.", _h_offer_weights, []),
         _a("set_weights", "Override comparison weights.",
            _h_offer_set_weights, [_p("overrides", "object", True)]),
     ]},
    {"id": "rejection_autopsy", "group": "WIN", "name": "Rejection autopsy",
     "description": "Records application outcomes and diagnoses where your funnel leaks.",
     "actions": [
         _a("record", "Record an outcome for an application.",
            _h_autopsy_record, [_p("job_id", "string", True), _p("outcome", "string", True),
                                               _p("stage", "string", True), _p("notes", "string"),
                                               _p("applied_at", "string")]),
         _a("autopsy", "Aggregate outcome stats.", _h_autopsy_run, []),
         _a("diagnose", "Pattern diagnosis with suggestions.", _h_autopsy_diagnose, []),
     ]},
    {"id": "streaks", "group": "WIN", "name": "Streaks & reps",
     "description": "Daily rep logging, current/longest streaks, goals, and weekly recaps.",
     "actions": [
         _a("log", "Log a rep of a kind (e.g. 'applications', 'outreach').",
            _h_streaks_log, [_p("kind", "string", True)]),
         _a("streaks", "Current and longest streaks.", _h_streaks_view, []),
         _a("set_goal", "Set a daily target for a rep kind.",
            _h_streaks_set_goal, [_p("kind", "string", True), _p("daily_target", "integer", True)]),
         _a("goals", "All daily goals.", _h_streaks_goals, []),
         _a("today", "Today's progress vs goals.", _h_streaks_today, []),
         _a("weekly", "Weekly recap.", _h_streaks_weekly, []),
         _a("card", "Shareable streak card.", _h_streaks_card, []),
     ]},
    {"id": "email_sync", "group": "WIN", "name": "Recruiter email scan",
     "description": "Scans Gmail for recruiter mail and proposes application stage updates. Proposals only: never applies updates and never sends mail.",
     "actions": [
         _a("scan", "Scan recruiter mail from the last N days and return proposed stage updates. Nothing is applied.",
            _h_email_scan, [_p("days", "integer", default=14, help="How many days back to scan.")]),
         _a("draft_followup", "Draft a follow-up email for an application. Draft only; the dashboard never sends email.",
            _h_email_draft, [_p("application_id", "string", True),
                                            _p("kind", "string", default="check_in", help="'check_in', 'thank_you', or 'nudge'.")]),
     ]},
    # ---------------- GOVERN ----------------
        {"id": "crew", "group": "GOVERN", "name": "Agent crew",
     "description": "Specialist personas, red-team scans, and compliance screening.",
     "actions": [
         _a("run", "Run a specialist persona on a task.",
            _h_crew_run, [_p("persona_id", "string", True), _p("task", "string", True),
                                     _p("context", "object")]),
         _a("plan", "CEO persona breaks a goal into tasks.",
            _h_crew_plan, [_p("goal", "string", True)]),
         _a("status", "Crew roster and availability.", _h_crew_status, []),
         _a("red_team", "Red-team scan of a text.",
            _h_crew_red_team, [_p("text", "string", True)]),
         _a("compliance", "Compliance screen of a task.",
            _h_crew_compliance, [_p("task", "string", True)]),
     ]},
    # ---------------- CORE ----------------
    {"id": "search", "group": "CORE", "name": "Job search",
     "description": "Search job boards, fetch posting details, track applications, record applications.",
     "actions": [
         _a("query", "Search jobs across boards.",
            _h_search_query, [_p("query", "string", True), _p("location", "string", True),
                                         _p("board", "string", default="all"),
                                         _p("limit", "integer", default=10),
                                         _p("remote_only", "boolean", default=False),
                                         _p("salary_min", "integer", default=0),
                                         _p("seniority", "string")]),
         _a("details", "Full details for a job id.",
            _h_search_details, [_p("job_id", "string", True)]),
         _a("boards", "Available job boards.", _h_search_boards, []),
         _a("track", "Tracked applications.", _h_search_track, []),
         _a("record_apply", "Record an application (dry-run preview first). CONFIRM-GATED: first call returns preview + hash; re-call with confirmed=true and the same hash.",
            _h_search_query,  # placeholder; dispatch uses GATED entry
            [_p("job_id", "string", True), _p("resume_path", "string", True),
             _p("cover_letter", "string"), _p("answers", "object"), _p("headless", "boolean", default=True),
             _p("confirmed", "boolean"), _p("preview_hash", "string")]),
     ]},
    {"id": "apply_queue", "group": "CORE", "name": "Apply queue",
     "description": "Local queue of jobs to apply to later.",
     "actions": [
         _a("add", "Add a job to the apply queue. CONFIRM-GATED.",
            _h_queue_list,  # placeholder; dispatch uses GATED entry
            [_p("job_id", "string", True), _p("scheduled_for", "string"),
             _p("confirmed", "boolean"), _p("preview_hash", "string")]),
         _a("list", "List queued jobs.", _h_queue_list, []),
         _a("remove", "Remove a job from the queue.",
            _h_queue_remove, [_p("job_id", "string", True)]),
     ]},
    {"id": "wizard", "group": "CORE", "name": "Profile wizard",
     "description": "Profile templates and LinkedIn-merge helpers.",
     "actions": [
         _a("blank", "Blank profile template.", _h_wizard_blank, []),
         _a("merge", "Merge a base profile with a LinkedIn export.",
            _h_wizard_merge, [_p("base", "object", True), _p("linkedin", "object", True)]),
     ]},
    {"id": "doctor", "group": "CORE", "name": "Doctor",
     "description": "Environment health checks: profile, compliance, sessions, boards.",
     "actions": [
         _a("run", "Run all checks.", _h_doctor_run, []),
         _a("profile", "Check the saved profile.", _h_doctor_profile, []),
     ]},
    {"id": "analytics", "group": "CORE", "name": "Analytics",
     "description": "Funnel stats, response rates, and stale-application reports.",
     "actions": [
         _a("report", "Full analytics report.",
            _h_analytics_report, [_p("stale_days", "integer", default=14)]),
         _a("funnel", "Funnel breakdown for a list of applications.",
            _h_analytics_funnel, [_p("applications", "array", True)]),
         _a("response_rate", "Response rate by board.",
            _h_analytics_response_rate, [_p("applications", "array", True)]),
         _a("stale", "Applications stale for N days.",
            _h_analytics_stale, [_p("applications", "array", True),
                                            _p("days", "integer", default=14)]),
     ]},
]

_ACTION_LOOKUP = {(t["id"], a["id"]): a for t in TOOLS for a in t["actions"]}

# ---------------------------------------------------------------------------
# /api/run dispatch
# ---------------------------------------------------------------------------

def _catalog_action(tool_id, action_id):
    """Public catalog entry for a tool/action (no handler internals)."""
    action = _ACTION_LOOKUP.get((tool_id, action_id))
    if action is None:
        return None
    return {"id": action["id"], "description": action["description"],
            "params": action["params"]}


class _BadParams(ValueError):
    """Params were not a JSON object — a client error, not a server bug."""


def run_action(tool_id, action_id, params):
    """Dispatch one action. Returns a JSON-safe response dict.

    Client-caused failures carry a ``_status`` hint (stripped by the HTTP
    layer before serializing) so the server answers 400 instead of 200.
    """
    action = _ACTION_LOOKUP.get((tool_id, action_id))
    if action is None:
        return {"ok": False, "error": f"unknown tool/action: {tool_id}/{action_id}",
                "_status": 400}

    gate = GATED.get((tool_id, action_id))
    try:
        if params is not None and not isinstance(params, dict):
            raise _BadParams("params must be an object")
        params = dict(params or {})
        if gate is not None:
            canonical, preview_text = gate["preview"](params)
            expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if params.get("confirmed") is True and params.get("preview_hash") == expected:
                data = gate["run"](params)
                return {"ok": True, "data": _clean(data)}
            if params.get("preview_hash") and params.get("preview_hash") != expected:
                payload = _preview_payload(preview_text)
                payload["ok"] = False
                payload["error"] = "preview mismatch — review the new preview"
                return payload
            return _preview_payload(preview_text)
        data = action["handler"](params)
        return {"ok": True, "data": _clean(data)}
    except _GateError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        # bad/missing params: the caller's mistake — log once, no traceback
        print(f"[webui] {tool_id}/{action_id} bad params: {exc}", file=sys.stderr)
        return {"ok": False, "error": str(exc), "_status": 400}
    except Exception as exc:  # never leak a traceback as a crash; log it once
        print(f"[webui] {tool_id}/{action_id} failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# /api/kpis
# ---------------------------------------------------------------------------

_KPI_KEYS = ["applications", "avg_fit", "streak_days", "interviews",
             "offers", "followups_due", "activity_30d", "funnel"]


def get_kpis():
    """Return dashboard KPIs; zeros + degraded flag if dashboard_data is absent."""
    try:
        import dashboard_data
        kpis = dashboard_data.compute_kpis(BASE_DIR)
        result = {k: kpis.get(k, 0) for k in _KPI_KEYS}
        result["degraded"] = False
        return _clean(result)
    except Exception as exc:
        print(f"[webui] dashboard_data unavailable: {exc}", file=sys.stderr)
        result = {k: ({} if k == "funnel" else 0) for k in _KPI_KEYS}
        result["degraded"] = True
        return result


# ---------------------------------------------------------------------------
# Token auth + LAN binding
# ---------------------------------------------------------------------------
# Every /api/* route requires ``Authorization: Bearer <token>``. The token
# lives in ``~/.veto_webui_token`` (override with ``VETO_WEBUI_TOKEN_FILE``),
# created on first serve with mode 0600. Static files stay open — the
# frontend JS handles the login gate. The token never appears in URLs or
# error messages. It is printed to the terminal only when the file is first
# created, on ``--regenerate-token``, or on ``--show-token`` — never on
# routine boots. Stdout is a log channel under process supervision, so do
# not pipe it to logs.

_TOKEN_FILE_ENV = "VETO_WEBUI_TOKEN_FILE"
DEFAULT_TOKEN_FILE = Path.home() / ".veto_webui_token"


def token_file_path() -> Path:
    """Token file location: ``VETO_WEBUI_TOKEN_FILE`` override or the default."""
    override = os.environ.get(_TOKEN_FILE_ENV)
    return Path(override).expanduser() if override else DEFAULT_TOKEN_FILE


def load_or_create_token(path: Path | None = None) -> str:
    """Return the bearer token, creating the file (mode 0600) when missing.

    An existing file with permissions looser than 0600 is tightened to 0600
    with a stderr warning. Raises RuntimeError for an empty file and OSError
    for filesystem failures.
    """
    path = Path(path) if path is not None else token_file_path()
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            os.chmod(path, 0o600)
            print(f"[webui] warning: token file {path} had permissions "
                  f"{mode:04o}; tightened to 0600", file=sys.stderr)
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(
                f"token file {path} is empty; delete it to generate a new token")
        return token
    token = secrets.token_urlsafe(32)
    try:
        # O_EXCL so the file is never truncated mid-read by a concurrent
        # serve; mode 0o600 so it never lands world-readable.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not path.exists():
            # Broken symlink or similar: the path doesn't resolve, yet
            # creation collides — do not recurse forever.
            raise OSError(
                f"token file {path} cannot be created "
                f"(dangling symlink?)")
        return load_or_create_token(path)  # lost the race; read the winner
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    # os.open's mode is masked by the umask — re-assert 0600 explicitly.
    os.chmod(path, 0o600)
    return token


def regenerate_token(path: Path | None = None) -> str:
    """Atomically replace the token file with a fresh token (mode 0600)."""
    path = Path(path) if path is not None else token_file_path()
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)  # atomic on POSIX
    return token


def resolve_lan_ip() -> str:
    """Best-effort LAN IPv4 for this machine. Raises OSError when unknown."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent: connect() just picks the source address the
        # kernel would use for the default route.
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError as exc:
        raise OSError(
            "could not determine this machine's LAN IPv4 address "
            f"(UDP route lookup and hostname resolution both failed: {exc})"
        ) from exc


def resolve_bind_host(host: str) -> str:
    """Map the --host flag to a bind address; ``lan`` auto-detects the LAN IP."""
    if host == "lan":
        try:
            return resolve_lan_ip()
        except OSError as exc:
            raise OSError(
                f"--host lan: {exc}; pass an explicit IP address instead"
            ) from exc
    return host


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "VetoWebUI/1.0"

    # -- helpers ---------------------------------------------------------
    def _send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: Path):
        ctype, _ = mimetypes.guess_type(str(path))
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
                "application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # keep logs tidy
        sys.stderr.write(f"[webui] {self.address_string()} {fmt % args}\n")

    # -- auth ------------------------------------------------------------
    def _token_from_file(self) -> str | None:
        """Read-only token lookup for servers that carry no ``auth_token``.

        A bare ``http.server.HTTPServer`` wrapping this handler has no token
        in memory. Read the token file env-aware, but never create it here:
        a missing/unreadable/empty file yields None (fail closed).
        """
        try:
            value = token_file_path().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return value or None

    def _deny(self) -> bool:
        self._send_json({"ok": False, "error": "unauthorized"}, status=401,
                        headers={"WWW-Authenticate": "Bearer"})
        return False

    def _require_auth(self) -> bool:
        """Gate /api/* routes on the bearer token.

        ``serve()`` always installs the token on the server instance, so it
        is never None there. When the handler is mounted on a server with no
        ``auth_token`` attribute, the token is looked up from the token file
        (read-only); if that fails the request is denied. The API is never
        served open. Returns True when the request may proceed, else sends
        a 401 and returns False. The token value is never written to logs or echoed.
        """
        token = getattr(self.server, "auth_token", None)
        if token is None:
            token = self._token_from_file()
        if not token:
            return self._deny()
        presented = self.headers.get("Authorization") or ""
        if presented.startswith("Bearer "):
            presented = presented[len("Bearer "):].strip()
        else:
            presented = ""
        try:
            ok = bool(presented) and hmac.compare_digest(presented, token)
        except TypeError:
            # compare_digest rejects non-ASCII str: deny with 401, not 500.
            ok = False
        if ok:
            return True
        return self._deny()

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if unquote(path).startswith("/api/"):
                if not self._require_auth():
                    return
            if path == "/api/tools":
                tools = []
                for t in TOOLS:
                    tools.append({
                        "id": t["id"], "group": t["group"], "name": t["name"],
                        "description": t["description"],
                        "actions": [
                            {"id": a["id"], "description": a["description"],
                             "params": a["params"]}
                            for a in t["actions"]
                        ],
                    })
                return self._send_json({"tools": tools})
            if path == "/api/kpis":
                return self._send_json(get_kpis())
            if path in ("/api/run",):
                return self._send_json(
                    {"ok": False, "error": "use POST"}, status=405)
            return self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc) or "internal error"},
                            status=500)

    def _serve_static(self, path):
        rel = unquote(path)
        if rel == "/" or rel == "":
            rel = "/index.html"
        rel = rel.lstrip("/")
        if not rel:
            rel = "index.html"
        target = (WEBUI_DIR / rel).resolve()
        try:
            target.relative_to(WEBUI_DIR.resolve())
        except ValueError:
            return self._send_json({"ok": False, "error": "forbidden"}, status=403)
        if target.is_file():
            return self._send_static(target)
        return self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if unquote(path).startswith("/api/"):
                if not self._require_auth():
                    return
            if path != "/api/run":
                return self._send_json({"ok": False, "error": "not found"},
                                       status=404)
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._send_json({"ok": False, "error": "bad request body"},
                                       status=400)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send_json({"ok": False, "error": "invalid JSON"},
                                       status=400)
            if not isinstance(body, dict):
                return self._send_json({"ok": False, "error": "expected a JSON object"},
                                       status=400)
            resp = run_action(body.get("tool"), body.get("action"),
                              body.get("params"))
            status = 200
            if isinstance(resp, dict):
                status = int(resp.pop("_status", 200) or 200)
            return self._send_json(resp, status=status)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc) or "internal error"},
                            status=500)

    # No directory listings, no PUT/DELETE.
    def do_PUT(self):
        self._send_json({"ok": False, "error": "method not allowed"}, status=405)

    def do_DELETE(self):
        self._send_json({"ok": False, "error": "method not allowed"}, status=405)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int, host: str = "127.0.0.1", token: str | None = None) -> _Server:
    """Create (not start) the HTTP server.

    Every /api/* route requires bearer auth. ``token=None`` (the default)
    loads the token from the token file, creating it when missing — the API
    is never served open. ``cmd_serve`` always passes an explicit token.
    """
    if token is None:
        token = load_or_create_token()
    server = _Server((host, port), _Handler)
    server.auth_token = token
    return server


# ---------------------------------------------------------------------------
# Entry point for cli.py: cmd_serve(args) -> int
# ---------------------------------------------------------------------------

def _print_startup(host: str, port: int, token: str, *, show_token: bool = True) -> None:
    """Print the bind URL (plus LAN URL off-loopback) and the auth token."""
    # flush=True: this process blocks in serve_forever right after, so when
    # stdout is a pipe/file the operator must still see these lines.
    print("Veto web UI is running", flush=True)
    if host == "0.0.0.0":
        # 0.0.0.0 is not a connectable URL — lead with the LAN URL instead.
        try:
            lan_host = resolve_lan_ip()
        except OSError:
            lan_host = "<lan-ip>"
        print(f"  LAN URL:  http://{lan_host}:{port}", flush=True)
        print("  (bound to ALL interfaces: 0.0.0.0)", flush=True)
    else:
        print(f"  URL:  http://{host}:{port}", flush=True)
        if host != "127.0.0.1":
            print(f"  LAN URL:  http://{host}:{port}", flush=True)
    if host != "127.0.0.1":
        bind_note = ("--host 0.0.0.0 binds ALL interfaces; " if host == "0.0.0.0"
                     else "")
        print(f"  WARNING: {bind_note}this server is plain HTTP (no TLS): the "
              f"bearer token travels in cleartext on the network. Use only "
              f"on a trusted LAN; for anything else use Tailscale.", flush=True)
    if show_token:
        print("  Token (keep private; send as 'Authorization: Bearer <token>'):",
              flush=True)
        print(f"  {token}", flush=True)
    print(f"  PID:  {os.getpid()}", flush=True)
    print("  Stop: press Ctrl-C", flush=True)


def cmd_serve(args) -> int:
    """Start the web UI server. Blocks until Ctrl-C."""
    token_path = token_file_path()
    if getattr(args, "regenerate_token", False):
        try:
            token = regenerate_token(token_path)
        except (OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"Web UI token regenerated and saved to {token_path} (mode 0600).",
              flush=True)
        print("Restart any running server for the new token to take effect "
              "(the old token stays valid until restart).", flush=True)
        print(token, flush=True)
        return 0
    if getattr(args, "show_token", False):
        try:
            print(load_or_create_token(token_path), flush=True)
        except (OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    port = getattr(args, "port", None) or os.environ.get("PORT") or DEFAULT_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        print(f"error: invalid port {port!r}", file=sys.stderr)
        return 2
    try:
        host = resolve_bind_host(getattr(args, "host", None) or "127.0.0.1")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Print the token only when the file was just created: stdout is a log
    # channel under process supervision, so routine boots stay quiet.
    token_created = not token_path.exists()
    try:
        token = load_or_create_token(token_path)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        server = serve(port, host=host, token=token)
    except OSError as exc:
        print(f"error: cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 2
    actual_port = server.server_address[1]
    _print_startup(host, actual_port, token, show_token=token_created)
    if not getattr(args, "no_browser", False):
        browser_url = (f"http://{host}:{actual_port}" if host != "0.0.0.0"
                       else f"http://127.0.0.1:{actual_port}")
        try:
            webbrowser.open(browser_url)
        except Exception as exc:  # headless environments
            print(f"(could not open browser: {exc})", file=sys.stderr)
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down web UI server.")
    finally:
        server.server_close()
    return 0


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Veto web UI server (stdlib only).")
    parser.add_argument("--port", type=int, default=None,
                        help=f"Port to bind (default {DEFAULT_PORT}; PORT env also works).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address to bind: 127.0.0.1 (default), 'lan' to "
                             "auto-detect this machine's LAN IP, or an explicit "
                             "IP/hostname.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open the browser automatically.")
    parser.add_argument("--regenerate-token", action="store_true",
                        help="Replace the web UI auth token with a fresh one, "
                             "print it, and exit without starting the server.")
    parser.add_argument("--show-token", action="store_true",
                        help="Print the current web UI auth token and exit "
                             "without starting the server.")
    args = parser.parse_args(argv)
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(_main())
