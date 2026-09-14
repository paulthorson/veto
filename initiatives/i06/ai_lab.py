#!/usr/bin/env python3
"""Initiative 06 — AI proficiency lab: practical role-based exercises.

Three exercise types per role track, all hands-on and deterministic:

* ``ethics`` — a realistic workplace scenario requiring a judgment
  call about AI use (disclosure, data boundaries, honesty). Each
  ethics exercise embeds a role-appropriate honesty consideration;
  the generalist ethics exercise states the job-application honesty
  rule verbatim (never use AI to invent experience on applications).
* ``evaluation`` — design an evaluation for an AI feature in your
  role: metrics, baselines, and failure modes. Checked for eval
  design concepts.
* ``failure_analysis`` — a postmortem of a realistic AI failure in
  your role: root cause, contributing factors, guardrails. Checked
  for analysis concepts.

How checking works (read this before trusting a "pass"): the checker
measures KEYWORD-CONCEPT COVERAGE, not reasoning quality. Each
exercise defines groups of concept keywords; a group counts as hit
when any of its keywords appears as a whole word. Matching is
word-boundary based, so "lie" no longer matches "client"/"believe"/
"relies" and "ground" no longer matches "background", and
ordinary-prose words ("must", "user", "test", "review", "check",
"instead", ...) were pruned from the keyword lists so they cannot
systematically lower the bar. Known residual limits, stated plainly:

* the checker is negation-blind: "we should not disclose" still
  satisfies the disclosure group;
* a keyword-stuffed paragraph with no real reasoning can pass;
* a "pass" means "mentioned enough of the key considerations" —
  nothing about argument quality, correctness, or depth.

This module performs no logging or persistence of its own — it
returns deterministic results only. When wired through
``ai_proficiency`` (``lab_tracks`` / ``lab_exercise`` /
``submit_lab_exercise``, CLI ``ai-skills lab-*``, MCP tools
``ai_lab_*``; see ``initiatives/i06/integration_notes.md``), a
passing submission is recorded there as PRACTICE — never a
credential or certification.

Each exercise discloses (brief, task, what good looks like); the
check spec stays server-side, following the ai_proficiency.py
pattern of never revealing the full answer key. Missed groups are
described generically in results ("concept 2 of 4: not addressed")
without naming the keywords.

Stdlib only, deterministic, no network.
"""

from __future__ import annotations

import re
from typing import Any

_EXERCISE_TYPES = ("ethics", "evaluation", "failure_analysis")


def _ex(brief: str, task: str, good_looks_like: str,
        groups: list[list[str]], min_groups: int,
        min_words: int = 40) -> dict[str, Any]:
    return {
        "brief": brief,
        "task": task,
        "good_looks_like": good_looks_like,
        "check": {"groups": groups, "min_groups": min_groups,
                  "min_words": min_words},
    }


