# Veto Changelog

Plain-English notes on what changed in Veto, most recent first. Written for users,
not engineers. If something isn't listed here, it didn't ship.

## Unreleased (on main, not yet in a numbered release)

These are built, reviewed, and committed to the main codebase. They will be part of
the next versioned release.

- **Mentor matching** — Get matched with mentors. Before you agree to share anything,
  you see an exact preview of what they'd see — nothing personal goes out without
  your explicit consent. Includes safety controls like blocking and quarantine.
- **Extension platform** — Developers can build extensions for Veto. Extensions run
  in a secure sandbox, versions are signed, and banned versions stay banned — they
  can't be reinstalled through rollback.
- **Interview & skills lab** — Practice interviews with feedback, tracking of your
  progress over time, drill recommendations for weak spots, and a map of your skill
  gaps (including soft skills).
- **Application studio** — A workspace for building each application: checks your
  resume against applicant-tracking systems, explains what changed between drafts,
  and keeps a library of your evidence (achievements, numbers, proof).
- **Fit decoder & career graph** — Explains in plain words why you're a good (or
  bad) fit for a job: which skills match, which are missing, and how your past roles
  connect to where you're headed.
- **Connector reliability** — Keeps Veto's connections to job boards, email, and
  calendar working: follow-up messaging, handoffs that survive interruptions, and
  graceful recovery when something drops.
- **Distribution** — Install Veto as an app on your phone or desktop, and back up
  or restore your data.

Still being built: collective intelligence, outcomes & lifecycle, adaptive fit engine,
daily operating loop, P0 craft. Coming next for connector reliability: a human
circuit breaker — every AI-drafted message waits for your review before it goes out,
and the AI earns your trust over time before you can switch the breaker off.

## v0.1.0 — 2026-09-12

First release. Veto searches 8 job boards (Greenhouse, Lever, Ashby, Adzuna, LinkedIn,
Indeed, ZipRecruiter), grills you about the gaps in your resume over chat or WhatsApp,
tailors every application to the role without inventing experience, fills in
application forms but stops before submitting — you click submit — and tracks your
applications, stages, and follow-ups. Runs as an MCP server with a terminal dashboard
and a local web UI you can open on your phone over your home network.
