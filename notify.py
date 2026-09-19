#!/usr/bin/env python3
"""Outbound notifications for the Veto MCP server.

``send(title, body, channel="auto")`` always appends the notification
to a local JSONL log (``notifications.jsonl`` next to this project, one
JSON object per line). Remote delivery is **off by default** and must be
enabled explicitly by the user — with no configuration, this module
never POSTs anywhere:

* ``ntfy`` — POSTs to ``https://ntfy.sh/<topic>`` (free push
  notifications to your phone via the ntfy app), title in the
  ``Title`` header. Enabled only via ``setup_ntfy()`` (or the
  ``notify setup-ntfy`` CLI / ``notification_setup_ntfy`` MCP tool),
  which generates a high-entropy random topic. Topics are never
  user-chosen: a guessable topic exposes the user's job search to
  anyone who guesses it, because ntfy.sh topics are public URLs with
  no access control. A ``NTFY_TOPIC`` env var is honored only as a
  migration path, and only if it passes the entropy gate.
* ``webhook`` — POSTs ``{"title", "body", "ts"}`` as JSON to the URL in
  ``JOB_MCP_WEBHOOK``. Enabled only when ``"webhook"`` is in the
  user's ``channels`` preference.

Delivery is stdlib-only (``urllib``), best-effort with a 5s timeout,
and ``send`` never raises: a failing or rejected sink is simply left
out of ``delivered_via`` (rejections are reported in
``sink_warnings``).

NOTE on .gitignore: this module deliberately does not edit it. The
notification log can contain job titles and company names, so add
``notifications.jsonl`` to ``.gitignore`` if you want it ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import urllib.request
from datetime import datetime, time as _time, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
NOTIFY_LOG = BASE_DIR / "notifications.jsonl"

#: User notification preferences (channel, quiet hours, digest cadence,
#: per-event opt-in). Created on first ``set_pref``; missing file means
#: defaults.
NOTIFY_PREFS = BASE_DIR / "notify_prefs.json"

#: Pending digest items queued during quiet hours or under a non-immediate
#: digest cadence.
DIGEST_FILE = BASE_DIR / "notify_digest.json"

_NTFY_BASE = "https://ntfy.sh"
_TIMEOUT_SECONDS = 5

#: Catalog of notifiable events. Every producer that calls ``send(..., event=...)``
#: must use one of these keys so per-event opt-in stays meaningful.
EVENT_CATALOG: dict[str, str] = {
    "reply_received": "Recruiter reply detected",
    "stage_proposed": "Reply-radar stage proposal",
    "followup_due": "Follow-up due reminder",
    "outcome_stale": "Stale application / missing outcome reminder",
    "provider_down": "Provider health alert (budget, CAPTCHA, outage)",
    "brief_ready": "Top-five daily brief ready",
    "digest": "Aggregated digest delivery",
}

#: Push channels. ``"log"`` is the local JSONL log (always written);
#: ``"ntfy"`` / ``"webhook"`` are the configured push sinks.
CHANNELS = ("log", "ntfy", "webhook")

_DIGEST_CADENCES = ("immediate", "daily", "weekly")


def default_prefs() -> dict[str, Any]:
    """Factory defaults.

    Remote sinks default OFF: ``channels`` is ``["log"]`` only, so with
    no user configuration nothing is ever POSTed anywhere. Quiet hours
    default to *off* (``None``): the control ships, the user enables
    it. This also keeps ``send()`` time-deterministic for callers
    that do not pass prefs.
    """
    return {
        "channels": ["log"],
        "quiet_hours": None,  # or {"start": "22:00", "end": "07:00"}
        "digest_cadence": "immediate",
        "events": {name: True for name in EVENT_CATALOG},
        "ntfy_topic": None,  # set by setup_ntfy(); never user-chosen
    }


def load_prefs(path: Path | None = None) -> dict[str, Any]:
    """Read prefs, merging over defaults so new keys never break old files."""
    prefs = default_prefs()
    try:
        raw = json.loads(Path(path or NOTIFY_PREFS).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return prefs
    if not isinstance(raw, dict):
        return prefs
    for key, value in raw.items():
        if key == "events" and isinstance(value, dict):
            merged = dict(prefs["events"])
            merged.update({k: bool(v) for k, v in value.items() if k in EVENT_CATALOG})
            prefs["events"] = merged
        elif key in prefs:
            prefs[key] = value
    # Sanitize: unknown channels / cadences fall back to defaults.
    prefs["channels"] = [c for c in prefs.get("channels", []) if c in CHANNELS] or ["log"]
    if prefs.get("digest_cadence") not in _DIGEST_CADENCES:
        prefs["digest_cadence"] = "immediate"
    qh = prefs.get("quiet_hours")
    if qh is not None and not _valid_quiet_hours(qh):
        prefs["quiet_hours"] = None
    return prefs


def save_prefs(prefs: dict[str, Any], path: Path | None = None) -> None:
    Path(path or NOTIFY_PREFS).write_text(
        json.dumps(prefs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _valid_quiet_hours(qh: Any) -> bool:
    if not isinstance(qh, dict):
        return False
    try:
        for key in ("start", "end"):
            hh, mm = str(qh[key]).split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                return False
        return True
    except (KeyError, ValueError, AttributeError):
        return False


def set_pref(key: str, value: Any, path: Path | None = None) -> dict[str, Any]:
    """Set one preference; ``key`` is ``channels`` / ``quiet_hours`` /
    ``digest_cadence`` / ``event:<name>``. Raises ``ValueError`` on bad input.
    """
    prefs = load_prefs(path)
    if key == "channels":
        channels = [c.strip() for c in str(value).split(",") if c.strip() in CHANNELS]
        if not channels:
            raise ValueError(f"channels must include at least one of {CHANNELS}")
        prefs["channels"] = channels
    elif key == "quiet_hours":
        if str(value).strip().lower() in ("off", "none", ""):
            prefs["quiet_hours"] = None
        else:
            try:
                start_s, end_s = str(value).split("-", 1)
                qh = {"start": start_s.strip(), "end": end_s.strip()}
            except ValueError:
                raise ValueError(
                    'quiet_hours must look like "22:00-07:00" or "off"'
                ) from None
            if not _valid_quiet_hours(qh):
                raise ValueError('quiet_hours must look like "22:00-07:00" or "off"')
            prefs["quiet_hours"] = qh
    elif key == "digest_cadence":
        if str(value) not in _DIGEST_CADENCES:
            raise ValueError(f"digest_cadence must be one of {_DIGEST_CADENCES}")
        prefs["digest_cadence"] = str(value)
    elif key.startswith("event:"):
        name = key.split(":", 1)[1]
        if name not in EVENT_CATALOG:
            raise ValueError(f"unknown event {name!r}; known: {sorted(EVENT_CATALOG)}")
        prefs["events"][name] = str(value).strip().lower() not in (
            "0", "false", "no", "off"
        )
    else:
        raise ValueError(
            "key must be channels, quiet_hours, digest_cadence, or event:<name>"
        )
    save_prefs(prefs, path)
    return prefs


def _local_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def in_quiet_hours(
    now: datetime | None = None, prefs: dict[str, Any] | None = None
) -> bool:
    """True when ``now`` (local time) falls inside the quiet-hours window.

    Handles windows that cross midnight (e.g. 22:00-07:00).
    """
    qh = (prefs or load_prefs()).get("quiet_hours")
    if not _valid_quiet_hours(qh):
        return False
    local = _local_now(now)
    start = _time(*[int(x) for x in qh["start"].split(":")])
    end = _time(*[int(x) for x in qh["end"].split(":")])
    t = local.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def event_enabled(event: str | None, prefs: dict[str, Any] | None = None) -> bool:
    """Per-event opt-in, fail-closed.

    Only catalogued events the user has explicitly enabled may be
    delivered. Unknown/None events are DISABLED: an event key a
    producer invented (or a typo of a real one) must never reach a
    remote sink. The local log always records everything regardless.
    """
    if event not in EVENT_CATALOG:
        return False
    events = (prefs or load_prefs()).get("events", {})
    return bool(events.get(event, False))


# ---------------------------------------------------------------------------
# ntfy sink setup — explicit opt-in, generated topics only
# ---------------------------------------------------------------------------

#: Plain-language privacy warning shown every time ntfy is enabled.
_NTFY_SETUP_WARNING = (
    "Privacy warning: ntfy.sh topics are PUBLIC. Anyone who knows or "
    "guesses your topic name can read every notification sent to it - "
    "no account, no password, no access control. Your topic name was "
    "randomly generated (43 characters), which makes guessing it "
    "practically impossible, but it is still readable by anyone you "
    "share it with. Do not rename it to something memorable: a "
    "guessable topic exposes your job search to anyone who guesses it."
)

#: Bytes of entropy for generated topics (secrets.token_urlsafe(32)
#: yields 43 characters from a 64-symbol alphabet).
_NTFY_TOPIC_BYTES = 32


def generate_ntfy_topic() -> str:
    """High-entropy random ntfy topic (``secrets.token_urlsafe(32)``)."""
    return secrets.token_urlsafe(_NTFY_TOPIC_BYTES)


def _topic_looks_generated(topic: str) -> bool:
    """Entropy gate for any topic this module did not generate.

    A generated topic is >= 32 characters drawn from a mixed alphabet.
    Anything shorter or single-alphabet (e.g. "myjobsearch") is
    guessable and fails closed.
    """
    t = (topic or "").strip()
    if len(t) < 32:
        return False
    classes = sum(
        [
            any(c.islower() for c in t),
            any(c.isupper() for c in t),
            any(c.isdigit() for c in t),
            any(c in "-_" for c in t),
        ]
    )
    return classes >= 2


def setup_ntfy(topic: str | None = None, path: Path | None = None) -> dict:
    """Enable the ntfy push sink with a generated high-entropy topic.

    The topic is always generated (never chosen): user-supplied topics
    are REJECTED with ``ValueError``. Rationale: any topic a human
    picks risks being guessable, and ntfy.sh topics are publicly
    readable by anyone who knows the name — there is no access
    control, so a guessable topic silently exposes the user's job
    search. Replacing the value silently would be worse: the user's
    ntfy app would stay subscribed to the name they typed while
    notifications go elsewhere. Rejection fails loud and forces the
    secure path.

    Returns ``{"ok": True, "topic": ..., "warning": ...}``; the caller
    must surface ``warning`` to the user verbatim (the CLI does).
    """
    if topic is not None:
        raise ValueError(
            "ntfy topics are generated, not chosen: a human-picked topic "
            "risks being guessable, and ntfy.sh topics are publicly "
            "readable by anyone who knows the name. Call setup_ntfy() "
            "without a topic."
        )
    new_topic = generate_ntfy_topic()
    prefs = load_prefs(path)
    prefs["ntfy_topic"] = new_topic
    channels = list(prefs.get("channels", ["log"]))
    if "ntfy" not in channels:
        channels.append("ntfy")
    prefs["channels"] = channels
    save_prefs(prefs, path)
    return {"ok": True, "topic": new_topic, "warning": _NTFY_SETUP_WARNING}


def _ntfy_topic(prefs: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve the ntfy topic for delivery.

    Preference order: the generated topic stored by ``setup_ntfy()``;
    then the ``NTFY_TOPIC`` env var (migration path) *only if it passes
    the entropy gate*. Returns ``(topic, warning)``; a non-None warning
    means the sink is disabled and the user must run setup.
    """
    stored = (prefs.get("ntfy_topic") or "").strip()
    if stored:
        if _topic_looks_generated(stored):
            return stored, None
        return None, (
            "the stored ntfy topic failed the entropy check; run "
            "'notify setup-ntfy' to generate a new one"
        )
    env_topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not env_topic:
        return None, None
    if _topic_looks_generated(env_topic):
        return env_topic, None
    return None, (
        "NTFY_TOPIC is set but looks guessable; ntfy delivery is disabled "
        "until you run 'notify setup-ntfy' (topics are generated, never "
        "chosen)"
    )


