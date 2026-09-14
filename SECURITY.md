# Security policy

## Reporting a vulnerability

If you find a security problem in Veto, please report it privately
rather than opening a public issue.

**Contact:** security@agenticgovernance.app

Please include: what you found, the version or commit you were on,
and steps to reproduce. Don't publish exploit details until there
has been a chance to fix the problem.

## What to expect

Veto is an unpaid personal project. Reports are read as time allows —
there is no guaranteed response time and no guaranteed fix timeline.

## Your credentials are your responsibility

Veto keeps tokens and credentials only on your own machine, in files
only you can read (see `docs/capability-report.md` §6). Anything that
leaves your machine — email sync, a model API call, a notification you
configured — goes through connections you set up with your own
credentials. Keep your machine secure, rotate any key you suspect is
exposed, and never paste secrets into issues, pull requests, or chat
logs.
