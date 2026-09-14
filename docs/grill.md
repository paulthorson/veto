# Per-application grilling

`grill.py` grills you once **per application** — sharp, targeted questions
generated from the gaps between a job description and your saved profile,
asked before anything is tailored or submitted. Answers flow into the
tailoring step, so your resume and cover letter use your real answers
instead of invented facts.

## How it works

1. `grill_start(job_id)` — extracts keywords from the job description
   (requirements sections, "experience with X" phrases), compares them
   against your profile, and generates up to 5 questions:
   - **quantify** — you list a skill but without numbers ("largest scale
     you've used it at?")
   - **gap** — a required keyword with no profile match ("any experience
     with X? totally fine to say none")
   - **logistics** — hybrid/travel/clearance/relocation/location signals
     your profile doesn't cover
   - **motivation** — one "why this role at this company?" for the cover
     letter (always included, always last)
2. The questions go out over your preferred channel
   (`grill_channel` in `preferences.json`).
3. `grill_answer` / `grill_ingest_reply` records answers;
   `grill_status` shows progress; `get_qa_pairs` hands answered Q&A to
   the tailoring step.

Questions never assert facts that aren't in your profile or the posting.
Nothing is submitted — grilling is strictly pre-application.

## Channels

Set `grill_channel` in `preferences.json`:

| Value      | Behavior |
|------------|----------|
| `whatsapp` (default) | Questions are formatted for the WhatsApp side chat. Only channel that can reach you proactively. |
| `gmail`    | Questions go out as an email to your own address; numbered replies are ingested back. |
| `chat`     | Questions are asked in the current chat. |
| `off`      | Skip grilling. |

`grill_on_apply: false` also disables grilling in apply flows.

### WhatsApp (default)

The only proactive channel. You need the Muse WhatsApp side chat linked
(1:1 user↔Muse conversation). Link it once here:

**https://agent.meta.ai/connect/channel?service=whatsapp**

**Routing rule:** a channel message can only be sent from that channel's
own side chat. From the main chat, proactive grilling requires a
scheduled task created *inside* the WhatsApp side chat that calls
`grill_status` / `grill_answer` here. `outbound_for_channel()` returns
`deliverable_in: "whatsapp_side_chat"` to make this explicit.

iMessage cannot be auto-sent (paired iPhones only expose a draft that
still needs you to hit send), Discord has no integration, and SMS needs
a paired Android with send capability — so WhatsApp is the real option.

### Gmail

`grill_channel: "gmail"` sends the questions as an email to your own
address (from the saved profile) with subject:

```
Grill: <title> at <company> — N questions before I tailor your application [grill:<session_id>]
```

Reply with numbered answers (`1. ...`, `Q2: ...` — multiline answers are
fine). `ingest_reply` parses the body, strips quoted history and
signatures, and records each answer.

**Approval rule:** sending the *first* grill email needs your explicit
go-ahead on the exact text. Standing approval covers later ones.

**Reply watching:** a scheduled check (or the `email_sync` module)
watches for replies by matching the `[grill:<session_id>]` subject tag,
then calls `grill_ingest_reply(job_id, body_text)`.

## Cron-from-WhatsApp pattern (proactive grilling)

To have grilling questions arrive without you asking first:

1. Open the **WhatsApp side chat**.
2. Create a scheduled task *in that chat* that runs `grill_start` for a
   queued job and sends you the resulting message.
3. Replies in that chat are recorded with `grill_answer`.

A cron created in the main chat cannot deliver into the WhatsApp side
chat — the schedule must live in the WhatsApp chat itself.

## Files

- `grill_sessions.json` — session store (keyed by job_id). Contains your
  answers: **add it to `.gitignore`** (the module does not edit
  `.gitignore` itself).