# ---------------------------------------------------------------------------
# Digest queue
# ---------------------------------------------------------------------------


def _load_digest(path: Path | None = None) -> list[dict[str, Any]]:
    try:
        raw = json.loads(Path(path or DIGEST_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def _save_digest(items: list[dict[str, Any]], path: Path | None = None) -> None:
    Path(path or DIGEST_FILE).write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def queue_for_digest(
    title: str,
    body: str,
    event: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one item to the digest buffer. Returns the queue depth."""
    items = _load_digest(path)
    items.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "title": str(title),
            "body": str(body),
            "event": event,
        }
    )
    _save_digest(items, path)
    return {"queued": True, "digest_depth": len(items)}


def flush_digest(
    prefs: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deliver queued digest items as one notification.

    Respects quiet hours unless ``force=True`` and respects the ``digest``
    event opt-in. The local log records the digest either way.
    """
    prefs = prefs or load_prefs()
    items = _load_digest()
    if not items:
        return {"ok": True, "delivered_via": [], "items": 0}
    if in_quiet_hours(now, prefs) and not force:
        return {
            "ok": True,
            "delivered_via": ["log"],
            "items": len(items),
            "deferred": "quiet_hours",
        }
    lines = [f"- {it['title']}" + (f": {it['body']}" if it.get("body") else "")
             for it in items]
    title = f"Veto digest ({len(items)} item{'s' if len(items) != 1 else ''})"
    body = "\n".join(lines)
    _save_digest([])
    _append_log(title, body, "auto", "digest")
    if not event_enabled("digest", prefs):
        return {"ok": True, "delivered_via": ["log"], "items": len(items),
                "suppressed": "event_opted_out"}
    result = _deliver(title, body, "auto", prefs)
    result["items"] = len(items)
    if "log" not in result["delivered_via"]:
        result["delivered_via"] = ["log"] + result["delivered_via"]
    return result


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _append_log(title: str, body: str, channel: str, event: str | None) -> bool:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "body": body,
        "channel": channel,
        "event": event,
    }
    try:
        with NOTIFY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _deliver(
    title: str, body: str, channel: str, prefs: dict[str, Any]
) -> dict[str, Any]:
    """Push through the explicitly enabled sinks. Never raises.

    A sink fires only when its channel is in the user's ``channels``
    preference AND it is configured (ntfy: generated or entropy-gated
    topic; webhook: ``JOB_MCP_WEBHOOK`` URL). Guessable ntfy topics are
    rejected and reported in ``sink_warnings``.
    """
    delivered: list[str] = []
    warnings: list[str] = []
    allowed = set(prefs.get("channels", ["log"]))
    if channel in ("auto", "ntfy") and "ntfy" in allowed:
        topic, warning = _ntfy_topic(prefs)
        if warning:
            warnings.append(warning)
        if topic and _post_ntfy(topic, title, body):
            delivered.append("ntfy")
    if channel in ("auto", "webhook") and "webhook" in allowed:
        webhook = os.environ.get("JOB_MCP_WEBHOOK", "").strip()
        if webhook and _post_webhook(webhook, title, body):
            delivered.append("webhook")
    result: dict[str, Any] = {"ok": True, "delivered_via": delivered}
    if warnings:
        result["sink_warnings"] = warnings
    return result


def _post_ntfy(topic: str, title: str, body: str) -> bool:
    """POST to ntfy.sh. Returns True on 2xx; never raises."""
    try:
        req = urllib.request.Request(
            f"{_NTFY_BASE}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _post_webhook(url: str, title: str, body: str) -> bool:
    """POST JSON to a generic webhook. Returns True on 2xx; never raises."""
    try:
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def send(
    title: str,
    body: str,
    channel: str = "auto",
    event: str | None = None,
    prefs: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict:
    """Send a notification. Never raises.

    Always writes to the local JSONL log; additionally delivers via ntfy
    and/or webhook ONLY when the user has explicitly enabled them:

    * remote sinks default OFF — with default prefs nothing is POSTed;
    * per-event opt-in: unknown events are disabled (fail-closed); a
      known ``event`` the user disabled is logged only;
    * channel allowlist: sinks outside ``prefs["channels"]`` are skipped;
    * ntfy requires a generated high-entropy topic (see
      ``setup_ntfy``); guessable topics are rejected, never posted to;
    * quiet hours / non-immediate digest cadence: the notification is queued
      into the digest buffer instead of being pushed.

    Returns ``{"ok": True, "delivered_via": [...]}`` where the list is a
    subset of ``["log", "ntfy", "webhook"]``; a queued notification reports
    ``"queued": True`` and a suppressed one reports ``"suppressed"`` with
    the reason.
    """
    title, body = str(title), str(body)
    prefs = prefs or load_prefs()
    logged = _append_log(title, body, channel, event)
    delivered: list[str] = ["log"] if logged else []

    if event is not None and not event_enabled(event, prefs):
        return {"ok": True, "delivered_via": delivered,
                "suppressed": "event_opted_out"}

    allowed = set(prefs.get("channels", CHANNELS))
    if channel != "auto" and channel not in allowed:
        return {"ok": True, "delivered_via": delivered,
                "suppressed": "channel_disabled"}

    if prefs.get("digest_cadence", "immediate") != "immediate" or in_quiet_hours(
        now, prefs
    ):
        queued = queue_for_digest(title, body, event)
        return {"ok": True, "delivered_via": delivered,
                "queued": True, **queued}

    result = _deliver(title, body, channel, prefs)
    merged: dict = {
        "ok": True,
        "delivered_via": delivered + result["delivered_via"],
    }
    if result.get("sink_warnings"):
        merged["sink_warnings"] = result["sink_warnings"]
    return merged


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register notification tools on the MCP server."""

    @mcp.tool()
    def notify_send(title: str, body: str, event: str | None = None) -> dict:
        """Send a notification honoring quiet hours, digest cadence, and
        per-event opt-in. ``event`` should be one of the catalog keys
        (see notification_prefs); unlisted events are logged locally but
        never delivered to a remote sink."""
        return send(title, body, event=event)

    @mcp.tool()
    def notification_prefs() -> dict:
        """Show notification preferences: channels, quiet hours, digest
        cadence, per-event opt-in flags, and whether ntfy is configured
        with a generated topic."""
        prefs = load_prefs()
        return {
            "channels": prefs["channels"],
            "quiet_hours": prefs["quiet_hours"],
            "digest_cadence": prefs["digest_cadence"],
            "events": prefs["events"],
            "event_catalog": EVENT_CATALOG,
            "ntfy_configured": bool(prefs.get("ntfy_topic")),
            "in_quiet_hours_now": in_quiet_hours(prefs=prefs),
        }

    @mcp.tool()
    def notification_setup_ntfy() -> dict:
        """Enable ntfy push notifications. Generates a random
        high-entropy topic (topics are never user-chosen — a guessable
        topic would expose the user's job search, since ntfy.sh topics
        are publicly readable). Returns the topic to subscribe to in the
        ntfy app plus a plain-language privacy warning to show the
        user."""
        return setup_ntfy()

    @mcp.tool()
    def notification_digest(action: str = "status", force: bool = False) -> dict:
        """Digest control: ``"status"`` shows the queued depth, ``"flush"``
        delivers queued items now (use after quiet hours end). ``force``
        flushes even during quiet hours."""
        if action == "flush":
            return flush_digest(force=force)
        return {
            "queued": len(_load_digest()),
            "cadence": load_prefs()["digest_cadence"],
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_result(result: Any, as_json: bool) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _cli_send(args: Any) -> int:
    result = send(args.title, args.body, channel=args.channel, event=args.event)
    _print_result(result, args.json)
    return 0


def _cli_setup_ntfy(args: Any) -> int:
    """Enable ntfy with a generated topic; always shows the privacy
    warning in plain language. A user-supplied --topic is rejected."""
    try:
        result = setup_ntfy(topic=args.topic)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    if args.json:
        _print_result(result, True)
    else:
        print(result["warning"])
        print()
        print("Your ntfy topic (subscribe to this exact name in the ntfy app):")
        print("  " + result["topic"])
    return 0


def _cli_prefs(args: Any) -> int:
    prefs = load_prefs()
    if args.json:
        _print_result(
            {
                "channels": prefs["channels"],
                "quiet_hours": prefs["quiet_hours"],
                "digest_cadence": prefs["digest_cadence"],
                "events": prefs["events"],
                "in_quiet_hours_now": in_quiet_hours(prefs=prefs),
            },
            True,
        )
    else:
        print("Channels:      " + ", ".join(prefs["channels"]))
        qh = prefs["quiet_hours"]
        print(
            "Quiet hours:   "
            + (f"{qh['start']}-{qh['end']} (local)" if qh else "off")
        )
        print("Digest:        " + prefs["digest_cadence"])
        print("Queued digest: " + str(len(_load_digest())))
        print("Per-event opt-in:")
        for name in sorted(EVENT_CATALOG):
            flag = "on " if prefs["events"].get(name) else "off"
            print(f"  [{flag}] {name}: {EVENT_CATALOG[name]}")
    return 0


def _cli_prefs_set(args: Any) -> int:
    try:
        prefs = set_pref(args.key, args.value)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    _print_result(prefs, args.json)
    return 0


def _cli_digest(args: Any) -> int:
    if args.flush:
        result = flush_digest(force=args.force)
    else:
        result = {
            "queued": len(_load_digest()),
            "cadence": load_prefs()["digest_cadence"],
        }
    _print_result(result, args.json)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "notify", help="Send notifications and manage notification controls."
    )
    sub = p.add_subparsers(dest="notify_cmd", required=True)

    ps = sub.add_parser("send", help="Send a notification.")
    ps.add_argument("--title", required=True)
    ps.add_argument("--body", required=True)
    ps.add_argument("--json", action="store_true",
                    help="Machine-readable JSON output.")
    ps.add_argument(
        "--channel", default="auto", choices=("auto", "log", "ntfy", "webhook")
    )
    ps.add_argument(
        "--event", default=None, choices=sorted(EVENT_CATALOG),
        help="Catalog event for per-event opt-in.",
    )
    ps.set_defaults(func=_cli_send)

    pp = sub.add_parser("prefs", help="Show notification preferences.")
    pp.add_argument("--json", action="store_true",
                    help="Machine-readable JSON output.")
    pp.set_defaults(func=_cli_prefs)

    pset = sub.add_parser("prefs-set", help="Set a notification preference.")
    pset.add_argument(
        "key", help="channels | quiet_hours | digest_cadence | event:<name>"
    )
    pset.add_argument(
        "value",
        help='e.g. "log,ntfy", "22:00-07:00"/"off", '
        '"immediate|daily|weekly", "off"',
    )
    pset.add_argument("--json", action="store_true",
                      help="Machine-readable JSON output.")
    pset.set_defaults(func=_cli_prefs_set)

    psetup = sub.add_parser(
        "setup-ntfy",
        help="Enable ntfy push with a generated high-entropy topic.",
    )
    psetup.add_argument(
        "--topic",
        default=None,
        help="REJECTED: ntfy topics are generated, never chosen.",
    )
    psetup.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output.")
    psetup.set_defaults(func=_cli_setup_ntfy)

    pd = sub.add_parser("digest", help="Inspect or flush the digest queue.")
    pd.add_argument("--json", action="store_true",
                    help="Machine-readable JSON output.")
    pd.add_argument("--flush", action="store_true",
                    help="Deliver queued digest items now.")
    pd.add_argument("--force", action="store_true",
                    help="Flush even during quiet hours.")
    pd.set_defaults(func=_cli_digest)

    return {"notify": lambda args: args.func(args)}
