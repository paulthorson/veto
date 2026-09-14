#!/usr/bin/env python3
"""Initiative 07 terminal wizard: guided mentor-matching & warm-path flows.

Usage (from the project directory)::

    .venv/bin/python -m initiatives.i07.cli mentor opt-in
    .venv/bin/python -m initiatives.i07.cli mentee match
    .venv/bin/python -m initiatives.i07.cli intro request --mentor-id <id>
    .venv/bin/python -m initiatives.i07.cli intro respond --id <hs-id> --approve --channel linkedin_dm
    .venv/bin/python -m initiatives.i07.cli intro withdraw --id <hs-id>
    .venv/bin/python -m initiatives.i07.cli session agenda --id <hs-id>
    .venv/bin/python -m initiatives.i07.cli warm plan --company "Initech"
    .venv/bin/python -m initiatives.i07.cli safety block --id <actor>

Every consequential step previews before it writes and asks for
confirmation (escape hatch: Ctrl-C or answering "no" aborts with no
writes). Empty states are honest; nothing is ever sent anywhere.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import mentors as _mentors
from initiatives.i07 import consent as _consent
from initiatives.i07 import safety as _safety
from initiatives.i07 import session_kit as _kit
from initiatives.i07 import warm_path as _warm


def _out(result: dict, as_json: bool) -> int:
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("ok", True) else 1


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted — nothing was written.")
        raise SystemExit(2)
    return val or default


def _confirm(summary: str) -> bool:
    print("\n--- preview ---")
    print(summary)
    print("---------------")
    try:
        ans = input("Proceed? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted — nothing was written.")
        raise SystemExit(2)
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# Mentor side
# ---------------------------------------------------------------------------


def cmd_mentor_opt_in(_args) -> int:
    print("== Mentor opt-in ==\nYour card is discoverable; contact stays sealed until mutual consent.")
    profile = {
        "name": _ask("Display name or initials (your privacy choice)"),
        "industry": _ask("Industry"),
        "role": _ask("Current role"),
        "seniority": _ask("Seniority band (ic1-3/ic4/ic5+/manager/director+/executive)"),
        "topics": [t.strip() for t in _ask(f"Topics, comma-separated ({', '.join(_mentors.TOPICS)})").split(",") if t.strip()],
        "linkedin_url": _ask("LinkedIn profile URL"),
        "availability": _ask("Availability (free text, optional)"),
        "bio": _ask("Short bio, ≤280 chars (optional)"),
    }
    contact = _ask("Show LinkedIn URL on your public card before consent? (hidden/linkedin_public)", "hidden")
    profile["preferred_contact"] = contact
    wont = _ask("Things you will NOT discuss, comma-separated (optional)")
    offers = _ask("Things you offer (e.g. mock interviews), comma-separated (optional)")
    profile["boundaries"] = {
        "offers": [o.strip() for o in offers.split(",") if o.strip()],
        "wont": [w.strip() for w in wont.split(",") if w.strip()],
        "note": _ask("Boundary note (optional)"),
    }
    if not _confirm(json.dumps(profile, indent=2)):
        return 2
    try:
        res = _mentors.mentor_opt_in(profile)
    except ValueError as exc:
        print(f"Invalid profile: {exc}")
        return 1
    return _out(res, True)


def cmd_mentor_card(args) -> int:
    if args.opt_out:
        if _confirm("Remove your mentor card? This cannot be undone here."):
            return _out(_mentors.mentor_opt_out(), True)
        return 2
    return _out(_mentors.my_mentor_card(), True)


# ---------------------------------------------------------------------------
# Mentee side
# ---------------------------------------------------------------------------


def cmd_mentee_match(_args) -> int:
    print("== Match questionnaire ==\nAnswer what you can; identity preferences are optional and never affect ranking.")
    answers = {
        "industry": _ask("Your industry"),
        "target_role": _ask("Target role"),
        "topics_ranked": [t.strip() for t in _ask("Topics, most important first, comma-separated").split(",") if t.strip()],
        "goal": _ask("Your goal in one sentence"),
        "context": _ask("Context (background, optional)"),
        "urgency": _ask("Urgency (this_week/this_month/exploring)", "exploring"),
        "expected_outcome": _ask("What would a useful session produce? (optional)"),
    }
    ident = _ask("Identity preferences for the mentor to see (optional, voluntary)")
    if ident:
        answers["identity_preferences"] = {"note": ident}
    excluded = _safety.discovery_exclusions("me")
    res = _mentors.matchmake(answers, consent_preview=True, exclude_mentor_ids=excluded)
    if not res.get("matches"):
        print(res.get("guidance"))
        return 0
    print(f"\n{len(res['matches'])} mentor(s) with open capacity (contact sealed until mutual consent):\n")
    for m in res["matches"]:
        print(f"- {m['name']} · {m['role']} · {m['industry']} (score {m['score']})")
        for r in m.get("reasons", [])[:3]:
            print(f"    · {r}")
        print(f"    id: {m['mentor_id']}")
    print(f"\n{res['guidance']}")
    return 0


# ---------------------------------------------------------------------------
# Introductions (two-sided consent)
# ---------------------------------------------------------------------------


def cmd_intro_request(args) -> int:
    mentor_id = args.mentor_id or _ask("Mentor id (from match results)")
    goal = _ask("Goal for this introduction (required)")
    label = _ask("Your display name or initials")
    contact_path = _ask("How the mentor may reach you after mutual consent (optional)")
    context = _ask("Context for the mentor (optional)")
    urgency = _ask("Urgency (this_week/this_month/exploring)", "exploring")
    expected = _ask("What a useful session would produce (optional)")
    preview = (
        f"Request introduction to mentor {mentor_id}\n"
        f"As: {label}\nGoal: {goal}\n"
        "This records YOUR consent. Nothing is sent; contact stays sealed "
        "until the mentor also approves."
    )
    if not _confirm(preview):
        return 2
    return _out(
        _consent.request_introduction(
            mentor_id, "me", label, goal,
            context=context, urgency=urgency,
            expected_outcome=expected, contact_path=contact_path,
        ),
        True,
    )


def cmd_intro_respond(args) -> int:
    hs_id = args.id or _ask("Handshake id")
    if bool(args.approve) == bool(args.decline):
        print("Specify exactly one of --approve or --decline (recording the mentor's actual reply).")
        return 1
    decision = "approve" if args.approve else "decline"
    channel = args.channel or _ask("How did the mentor communicate this? (e.g. linkedin_dm, email)")
    note = _ask("Note (optional)")
    if not _confirm(f"Record mentor decision: {decision} (channel: {channel})"):
        return 2
    return _out(_consent.mentor_respond(hs_id, decision, channel=channel, note=note), True)


def cmd_intro_withdraw(args) -> int:
    hs_id = args.id or _ask("Handshake id")
    reason = _ask("Reason (optional)")
    if not _confirm(f"Withdraw from handshake {hs_id}? This is immediate and final."):
        return 2
    return _out(_consent.withdraw(hs_id, "me", reason=reason), True)


def cmd_intro_list(args) -> int:
    handshakes = _consent.list_handshakes(actor="me", state=args.state)
    if not handshakes:
        print("No introductions yet. Run the match questionnaire to find mentors.")
        return 0
    for hs in handshakes:
        print(f"- {hs['id']} · mentor {hs['mentor_id']} · {hs['state']} · goal: {hs.get('goal','')[:60]}")
    return 0


def cmd_intro_reveal(args) -> int:
    hs_id = args.id or _ask("Handshake id")
    return _out(_consent.reveal_contact(hs_id, "me"), True)


# ---------------------------------------------------------------------------
# Session kit
# ---------------------------------------------------------------------------


def cmd_session(args) -> int:
    hs_id = args.id or _ask("Handshake id")
    if args.sub == "agenda":
        res = _kit.build_agenda(hs_id, "me")
        print(res.get("agenda", res))
        return 0 if res.get("ok") else 1
    if args.sub == "packet":
        return _out(_kit.context_packet(hs_id, "me"), True)
    if args.sub == "questions":
        topics = [t.strip() for t in _ask("Topics, comma-separated").split(",") if t.strip()]
        goal = _ask("Session goal (optional)")
        res = _kit.question_builder(topics, goal=goal)
        if res.get("ok"):
            for q in res["questions"]:
                print(f"- [{q['topic']}] {q['question']}")
            return 0
        return _out(res, True)
    if args.sub == "note":
        kind = _ask("Kind (note/takeaway/action)", "note")
        text = _ask("Note text")
        return _out(_kit.add_note(hs_id, "me", text, kind=kind), True)
    if args.sub == "followup":
        res = _kit.followup_plan(hs_id, "me")
        print(res.get("plan", res))
        return 0 if res.get("ok") else 1
    if args.sub == "feedback":
        while True:
            raw = _ask("How useful was the session? (1-5)")
            if raw.isdigit() and 1 <= int(raw) <= 5:
                score = int(raw)
                break
            print("Enter a number from 1 to 5.")
        note = _ask("Note (optional)")
        return _out(_kit.session_feedback(hs_id, "me", score, note=note), True)
    return 1


# ---------------------------------------------------------------------------
# Warm paths + safety
# ---------------------------------------------------------------------------


def cmd_warm_plan(args) -> int:
    company = args.company or _ask("Target company")
    res = _warm.plan_warm_path(company)
    if not res.get("ok"):
        return _out(res, True)
    if not res["plans"]:
        print(res["guidance"])
        return 0
    for p in res["plans"]:
        print(f"\n== {p['contact_name']} ({p['source']}, {p.get('degrees', '?')}°): {p.get('via', '')}")
        print(f"Subject: {p['draft']['subject']}\n{p['draft']['body']}")
        print(f"-- draft {p['draft_id']} saved for review; NOTHING SENT --")
    print("\nHonesty checklist:")
    for item in res["honesty_checklist"]:
        print(f"  [ ] {item}")
    return 0


def cmd_safety(args) -> int:
    if args.sub == "block":
        target = args.id or _ask("Actor id to block")
        if _confirm(f"Block {target}? They will disappear from your discovery results."):
            return _out(_safety.block_actor("me", target, reason=_ask("Reason (optional)")), True)
        return 2
    if args.sub == "unblock":
        target = args.id or _ask("Actor id to unblock")
        return _out(_safety.unblock_actor("me", target), True)
    if args.sub == "report":
        target = args.id or _ask("Actor id to report")
        print(f"Categories: {', '.join(_safety.REPORT_CATEGORIES)}")
        category = _ask("Category")
        detail = _ask("Detail (optional)")
        if _confirm(f"Report {target} for {category}?"):
            return _out(_safety.report("me", target, category, detail=detail), True)
        return 2
    if args.sub == "delete-me":
        if _confirm("Erase ALL your mentorship data (handshakes, questionnaires)? The audit trail keeps a tombstone."):
            return _out(_safety.delete_mentee_data("me", requested_by="me"), True)
        return 2
    return 1


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="veto-i07", description="Mentor matching & warm paths (Initiative 07)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mentor", help="Mentor card management")
    msub = pm.add_subparsers(dest="msub", required=True)
    msub.add_parser("opt-in", help="Guided mentor opt-in").set_defaults(fn=cmd_mentor_opt_in)
    mc = msub.add_parser("card", help="Show your card")
    mc.add_argument("--opt-out", action="store_true")
    mc.set_defaults(fn=cmd_mentor_card)

    pe = sub.add_parser("mentee", help="Mentee flows")
    esub = pe.add_subparsers(dest="esub", required=True)
    esub.add_parser("match", help="Guided match questionnaire").set_defaults(fn=cmd_mentee_match)

    pi = sub.add_parser("intro", help="Two-sided consent introductions")
    isub = pi.add_subparsers(dest="isub", required=True)
    ir = isub.add_parser("request", help="Request an introduction (records your consent)")
    ir.add_argument("--mentor-id")
    ir.set_defaults(fn=cmd_intro_request)
    irp = isub.add_parser("respond", help="Record the mentor's reply (approve/decline)")
    irp.add_argument("--id")
    irp.add_argument("--approve", action="store_true")
    irp.add_argument("--decline", action="store_true")
    irp.add_argument("--channel")
    irp.set_defaults(fn=cmd_intro_respond)
    iw = isub.add_parser("withdraw", help="Withdraw (either party, any time)")
    iw.add_argument("--id")
    iw.set_defaults(fn=cmd_intro_withdraw)
    il = isub.add_parser("list", help="List your introductions")
    il.add_argument("--state")
    il.set_defaults(fn=cmd_intro_list)
    iv = isub.add_parser("reveal", help="Reveal contact (mutual consent only)")
    iv.add_argument("--id")
    iv.set_defaults(fn=cmd_intro_reveal)

    ps = sub.add_parser("session", help="Session kit (mutual consent required)")
    ps.add_argument("sub", choices=["agenda", "packet", "questions", "note", "followup", "feedback"])
    ps.add_argument("--id")
    ps.set_defaults(fn=cmd_session)

    pw = sub.add_parser("warm", help="Warm-path planner (drafts only, never sends)")
    wsub = pw.add_subparsers(dest="wsub", required=True)
    wpl = wsub.add_parser("plan", help="Plan warm outreach to a company")
    wpl.add_argument("--company")
    wpl.set_defaults(fn=cmd_warm_plan)

    pf = sub.add_parser("safety", help="Block, report, deletion")
    fsub = pf.add_subparsers(dest="fsub", required=True)
    for name in ("block", "unblock", "report"):
        sp = fsub.add_parser(name)
        sp.add_argument("--id")
        sp.set_defaults(fn=cmd_safety, sub=name)
    sdel = fsub.add_parser("delete-me")
    sdel.set_defaults(fn=cmd_safety, sub="delete-me")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
