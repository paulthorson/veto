"""Extension entrypoint: Role Radar Digest (reference extension).

Read-only daily digest of watched roles and application stages. It drafts
a local summary and queues a notification *proposal* — it never sends,
submits, or messages anyone. Built with the Initiative 11 scaffold to
prove the scaffold's value.

The host executes this module with exactly one object in scope: ``ctx``
(the ExtensionContext). Capabilities come from manifest.json — code that
reaches beyond them is denied at runtime and rejected at install.
"""


def _snapshot(ctx):
    """Collect the digest inputs through the broker (scope-checked)."""
    watches = ctx.data("watches:read") or []
    applications = ctx.data("applications:read") or []
    jobs = ctx.data("jobs:read") or []
    return {"watches": watches, "applications": applications, "jobs": jobs}


def _render_digest(snapshot):
    lines = ["# Role Radar Digest", ""]
    watches = snapshot["watches"]
    applications = snapshot["applications"]
    lines.append(f"Watched roles: {len(watches)}")
    for w in watches[:10]:
        title = w.get("title", "?") if isinstance(w, dict) else str(w)
        lines.append(f"- {title}")
    lines.append("")
    lines.append(f"Applications tracked: {len(applications)}")
    by_stage: dict[str, int] = {}
    for a in applications:
        stage = a.get("stage", "unknown") if isinstance(a, dict) else "unknown"
        by_stage[stage] = by_stage.get(stage, 0) + 1
    for stage, count in sorted(by_stage.items()):
        lines.append(f"- {stage}: {count}")
    lines.append("")
    lines.append(
        "_Draft only: review in the host before acting. "
        "This extension cannot submit applications or send messages._")
    return "\n".join(lines)


def _role_radar_digest_read(ctx, **params):
    """Read-only snapshot of watches and applications (no side effects)."""
    snapshot = _snapshot(ctx)
    return {"watches": len(snapshot["watches"]),
            "applications": len(snapshot["applications"])}


def _role_radar_digest_draft(ctx, **params):
    """Draft the digest as a local artifact (never sent anywhere)."""
    digest = _render_digest(_snapshot(ctx))
    path = ctx.draft_artifact("role-radar-digest", digest)
    return {"draft": path, "note": "local draft; nothing was sent"}


def _role_radar_digest_notify(ctx, **params):
    """Queue a notification *proposal* — the host shows it; the user acts."""
    snapshot = _snapshot(ctx)
    title = (f"Role Radar: {len(snapshot['watches'])} watched roles, "
             f"{len(snapshot['applications'])} applications")
    nid = ctx.queue_notification(
        title,
        "Your digest draft is ready for review. Nothing was sent.")
    return {"queued_notification": nid,
            "note": "proposal only; the host notification center owns sending"}


ACTIONS = {
    'role-radar-digest-read': _role_radar_digest_read,
    'role-radar-digest-draft': _role_radar_digest_draft,
    'role-radar-digest-notify': _role_radar_digest_notify,
}
