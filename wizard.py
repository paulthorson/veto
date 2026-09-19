#!/usr/bin/env python3
"""Onboarding wizard for the Veto MCP server.

Run interactively::

    python3 wizard.py
    python3 wizard.py --grill   # deep interrogation mode: per-role quantified
                                # achievements, skill years, career highlights
    python3 wizard.py --phone   # phone-access setup: Home Wi-Fi or Tailscale,
                                # so the web dashboard can be opened on a phone

Answers can also be piped via stdin for scripted runs, e.g.::

    printf 'Jane Doe\\n...\\n2\\n' | python3 wizard.py

The wizard collects contact info, job-search preferences, and skills,
optionally ingests a LinkedIn data-export ZIP, and writes the merged
result to ``profiles/profile.json``.

Beyond the basics it also collects targeting (industries, company size,
role track, seniority), compensation expectations, logistics (notice
period, travel, sponsorship, clearance), and deal-breakers — which are
also saved to ``preferences.json`` (blocklist) so searches respect them.
In --grill mode it interrogates every role for quantified achievements,
which power the resume builder and per-job tailoring.

The written profile includes at least the keys the browser-apply hook
needs: full_name (plus first_name/last_name), email, phone, location,
linkedin_url, website, and cover_letter (empty unless the user supplies
one later).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import compliance
import prefs
import watch

BASE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = BASE_DIR / "profiles"
PROFILE_PATH = PROFILES_DIR / "profile.json"
WATCHES_PATH = BASE_DIR / "watches.json"
COMPLIANCE_PATH = compliance.COMPLIANCE_FILE

# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def ask(prompt: str, default: str = "") -> str:
    """Ask a question, showing the default. Enter accepts the default."""
    if default:
        answer = input(f"{prompt} [{default}]: ").strip()
        return answer if answer else default
    return input(f"{prompt}: ").strip()


def ask_list(prompt: str, default: str = "") -> list[str]:
    """Ask for a comma-separated list; returns a cleaned list."""
    raw = ask(prompt, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    """Ask a yes/no question; returns True for yes."""
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def dedupe(items: list[str]) -> list[str]:
    """Dedupe strings case-insensitively, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


# ---------------------------------------------------------------------------
# LinkedIn export ingestion
# ---------------------------------------------------------------------------


def _read_zip_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    """Read a CSV from the ZIP; return [] if missing or unreadable."""
    try:
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig").read()
    except KeyError:
        return []
    reader = csv.DictReader(io.StringIO(text))
    return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def _normalise_date(value: str) -> str:
    """Normalise a LinkedIn date like 'January 2020' to '2020-01'."""
    value = (value or "").strip()
    if not value:
        return ""
    months = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }
    parts = value.split()
    if len(parts) == 2 and parts[0].lower() in months and parts[1].isdigit():
        return f"{parts[1]}-{months[parts[0].lower()]}"
    if len(parts) == 1 and value.isdigit():
        return value
    return value  # keep as-is when it doesn't match a known pattern