EXERCISES: dict[str, dict[str, dict[str, Any]]] = {
    "engineer": {
        "ethics": _ex(
            brief=("Your team ships an AI code-review bot. A teammate "
                   "starts pasting customer production data into the "
                   "bot's prompt box to 'get better reviews'."),
            task=("Write the message you'd send the team: what the rule "
                  "is, why it exists, and what to do instead. Then state "
                  "the one disclosure you'd add to any AI-assisted code "
                  "you submit for review."),
            good_looks_like=("Names the data boundary (no customer data "
                             "in third-party prompts), gives a safe "
                             "alternative (redacted examples, synthetic "
                             "data), and discloses AI assistance "
                             "honestly."),
            groups=[
                ["customer data", "production data", "pii", "sensitive data"],
                ["disclose", "disclosure", "mention", "flag"],
                ["redact", "synthetic", "anonymize", "mock data",
                 "sanitize"],
                ["policy", "rule", "not allowed", "must not", "forbidden"],
            ],
            min_groups=3,
        ),
        "evaluation": _ex(
            brief=("You're adding an LLM-powered 'summarize this ticket' "
                   "button to your team's support tool."),
            task=("Design the ship/no-ship evaluation: what you measure, "
                  "your baseline, how many examples, and what failure "
                  "would block launch. Be specific enough that a teammate "
                  "could run it."),
            good_looks_like=("Names 2+ metrics (accuracy + a guardrail "
                             "metric like hallucination rate), a human or "
                             "heuristic baseline, a sample size, and a "
                             "blocking failure criterion."),
            groups=[
                ["accuracy", "correctness", "precision", "recall", "f1"],
                ["baseline", "human", "compare"],
                ["hallucination", "grounded", "faithful", "made up"],
                ["sample", "n=", "100", "200", "500"],
                ["block", "threshold", "gate", "launch criteria"],
            ],
            min_groups=4,
        ),
        "failure_analysis": _ex(
            brief=("The ticket summarizer shipped. A summary invented a "
                   "refund promise the company never made; support sent "
                   "it to a customer."),
            task=("Write the postmortem: root cause, two contributing "
                  "factors, and three guardrails you'd add before "
                  "re-enabling it."),
            good_looks_like=("Distinguishes root cause (ungrounded "
                             "generation) from contributors (no human "
                             "review, no grounding check), and proposes "
                             "concrete guardrails (citation requirement, "
                             "human approval, kill switch)."),
            groups=[
                ["hallucinat", "ungrounded", "invented", "made up",
                 "faithful"],
                ["human in the loop", "human review", "reviewer",
                 "approval", "oversight"],
                ["ground", "retriev", "citation", "quoted"],
                ["rollback", "kill switch", "disable", "guardrail",
                 "monitor"],
            ],
            min_groups=3,
        ),
    },
    "product": {
        "ethics": _ex(
            brief=("Your AI feature drafts customer-facing messages. "
                   "Marketing wants to ship without labeling them as "
                   "AI-written: 'users won't care.'"),
            task=("Make the call: label or not? Write the reasoning "
                  "you'd put in the launch doc, including who could be "
                  "harmed by each choice."),
            good_looks_like=("Takes a position, names the harmed parties "
                             "(users misled, support handling fallout), "
                             "and proposes a disclosure design."),
            groups=[
                ["label", "disclose", "disclosure", "transparent"],
                ["trust", "misled", "deceived", "harm"],
                ["user trust", "customer harm", "harmed users",
                 "misled customers", "end users"],
                ["support team", "fallout", "escalation", "complaint",
                 "support burden"],
            ],
            min_groups=3,
        ),
        "evaluation": _ex(
            brief=("You're launching an AI 'recommended next step' card in "
                   "your product's dashboard."),
            task=("Define the success metric, a guardrail metric, the "
                  "experiment design (who sees what), and what would make "
                  "you roll it back."),
            good_looks_like=("Separates success (adoption, task "
                             "completion) from guardrails (complaints, "
                             "bad recommendations), names an A/B or "
                             "holdout, and a rollback trigger."),
            groups=[
                ["adoption", "click", "conversion", "task completion",
                 "success metric"],
                ["guardrail", "complaint", "negative", "harm"],
                ["a/b", "experiment", "holdout", "control", "treatment"],
                ["rollback", "revert", "threshold", "trigger"],
            ],
            min_groups=3,
        ),
        "failure_analysis": _ex(
            brief=("The recommendation card started suggesting a "
                   "deprecated plan to new users; three enterprise "
                   "accounts complained."),
            task=("Postmortem: why did the eval miss this, and what "
                  "changes to the eval and the rollout would catch it "
                  "next time?"),
            good_looks_like=("Finds the eval gap (stale training data / "
                             "no freshness check), and adds both eval "
                             "and rollout fixes (canary, monitoring)."),
            groups=[
                ["stale", "outdated", "deprecated", "freshness", "drift"],
                ["canary", "gradual", "rollout", "staged"],
                ["monitor", "alert", "dashboard", "anomaly"],
                ["eval", "test coverage", "coverage", "missed",
                 "evaluation gap"],
            ],
            min_groups=3,
        ),
    },
    "data": {
        "ethics": _ex(
            brief=("A stakeholder asks you to train a churn model on data "
                   "that includes fields users never consented to share "
                   "for modeling."),
            task=("Write your response: what you will and won't use, the "
                  "principle behind it, and the compliant alternative."),
            good_looks_like=("Refuses the non-consented fields, cites "
                             "consent/purpose limitation, and proposes "
                             "using consented features or gathering "
                             "consent."),
            groups=[
                ["consent", "consented", "permission"],
                ["refuse", "refusal", "won't", "decline", "can't use"],
                ["purpose", "limitation", "privacy"],
                ["alternative data", "consented features", "collect consent",
                 "opt-in"],
            ],
            min_groups=3,
        ),
        "evaluation": _ex(
            brief=("You built an LLM classifier for support-ticket "
                   "topics; the old rules-based system is the baseline."),
            task=("Design the offline eval: dataset, metrics (including "
                  "one fairness or slice metric), and the bar for "
                  "replacing the old system."),
            good_looks_like=("Labeled eval set, accuracy + slice/fairness "
                             "metrics, statistical bar vs baseline, "
                             "error analysis plan."),
            groups=[
                ["labeled", "ground truth", "annotated", "eval set"],
                ["baseline", "rules-based", "current system", "compare"],
                ["slice", "fairness", "segment", "subgroup", "bias"],
                ["significant", "confidence", "threshold", "statistical"],
            ],
            min_groups=3,
        ),
        "failure_analysis": _ex(
            brief=("The classifier silently degraded after a product "
                   "rename; topics were misrouted for two weeks before "
                   "anyone noticed."),
            task=("Postmortem: root cause, why detection took two weeks, "
                  "and the monitoring you'd add."),
            good_looks_like=("Names distribution shift + missing "
                             "monitoring, explains the detection gap (no "
                             "alerts on prediction distribution), adds "
                             "drift monitoring."),
            groups=[
                ["drift", "shift", "distribution", "stale"],
                ["monitor", "monitoring", "alert", "dashboard", "anomaly"],
                ["label", "feedback", "ground truth"],
                ["retrain", "refresh", "model update"],
            ],
            min_groups=3,
        ),
    },
    "design": {
        "ethics": _ex(
            brief=("Your AI mockup generator produces realistic faces for "
                   "placeholder avatars. A PM wants to use them in the "
                   "shipped product."),
            task=("Decide and defend: ship the AI faces or not? Address "
                  "consent, representation, and what you'd use instead."),
            good_looks_like=("Refuses or heavily qualifies: non-consented "
                             "likenesses, bias in generated faces, "
                             "proposes illustrated/abstract avatars."),
            groups=[
                ["consent", "likeness", "real people"],
                ["bias", "representation", "stereotype", "diverse"],
                ["illustrat", "abstract", "initials", "avatar placeholder"],
                ["disclose", "label"],
            ],
            min_groups=3,
        ),
        "evaluation": _ex(
            brief=("You're testing an AI layout assistant with real "
                   "designers."),
            task=("Design the usability eval: tasks, what you measure "
                  "beyond 'liked it', and how you detect over-reliance "
                  "on the AI's suggestions."),
            good_looks_like=("Task-based study, quality + efficiency "
                             "metrics, and an over-reliance probe "
                             "(e.g. planted bad suggestion)."),
            groups=[
                ["task-based", "usability", "scenario", "study task"],
                ["quality", "correctness", "expert review"],
                ["time on task", "efficiency", "faster", "completion time"],
                ["over-reliance", "blindly", "planted", "bad suggestion",
                 "over-trust"],
            ],
            min_groups=3,
        ),
        "failure_analysis": _ex(
            brief=("The layout assistant suggested dark patterns (hidden "
                   "unsubscribe) to three designers, who shipped two of "
                   "them."),
            task=("Postmortem: where did the safeguard fail, and what "
                  "product and process changes prevent recurrence?"),
            good_looks_like=("Names the missing value-alignment guardrail, "
                             "adds pattern blocklists + designer review "
                             "checkpoints, and a reporting path."),
            groups=[
                ["dark pattern", "deceptive", "manipulative", "unethical"],
                ["blocklist", "filter", "guardrail", "constraint"],
                ["human review", "reviewer", "checkpoint", "approval"],
                ["reporting path", "flag", "escalate", "report the"],
            ],
            min_groups=3,
        ),
    },
    "sales": {
        "ethics": _ex(
            brief=("Your AI prospecting tool drafts outreach that "
                   "implies you met the prospect before ('great chatting "
                   "at the summit!') when you didn't."),
            task=("Fix the template policy: what's allowed, what's "
                  "banned, and the test you'd run on every new template."),
            good_looks_like=("Bans false-familiarity claims, requires "
                             "verifiable personalization, adds a template "
                             "review checklist."),
            groups=[
                ["false", "lie", "lying", "dishonest", "mislead"],
                ["ban", "not allowed", "never", "prohibit"],
                ["verif", "verified", "accurate", "honest", "truthful"],
                ["checklist", "approve", "template review", "spot-check"],
            ],
            min_groups=3,
        ),
        "evaluation": _ex(
            brief=("You're piloting AI call summaries for your sales team."),
            task=("Define pilot success: metrics, the comparison group, "
                  "pilot length, and the failure that ends the pilot."),
            good_looks_like=("Rep-time-saved + accuracy metrics, control "
                             "group of reps, fixed pilot window, kill "
                             "criterion (e.g. invented commitments)."),
            groups=[
                ["time saved", "efficiency", "hours"],
                ["accuracy", "correct", "error"],
                ["control", "comparison", "baseline"],
                ["kill", "kill criterion", "stop the pilot", "end the pilot",
                 "threshold"],
            ],
            min_groups=3,
        ),
        "failure_analysis": _ex(
            brief=("An AI summary invented a discount the rep never "
                   "offered; the customer is holding you to it."),
            task=("Postmortem: immediate response, root cause, and the "
                  "guardrail before summaries go back to reps."),
            good_looks_like=("Owns the error with the customer, names "
                             "ungrounded generation + no rep review, adds "
                             "mandatory rep sign-off + number verification."),
            groups=[
                ["hallucinat", "invented", "ungrounded", "made up"],
                ["sign-off", "approve", "rep review", "human approval"],
                ["verify", "numbers", "quotes", "cross-check",
                 "double-check"],
                ["apolog", "make right", "take responsibility", "own the"],
            ],
            min_groups=3,
        ),
    },
    "generalist": {
        "ethics": _ex(
            brief=("You use an AI assistant to draft a cover letter. It "
                   "invents a certification you don't have and phrases "
                   "it plausibly."),
            task=("What do you do with that draft? State the rule you'd "
                  "follow for every AI-assisted application material, and "
                  "why the invented line can never stay."),
            good_looks_like=("Deletes the invented claim, states the "
                             "honesty rule (AI drafts, you verify every "
                             "fact), and explains the risk (background "
                             "check, integrity)."),
            groups=[
                ["delete", "remove", "never", "won't use"],
                ["verify", "fact-check", "accurate", "check every"],
                ["honest", "integrity", "lie"],
                ["background check", "caught", "integrity risk",
                 "reputation"],
            ],
            min_groups=3,
        ),
        "evaluation": _ex(
            brief=("You want an AI tool to triage your inbox into "
                   "'urgent' and 'later'."),
            task=("Design your personal eval: how you test it for a week, "
                  "what you measure, and when you'd stop trusting it."),
            good_looks_like=("Defines a test week with spot checks, "
                             "measures miss rate on truly urgent items, "
                             "sets a stop-trust threshold."),
            groups=[
                ["spot check", "verify", "sample", "spot-check"],
                ["miss rate", "missed", "false negative", "urgent missed"],
                ["trial week", "test period", "trial", "evaluation week",
                 "one week"],
                ["stop using", "threshold", "abandon", "stop trusting"],
            ],
            min_groups=3,
        ),
        "failure_analysis": _ex(
            brief=("The triage tool marked a job offer email as 'later'; "
                   "you found it after the deadline passed."),
            task=("Postmortem for one person: what failed, what you "
                  "change about the tool, and what you change about your "
                  "own process."),
            good_looks_like=("Names the failure mode (no high-stakes "
                             "override), adds sender/keyword allowlists, "
                             "keeps a human scan of key senders."),
            groups=[
                ["allowlist", "important senders", "vip", "filter"],
                ["human scan", "glance", "manual check", "eyeball"],
                ["over-reliance", "blindly", "over-trust", "complacency"],
                ["backup", "failsafe", "redundant"],
            ],
            min_groups=3,
        ),
    },
}

