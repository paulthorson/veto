# Feedback — local capture only, no automated send

**Settled posture:** Directive §5, Rule 2. Veto never transmits user data to
infrastructure the author operates or receives from — not crash reports, not
analytics, not contributions, not error logs, not issues. Automatic filing to
GitHub is dead. There is no acceptance log and no shared bot token; neither
will be built.

**Status:** Approved scope by the operator, 2026-09-14 (chat). Initiative 10
(Distribution) follow-on — it concerns Veto running on user machines.

**2026-09-14 rework:** This document previously specified automatic private
issue filing (auto-filing scrubbed crash/problem reports into the author's
issue tracker, remote dedup, a provisioned bot PAT, and a first-run consent
gate that blocked startup on "telemetry as the price of free"). All of that
is deleted. Feedback is now local capture only; any send is user-initiated.

## The requirement

Users can capture bugs and crash context locally, inspect the exact payload,
and — only if they choose — deliver it themselves through their own channels.
Veto provides no send path to the author.

## What to build

1. **"Report a problem" in-app.** One-tap from the dashboard and the web UI /
   PWA. Stages a LOCAL draft file; captures: what the user was doing, their
   description, app version, platform, recent log tail. Nothing is transmitted.
2. **Automatic crash capture.** Unhandled exceptions are caught, fingerprinted
   (crash signature from stack frames, not messages), and stored locally only.
3. **Local drafts.** Drafts are written to `~/.veto/feedback/drafts/` as
   `<timestamp>-<crash-signature-or-slug>.json` (JSON envelope) plus a
   human-readable `.md` rendering. Draft directory `0700`, files `0600`.
   Drafts never leave the machine through Veto.
4. **Exact-payload preview.** The draft file IS the payload. The in-app preview
   renders the file byte-for-byte — no summarizing, no truncation that hides
   content. What the user reads is exactly what they would be handing to
   anyone.
5. **User-initiated send only.** Veto has no send mechanism for feedback: no
   `gh` invocation, no `api.github.com` calls, no stored filing credentials,
   no auto-file, no remote dedup/comment/reopen. To share a report, the user
   opens the draft in their editor or browser and delivers it themselves
   (e.g. pastes it into a GitHub issue they open under their own account,
   attaches it to an email they write). Veto is not a party to that action.
6. **No remote state.** Because nothing is filed remotely, there is no remote
   dedup, no issue comments, no reopening closed issues, and no rate limit
   against a remote tracker. Local dedup (same crash signature grouping
   drafts in the drafts directory) is permitted as a local convenience only.

## What this explicitly does NOT build

- No automatic filing to GitHub (or anywhere else) on the user's behalf.
- No "required condition of the free license" framing. Reporting is optional;
  Veto starts and runs fully regardless of any reporting choice. There is no
  first-run consent gate and no gate may block startup.
- No acceptance log (no record of "the user agreed to send reports").
- No shared bot token, no fine-grained PAT provisioned by the author, no
  author-controlled filing identity. Veto stores no credentials for filing
  feedback.
- No usage analytics tied to feedback. (Consent-gated local analytics are a
  separate i12 concern; they are local and off by default.)
- No PII-scrub pipeline dependency: the Initiative 08 scrub/quarantine
  pipeline was removed (directive §4, Rule 2). The exact-payload preview
  serves the transparency role — the user sees every byte before deciding
  whether to share it, so there is no "scrubbed on their behalf" step.

## Privacy properties (asserted, testable)

- No outbound network connection is opened by the feedback path. Tests must
  assert the feedback module imports no HTTP client and performs no socket
  I/O (the capability-report runtime observation, directive §8, is the
  final word).
- Drafts stay under `~/.veto/feedback/` with restrictive permissions;
  nothing is written to world-readable locations.
- Crash fingerprints derive from stack frames, not messages, so report
  grouping cannot smuggle user text into a filename or signature.

## Governance

- Full framework compliance (ag_review.py, blind review, append-only verdicts).
- Security-domain review is the critical gate. With no send path, adversarial
  review targets: local file permissions, draft filename injection (signature
  → filename must be sanitized), and log-tail content handling (the preview
  must be byte-accurate, and drafts must not be readable by other local users).