def parse_linkedin_export_zip(zip_path: str | Path) -> dict:
    """Parse a LinkedIn data-export ZIP into profile fragments.

    Handles (case-insensitively) Profile.csv, Positions.csv,
    Education.csv and Skills.csv from the official export, returning a
    dict with keys: full_name, first_name, last_name, headline, summary,
    location, linkedin_url, experience (list), education (list), skills
    (list). Missing files just yield empty values.
    """
    zip_path = Path(zip_path).expanduser()
    if not zip_path.is_file():
        raise FileNotFoundError(f"LinkedIn export ZIP not found: {zip_path}")

    data: dict = {
        "full_name": "", "first_name": "", "last_name": "", "headline": "",
        "summary": "", "location": "", "linkedin_url": "",
        "experience": [], "education": [], "skills": [],
    }

    with zipfile.ZipFile(zip_path) as zf:
        names = {n.lower(): n for n in zf.namelist()}

        def get(name: str) -> list[dict[str, str]]:
            return _read_zip_csv(zf, names[name.lower()]) if name.lower() in names else []

        # --- Profile.csv: first/last name, headline, summary, location ---
        for row in get("Profile.csv"):
            keys = {k.lower(): k for k in row}
            getc = lambda *cands: next(
                (row[keys[c]] for c in cands if c in keys), ""
            )
            first = getc("first name", "firstname")
            last = getc("last name", "lastname")
            if not data["full_name"] and (first or last):
                data["first_name"] = first
                data["last_name"] = last
                data["full_name"] = f"{first} {last}".strip()
            data["headline"] = data["headline"] or getc("headline")
            data["summary"] = data["summary"] or getc("summary")
            data["location"] = data["location"] or getc(
                "geo location", "location", "geo_location"
            )

        # --- Positions.csv: title, company, start/end dates, description ---
        for row in get("Positions.csv"):
            keys = {k.lower(): k for k in row}
            getc = lambda *cands: next(
                (row[keys[c]] for c in cands if c in keys), ""
            )
            title = getc("title")
            company = getc("company name", "company")
            if not title and not company:
                continue
            start = _normalise_date(getc("started on", "startedon", "start date"))
            end_raw = getc("finished on", "finishedon", "end date")
            end = "" if end_raw.lower() in {"", "present"} else _normalise_date(end_raw)
            data["experience"].append({
                "title": title,
                "company": company,
                "location": getc("location"),
                "start": start,
                "end": end,  # "" means current role
                "description": getc("description"),
            })

        # --- Education.csv: school, degree, field of study, dates ---
        for row in get("Education.csv"):
            keys = {k.lower(): k for k in row}
            getc = lambda *cands: next(
                (row[keys[c]] for c in cands if c in keys), ""
            )
            school = getc("school name", "school")
            if not school:
                continue
            data["education"].append({
                "school": school,
                "degree": getc("degree name", "degree"),
                "field": getc("field of study", "field"),
                "start": _normalise_date(getc("start date", "started on")),
                "end": _normalise_date(getc("end date", "finished on")),
            })

        # --- Skills.csv: skill names ---
        for row in get("Skills.csv"):
            keys = {k.lower(): k for k in row}
            getc = lambda *cands: next(
                (row[keys[c]] for c in cands if c in keys), ""
            )
            skill = getc("name", "skill", "skills")
            if skill:
                data["skills"].append(skill)

    data["skills"] = dedupe(data["skills"])
    return data


# ---------------------------------------------------------------------------


def merge_profile(base: dict, linkedin: dict) -> dict:
    """Merge LinkedIn fragments into the wizard profile.

    Scalar fields are only filled when the wizard left them empty;
    experience/education are appended (skipping duplicates); skills are
    merged and deduped.
    """
    for key in ("full_name", "headline", "summary", "location"):
        if not base.get(key) and linkedin.get(key):
            base[key] = linkedin[key]
    if not base.get("first_name") and linkedin.get("first_name"):
        base["first_name"] = linkedin["first_name"]
    if not base.get("last_name") and linkedin.get("last_name"):
        base["last_name"] = linkedin["last_name"]

    seen_exp = {
        (e.get("title", "").lower(), e.get("company", "").lower())
        for e in base.get("experience", [])
    }
    for exp in linkedin.get("experience", []):
        key = (exp.get("title", "").lower(), exp.get("company", "").lower())
        if key not in seen_exp and any(exp.values()):
            base.setdefault("experience", []).append(exp)
            seen_exp.add(key)

    seen_edu = {e.get("school", "").lower() for e in base.get("education", [])}
    for edu in linkedin.get("education", []):
        if edu.get("school") and edu["school"].lower() not in seen_edu:
            base.setdefault("education", []).append(edu)
            seen_edu.add(edu["school"].lower())

    base["skills"] = dedupe(base.get("skills", []) + linkedin.get("skills", []))
    return base