# Single source of truth: tracks are the EXERCISES keys. LAB_TRACKS is
# derived so the two can never silently diverge.
LAB_TRACKS = tuple(EXERCISES)


def _keyword_pattern(keyword: str) -> "re.Pattern[str]":
    """Compile a word-boundary matcher for one keyword.

    Single words/stems match at a word boundary and may continue with
    word characters, so inflections and stems work ("hallucinat"
    matches "hallucinated", "lie" matches "lies") while substrings do
    not ("lie" does NOT match "client"/"believe"/"relies"; "ground"
    does NOT match "background"). Multi-word phrases must appear
    exactly, bounded by word boundaries on both ends.
    """
    kw = keyword.strip()
    if " " in kw:
        return re.compile(r"\b" + re.escape(kw) + r"\b")
    return re.compile(r"\b" + re.escape(kw) + r"\w*")


def _keyword_hit(keyword: str, text: str) -> bool:
    """True when the keyword matches text under word-boundary rules."""
    return _keyword_pattern(keyword).search(text) is not None


def validate_lab_track(track: str | None) -> str:
    """Return the canonical lab track; raise ValueError on unknown."""
    t = (track or "").strip().lower()
    if t not in EXERCISES:
        raise ValueError(
            f"Unknown lab track {track!r}. Choose one of: "
            f"{', '.join(LAB_TRACKS)}.")
    return t


