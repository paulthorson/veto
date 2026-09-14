"""Extension entrypoint: Apply Pilot (non-vacuous policy-kit fixture).

Unlike the minimal role-radar-digest fixture, this extension declares a
confirm-required action, a network destination, and profile:read — so the
policy suite exercises the confirmation gate, the rate limiter, the
circuit breaker, and PII redaction for real instead of passing vacuously.

Test fixture only. Not for distribution.
"""


def _lookup(ctx, **params):
    """Read-only lookup: profile name + job API status."""
    profile = ctx.data("profile:read")
    resp = ctx.http.get("https://api.example.com/jobs")
    return {"profile_name": profile.get("name"),
            "api_status": resp.status}


def _send_application(ctx, **params):
    """Confirm-required submit. Only runs with a host-minted token."""
    return {"sent": True, "job_id": params.get("job_id")}


ACTIONS = {
    "lookup": _lookup,
    "send-application": _send_application,
}