def blank_profile() -> dict:
    """Return the profile skeleton with every schema key present."""
    return {
        "full_name": "",
        "first_name": "",
        "last_name": "",
        "email": "",
        "phone": "",
        "location": "",
        "linkedin_url": "",
        "website": "",
        "cover_letter": "",
        "summary": "",
        "target_titles": [],
        "preferred_locations": [],
        "remote_preference": "any",
        "minimum_salary": "",
        "years_experience": "",
        "skills": [],
        "work_authorization": "",
        "experience": [],
        "education": [],
        # --- Targeting ---
        "target_industries": [],
        "excluded_industries": [],
        "company_sizes": [],
        "role_tracks": [],
        "seniority_levels": [],
        "job_types": [],
        "excluded_companies": [],
        "excluded_keywords": [],
        "preferred_companies": [],
        # --- Compensation ---
        "target_salary": "",
        "salary_floor": "",
        "equity_importance": "medium",
        # --- Logistics ---
        "notice_period": "",
        "available_from": "immediately",
        "willing_to_travel": "",
        "willing_to_relocate": "no",
        "needs_sponsorship": "no",
        "security_clearance": "none",
        "certifications": [],
        "languages": [],
        # --- Grill-mode deep dive ---
        "achievements": [],
        "career_highlights": [],
        "skill_years": {},
    }


# ---------------------------------------------------------------------------
# Wizard flow
# ---------------------------------------------------------------------------


def collect_basics() -> dict:
    """Interactive Q&A for the core profile fields."""
    profile = blank_profile()
    print("\n=== Veto Onboarding Wizard ===\n")
    print("Press Enter to accept the [default] shown, or type your own.\n")

    print("--- Contact & identity ---")
    profile["full_name"] = ask("Full name")
    parts = profile["full_name"].split()
    profile["first_name"] = ask("First name", parts[0] if parts else "")
    profile["last_name"] = ask(
        "Last name", " ".join(parts[1:]) if len(parts) > 1 else ""
    )
    profile["email"] = ask("Email")
    profile["phone"] = ask("Phone")
    profile["location"] = ask("Location (city, state)", "New York, NY")
    profile["linkedin_url"] = ask("LinkedIn profile URL")
    profile["website"] = ask("Website / portfolio URL (optional)")

    print("\n--- Job search preferences ---")
    profile["target_titles"] = ask_list("Target job titles (comma-separated)")
    profile["preferred_locations"] = ask_list(
        "Preferred locations (comma-separated)", "Remote"
    )
    remote = ask("Remote preference", "any").lower()
    while remote not in {"remote", "hybrid", "onsite", "any"}:
        print("  Please choose one of: remote / hybrid / onsite / any")
        remote = ask("Remote preference", "any").lower()
    profile["remote_preference"] = remote
    profile["minimum_salary"] = ask("Minimum salary (optional)")
    profile["years_experience"] = ask("Years of experience")
    profile["skills"] = dedupe(ask_list("Top skills (comma-separated)"))
    profile["work_authorization"] = ask(
        "Work authorization status", "US citizen"
    )

    print("\n--- Professional summary ---")
    profile["summary"] = ask(
        "One-paragraph professional summary (optional)"
    )
    return profile


def collect_targets(profile: dict) -> dict:
    """Interactive Q&A for job targeting: industries, size, track, filters."""
    print("\n--- Job targeting ---")
    profile["target_industries"] = dedupe(ask_list(
        "Target industries (comma-separated, e.g. fintech, healthtech)"
    ))
    profile["excluded_industries"] = dedupe(ask_list(
        "Industries to avoid (comma-separated, optional)"
    ))
    profile["company_sizes"] = dedupe(ask_list(
        "Company sizes (comma-separated: startup, mid-size, enterprise)"
    ))
    profile["role_tracks"] = dedupe(ask_list(
        "Role tracks (comma-separated: IC, manager)", "IC"
    ))
    profile["seniority_levels"] = dedupe(ask_list(
        "Seniority levels (comma-separated: entry, mid, senior, staff, principal)"
    ))
    profile["job_types"] = dedupe(ask_list(
        "Job types (comma-separated: full-time, contract)", "full-time"
    ))
    profile["excluded_companies"] = dedupe(ask_list(
        "Companies to exclude from searches (comma-separated, optional)"
    ))
    profile["excluded_keywords"] = dedupe(ask_list(
        "Keywords to exclude (comma-separated, e.g. crypto, gambling)"
    ))
    profile["preferred_companies"] = dedupe(ask_list(
        "Dream companies (comma-separated, optional)"
    ))
    return profile


