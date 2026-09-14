# Veto public tools — methodology

_How the Initiative 12 public demos work, what they can and cannot tell you, and what data they touch._

## What the tools are

Four interactive demos. Each runs entirely on your device, needs no account, and never asks for your resume:

| Tool | What it does | Engine |
|---|---|---|
| JD decoder | Reads a pasted job description for transparency signals (salary range, equity concreteness, transparency score 0–100), overwork language, vagueness, growth signals, green flags | Deterministic text analysis (patterns + counting). No model, no API, no network. |
| Fit explainer | Explains a five-factor fit score against a compact typed profile (skills you list, seniority, locations, salary expectations; recency is read from the posting itself) | Same five-factor scorer as the local app, minus your private evidence library |
| Application-risk check | Lists blockers, warnings, and next steps before you apply | Fit scorer + JD decoder; proposal-only |
| Role comparison | Compares up to 4 roles across fit, risk, compensation, location, readiness | Fit scorer + JD decoder per role |

The five fit factors are skills (50), seniority (15), salary (15), location (15), and recency (5) — fixed weights, identical to the local app's scorer.

## What they cannot do

- They only see the text you paste. They know nothing about the company beyond that text.
- They cannot predict hiring outcomes. A "strong" decode means the posting is transparent and concrete — not that you will get an interview.
- Compensation is quoted from the posting text only. Absent data is reported as absent, never filled in with market estimates.
- The fit explainer uses a compact typed profile, not your resume. Real scoring links every claim to evidence in your local profile with provenance.

## Data handling

Everything in this section describes a local-only architecture. There is no
server, no upload, no sync: the telemetry store is a file on your device
(`~/.local/share/veto/i12_telemetry.json`), and the quarantine is a directory
next to it with `0700` permissions. "Telemetry" here means local usage
counters, not data sent anywhere. Input text never leaves your device,
including when you opt in to analytics — there is nothing to send it to.

- Analytics are off by default. If you opt in, events carry metadata tokens
  only (tool name, surface, session id) — never resume text, job-description
  text, names, or contact details.
- The session id is an opaque token supplied by the caller (the local app);
  the telemetry store neither generates nor rotates it. Within the local
  store it is a persistent pseudonymous correlator, not an anonymous one —
  but it is a compact alphanumeric token that never leaves your device.
- A runtime tripwire detects violations of this: if any content field ever
  appears in a telemetry payload, analytics shut off immediately. The
  offending event is preserved in the local access-controlled quarantine
  (directory 0700, files 0600 — OS file permissions only, no encryption;
  never auto-deleted) so an independent reviewer can inspect exactly what
  tripped the wire; the live event store is cleared to halt further
  collection; and a metadata-only incident record — incident_id, at,
  trigger, finding_kinds, quarantine_file or quarantine_error, status,
  never the offending content — is appended to the audit log.
  Re-enable requires, in order: a clearing entry naming the contracted
  security/privacy specialist (a contracted role, currently unfilled —
  no specialist has been retained), verification by an independent reviewer (a second
  person, distinct from the clearing specialist — the code rejects
  self-verification) against the quarantined payload, and no veto in force.
  The roadmap owner retains veto.

## Honest limitations

- Pattern-based decoding misses nuance a human reader catches (sarcasm, implied expectations, culture).
- "No salary range found" means the posting didn't state one — not that the role is underpaid.
- Overwork-language flags are quotes with a plain-English read, not accusations about the employer.
- The demos are screening aids, not career advice. For consequential decisions, use the full local app with your real evidence — or better, talk to people who work there.