def validate_exercise_type(kind: str | None) -> str:
    """Return the canonical exercise type; raise ValueError on unknown."""
    k = (kind or "").strip().lower()
    if k not in _EXERCISE_TYPES:
        raise ValueError(
            f"Unknown exercise type {kind!r}. Choose one of: "
            f"{', '.join(_EXERCISE_TYPES)}.")
    return k


def lab_tracks() -> list[dict[str, Any]]:
    """All lab tracks with the exercise types each offers."""
    return [{"track": t, "exercise_types": list(_EXERCISE_TYPES)}
            for t in LAB_TRACKS]


def get_lab_exercise(track: str, exercise_type: str) -> dict[str, Any]:
    """Return the disclosed exercise (brief, task, good-looks-like).

    The check spec is intentionally not returned — same pattern as
    ai_proficiency.exercise (answer key stays server-side).
    """
    track = validate_lab_track(track)
    exercise_type = validate_exercise_type(exercise_type)
    spec = EXERCISES[track][exercise_type]
    return {
        "track": track,
        "exercise_type": exercise_type,
        "brief": spec["brief"],
        "task": spec["task"],
        "good_looks_like": spec["good_looks_like"],
        "min_words": spec["check"]["min_words"],
    }


def check_lab_exercise(track: str, exercise_type: str,
                       response: str) -> dict[str, Any]:
    """Deterministically check a lab exercise response.

    Group-based concept coverage (like ai_proficiency._eval_check) plus
    a minimum word count, using word-boundary keyword matching. Never
    reveals the full answer key — missed groups are described
    generically ("concept 2 of 4: not addressed") without naming
    keywords.

    Honest limits (see module docstring): this measures keyword
    presence, not reasoning quality; it is negation-blind, and a
    keyword-stuffed response can pass.
    """
    track = validate_lab_track(track)
    exercise_type = validate_exercise_type(exercise_type)
    spec = EXERCISES[track][exercise_type]["check"]
    text = (response or "").lower()
    words = len(text.split())
    group_hits = [[kw for kw in group if _keyword_hit(kw, text)]
                  for group in spec["groups"]]
    passed_groups = sum(1 for h in group_hits if h)
    need = spec.get("min_groups", len(spec["groups"]))
    min_words = spec.get("min_words", 0)
    passed = passed_groups >= need and words >= min_words
    missed = sum(1 for h in group_hits if not h)
    missed_descriptions = [
        f"concept {i + 1} of {len(spec['groups'])}: not addressed"
        for i, h in enumerate(group_hits) if not h
    ]
    return {
        "passed": passed,
        "score": round(passed_groups / len(spec["groups"]), 2),
        "groups_hit": passed_groups,
        "groups_total": len(spec["groups"]),
        "groups_missed": missed,
        "missed_group_descriptions": missed_descriptions,
        "words": words,
        "min_words": min_words,
    }