def collect_compensation(profile: dict) -> dict:
    """Interactive Q&A for compensation expectations."""
    print("\n--- Compensation ---")
    profile["target_salary"] = ask("Target total compensation (optional)")
    profile["salary_floor"] = ask(
        "Absolute salary floor — below this, don't bother (optional)"
    )
    equity = ask("How important is equity? (low / medium / high)",
                 "medium").lower()
    while equity not in {"low", "medium", "high"}:
        print("  Please choose one of: low / medium / high")
        equity = ask("How important is equity? (low / medium / high)",
                     "medium").lower()
    profile["equity_importance"] = equity
    return profile


def collect_logistics(profile: dict) -> dict:
    """Interactive Q&A for availability, travel, sponsorship, clearance."""
    print("\n--- Logistics ---")
    profile["notice_period"] = ask(
        "Notice period (e.g. 2 weeks, immediate)"
    )
    profile["available_from"] = ask(
        "Available to start", "immediately"
    )
    profile["willing_to_travel"] = ask(
        "Willing to travel? (e.g. no, up to 25%)", "no"
    )
    profile["willing_to_relocate"] = ask(
        "Willing to relocate? (yes / no)", "no"
    ).lower()
    profile["needs_sponsorship"] = (
        "yes" if ask_yes_no("Do you need visa sponsorship?") else "no"
    )
    profile["security_clearance"] = ask(
        "Security clearance (e.g. none, active TS/SCI)", "none"
    )
    profile["languages"] = dedupe(ask_list(
        "Languages spoken (comma-separated)"
    ))
    profile["certifications"] = dedupe(ask_list(
        "Certifications / licenses (comma-separated, optional)"
    ))
    return profile


def collect_grill(profile: dict) -> dict:
    """Deep interrogation mode: quantified achievements per role.

    Walks every experience entry asking for up to two quantified wins
    ("with numbers: %, $, users, latency"), then three career highlights,
    then years of hands-on experience per skill. Empty answers skip —
    everything here is optional, but the more numbers you give, the
    stronger the tailored resumes and cover letters.
    """
    print("\n--- Grill mode: quantified achievements ---")
    print("For each role, give your biggest wins WITH numbers "
          "(%, $, users, latency, ...).")
    print("Press Enter on an empty line to skip.\n")

    achievements: list[dict] = []
    for exp in profile.get("experience", []):
        title = exp.get("title", "?")
        company = exp.get("company", "?")
        print(f"Role: {title} @ {company}")
        for n in (1, 2):
            win = ask(f"  Quantified win #{n}")
            if not win:
                break
            achievements.append({
                "company": company,
                "title": title,
                "achievement": win,
            })
    profile["achievements"] = achievements

    print("\nTop 3 career highlights (one line each, empty to skip):")
    highlights = []
    for n in (1, 2, 3):
        highlight = ask(f"  Highlight #{n}")
        if highlight:
            highlights.append(highlight)
    profile["career_highlights"] = highlights

    print("\nYears of hands-on experience per skill (empty to skip):")
    skill_years: dict[str, str] = {}
    for skill in profile.get("skills", []):
        years = ask(f"  {skill}")
        if years:
            skill_years[skill] = years
    profile["skill_years"] = skill_years
    return profile


