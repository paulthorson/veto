# Automation risk: what to automate, what to keep human

_Veto automates searching, scoring, drafting, and filling — and refuses to automate sending, submitting, introducing, or sharing without your explicit review at the moment of action. This guide explains the line and how to hold it with any tool._

## The principle

Automate **preparation**; keep humans on **consequential actions**. A consequential action is one that affects another person or creates a commitment: sending an application, emailing a recruiter, posting publicly, sharing your data.

## Safe to automate

- **Discovery:** searching boards, deduping postings, tracking freshness.
- **Analysis:** fit scoring, JD decoding, evidence mapping — as long as weights, evidence, and confidence stay visible.
- **Drafting:** resume tailoring, cover letters, outreach drafts — as long as every claim traces to approved evidence and you review the diff before anything moves.
- **Filling:** form field completion with a preview step.

## Never automate without moment-of-action review

- Submitting an application.
- Sending any message to a human (recruiter, referrer, hiring manager).
- Publishing or sharing anything containing your data.
- Deleting or withdrawing (withdrawing an application is a commitment too).

"Review" means you see the exact payload and explicitly confirm — not a pre-approved blanket permission, not a 5-second undo window.

## Warning signs a tool has crossed the line

- It asks for blanket "auto-apply" permission.
- Confirmations are pre-checked or buried in settings.
- It rephrases your experience into claims you didn't approve.
- It can't show you what it sent, when, and to whom.
- Its analytics include your resume or message content (Veto's tripwire exists precisely because this happens in the wild — any content field in telemetry shuts analytics off pending specialist review).

## The CAPTCHA rule

If a site shows a CAPTCHA, automation stops and hands control to you. Bypassing it — via a service, a model, or a trick — typically violates the site's terms and poisons your own audit trail. Veto pauses on CAPTCHA by design; treat any tool that doesn't as untrustworthy.

## Your checklist before enabling any automation

1. Can I preview the exact action before it executes?
2. Is confirmation required at the moment of action, every time?
3. Can I see a complete history of what was done on my behalf?
4. Can I revoke the permission in one step?
5. Does its analytics/telemetry exclude my content? (Ask. Read the policy.)

Five yeses or it doesn't get enabled.

## Further reading

- [Qualified applications](qualified-applications.md) — the loop automation serves.
- [Methodology](../methodology.md) — how Veto's own tools draw the line.