def seed_preferences(profile: dict) -> dict:
    """Merge the wizard's deal-breakers into preferences.json (dedupe)."""
    current = prefs.load_preferences()
    current["blocked_companies"] = dedupe(
        current.get("blocked_companies", [])
        + profile.get("excluded_companies", [])
    )
    current["blocked_keywords"] = dedupe(
        current.get("blocked_keywords", [])
        + profile.get("excluded_industries", [])
        + profile.get("excluded_keywords", [])
    )
    current["preferred_companies"] = dedupe(
        current.get("preferred_companies", [])
        + profile.get("preferred_companies", [])
    )
    return prefs.save_preferences(current)


def maybe_create_watches(profile: dict) -> list[str]:
    """Offer to seed job watches from the user's target titles."""
    titles = profile.get("target_titles", [])
    if not titles:
        return []
    if not ask_yes_no(
        "Create job watches for your top target titles now?"
    ):
        return []
    location = (profile.get("preferred_locations") or [""])[0]
    watches = watch.load_watches(WATCHES_PATH)
    created = []
    for title in titles[:3]:
        name = f"{title} ({location})".strip()
        watch.add_watch(watches, name=name, query=title,
                        location=location or "", board="all")
        created.append(name)
    watch.save_watches(WATCHES_PATH, watches)
    print(f"  Created {len(created)} watch(es): {', '.join(created)}")
    print("  Run `python watch.py` (or schedule it via cron) to check them.")
    return created


def collect_compliance() -> str:
    """Show the ToS risk summary and record the user's mode choice.

    strict  -> official APIs only (Greenhouse, Lever, Ashby, Adzuna).
    standard -> all boards, but the user must explicitly acknowledge the
               scraping ToS risk first. (The scraping tier currently has
               no live boards.)
    """
    print("\n--- Search compliance ---")
    print("Job boards fall into two risk tiers:")
    print("  official   Greenhouse, Lever, Ashby, Adzuna — public/official")
    print("             APIs. ToS-friendly; always preferred.")
    print("  scraping   No live boards. Glassdoor is a locked stub that")
    print("             performs no scraping; the LinkedIn, Indeed, and")
    print("             ZipRecruiter providers were removed. The tier")
    print("             machinery (budgets, circuit breaker, ToS-risk")
    print("             acknowledgment) remains in the codebase.")
    print("This tool stores no credentials and never auto-submits")
    print("applications without your explicit confirmation.")
    mode = ask(
        "Compliance mode (strict = official APIs only, "
        "standard = all boards)", "strict"
    ).strip().lower()
    while mode not in {"strict", "standard"}:
        print("  Please choose one of: strict / standard")
        mode = ask(
            "Compliance mode (strict = official APIs only, "
            "standard = all boards)", "strict"
        ).strip().lower()
    compliance.set_mode(mode, path=COMPLIANCE_PATH)
    if mode == "standard":
        print("\nStandard mode still requires the ToS-risk acknowledgment,")
        print("even though no board currently scrapes.")
        print("Please confirm you understand the risk described above.")
        while True:
            if ask_yes_no("Acknowledge the scraping ToS risk?", default=False):
                compliance.acknowledge_risks(path=COMPLIANCE_PATH)
                break
            print("  Without acknowledgment, falling back to strict mode.")
            mode = "strict"
            compliance.set_mode(mode, path=COMPLIANCE_PATH)
            break
    print(f"  Compliance mode: {mode}")
    return mode


def collect_grill_channel() -> dict:
    """Ask how the user wants to receive per-application grill questions.

    Baked into onboarding: saved to preferences.json as grill_on_apply +
    grill_channel so every install carries the user's channel choice.
    """
    print("\n--- Application grilling ---")
    print("Before an application goes out, I can grill you with a few")
    print("targeted questions (gaps between the job post and your profile)")
    print("so the application is tailored with your real answers.")
    on_apply = ask_yes_no(
        "Grill me before each application? (recommended)", default=True
    )
    channel = "off"
    if on_apply:
        print("Where should the questions go?")
        print("  chat      ask right here in chat")
        print("  whatsapp  message you on WhatsApp (link it in the Muse app")
        print("            under Messaging Channels when ready)")
        print("  gmail     email you the questions; you reply with numbered")
        print("            answers (connect Gmail in the Muse app first)")
        channel = ask("Channel (chat/whatsapp/gmail)", "chat").strip().lower()
        while channel not in {"chat", "whatsapp", "gmail"}:
            print("  Please choose one of: chat / whatsapp / gmail")
            channel = ask("Channel (chat/whatsapp/gmail)",
                          "chat").strip().lower()
        if channel == "whatsapp":
            print("  Link WhatsApp any time from the Muse app: Messaging")
            print("  Channels. Grilling works in chat until then.")
        elif channel == "gmail":
            print("  Connect Gmail any time from the Muse app's Connectors.")
            print("  Grilling works in chat until then.")
    current = prefs.load_preferences()
    current["grill_on_apply"] = on_apply
    current["grill_channel"] = channel
    prefs.save_preferences(current)
    print(f"  Grilling: {'on via ' + channel if on_apply else 'off'}")
    return {"grill_on_apply": on_apply, "grill_channel": channel}


def linkedin_ingestion() -> dict | None:
    """Offer the LinkedIn ingestion options; return fragments or None."""
    print("\n--- LinkedIn ingestion ---")
    print("  1. Path to LinkedIn data-export ZIP")
    print("     (Settings & Privacy > Data Privacy > Get a copy of your data)")
    print("  2. Skip")
    choice = ask("Choose an option", "2").strip()

    if choice == "1":
        zip_path = ask("Path to the LinkedIn export ZIP")
        try:
            data = parse_linkedin_export_zip(zip_path)
        except FileNotFoundError as exc:
            print(f"  !! {exc} — continuing without LinkedIn data.")
            return None
        except zipfile.BadZipFile:
            print("  !! That file is not a valid ZIP — continuing without "
                  "LinkedIn data.")
            return None
        print(f"  Parsed: {len(data['experience'])} position(s), "
              f"{len(data['education'])} education entr(y/ies), "
              f"{len(data['skills'])} skill(s).")
        return data

    print("  Skipping LinkedIn ingestion.")
    return None


# ---------------------------------------------------------------------------
# Phone access setup (--phone)
# ---------------------------------------------------------------------------

# Mirrors the `serve` default port in webui.py (DEFAULT_PORT) and cli.py
# (--port default). Keep all three in sync if the default ever changes;
# single-sourcing it via an import across modules isn't worth the coupling,
# so this comment is the contract.
WEBUI_PORT = 8765


def _lan_ip() -> str | None:
    """Best-effort LAN IP of this computer (stdlib only).

    First the UDP-socket trick (connect a UDP socket to a public address;
    nothing is actually sent — the kernel just picks the outbound
    interface), then a gethostbyname(gethostname()) fallback. Returns
    None when nothing non-loopback can be determined.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 53))  # no traffic is sent
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


def _phone_wifi() -> int:
    """Home Wi-Fi mode: same-network access, no extra software needed."""
    print("\n--- Home Wi-Fi mode ---")
    print("Fast and simple. Your phone just needs to be on the same")
    print("Wi-Fi network as this computer.\n")
    ip = _lan_ip()
    if ip:
        print(f"  Your computer's LAN IP: {ip}")
    else:
        print("  Could not auto-detect your LAN IP — `python3 cli.py serve")
        print("  --host lan` resolves it for you at startup.")
    print("\n1. On this computer, in the project directory, start the")
    print("   dashboard bound to the LAN:")
    print()
    print("       python3 cli.py serve --host lan")
    print()
    print("2. The server terminal prints an auth token at startup — keep it")
    print("   handy.")
    print("3. On your iPhone (connected to the SAME Wi-Fi), open Safari to:")
    print("       http://<lan-ip>:8765   (e.g. http://192.168.1.42:8765)")
    print("4. Type the token from the server terminal into the phone's")
    print("   login screen. Done — the dashboard is on your phone.")
    print()
    print("Security note: Home Wi-Fi mode has NO HTTPS, so the token travels")
    print("in cleartext on your LAN. Only use it on a network you trust")
    print("(your home Wi-Fi), never on cafe/hotel/public Wi-Fi. For anywhere")
    print("else, use Tailscale mode (re-run: python3 wizard.py --phone).")
    print()
    print("Troubleshooting:")
    print("  * Page won't load on the phone? Confirm the phone is on the")
    print("    same Wi-Fi network, and check this computer's firewall allows")
    print(f"    incoming connections on port {WEBUI_PORT}.")
    print("  * Token rejected? Tokens are case-sensitive; copy it exactly")
    print("    from the server terminal. Still failing? Regenerate it with")
    print("    `python3 cli.py serve --regenerate-token` (or delete the")
    print("    token file at ~/.veto_webui_token).")
    return 0


def _phone_tailscale() -> int:
    """Tailscale mode: encrypted end-to-end, works off-network.

    Tailscale is strictly optional — Home Wi-Fi mode needs no extra
    software at all.
    """
    print("\n--- Tailscale mode (optional) ---")
    print("Encrypted end-to-end (WireGuard) and works off-network — coffee")
    print("shop, cellular, anywhere. Tailscale is never required: Home Wi-Fi")
    print("mode above works fine on a trusted network.\n")

    if shutil.which("tailscale") is None:
        print("Tailscale isn't installed on this computer yet. Install it on")
        print("BOTH sides:")
        print()
        print("  1. This computer:  https://tailscale.com/download")
        print("     (pick your OS, install, then come back here)")
        print("  2. iPhone: install the Tailscale app from the App Store and")
        print("     sign in with the SAME account as the computer.")
        print()
        print("Then bring the computer onto your tailnet:")
        print()
        print("       tailscale up")
        print()
        print("After that, re-run this wizard to finish setup:")
        print()
        print("       python3 wizard.py --phone")
        return 1

    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=10,
        )
        tail_ip = (
            result.stdout.strip().splitlines()[0]
            if result.returncode == 0 and result.stdout.strip()
            else ""
        )
    except (OSError, subprocess.SubprocessError):
        tail_ip = ""
    if not tail_ip:
        print("Tailscale is installed, but I couldn't read a tailnet IPv4")
        print("address. Is `tailscale up` connected? Try:")
        print()
        print("       tailscale up")
        print("       tailscale ip -4")
        print()
        print("Then re-run:  python3 wizard.py --phone")
        return 1

    print(f"  Your tailnet IPv4: {tail_ip}\n")
    print("1. On this computer, start the dashboard bound to the tailnet IP:")
    print()
    print(f"       python3 cli.py serve --host {tail_ip}")
    print()
    print("2. The server terminal prints an auth token at startup — keep it")
    print("   handy.")
    print("3. On your iPhone (Tailscale app installed, signed in with the")
    print("   SAME account, connected): open Safari to:")
    print(f"       http://{tail_ip}:{WEBUI_PORT}")
    print("4. Type the token from the server terminal into the phone's")
    print("   login screen. Done — the dashboard follows you off-network,")
    print("   still encrypted end-to-end.")
    print()
    print("A step-by-step walkthrough is also in the dashboard:")
    print("open the dashboard, then visit the Tailscale guide page.")
    return 0


def phone_access() -> int:
    """Phone-access setup flow: Home Wi-Fi or Tailscale.

    Short-circuits the onboarding wizard — runs instead of it, then
    returns. Mirrors the `--grill` pattern in shape but takes over
    entirely.
    """
    print("\n=== Phone access setup ===\n")
    print("Open the Veto web dashboard on your iPhone:")
    print("  1. Start the server so it listens on the network (not just")
    print("     your own screen).")
    print("  2. Enter the auth token — printed in the server terminal at")
    print("     startup — on the phone's login screen.\n")
    print("Pick a connection mode:")
    print("  1. Home Wi-Fi  — same network, quick, no extra software")
    print("                   (no HTTPS — trusted networks only)")
    print("  2. Tailscale   — encrypted end-to-end, works off-network")
    print("                   (optional; needs the Tailscale app on the iPhone)")
    choice = ask("Choose an option", "1").strip()
    while choice not in {"1", "2"}:
        print("  Please choose one of: 1 / 2")
        choice = ask("Choose an option", "1").strip()
    if choice == "1":
        return _phone_wifi()
    return _phone_tailscale()


def print_summary(profile: dict) -> None:
    """Print a readable summary of the finished profile."""
    print("\n=== Profile summary ===")
    print(f"Name:       {profile['full_name']}")
    print(f"Email:      {profile['email']}")
    print(f"Phone:      {profile['phone']}")
    print(f"Location:   {profile['location']}")
    print(f"LinkedIn:   {profile['linkedin_url'] or '(not set)'}")
    print(f"Website:    {profile['website'] or '(not set)'}")
    print(f"Targets:    {', '.join(profile['target_titles']) or '(none)'}")
    print(f"Locations:  {', '.join(profile['preferred_locations'])}")
    print(f"Remote:     {profile['remote_preference']}")
    print(f"Min salary: {profile['minimum_salary'] or '(not set)'}")
    print(f"Experience: {profile['years_experience']} year(s), "
          f"{len(profile['experience'])} position(s) on file")
    print(f"Education:  {len(profile['education'])} entr(y/ies) on file")
    print(f"Skills:     {', '.join(profile['skills']) or '(none)'}")
    print(f"Work auth:  {profile['work_authorization']}")
    print(f"Industries: {', '.join(profile.get('target_industries', [])) or '(any)'}")
    print(f"Excluded:   {', '.join(profile.get('excluded_companies', [])) or '(none)'}")
    print(f"Job types:  {', '.join(profile.get('job_types', [])) or '(any)'}")
    print(f"Target comp:{profile.get('target_salary') or '(not set)'}  "
          f"Floor: {profile.get('salary_floor') or '(not set)'}")
    print(f"Notice:     {profile.get('notice_period') or '(not set)'}, "
          f"available {profile.get('available_from') or '(not set)'}")
    print(f"Sponsor:    {profile.get('needs_sponsorship', 'no')}, "
          f"clearance: {profile.get('security_clearance', 'none')}")
    print(f"Wins:       {len(profile.get('achievements', []))} quantified "
          f"achievement(s), "
          f"{len(profile.get('career_highlights', []))} highlight(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Veto onboarding wizard: builds your profile."
    )
    parser.add_argument(
        "--grill", action="store_true",
        help="Deep interrogation mode: quantified achievements per role, "
             "career highlights, and years per skill.",
    )
    parser.add_argument(
        "--phone", action="store_true",
        help="Phone-access setup: Home Wi-Fi or Tailscale so the web "
             "dashboard can be opened on a phone. Runs instead of the "
             "onboarding flow.",
    )
    args = parser.parse_args(argv)

    if args.phone:
        return phone_access()

    profile = collect_basics()
    profile = collect_targets(profile)
    profile = collect_compensation(profile)
    profile = collect_logistics(profile)
    if args.grill:
        profile = collect_grill(profile)
    linkedin_data = linkedin_ingestion()
    if linkedin_data:
        profile = merge_profile(profile, linkedin_data)

    seed_preferences(profile)
    maybe_create_watches(profile)
    mode = collect_compliance()
    grill_cfg = collect_grill_channel()

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    with PROFILE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print_summary(profile)
    print(f"\nSaved profile to {PROFILE_PATH}")
    print(f"Compliance mode: {mode} (recorded in compliance.json)")
    grill_state = ("on via " + grill_cfg["grill_channel"]
                   if grill_cfg["grill_on_apply"] else "off")
    print(f"Grilling: {grill_state} (recorded in preferences.json)")
    print("\nNext steps:")
    print("  1. Generate your resume:")
    print("       python3 resume_builder.py")
    print("  2. In the MCP server, use the get_profile / apply_to_job tools")
    print("     — they read this profile automatically, including the")
    print("     name/email/phone/location/LinkedIn/website/cover_letter")
    print("     fields the browser hook needs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
