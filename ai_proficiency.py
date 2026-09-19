#!/usr/bin/env python3
"""AI-proficiency training for the Veto MCP server.

Employers now screen for AI fluency. This module takes a candidate from
zero to demonstrably AI-proficient *in their own role*:

``TRACKS``
    Role tracks (engineer, product, data, design, marketing, sales,
    generalist). Each lists concrete AI skills for that role.

``diagnose(track, answers)``
    5-question diagnostic per track (4 multiple-choice + 1 short
    free-text) -> level ``foundations`` / ``practitioner`` / ``advanced``
    plus a gap list. Deterministic scoring, no LLM, no network.

``plan(track, level)``
    Ordered learning plan: bite-size lessons per track, each with a
    hands-on exercise (a do-this-now task, not just reading).

``exercise(track, lesson_id)`` / ``submit_exercise(session_id, response)``
    Presents the exercise, then checks the response deterministically
    (concept/keyword checks, no LLM) and gives feedback + next lesson.

``progress()``
    Lessons completed, streak days, per-track level — stored in
    ``ai_proficiency.json``.

Honest framing (hard rule): this logs PRACTICE, never a credential. The
progress output says so explicitly. Every track includes an ethics
lesson: AI disclosure norms + the project's honesty rule — never use AI
to invent experience on applications.

Stdlib only.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Initiative 06 AI proficiency lab (same repo).
from initiatives.i06 import ai_lab as _i06_ai_lab
from initiatives.i06 import longitudinal as _i06_longitudinal

log = logging.getLogger("veto-mcp.ai_proficiency")

BASE_DIR = Path(__file__).resolve().parent
STORE_PATH = BASE_DIR / "ai_proficiency.json"

LEVELS = ("foundations", "practitioner", "advanced")

# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

_Q = dict  # shorthand, questions are plain dicts


def _ethics_lesson(prefix: str) -> dict[str, Any]:
    return {
        "id": f"{prefix}-ethics",
        "title": "Ethics: disclose AI use, never invent",
        "skill": "AI ethics & disclosure",
        "est_minutes": 10,
        "body": (
            "Two rules that keep AI a career asset instead of a liability.\n"
            "- DISCLOSE substantive AI help: if AI wrote a work artifact "
            "(report, code, design), say so to collaborators and follow your "
            "employer's AI policy. Undisclosed AI work discovered later "
            "reads as deception.\n"
            "- NEVER invent: do not let AI fabricate job titles, dates, "
            "skills, or achievements on a resume or application. Veto runs "
            "an honesty check for exactly this reason — invented experience "
            "wastes everyone's time and can cost you an offer or a job.\n"
            "The line: AI may help you *express* what you did; it may never "
            "*invent* what you did."
        ),
        "exercise": {
            "prompt": (
                "In one or two sentences, explain why letting AI invent a "
                "job title on your resume is wrong — and what you should do "
                "instead when AI drafts your application materials."
            ),
            "check": {
                "groups": [
                    ["no", "not", "never", "false", "wrong", "dishonest"],
                    ["invent", "lie", "fake", "made up", "fabricat"],
                    ["review", "check", "verify", "edit", "truth", "honest"],
                ],
                "min_groups": 2,
            },
            "hint": "Say plainly that inventing is wrong, and mention "
                    "reviewing/verifying AI drafts against the truth.",
        },
    }


TRACKS: dict[str, dict[str, Any]] = {
    "engineer": {
        "label": "Software Engineer",
        "blurb": "Ship AI features: APIs, RAG, agents, and evals.",
        "skills": [
            "Prompt engineering for code",
            "LLM API integration",
            "RAG concepts",
            "Agent workflows",
            "Evals & regression testing",
            "AI-assisted code review",
            "Cost/latency tradeoffs",
            "AI ethics & disclosure",
        ],
        "diagnostic": [
            {
                "id": "eng-d1", "skill": "LLM API integration", "kind": "mcq",
                "prompt": "You're calling an LLM API in production. Which "
                          "practice most reduces cost without hurting quality?",
                "choices": [
                    "Always use the largest available model",
                    "Cache repeated prompts and route simple tasks to a smaller model",
                    "Set temperature to 2.0",
                    "Send the entire database with every call",
                ],
                "answer": 1,
                "why": "Caching + model routing cuts tokens and cost; "
                       "temperature and context-bloat don't.",
            },
            {
                "id": "eng-d2", "skill": "RAG concepts", "kind": "mcq",
                "prompt": "What problem does RAG (retrieval-augmented "
                          "generation) primarily solve?",
                "choices": [
                    "Faster model training",
                    "Hallucination on private or recent knowledge, by grounding answers in retrieved docs",
                    "GPU costs",
                    "Only the context-window limit",
                ],
                "answer": 1,
                "why": "RAG grounds the model in retrieved documents so it "
                       "answers from real sources instead of guessing.",
            },
            {
                "id": "eng-d3", "skill": "Agent workflows", "kind": "mcq",
                "prompt": "An agent loop is best described as:",
                "choices": [
                    "One prompt, one answer",
                    "The model plans, calls tools, observes results, and repeats until done",
                    "Fine-tuning on company data",
                    "A chatbot with a nice UI",
                ],
                "answer": 1,
                "why": "Agents iterate: plan -> act via tools -> observe -> "
                       "repeat, with guardrails.",
            },
            {
                "id": "eng-d4", "skill": "Evals & regression testing",
                "kind": "mcq",
                "prompt": "You changed a system prompt. How do you know it "
                          "didn't get worse?",
                "choices": [
                    "Ship it and watch support tickets",
                    "Run a fixed set of eval cases and compare pass rates",
                    "Ask the model whether it's better now",
                    "Check that the token count went down",
                ],
                "answer": 1,
                "why": "Evals give a repeatable quality signal; vibes and "
                       "ticket-watching don't.",
            },
            {
                "id": "eng-d5", "skill": "AI-assisted code review",
                "kind": "free",
                "prompt": "In 2-3 sentences, describe how you'd use an AI "
                          "coding assistant to review a pull request without "
                          "letting bugs slip through.",
                "concepts": ["test", "verify", "specific", "run", "edge"],
                "why": "Strong answers mention running tests, verifying "
                       "claims specifically, and checking edge cases — not "
                       "blindly trusting the review.",
            },
        ],
        "lessons": [
            {
                "id": "eng-l1", "title": "Prompting for code that works",
                "skill": "Prompt engineering for code", "est_minutes": 15,
                "body": (
                    "Vague prompts produce plausible-but-broken code. Do this:\n"
                    "- Name the language AND version (\"Python 3.12\").\n"
                    "- State constraints: input shapes, edge cases, error handling.\n"
                    "- Ask for a usage example or test, not just the function.\n"
                    "- Iterate: paste the error back and ask for a fix, don't restart."
                ),
                "exercise": {
                    "prompt": "Write a 2-4 sentence prompt asking an AI to "
                              "write a Python function that parses dates from "
                              "a CSV column and handles empty cells.",
                    "check": {"keywords": ["python", "csv", "date", "empty"],
                              "min_ratio": 0.75},
                    "hint": "Name the language, the input format, and the edge case.",
                },
            },
            {
                "id": "eng-l2", "title": "LLM APIs in production",
                "skill": "LLM API integration", "est_minutes": 15,
                "body": (
                    "A raw API call is a demo; production needs a wrapper:\n"
                    "- Retries with backoff + timeouts (models flake).\n"
                    "- Structured output: ask for JSON and validate against a schema.\n"
                    "- Log prompts/outputs for debugging and evals.\n"
                    "- Cache identical requests; route easy tasks to small models."
                ),
                "exercise": {
                    "prompt": "Name two things you'd add around a raw LLM "
                              "API call before shipping it to users.",
                    "check": {"keywords": ["retr", "timeout", "valid", "schema",
                                           "json", "log", "cach", "fallback",
                                           "rate"],
                              "min_ratio": 0.25},
                    "hint": "Think: what happens when the call fails, returns "
                            "garbage, or gets called twice?",
                },
            },
            {
                "id": "eng-l3", "title": "RAG in 15 minutes",
                "skill": "RAG concepts", "est_minutes": 15,
                "body": (
                    "RAG = retrieve, then generate from retrieved context.\n"
                    "- Chunk docs into small passages; embed them into vectors.\n"
                    "- At query time, embed the question and fetch top-k chunks.\n"
                    "- Stuff chunks into the prompt; instruct the model to cite them.\n"
                    "- Failure modes: stale index, bad chunking, no citations."
                ),
                "exercise": {
                    "prompt": "In your own words, what are the three steps "
                              "of a RAG pipeline?",
                    "check": {"keywords": ["retriev", "embed", "chunk",
                                           "generat", "search", "index"],
                              "min_ratio": 0.5},
                    "hint": "Find relevant docs -> turn text into searchable "
                            "form -> generate an answer from them.",
                },
            },
            {
                "id": "eng-l4", "title": "Agent workflows and guardrails",
                "skill": "Agent workflows", "est_minutes": 20,
                "body": (
                    "Agents = model + tools + loop (plan, act, observe).\n"
                    "- Give tools narrow schemas; the model picks when to call them.\n"
                    "- Guardrails are the product: spending caps, approval "
                    "gates for irreversible actions, max iterations.\n"
                    "- Log every tool call — debugging agents without traces "
                    "is guesswork."
                ),
                "exercise": {
                    "prompt": "Describe one task in your work an agent could "
                              "do, and one guardrail you'd put on it.",
                    "check": {"groups": [["tool", "api", "function", "search",
                                           "ticket", "deploy"],
                                          ["guardrail", "limit", "approv",
                                           "human", "review", "budget", "cap"]],
                              "min_groups": 2},
                    "hint": "Name what the agent would touch, then name what "
                            "stops it from going too far.",
                },
            },
            {
                "id": "eng-l5", "title": "Evals: know when you got worse",
                "skill": "Evals & regression testing", "est_minutes": 15,
                "body": (
                    "Prompts are code; they need tests.\n"
                    "- Build a golden set: 20-50 real inputs with expected outputs.\n"
                    "- Re-run on every prompt/model change; track pass rate.\n"
                    "- Good cases are specific, realistic, and cover edge cases.\n"
                    "- Judge with rules first (exact match, regex), LLM-judge last."
                ),
                "exercise": {
                    "prompt": "List three properties of a good eval case.",
                    "check": {"keywords": ["expected", "real", "specific",
                                           "diverse", "edge", "repeat"],
                              "min_ratio": 0.34},
                    "hint": "What would make you trust a test to catch a regression?",
                },
            },
            {
                "id": "eng-l6", "title": "Cost and latency tradeoffs",
                "skill": "Cost/latency tradeoffs", "est_minutes": 10,
                "body": (
                    "AI features have a unit cost — design around it.\n"
                    "- Model tiers: small for classification, large for reasoning.\n"
                    "- Cache aggressively; stream output so latency feels low.\n"
                    "- Estimate $/1k requests before launch, not after.\n"
                    "- Batch offline work; reserve realtime for what needs it."
                ),
                "exercise": {
                    "prompt": "Your AI feature costs $0.02/request at 10k "
                              "requests/day. Name two ways to cut cost by "
                              "half or more.",
                    "check": {"keywords": ["cach", "smaller", "model", "batch",
                                           "tier", "stream"],
                              "min_ratio": 0.34},
                    "hint": "Fewer tokens per request, or cheaper tokens per request.",
                },
            },
            _ethics_lesson("eng"),
        ],
    },
    "product": {
        "label": "Product Manager",
        "blurb": "Scope, evaluate, and ship AI features users trust.",
        "skills": [
            "AI product sense",
            "AI feature scoping",
            "Eval design",
            "Human-in-the-loop UX",
            "Prompt management",
            "Measuring AI quality",
            "AI ethics & disclosure",
        ],
        "diagnostic": [
            {
                "id": "pm-d1", "skill": "AI product sense", "kind": "mcq",
                "prompt": "An AI feature is wrong about 5% of the time. "
                          "Best product move?",
                "choices": [
                    "Hide the error rate from users",
                    "Ship with confidence display and a human-review path for high-stakes actions",
                    "Add more emojis to distract",
                    "Make it faster so nobody notices",
                ],
                "answer": 1,
                "why": "Design for the failure rate: show uncertainty and "
                       "keep humans in charge of consequences.",
            },
            {
                "id": "pm-d2", "skill": "Eval design", "kind": "mcq",
                "prompt": "What is an eval in AI product work?",
                "choices": [
                    "An A/B test",
                    "A repeatable test set measuring output quality before and after changes",
                    "A user survey",
                    "A code review",
                ],
                "answer": 1,
                "why": "Evals are regression tests for model behavior.",
            },
            {
                "id": "pm-d3", "skill": "Human-in-the-loop UX", "kind": "mcq",
                "prompt": "\"Human-in-the-loop\" means:",
                "choices": [
                    "Humans train the model",
                    "A person reviews or approves AI outputs before they take effect",
                    "The AI chats with humans",
                    "An open office layout",
                ],
                "answer": 1,
                "why": "HITL keeps a human as the decision gate on "
                       "consequential outputs.",
            },
            {
                "id": "pm-d4", "skill": "Measuring AI quality", "kind": "mcq",
                "prompt": "Best success metric for an AI support-drafting feature?",
                "choices": [
                    "Tokens generated per day",
                    "% of drafts sent without edits, plus CSAT",
                    "Model parameter count",
                    "Number of prompts written",
                ],
                "answer": 1,
                "why": "Measure outcomes (accepted drafts, satisfied users), "
                       "not model activity.",
            },
            {
                "id": "pm-d5", "skill": "AI feature scoping", "kind": "free",
                "prompt": "Describe an AI feature for a product you know, "
                          "including where a human stays in the loop.",
                "concepts": ["human", "review", "approv", "check", "loop",
                             "verif"],
                "why": "Strong answers name the AI job AND the human "
                       "checkpoint — not full automation.",
            },
        ],
        "lessons": [
            {
                "id": "pm-l1", "title": "AI product sense",
                "skill": "AI product sense", "est_minutes": 15,
                "body": (
                    "AI is great at: drafting, summarizing, classifying, "
                    "translating, brainstorming.\n"
                    "AI is risky at: facts without sources, irreversible "
                    "actions, high-stakes decisions alone.\n"
                    "Product sense = matching the capability to a job where "
                    "the failure mode is cheap and visible."
                ),
                "exercise": {
                    "prompt": "Name one feature in a product you use where "
                              "AI would help, and one where it would be "
                              "dangerous. One sentence each.",
                    "check": {"groups": [["draft", "summar", "search", "help",
                                           "suggest", "triage"],
                                          ["danger", "risk", "wrong", "harm",
                                           "stake", "hallucinat"]],
                              "min_groups": 2},
                    "hint": "One low-stakes helper, one high-stakes risk.",
                },
            },
            {
                "id": "pm-l2", "title": "Scoping AI features",
                "skill": "AI feature scoping", "est_minutes": 15,
                "body": (
                    "Scope doc for an AI feature needs four things:\n"
                    "- The job and its failure cost (what happens when wrong).\n"
                    "- The human checkpoint (approve, edit, or just view?).\n"
                    "- The eval: how you'll know quality moved.\n"
                    "- The fallback: what users get when the model fails."
                ),
                "exercise": {
                    "prompt": "Pick an AI feature idea and write its failure "
                              "cost and fallback in two sentences.",
                    "check": {"keywords": ["wrong", "fail", "fallback", "instead",
                                           "manual", "cost", "risk"],
                              "min_ratio": 0.29},
                    "hint": "What breaks if it's wrong, and what does the "
                            "user get instead?",
                },
            },
            {
                "id": "pm-l3", "title": "Designing evals with your team",
                "skill": "Eval design", "est_minutes": 15,
                "body": (
                    "You don't need ML expertise to own evals:\n"
                    "- Collect 30 real examples from users or support tickets.\n"
                    "- Define pass/fail per example with the team.\n"
                    "- Re-run before every launch; treat drops as launch blockers.\n"
                    "- Refresh the set quarterly — it goes stale."
                ),
                "exercise": {
                    "prompt": "Where would you source 30 realistic eval "
                              "examples for an AI triage feature?",
                    "check": {"keywords": ["ticket", "support", "real", "user",
                                           "history", "log", "past"],
                              "min_ratio": 0.29},
                    "hint": "Think about where real inputs already pile up.",
                },
            },
            {
                "id": "pm-l4", "title": "Human-in-the-loop UX patterns",
                "skill": "Human-in-the-loop UX", "est_minutes": 15,
                "body": (
                    "Patterns that work:\n"
                    "- Draft + approve: AI proposes, human sends.\n"
                    "- Confidence display: show certainty, route low-confidence to review.\n"
                    "- Undo everywhere: AI actions must be reversible.\n"
                    "- Progressive autonomy: start supervised, earn trust with data."
                ),
                "exercise": {
                    "prompt": "Which HITL pattern fits an AI refund-approval "
                              "tool, and why? Two sentences.",
                    "check": {"keywords": ["approv", "review", "human",
                                           "confidence", "undo", "supervis"],
                              "min_ratio": 0.34},
                    "hint": "Money is irreversible — which pattern keeps a "
                            "human in charge?",
                },
            },
            {
                "id": "pm-l5", "title": "Prompt management",
                "skill": "Prompt management", "est_minutes": 10,
                "body": (
                    "Prompts are product assets:\n"
                    "- Version them like code; review changes.\n"
                    "- Keep a changelog: what changed, eval delta.\n"
                    "- Separate instructions from user content (injection safety).\n"
                    "- One owner per prompt; no drive-by edits."
                ),
                "exercise": {
                    "prompt": "Why is letting anyone edit a production "
                              "prompt directly a bad idea? Two sentences.",
                    "check": {"keywords": ["eval", "regress", "version",
                                           "break", "test", "review"],
                              "min_ratio": 0.34},
                    "hint": "What breaks when changes aren't tested?",
                },
            },
            {
                "id": "pm-l6", "title": "Measuring AI quality",
                "skill": "Measuring AI quality", "est_minutes": 10,
                "body": (
                    "Good AI metrics:\n"
                    "- Task success: did the user accept/use the output?\n"
                    "- Correction rate: how much editing before use?\n"
                    "- Escalation rate: how often did HITL kick in?\n"
                    "Bad AI metrics: tokens, prompts written, model size."
                ),
                "exercise": {
                    "prompt": "Name two outcome metrics (not activity "
                              "metrics) for an AI meeting-notes feature.",
                    "check": {"keywords": ["accept", "edit", "correction",
                                           "shar", "use", "action", "time"],
                              "min_ratio": 0.29},
                    "hint": "What would prove users got value?",
                },
            },
            _ethics_lesson("pm"),
        ],
    },
    "data": {
        "label": "Data Professional",
        "blurb": "Faster analysis with AI — without trusting it blindly.",
        "skills": [
            "LLM-assisted analysis",
            "Text-to-SQL safety",
            "Embeddings & semantic search",
            "RAG over company docs",
            "Pipeline evals",
            "AI ethics & disclosure",
        ],
        "diagnostic": [
            {
                "id": "da-d1", "skill": "Text-to-SQL safety", "kind": "mcq",
                "prompt": "Biggest risk of text-to-SQL against production?",
                "choices": [
                    "Slow queries",
                    "Wrong or malicious SQL running against production data — always validate and limit scope",
                    "Ugly generated code",
                    "API cost",
                ],
                "answer": 1,
                "why": "Generated SQL must be validated and sandboxed; a "
                       "bad query can read or wreck prod.",
            },
            {
                "id": "da-d2", "skill": "Embeddings & semantic search",
                "kind": "mcq",
                "prompt": "Embeddings are most useful for:",
                "choices": [
                    "Training models faster",
                    "Semantic search and similarity matching",
                    "Database backups",
                    "Encryption",
                ],
                "answer": 1,
                "why": "Embeddings turn text into vectors so \"similar "
                       "meaning\" becomes computable.",
            },
            {
                "id": "da-d3", "skill": "LLM-assisted analysis", "kind": "mcq",
                "prompt": "Before trusting an LLM summary of a dataset, you should:",
                "choices": [
                    "Spot-check its claims against source rows",
                    "Trust it — models are good at math",
                    "Ask for a longer summary",
                    "Translate it to another language",
                ],
                "answer": 1,
                "why": "LLMs confidently misread data; verification is the job.",
            },
            {
                "id": "da-d4", "skill": "RAG over company docs", "kind": "mcq",
                "prompt": "A RAG pipeline over company docs returns stale answers. Likely cause?",
                "choices": [
                    "Model too small",
                    "Index not refreshed, or chunking broke on new formats",
                    "Too many users",
                    "Wrong font in the docs",
                ],
                "answer": 1,
                "why": "Stale retrieval = stale index. RAG is only as fresh "
                       "as its pipeline.",
            },
            {
                "id": "da-d5", "skill": "LLM-assisted analysis", "kind": "free",
                "prompt": "How would you use AI to speed up exploratory "
                          "analysis of a new dataset while staying accurate?",
                "concepts": ["verify", "sample", "check", "sql", "plot",
                             "valid"],
                "why": "Strong answers pair AI speed (draft SQL, plots) "
                       "with verification (spot-checks, validation).",
            },
        ],
        "lessons": [
            {
                "id": "da-l1", "title": "Text-to-SQL without the foot-gun",
                "skill": "Text-to-SQL safety", "est_minutes": 15,
                "body": (
                    "Text-to-SQL is powerful and dangerous.\n"
                    "- Always run generated SQL read-only, on a replica or "
                    "with a row limit.\n"
                    "- Read the query before running: check joins and filters.\n"
                    "- Give the model the schema + a few example queries.\n"
                    "- Never let it touch prod writes."
                ),
                "exercise": {
                    "prompt": "List three safeguards you'd require before "
                              "letting analysts run AI-generated SQL.",
                    "check": {"keywords": ["read-only", "readonly", "limit",
                                           "review", "schema", "replica",
                                           "valid", "test"],
                              "min_ratio": 0.25},
                    "hint": "What stops a bad query from hurting prod?",
                },
            },
            {
                "id": "da-l2", "title": "LLM-assisted exploratory analysis",
                "skill": "LLM-assisted analysis", "est_minutes": 15,
                "body": (
                    "Use AI as a fast junior analyst, not an oracle:\n"
                    "- Have it draft pandas/SQL for profiling: nulls, "
                    "distributions, outliers.\n"
                    "- Ask it to suggest plots, then inspect them yourself.\n"
                    "- Verify every surprising number against raw rows.\n"
                    "- Keep a log of what you checked — that's the analysis."
                ),
                "exercise": {
                    "prompt": "You get a new 2M-row events table. Write the "
                              "prompt you'd give AI for first-pass profiling.",
                    "check": {"keywords": ["null", "distribution", "profil",
                                           "outlier", "schema", "sample"],
                              "min_ratio": 0.34},
                    "hint": "Ask for the standard data-quality checks by name.",
                },
            },
            {
                "id": "da-l3", "title": "Embeddings and semantic search",
                "skill": "Embeddings & semantic search", "est_minutes": 15,
                "body": (
                    "When keyword search fails, embeddings help:\n"
                    "- Embed documents once; embed queries at search time.\n"
                    "- Cosine similarity ranks \"same meaning\" results.\n"
                    "- Great for: ticket similarity, doc search, dedup.\n"
                    "- Watch for: stale embeddings after content changes."
                ),
                "exercise": {
                    "prompt": "Name a dataset at work where semantic search "
                              "would beat keyword search, and why.",
                    "check": {"keywords": ["ticket", "doc", "search", "similar",
                                           "synonym", "meaning", "support"],
                              "min_ratio": 0.29},
                    "hint": "Where do people describe the same thing in "
                            "different words?",
                },
            },
            {
                "id": "da-l4", "title": "RAG for data teams",
                "skill": "RAG over company docs", "est_minutes": 15,
                "body": (
                    "Data teams sit on tribal knowledge — RAG unlocks it:\n"
                    "- Index runbooks, metric definitions, past incident notes.\n"
                    "- Metric-definition Q&A kills \"what does churned mean\" pings.\n"
                    "- Refresh the index on a schedule; version it.\n"
                    "- Show sources with every answer so analysts can verify."
                ),
                "exercise": {
                    "prompt": "What would you index first for a metrics-Q&A "
                              "bot, and how would you keep it fresh?",
                    "check": {"keywords": ["metric", "definition", "runbook",
                                           "refresh", "schedule", "glossary"],
                              "min_ratio": 0.34},
                    "hint": "The docs people ask about most, plus a refresh plan.",
                },
            },
            {
                "id": "da-l5", "title": "Evals for AI pipelines",
                "skill": "Pipeline evals", "est_minutes": 10,
                "body": (
                    "AI in a pipeline needs the same rigor as any transform:\n"
                    "- Golden sets: known inputs -> expected outputs.\n"
                    "- Monitor: null rates, schema drift, output length shifts.\n"
                    "- Alert on change; quarantine bad batches.\n"
                    "- Re-validate when the model version changes."
                ),
                "exercise": {
                    "prompt": "An AI categorization step's accuracy drops "
                              "after a model update. What's your first "
                              "debugging move?",
                    "check": {"keywords": ["eval", "golden", "compar", "revert",
                                           "version", "sample"],
                              "min_ratio": 0.34},
                    "hint": "How do you prove it got worse, and what do you "
                            "roll back to?",
                },
            },
            _ethics_lesson("da"),
        ],
    },
    "design": {
        "label": "Designer",
        "blurb": "Concept faster, prototype in hours, present with honesty.",
        "skills": [
            "AI concepting",
            "Prompt craft for visuals",
            "Rapid AI prototyping",
            "Research synthesis with AI",
            "Design-system-aware generation",
            "AI ethics & disclosure",
        ],
        "diagnostic": [
            {
                "id": "de-d1", "skill": "AI concepting", "kind": "mcq",
                "prompt": "Best use of generative AI in early design?",
                "choices": [
                    "Final production assets",
                    "Rapid divergent concepts to react to and refine",
                    "Replacing user research entirely",
                    "Writing production CSS",
                ],
                "answer": 1,
                "why": "AI is a divergence engine; judgment and craft stay human.",
            },
            {
                "id": "de-d2", "skill": "AI concepting", "kind": "mcq",
                "prompt": "When showing AI-generated UI to stakeholders, you should:",
                "choices": [
                    "Present it as the final decision",
                    "Label it clearly as exploration, not a decision",
                    "Hide how it was made",
                    "Skip critique — it's efficient",
                ],
                "answer": 1,
                "why": "Unlabeled AI concepts get mistaken for commitments.",
            },
            {
                "id": "de-d3", "skill": "Design-system-aware generation",
                "kind": "mcq",
                "prompt": "A design system helps AI UI generation by:",
                "choices": [
                    "Doing nothing useful",
                    "Constraining outputs to real components and tokens",
                    "Making generation slower",
                    "Adding more colors",
                ],
                "answer": 1,
                "why": "Constraints turn generic AI output into shippable UI.",
            },
            {
                "id": "de-d4", "skill": "Research synthesis with AI",
                "kind": "mcq",
                "prompt": "AI-synthesized user research is risky because:",
                "choices": [
                    "It's too fast",
                    "It can smooth real quotes into plausible fiction — always trace claims to source",
                    "It's expensive",
                    "Users dislike fast research",
                ],
                "answer": 1,
                "why": "Synthesis without traceability manufactures insights.",
            },
            {
                "id": "de-d5", "skill": "Rapid AI prototyping", "kind": "free",
                "prompt": "Describe how you'd use AI to go from a rough "
                          "idea to a testable prototype in one day.",
                "concepts": ["prototype", "test", "iterate", "wireframe",
                             "feedback"],
                "why": "Strong answers show a loop: generate -> build -> "
                       "test with users -> iterate.",
            },
        ],
        "lessons": [
            {
                "id": "de-l1", "title": "Divergent concepting with AI",
                "skill": "AI concepting", "est_minutes": 15,
                "body": (
                    "Use AI to widen the funnel before you narrow it:\n"
                    "- Generate 10 directions in the time one used to take.\n"
                    "- Vary the brief: audience, tone, constraints.\n"
                    "- Curate ruthlessly — your taste is the value.\n"
                    "- Never present raw output as finished work."
                ),
                "exercise": {
                    "prompt": "Write a brief for AI to generate 5 onboarding "
                              "screen concepts, with audience and constraints.",
                    "check": {"keywords": ["audience", "constraint", "tone",
                                           "onboarding", "5", "five", "style"],
                              "min_ratio": 0.43},
                    "hint": "Who is it for, and what must it respect?",
                },
            },
            {
                "id": "de-l2", "title": "Prompt craft for visuals",
                "skill": "Prompt craft for visuals", "est_minutes": 15,
                "body": (
                    "Better visual prompts:\n"
                    "- Composition first: layout, hierarchy, focal point.\n"
                    "- Then style: reference real movements, not \"modern\".\n"
                    "- Negative prompts: what to exclude (text, watermarks).\n"
                    "- Iterate on one variable at a time."
                ),
                "exercise": {
                    "prompt": "Write a prompt for a hero image: specify "
                              "composition, style, and one exclusion.",
                    "check": {"groups": [["composition", "layout", "left",
                                           "center", "foreground", "background"],
                                          ["style", "minimal", "editorial",
                                           "photoreal", "illustration"],
                                          ["no ", "without", "exclude", "avoid"]],
                              "min_groups": 2},
                    "hint": "Say what's where, what it looks like, and what "
                            "to leave out.",
                },
            },
            {
                "id": "de-l3", "title": "Prototype in hours, not sprints",
                "skill": "Rapid AI prototyping", "est_minutes": 20,
                "body": (
                    "AI collapses prototype cost:\n"
                    "- Describe the flow; generate clickable mockups.\n"
                    "- Fake the backend with realistic sample data.\n"
                    "- Test with 3 users the same day; note what confused them.\n"
                    "- Prototype to learn, not to impress."
                ),
                "exercise": {
                    "prompt": "Outline a one-day prototype plan for testing "
                              "a new checkout flow.",
                    "check": {"keywords": ["mock", "test", "user", "flow",
                                           "feedback", "iterat"],
                              "min_ratio": 0.34},
                    "hint": "Build, test with users, learn — all in a day.",
                },
            },
            {
                "id": "de-l4", "title": "Research synthesis without fiction",
                "skill": "Research synthesis with AI", "est_minutes": 15,
                "body": (
                    "AI speeds synthesis; traceability keeps it honest:\n"
                    "- Feed it transcripts, ask for themes WITH quote citations.\n"
                    "- Verify every cited quote in the source.\n"
                    "- Treat uncited claims as hypotheses, not findings.\n"
                    "- You decide what it means — AI organizes, you interpret."
                ),
                "exercise": {
                    "prompt": "What two rules would you set for AI-assisted "
                              "interview synthesis?",
                    "check": {"keywords": ["cit", "quote", "verif", "source",
                                           "trace", "transcript"],
                              "min_ratio": 0.34},
                    "hint": "How do you keep every claim tied to real evidence?",
                },
            },
            {
                "id": "de-l5", "title": "Generate inside your design system",
                "skill": "Design-system-aware generation", "est_minutes": 10,
                "body": (
                    "Unconstrained AI makes pretty mush. Constrain it:\n"
                    "- Paste component names, tokens, and spacing rules into prompts.\n"
                    "- Ask for variants OF existing components, not new ones.\n"
                    "- Reject anything that invents a new pattern silently.\n"
                    "- Feed good outputs back as examples."
                ),
                "exercise": {
                    "prompt": "What would you include in a prompt so AI "
                              "generates a card variant matching your design system?",
                    "check": {"keywords": ["token", "component", "spacing",
                                           "color", "type", "variant", "system"],
                              "min_ratio": 0.29},
                    "hint": "Name the system pieces the output must respect.",
                },
            },
            _ethics_lesson("de"),
        ],
    },
    "marketing": {
        "label": "Marketer",
        "blurb": "More output, same voice — with verification built in.",
        "skills": [
            "AI-assisted copywriting",
            "Research & synthesis",
            "Content repurposing",
            "Brand voice control",
            "Campaign ideation",
            "AI ethics & disclosure",
        ],
        "diagnostic": [
            {
                "id": "mk-d1", "skill": "AI-assisted copywriting",
                "kind": "mcq",
                "prompt": "AI writes 50 blog drafts. Biggest quality risk?",
                "choices": [
                    "They're produced too fast",
                    "Generic, undifferentiated content that dilutes the brand",
                    "API cost",
                    "They're too long",
                ],
                "answer": 1,
                "why": "Undifferentiated AI content is a brand tax; voice "
                       "control is the job.",
            },
            {
                "id": "mk-d2", "skill": "Brand voice control", "kind": "mcq",
                "prompt": "To keep AI copy on-brand, you:",
                "choices": [
                    "Hope for the best",
                    "Provide voice examples and do-not-use lists in the prompt",
                    "Write everything in caps lock",
                    "Publish unedited",
                ],
                "answer": 1,
                "why": "Examples + constraints beat adjectives like \"bold\".",
            },
            {
                "id": "mk-d3", "skill": "Research & synthesis", "kind": "mcq",
                "prompt": "Before publishing AI-assisted research, you:",
                "choices": [
                    "Publish immediately",
                    "Verify claims against original sources",
                    "Add stock photos",
                    "Shorten it",
                ],
                "answer": 1,
                "why": "AI research is a draft until sources check out.",
            },
            {
                "id": "mk-d4", "skill": "Campaign ideation", "kind": "mcq",
                "prompt": "A good prompt for campaign angles includes:",
                "choices": [
                    "\"Make it go viral\"",
                    "Audience, offer, constraints, and examples",
                    "\"Be creative\"",
                    "Nothing — keep it open",
                ],
                "answer": 1,
                "why": "Specific inputs produce usable angles; vibes produce sludge.",
            },
            {
                "id": "mk-d5", "skill": "Content repurposing", "kind": "free",
                "prompt": "How would you use AI to turn one webinar into a "
                          "week of content without sounding robotic?",
                "concepts": ["edit", "voice", "repurpose", "human", "review",
                             "angle"],
                "why": "Strong answers show a pipeline: extract, adapt per "
                       "channel, human voice-check.",
            },
        ],
        "lessons": [
            {
                "id": "mk-l1", "title": "Copy in your voice, not the model's",
                "skill": "Brand voice control", "est_minutes": 15,
                "body": (
                    "Default AI copy sounds like everyone. Fix it:\n"
                    "- Paste 3 examples of your best copy into the prompt.\n"
                    "- Add a do-not-use word list (\"delve\", \"game-changer\"...).\n"
                    "- Specify audience and the one action they should take.\n"
                    "- Edit the output — AI drafts, you decide."
                ),
                "exercise": {
                    "prompt": "Write the voice section of a prompt: 2 voice "
                              "traits, 3 banned words, 1 audience.",
                    "check": {"keywords": ["audience", "tone", "voice", "ban",
                                           "avoid", "example"],
                              "min_ratio": 0.34},
                    "hint": "Traits + banned words + who it's for.",
                },
            },
            {
                "id": "mk-l2", "title": "Research synthesis at speed",
                "skill": "Research & synthesis", "est_minutes": 15,
                "body": (
                    "AI compresses research time:\n"
                    "- Dump sources in; ask for claims WITH citations.\n"
                    "- Verify the 3 most important claims yourself.\n"
                    "- Ask \"what's missing or one-sided here?\" to fight bias.\n"
                    "- Keep a source list — it's part of the deliverable."
                ),
                "exercise": {
                    "prompt": "What would you ask AI to do with 10 competitor "
                              "blog posts before writing your own?",
                    "check": {"keywords": ["summar", "theme", "gap", "angle",
                                           "cit", "verif"],
                              "min_ratio": 0.34},
                    "hint": "Themes, gaps, and what to verify.",
                },
            },
            {
                "id": "mk-l3", "title": "One webinar, a week of content",
                "skill": "Content repurposing", "est_minutes": 15,
                "body": (
                    "Repurposing pipeline:\n"
                    "- Transcript -> 5 key moments with timestamps.\n"
                    "- Each moment -> channel-native format (thread, short, "
                    "carousel, email).\n"
                    "- Rewrite per channel; don't cross-post verbatim.\n"
                    "- Human pass on every piece before scheduling."
                ),
                "exercise": {
                    "prompt": "List 4 formats you'd repurpose one webinar "
                              "into, with one line on each angle.",
                    "check": {"keywords": ["thread", "short", "video",
                                           "carousel", "email", "post",
                                           "clip", "blog"],
                              "min_ratio": 0.25},
                    "hint": "Think channel-native: what fits where?",
                },
            },
            {
                "id": "mk-l4", "title": "Campaign ideation that isn't sludge",
                "skill": "Campaign ideation", "est_minutes": 10,
                "body": (
                    "AI ideation works with tight briefs:\n"
                    "- Audience, offer, constraint, examples — then \"give me 10\".\n"
                    "- Force variety: \"each angle must differ in emotion\".\n"
                    "- Kill 8, develop 2. Your judgment is the product.\n"
                    "- Test the survivors small before scaling."
                ),
                "exercise": {
                    "prompt": "Write a tight brief for 10 campaign angles "
                              "for a product launch.",
                    "check": {"keywords": ["audience", "offer", "constraint",
                                           "example", "10", "ten"],
                              "min_ratio": 0.34},
                    "hint": "Who, what, limits, examples — then the ask.",
                },
            },
            {
                "id": "mk-l5", "title": "Quality control for AI content",
                "skill": "AI-assisted copywriting", "est_minutes": 10,
                "body": (
                    "Ship a checklist, not hope:\n"
                    "- Facts verified against sources.\n"
                    "- Voice check: would we say this out loud?\n"
                    "- No banned words; CTA present and correct.\n"
                    "- One human signs off — AI never publishes alone."
                ),
                "exercise": {
                    "prompt": "Write a 4-item pre-publish checklist for "
                              "AI-assisted posts.",
                    "check": {"keywords": ["fact", "verif", "voice", "cta",
                                           "human", "review", "brand"],
                              "min_ratio": 0.29},
                    "hint": "Facts, voice, CTA, human sign-off.",
                },
            },
            _ethics_lesson("mk"),
        ],
    },
    "sales": {
        "label": "Sales",
        "blurb": "Sharper prep, personal outreach, no hallucinated facts.",
        "skills": [
            "AI prospect research",
            "Personalized outreach",
            "Call preparation",
            "Follow-up drafting",
            "CRM hygiene with AI",
            "AI ethics & disclosure",
        ],
        "diagnostic": [
            {
                "id": "sa-d1", "skill": "Personalized outreach", "kind": "mcq",
                "prompt": "AI-personalized outreach works best when:",
                "choices": [
                    "Fully automated at maximum volume",
                    "AI drafts from real research, and a human reviews before send",
                    "No research — speed wins",
                    "Sent as mass blasts",
                ],
                "answer": 1,
                "why": "Research + human review beats both generic blasts "
                       "and unsupervised automation.",
            },
            {
                "id": "sa-d2", "skill": "Call preparation", "kind": "mcq",
                "prompt": "Before a discovery call, AI can best:",
                "choices": [
                    "Replace you on the call",
                    "Summarize the prospect's 10-K and news into bullets plus question ideas",
                    "Guarantee the close",
                    "Dial the phone",
                ],
                "answer": 1,
                "why": "AI compresses prep; you still run the conversation.",
            },
            {
                "id": "sa-d3", "skill": "Follow-up drafting", "kind": "mcq",
                "prompt": "Biggest risk of AI-written follow-ups:",
                "choices": [
                    "There is none",
                    "Hallucinated details about the prospect — verify every fact",
                    "They're too short",
                    "They're too polite",
                ],
                "answer": 1,
                "why": "A wrong \"fact\" about the prospect kills trust instantly.",
            },
            {
                "id": "sa-d4", "skill": "AI prospect research", "kind": "mcq",
                "prompt": "Best prompt for account research:",
                "choices": [
                    "\"Tell me about Acme\"",
                    "\"Summarize Acme's latest earnings and 3 pain points relevant to [product], with sources\"",
                    "\"Write a poem about Acme\"",
                    "\"Guess Acme's revenue\"",
                ],
                "answer": 1,
                "why": "Specific asks + sources = usable intel; vague asks "
                       "= fiction risk.",
            },
            {
                "id": "sa-d5", "skill": "Call preparation", "kind": "free",
                "prompt": "Describe your AI-assisted prep routine the night "
                          "before a big demo.",
                "concepts": ["research", "notes", "questions", "review",
                             "practice"],
                "why": "Strong answers show research, distilled notes, and "
                       "prepared questions — not winging it.",
            },
        ],
        "lessons": [
            {
                "id": "sa-l1", "title": "Prospect research in 10 minutes",
                "skill": "AI prospect research", "est_minutes": 15,
                "body": (
                    "Deep-enough research, fast:\n"
                    "- Prompt: earnings summary + pains relevant to your "
                    "product + sources.\n"
                    "- Pull recent news, leadership changes, tech stack hints.\n"
                    "- Output: 5 bullets you can actually use on a call.\n"
                    "- Verify anything you'll say out loud."
                ),
                "exercise": {
                    "prompt": "Write the research prompt you'd use for a new "
                              "target account.",
                    "check": {"keywords": ["source", "earning", "pain",
                                           "news", "bullet", "summar"],
                              "min_ratio": 0.34},
                    "hint": "What intel, with what proof?",
                },
            },
            {
                "id": "sa-l2", "title": "Personalization that isn't creepy",
                "skill": "Personalized outreach", "est_minutes": 15,
                "body": (
                    "Good personalization:\n"
                    "- One real observation (their post, launch, hire).\n"
                    "- Tied to a plausible pain — not a stretch.\n"
                    "- Short. One ask.\n"
                    "- Human review before send, always."
                ),
                "exercise": {
                    "prompt": "Draft a 3-sentence cold email using one real "
                              "observation about a prospect.",
                    "check": {"keywords": ["you", "noticed", "saw", "congrats",
                                           "launch", "post"],
                              "min_ratio": 0.34},
                    "hint": "Observation, pain tie-in, one ask.",
                },
            },
            {
                "id": "sa-l3", "title": "Call prep that wins",
                "skill": "Call preparation", "est_minutes": 15,
                "body": (
                    "Night-before routine:\n"
                    "- AI brief: company, news, likely pains, tech stack.\n"
                    "- Draft 5 discovery questions tied to those pains.\n"
                    "- Prepare 2 customer stories that mirror their situation.\n"
                    "- Rehearse the first 2 minutes out loud."
                ),
                "exercise": {
                    "prompt": "List what goes into your one-page AI call "
                              "brief.",
                    "check": {"keywords": ["news", "pain", "question",
                                           "story", "stakeholder", "goal"],
                              "min_ratio": 0.34},
                    "hint": "Intel, questions, stories — one page.",
                },
            },
            {
                "id": "sa-l4", "title": "Follow-ups people answer",
                "skill": "Follow-up drafting", "est_minutes": 10,
                "body": (
                    "AI drafts, you verify:\n"
                    "- Reference something real from the call (notes, not memory).\n"
                    "- One clear next step with a date.\n"
                    "- Check every prospect fact before sending.\n"
                    "- Short beats thorough."
                ),
                "exercise": {
                    "prompt": "What must you verify before sending an "
                              "AI-drafted follow-up?",
                    "check": {"keywords": ["fact", "verif", "name", "check",
                                           "accur", "true"],
                              "min_ratio": 0.34},
                    "hint": "What could the AI have gotten wrong?",
                },
            },
            {
                "id": "sa-l5", "title": "CRM hygiene with AI",
                "skill": "CRM hygiene with AI", "est_minutes": 10,
                "body": (
                    "AI makes CRM upkeep painless:\n"
                    "- Summarize call notes into fields right after the call.\n"
                    "- Draft next-step tasks from the summary.\n"
                    "- Flag stale deals with no activity.\n"
                    "- You approve what gets written — AI doesn't own the record."
                ),
                "exercise": {
                    "prompt": "Describe your end-of-day AI CRM routine in "
                              "two sentences.",
                    "check": {"keywords": ["summar", "notes", "task",
                                           "next", "review", "approv"],
                              "min_ratio": 0.34},
                    "hint": "Notes in, tasks out, you approve.",
                },
            },
            _ethics_lesson("sa"),
        ],
    },
    "generalist": {
        "label": "Generalist",
        "blurb": "Everyday AI leverage: research, write, analyze, automate.",
        "skills": [
            "Prompting fundamentals",
            "AI-assisted research",
            "AI-assisted writing",
            "Document analysis",
            "Everyday automation",
            "AI ethics & disclosure",
        ],
        "diagnostic": [
            {
                "id": "ge-d1", "skill": "Prompting fundamentals",
                "kind": "mcq",
                "prompt": "Which prompt gets the best result?",
                "choices": [
                    "\"Write about marketing\"",
                    "\"Draft a 150-word LinkedIn post for a fintech launch, confident tone, no hype words\"",
                    "\"Do my job\"",
                    "\"Hello\"",
                ],
                "answer": 1,
                "why": "Format + length + audience + tone constraints beat "
                       "vague asks.",
            },
            {
                "id": "ge-d2", "skill": "AI-assisted research", "kind": "mcq",
                "prompt": "The AI gives you a fact for a report. You:",
                "choices": [
                    "Paste it in",
                    "Verify it against a source before using",
                    "Reword it so it looks original",
                    "Trust it because the answer was long",
                ],
                "answer": 1,
                "why": "Unverified AI facts are rumors with good grammar.",
            },
            {
                "id": "ge-d3", "skill": "Document analysis", "kind": "mcq",
                "prompt": "To summarize a 40-page PDF well, you:",
                "choices": [
                    "Paste it and say \"summarize\"",
                    "Ask for structure — key decisions, owners, deadlines — then spot-check",
                    "Read it yourself and skip the AI",
                    "Ask for a poem about it",
                ],
                "answer": 1,
                "why": "Structured asks + spot-checks beat generic summaries.",
            },
            {
                "id": "ge-d4", "skill": "Prompting fundamentals", "kind": "mcq",
                "prompt": "\"Temperature\" in AI settings controls:",
                "choices": [
                    "Room temperature",
                    "Randomness/creativity of the outputs",
                    "Response speed",
                    "Price per request",
                ],
                "answer": 1,
                "why": "Low temperature = focused; high = creative/risky.",
            },
            {
                "id": "ge-d5", "skill": "Everyday automation", "kind": "free",
                "prompt": "Name one task in your week you'd hand to AI, "
                          "and how you'd check its work.",
                "concepts": ["check", "verify", "review", "edit"],
                "why": "Strong answers pair delegation with a verification step.",
            },
        ],
        "lessons": [
            {
                "id": "ge-l1", "title": "Prompts that actually work",
                "skill": "Prompting fundamentals", "est_minutes": 15,
                "body": (
                    "The 4-part prompt:\n"
                    "- Role/context: who the AI is helping and why.\n"
                    "- Task: the one thing to produce.\n"
                    "- Constraints: length, format, tone, what to avoid.\n"
                    "- Example: one sample of good output beats paragraphs "
                    "of description."
                ),
                "exercise": {
                    "prompt": "Rewrite this bad prompt using the 4-part "
                              "structure: \"Write about our product.\"",
                    "check": {"keywords": ["audience", "word", "tone",
                                           "format", "example"],
                              "min_ratio": 0.4},
                    "hint": "Add context, constraints, and an example.",
                },
            },
            {
                "id": "ge-l2", "title": "Research without the rumors",
                "skill": "AI-assisted research", "est_minutes": 15,
                "body": (
                    "AI research workflow:\n"
                    "- Ask for claims WITH sources, not just answers.\n"
                    "- Verify the 2-3 claims that matter most.\n"
                    "- Ask \"what would contradict this?\" for balance.\n"
                    "- Keep the source list with your notes."
                ),
                "exercise": {
                    "prompt": "What do you ask for alongside the answer when "
                              "researching with AI?",
                    "check": {"keywords": ["source", "cit", "link", "verif",
                                           "evidence"],
                              "min_ratio": 0.4},
                    "hint": "What lets you check the work?",
                },
            },
            {
                "id": "ge-l3", "title": "Writing with AI, in your voice",
                "skill": "AI-assisted writing", "est_minutes": 15,
                "body": (
                    "Draft fast, sound like you:\n"
                    "- Give a rough outline or bullet dump first — AI "
                    "expands, you direct.\n"
                    "- Specify tone with examples, not adjectives.\n"
                    "- Edit the draft: cut 20%, add one specific detail.\n"
                    "- Read it aloud; fix anything you wouldn't say."
                ),
                "exercise": {
                    "prompt": "Describe your edit pass on an AI draft in "
                              "two sentences.",
                    "check": {"keywords": ["edit", "cut", "voice", "read",
                                           "specific", "tone"],
                              "min_ratio": 0.34},
                    "hint": "What do you change to make it yours?",
                },
            },
            {
                "id": "ge-l4", "title": "Tame long documents",
                "skill": "Document analysis", "est_minutes": 15,
                "body": (
                    "Long docs, fast:\n"
                    "- Ask for decisions, owners, and deadlines first.\n"
                    "- Then: \"what did it NOT decide?\" — gaps matter.\n"
                    "- Spot-check 2 sections against the summary.\n"
                    "- For contracts/legal: AI triages, a human (or lawyer) decides."
                ),
                "exercise": {
                    "prompt": "What three things do you ask for when "
                              "summarizing a long meeting doc?",
                    "check": {"keywords": ["decision", "owner", "deadline",
                                           "action", "next"],
                              "min_ratio": 0.4},
                    "hint": "What was decided, by whom, by when?",
                },
            },
            {
                "id": "ge-l5", "title": "Automate the boring parts",
                "skill": "Everyday automation", "est_minutes": 15,
                "body": (
                    "Start small:\n"
                    "- Pick one repetitive weekly task (status updates, "
                    "meeting notes, data cleanup).\n"
                    "- Build the AI step, keep a human checkpoint.\n"
                    "- Measure: time saved per week.\n"
                    "- Expand only what survives contact with reality."
                ),
                "exercise": {
                    "prompt": "Name one weekly task you'd automate with AI "
                              "and where the human checkpoint goes.",
                    "check": {"groups": [["automat", "ai", "draft", "generat"],
                                          ["check", "review", "approv",
                                           "human", "verif"]],
                              "min_groups": 2},
                    "hint": "The task, plus where a human looks before it ships.",
                },
            },
            _ethics_lesson("ge"),
        ],
    },
}

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _load_store() -> dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"tracks": {}, "completions": [], "sessions": {}}
    if not isinstance(data, dict):
        return {"tracks": {}, "completions": [], "sessions": {}}
    for key in ("tracks", "completions", "sessions"):
        if key == "completions":
            if not isinstance(data.get(key), list):
                data[key] = []
        elif not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _save_store(store: dict[str, Any]) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(store, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STORE_PATH)


def _track_or_error(track: str) -> dict[str, Any] | dict[str, str]:
    if track not in TRACKS:
        return {"error": f"Unknown track {track!r}. Choose from: "
                         f"{', '.join(sorted(TRACKS))}."}
    return TRACKS[track]


def _lesson_or_error(track: str, lesson_id: str) -> dict[str, Any]:
    t = _track_or_error(track)
    if "error" in t:
        return t
    for lesson in t["lessons"]:
        if lesson["id"] == lesson_id:
            return lesson
    return {"error": f"Unknown lesson {lesson_id!r} in track {track!r}."}


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


def _norm_mcq_answer(answer: Any, n_choices: int) -> int | None:
    """Accept an int index or a letter like 'B'."""
    if isinstance(answer, bool):
        return None
    if isinstance(answer, int):
        return answer if 0 <= answer < n_choices else None
    if isinstance(answer, str):
        s = answer.strip().upper()
        if len(s) == 1 and "A" <= s <= "Z":
            idx = ord(s) - ord("A")
            return idx if idx < n_choices else None
    return None


def _free_text_score(concepts: list[str], text: str) -> tuple[float, list[str], list[str]]:
    text_l = text.lower()
    matched = [c for c in concepts if c.lower() in text_l]
    missed = [c for c in concepts if c not in matched]
    ratio = len(matched) / len(concepts) if concepts else 0.0
    return ratio, matched, missed


def diagnose(track: str, answers: list[Any]) -> dict[str, Any]:
    """Score a 5-question diagnostic.

    Args:
        track: Track id from TRACKS.
        answers: One answer per diagnostic question, in order. MCQ
            answers are int indexes (or letters "A"-"D"); the free-text
            answer is a string.

    Returns:
        {"track", "level", "score", "max", "per_question", "gaps"}.
        ``gaps`` lists skills to work on, each mapped to a lesson.
    """
    t = _track_or_error(track)
    if "error" in t:
        return t
    questions = t["diagnostic"]
    if len(answers) != len(questions):
        return {"error": f"Expected {len(questions)} answers, got {len(answers)}."}

    per_question: list[dict[str, Any]] = []
    total = 0.0
    for q, ans in zip(questions, answers):
        if q["kind"] == "mcq":
            picked = _norm_mcq_answer(ans, len(q["choices"]))
            correct = picked is not None and picked == q["answer"]
            pts = 1.0 if correct else 0.0
            per_question.append({
                "id": q["id"], "skill": q["skill"], "correct": correct,
                "points": pts,
                "explanation": q["why"],
            })
        else:
            ratio, matched, missed = _free_text_score(
                q["concepts"], str(ans or ""))
            pts = round(ratio, 2)
            per_question.append({
                "id": q["id"], "skill": q["skill"],
                "correct": ratio >= 0.6, "points": pts,
                "matched_concepts": matched, "missed_concepts": missed,
                "explanation": q["why"],
            })
        total += pts

    total = round(total, 2)
    if total >= 3.5:
        level = "advanced"
    elif total >= 2.0:
        level = "practitioner"
    else:
        level = "foundations"

    gaps: list[dict[str, Any]] = []
    seen_skills: set[str] = set()
    for pq in per_question:
        if pq["points"] < 0.5 and pq["skill"] not in seen_skills:
            seen_skills.add(pq["skill"])
            lesson_id = next(
                (ls["id"] for ls in t["lessons"] if ls["skill"] == pq["skill"]),
                t["lessons"][0]["id"],
            )
            gaps.append({"skill": pq["skill"], "lesson_id": lesson_id})

    store = _load_store()
    tstate = store["tracks"].setdefault(track, {})
    tstate["level"] = level
    tstate["diagnosed_at"] = datetime.now(timezone.utc).isoformat()
    _save_store(store)

    log.info("diagnosed %s: %s (%.2f/5)", track, level, total)
    return {
        "track": track, "level": level, "score": total, "max": 5,
        "per_question": per_question, "gaps": gaps,
    }


def list_tracks() -> list[dict[str, Any]]:
    """All tracks with labels, blurbs, and skill lists."""
    return [
        {"id": tid, "label": t["label"], "blurb": t["blurb"],
         "skills": list(t["skills"]), "lessons": len(t["lessons"])}
        for tid, t in TRACKS.items()
    ]


# ---------------------------------------------------------------------------
# Learning plan
# ---------------------------------------------------------------------------


_START_HERE = {"foundations": 0, "practitioner": 2, "advanced": 4}


def plan(track: str, level: str | None = None) -> dict[str, Any]:
    """Ordered learning plan for a track.

    Args:
        track: Track id. level: foundations|practitioner|advanced; when
            None, uses the stored diagnosis or defaults to foundations.

    Returns:
        {"track", "level", "start_here", "lessons": [{id,title,skill,
        est_minutes,done}]}.
    """
    t = _track_or_error(track)
    if "error" in t:
        return t
    if level is None:
        level = _load_store()["tracks"].get(track, {}).get("level",
                                                            "foundations")
    if level not in LEVELS:
        return {"error": f"Unknown level {level!r}. Use: {', '.join(LEVELS)}."}
    done = set(
        _load_store()["tracks"].get(track, {}).get("completed", []))
    lessons = [
        {"id": ls["id"], "title": ls["title"], "skill": ls["skill"],
         "est_minutes": ls.get("est_minutes", 15),
         "done": ls["id"] in done}
        for ls in t["lessons"]
    ]
    start = min(_START_HERE[level], len(lessons) - 1)
    return {"track": track, "level": level, "start_here": start,
            "lessons": lessons}


def get_lesson(track: str, lesson_id: str) -> dict[str, Any]:
    """Full lesson content (body + exercise prompt, no answer key)."""
    lesson = _lesson_or_error(track, lesson_id)
    if "error" in lesson:
        return lesson
    return {
        "track": track, "id": lesson["id"], "title": lesson["title"],
        "skill": lesson["skill"], "est_minutes": lesson.get("est_minutes", 15),
        "body": lesson["body"],
        "exercise_prompt": lesson["exercise"]["prompt"],
        "hint": lesson["exercise"]["hint"],
    }


# ---------------------------------------------------------------------------
# Exercises
# ---------------------------------------------------------------------------


def _eval_check(spec: dict[str, Any], response: str) -> dict[str, Any]:
    """Deterministic exercise check. Never reveals the full answer key."""
    text = (response or "").lower()
    words = len(text.split())
    if "groups" in spec:
        group_hits: list[list[str]] = []
        for group in spec["groups"]:
            group_hits.append([kw for kw in group if kw.lower() in text])
        passed_groups = sum(1 for h in group_hits if h)
        need = spec.get("min_groups", len(spec["groups"]))
        passed = passed_groups >= need
        matched = [kw for h in group_hits for kw in h]
        missed = [g for g, h in zip(spec["groups"], group_hits) if not h]
        return {"passed": passed,
                "score": round(passed_groups / len(spec["groups"]), 2),
                "matched": matched, "missed": missed, "words": words}
    keywords = spec.get("keywords", [])
    matched = [kw for kw in keywords if kw.lower() in text]
    missed = [kw for kw in keywords if kw not in matched]
    ratio = len(matched) / len(keywords) if keywords else 0.0
    min_words = spec.get("min_words", 0)
    passed = ratio >= spec.get("min_ratio", 0.5) and words >= min_words
    return {"passed": passed, "score": round(ratio, 2),
            "matched": matched, "missed": missed, "words": words}


def exercise(track: str, lesson_id: str) -> dict[str, Any]:
    """Start an exercise session for a lesson.

    Returns {"session_id", "track", "lesson_id", "title",
    "exercise_prompt", "hint"}. The check rubric stays server-side.
    """
    lesson = _lesson_or_error(track, lesson_id)
    if "error" in lesson:
        return lesson
    session_id = uuid.uuid4().hex[:12]
    store = _load_store()
    store["sessions"][session_id] = {
        "track": track, "lesson_id": lesson_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "submitted": False,
    }
    _save_store(store)
    return {
        "session_id": session_id, "track": track, "lesson_id": lesson_id,
        "title": lesson["title"],
        "exercise_prompt": lesson["exercise"]["prompt"],
        "hint": lesson["exercise"]["hint"],
    }


def submit_exercise(session_id: str, response: str) -> dict[str, Any]:
    """Check an exercise response deterministically.

    Returns {"passed", "score", "feedback", "matched", "missed_concepts",
    "next_lesson"}. Passing records the lesson as complete.
    """
    store = _load_store()
    session = store["sessions"].get(session_id)
    if not session:
        return {"error": f"Unknown session {session_id!r}. Start one with "
                         "the exercise tool first."}
    if session.get("submitted"):
        return {"error": "This exercise was already submitted."}
    lesson = _lesson_or_error(session["track"], session["lesson_id"])
    if "error" in lesson:
        return lesson

    check = _eval_check(lesson["exercise"]["check"], response)
    session["submitted"] = True
    session["score"] = check["score"]
    session["passed"] = check["passed"]

    track = session["track"]
    lessons = TRACKS[track]["lessons"]
    idx = next(i for i, ls in enumerate(lessons)
               if ls["id"] == session["lesson_id"])
    next_lesson = lessons[idx + 1]["id"] if idx + 1 < len(lessons) else None

    if check["passed"]:
        tstate = store["tracks"].setdefault(track, {})
        completed = tstate.setdefault("completed", [])
        if session["lesson_id"] not in completed:
            completed.append(session["lesson_id"])
        store["completions"].append({
            "track": track, "lesson_id": session["lesson_id"],
            "ts": datetime.now(timezone.utc).isoformat(),
            "score": check["score"],
        })
        feedback = (
            f"Passed ({check['score']:.0%} concept coverage). "
            f"{'Next up: ' + next_lesson if next_lesson else 'Track complete — nice work.'}"
        )
    else:
        missed = check["missed"]
        if missed and isinstance(missed[0], list):
            hint_bits = "; ".join("one of: " + ", ".join(g[:3]) for g in missed)
        else:
            hint_bits = ", ".join(missed[:4]) if missed else "more detail"
        feedback = (
            f"Not quite ({check['score']:.0%} concept coverage). Try again "
            f"— think about: {hint_bits}. Hint: {lesson['exercise']['hint']}"
        )
    _save_store(store)
    log.info("exercise %s submitted: passed=%s", session_id, check["passed"])
    return {
        "session_id": session_id, "track": track,
        "lesson_id": session["lesson_id"], "passed": check["passed"],
        "score": check["score"], "feedback": feedback,
        "matched": check["matched"], "next_lesson": next_lesson,
    }


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

_PRACTICE_NOTE = (
    "This log records practice activity only — lessons attempted and "
    "completed. It is not a credential, certification, or verified skill "
    "assessment. Cite completed practice honestly; never present it as a "
    "certificate."
)


def _streak_days(completions: list[dict[str, Any]]) -> int:
    days = sorted(
        {c["ts"][:10] for c in completions if c.get("ts")}, reverse=True)
    if not days:
        return 0
    today = date.today().isoformat()
    # Streak counts back from today; allow it to start yesterday (not yet
    # practiced today).
    cursor = today if days[0] == today else _shift(today, -1)
    streak = 0
    for d in days:
        if d == cursor:
            streak += 1
            cursor = _shift(cursor, -1)
        elif d < cursor:
            break
    return streak


def _shift(day_iso: str, delta: int) -> str:
    y, m, d = (int(x) for x in day_iso.split("-"))
    from datetime import timedelta
    return (date(y, m, d) + timedelta(days=delta)).isoformat()


def progress() -> dict[str, Any]:
    """Practice progress across tracks: levels, completions, streak."""
    store = _load_store()
    completions = store["completions"]
    streak = _streak_days(completions)
    tracks: dict[str, Any] = {}
    for tid, t in TRACKS.items():
        tstate = store["tracks"].get(tid, {})
        completed = tstate.get("completed", [])
        last = max(
            (c["ts"] for c in completions if c.get("track") == tid),
            default=None,
        )
        tracks[tid] = {
            "label": t["label"],
            "level": tstate.get("level"),
            "diagnosed": bool(tstate.get("level")),
            "completed": completed,
            "completed_count": len(completed),
            "total_lessons": len(t["lessons"]),
            "last_practice": last,
        }
    return {
        "tracks": tracks,
        "total_completed": sum(v["completed_count"] for v in tracks.values()),
        "streak_days": streak,
        "note": _PRACTICE_NOTE,
    }


# ---------------------------------------------------------------------------
# Initiative 06 — AI proficiency lab: ethics, evaluation, failure analysis
# ---------------------------------------------------------------------------
#
# ``lab_tracks()`` lists role tracks and their exercise types.
# ``lab_exercise(track, exercise_type)`` opens a session (brief + task
# disclosed; the check spec stays server-side).
# ``submit_lab_exercise(session_id, response)`` checks the response
# deterministically. Passing records a lab completion (practice only, not
# a formal qualification) and appends to the longitudinal practice
# history (best-effort; submitting never breaks if the store is
# unavailable).

#: Tests redirect the longitudinal history here; None = default path.
_LAB_LONGITUDINAL_HISTORY_PATH: Path | None = None


def lab_tracks() -> list[dict[str, Any]]:
    """Role tracks offering lab exercises (ethics / evaluation /
    failure_analysis per track)."""
    return _i06_ai_lab.lab_tracks()


def lab_exercise(track: str, exercise_type: str) -> dict[str, Any]:
    """Start a lab exercise session.

    Args:
        track: Role track, e.g. "engineer".
        exercise_type: "ethics" | "evaluation" | "failure_analysis".

    Returns {"session_id", "track", "exercise_type", "brief", "task",
    "good_looks_like", "min_words"} — the check rubric stays
    server-side. Submit the response via submit_lab_exercise.
    """
    try:
        shown = _i06_ai_lab.get_lab_exercise(track, exercise_type)
    except ValueError as exc:
        return {"error": str(exc)}
    session_id = "lab_" + uuid.uuid4().hex[:10]
    store = _load_store()
    store.setdefault("lab_sessions", {})[session_id] = {
        "track": shown["track"],
        "exercise_type": shown["exercise_type"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "submitted": False,
    }
    _save_store(store)
    return {"session_id": session_id, **shown}


def submit_lab_exercise(
    session_id: str,
    response: str,
    input_modality: str = "text",
) -> dict[str, Any]:
    """Check a lab exercise response deterministically.

    Args:
        session_id: From lab_exercise.
        response: The learner's response text.
        input_modality: "text" (default) or "voice" — recorded per
            answer for the privacy ledger.

    Returns {"passed", "score", "feedback", "groups_hit",
    "groups_total", "input_modality"}. Passing records a lab
    completion (practice logged, not a credential).
    """
    store = _load_store()
    session = store.setdefault("lab_sessions", {}).get(session_id)
    if not session:
        return {"error": f"Unknown lab session {session_id!r}. Start one "
                         "with the lab exercise tool first."}
    if session.get("submitted"):
        return {"error": "This lab exercise was already submitted."}
    check = _i06_ai_lab.check_lab_exercise(
        session["track"], session["exercise_type"], response)
    session["submitted"] = True
    session["score"] = check["score"]
    session["passed"] = check["passed"]
    session["input_modality"] = input_modality or "text"

    if check["passed"]:
        store["completions"].append({
            "track": session["track"],
            "lesson_id": f"lab:{session['exercise_type']}",
            "kind": "lab",
            "ts": datetime.now(timezone.utc).isoformat(),
            "score": check["score"],
        })
        feedback = (
            f"Passed ({check['groups_hit']}/{check['groups_total']} "
            f"concept groups, {check['words']} words). Logged as "
            f"practice — not a credential.")
    else:
        feedback = (
            f"Not quite ({check['groups_hit']}/{check['groups_total']} "
            f"concept groups). Needs >= "
            f"{_i06_ai_lab.EXERCISES[session['track']][session['exercise_type']]['check']['min_groups']} "
            f"groups and {check['min_words']}+ words — revisit the task "
            f"prompt and cover more of the considerations.")
    _save_store(store)
    _record_lab_longitudinal(session)
    log.info("lab exercise %s submitted: passed=%s", session_id,
             check["passed"])
    return {
        "session_id": session_id,
        "track": session["track"],
        "exercise_type": session["exercise_type"],
        "passed": check["passed"],
        "score": check["score"],
        "feedback": feedback,
        "groups_hit": check["groups_hit"],
        "groups_total": check["groups_total"],
        "input_modality": session["input_modality"],
    }


def _record_lab_longitudinal(session: dict[str, Any]) -> None:
    """Best-effort longitudinal record; never raises."""
    try:
        _i06_longitudinal.record_result(
            source="ai_lab",
            label=f"{session['track']}:{session['exercise_type']}",
            dimension_scores={"evidence": round(session["score"] * 100),
                              "clarity": None, "structure": None,
                              "trade_offs": None,
                              "question_quality": None},
            overall=round(session["score"] * 100),
            mode=session["exercise_type"],
            session_id="",
            history_path=_LAB_LONGITUDINAL_HISTORY_PATH,
        )
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        log.warning("ai-lab longitudinal record failed: %s", exc)


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the AI-proficiency MCP tools."""
    _impl = globals()

    @mcp.tool()
    def ai_tracks() -> list[dict[str, Any]]:
        """List the AI-proficiency training tracks (engineer, product, data, design, marketing, sales, generalist) with their skills."""
        return _impl["list_tracks"]()

    @mcp.tool()
    def ai_diagnose(track: str) -> dict[str, Any]:
        """Start the 5-question diagnostic for a track.

        Args:
            track: Track id, e.g. "engineer".

        Returns:
            The diagnostic questions (choices included, answer key withheld).
            Collect answers in order and submit via ai_submit_diagnosis.
        """
        t = _impl["_track_or_error"](track)
        if "error" in t:
            return t
        return {
            "track": track,
            "questions": [
                {k: q[k] for k in ("id", "skill", "kind", "prompt",
                                   "choices" if q["kind"] == "mcq" else "id")
                 if k in q}
                for q in t["diagnostic"]
            ],
            "instructions": "Answer the 4 multiple-choice questions with the "
                            "choice letter or index, then the free-text "
                            "question in 2-3 sentences, in order.",
        }

    @mcp.tool()
    def ai_submit_diagnosis(track: str, answers: list[Any]) -> dict[str, Any]:
        """Score diagnostic answers -> level (foundations/practitioner/advanced) + skill gaps.

        Args:
            track: Track id. answers: One answer per question, in order
                (choice letter/index for MCQs, free text for the last).
        """
        return _impl["diagnose"](track, answers)

    @mcp.tool()
    def ai_plan(track: str, level: str | None = None) -> dict[str, Any]:
        """Ordered learning plan for a track: lessons with hands-on exercises.

        Args:
            track: Track id. level: foundations|practitioner|advanced;
                defaults to the stored diagnosis.
        """
        return _impl["plan"](track, level)

    @mcp.tool()
    def ai_lesson(track: str, lesson_id: str) -> dict[str, Any]:
        """Full lesson content for a track lesson.

        Args:
            track: Track id. lesson_id: Lesson id from ai_plan.
        """
        return _impl["get_lesson"](track, lesson_id)

    @mcp.tool()
    def ai_exercise(track: str, lesson_id: str) -> dict[str, Any]:
        """Start a hands-on exercise session for a lesson.

        Args:
            track: Track id. lesson_id: Lesson id from ai_plan.

        Returns:
            {"session_id", "exercise_prompt", "hint", ...}. Submit the
            learner's response via ai_submit_exercise.
        """
        return _impl["exercise"](track, lesson_id)

    @mcp.tool()
    def ai_submit_exercise(session_id: str, response: str) -> dict[str, Any]:
        """Check an exercise response deterministically and give feedback.

        Args:
            session_id: From ai_exercise. response: The learner's answer.

        Returns:
            {"passed", "score", "feedback", "next_lesson"}. Passing records
            the lesson as complete (practice logged, not a credential).
        """
        return _impl["submit_exercise"](session_id, response)

    @mcp.tool()
    def ai_progress() -> dict[str, Any]:
        """Practice progress: per-track levels, lessons completed, streak days.

        This logs practice activity only — it is not a credential or
        certification.
        """
        return _impl["progress"]()

    @mcp.tool()
    def ai_lab_tracks() -> list[dict[str, Any]]:
        """Role tracks offering practical lab exercises (ethics,
        evaluation, failure_analysis per track)."""
        return _impl["lab_tracks"]()

    @mcp.tool()
    def ai_lab_exercise(track: str, exercise_type: str) -> dict[str, Any]:
        """Start a practical lab exercise for a role track.

        Args:
            track: Role track, e.g. "engineer". exercise_type:
                "ethics" | "evaluation" | "failure_analysis".

        Returns:
            {"session_id", "brief", "task", "good_looks_like",
            "min_words"}. Submit the learner's response via
            ai_lab_submit.
        """
        return _impl["lab_exercise"](track, exercise_type)

    @mcp.tool()
    def ai_lab_submit(session_id: str, response: str,
                      input_modality: str = "text") -> dict[str, Any]:
        """Check a lab exercise response deterministically.

        Args:
            session_id: From ai_lab_exercise. response: The learner's
                answer. input_modality: "text" (default) or "voice".

        Returns:
            {"passed", "score", "feedback"}. Passing records a lab
            completion (practice logged, not a credential).
        """
        return _impl["submit_lab_exercise"](
            session_id, response, input_modality=input_modality)


def _print_json_or(data: Any, as_json: bool, fallback: str = "") -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(fallback or json.dumps(data, indent=2, ensure_ascii=False))


def cmd_ai_skills(args: Any) -> int:
    """CLI handler for `ai-skills`."""
    as_json = getattr(args, "json", False)
    action = args.action

    if action == "tracks":
        tracks = list_tracks()
        if as_json:
            print(json.dumps(tracks, indent=2))
        else:
            for t in tracks:
                print(f"{t['id']:12} {t['label']} — {t['blurb']}")
        return 0

    if action == "diagnose":
        # diagnose() needs answers; here we only present the questions.
        t = _track_or_error(args.track)
        if "error" in t:
            print(t["error"])
            return 1
        if as_json:
            print(json.dumps(
                [{"id": q["id"], "skill": q["skill"], "kind": q["kind"],
                  "prompt": q["prompt"],
                  **({"choices": q["choices"]} if q["kind"] == "mcq" else {})}
                 for q in t["diagnostic"]], indent=2))
        else:
            for i, q in enumerate(t["diagnostic"], 1):
                print(f"Q{i} [{q['skill']}] {q['prompt']}")
                if q["kind"] == "mcq":
                    for j, c in enumerate(q["choices"]):
                        print(f"   {chr(65 + j)}. {c}")
                else:
                    print("   (free text, 2-3 sentences)")
                print()
            print("Submit with: ai-skills answer "
                  f"{args.track} --answers '[...]'")
        return 0

    if action == "answer":
        try:
            answers = json.loads(args.answers)
        except (json.JSONDecodeError, TypeError):
            print("Could not parse --answers as JSON.")
            return 1
        res = diagnose(args.track, answers)
        if "error" in res:
            print(res["error"])
            return 1
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Level: {res['level']} ({res['score']}/{res['max']})")
            for pq in res["per_question"]:
                mark = "+" if pq["correct"] else "x"
                print(f" [{mark}] {pq['skill']}: {pq['explanation']}")
            if res["gaps"]:
                print("Gaps to work on:")
                for g in res["gaps"]:
                    print(f"  - {g['skill']} -> lesson {g['lesson_id']}")
        return 0

    if action == "plan":
        res = plan(args.track, args.level)
        if "error" in res:
            print(res["error"])
            return 1
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Track: {args.track} ({res['level']}), "
                  f"start at lesson {res['start_here'] + 1}")
            for i, ls in enumerate(res["lessons"], 1):
                done = "[done]" if ls["done"] else ""
                marker = ">>>" if i - 1 == res["start_here"] else "   "
                print(f"{marker} {i}. {ls['id']} — {ls['title']} "
                      f"({ls['est_minutes']}m) {done}")
        return 0

    if action == "lesson":
        res = get_lesson(args.track, args.lesson_id)
        if "error" in res:
            print(res["error"])
            return 1
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print(f"# {res['title']} [{res['skill']}]")
            print(res["body"])
            print(f"\nExercise: {res['exercise_prompt']}")
            print(f"Hint: {res['hint']}")
        return 0

    if action == "exercise":
        res = exercise(args.track, args.lesson_id)
        if "error" in res:
            print(res["error"])
            return 1
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Session: {res['session_id']}")
            print(f"Exercise: {res['exercise_prompt']}")
            print(f"Hint: {res['hint']}")
            print(f"\nSubmit with: ai-skills submit {res['session_id']} "
                  "--response \"...\"")
        return 0

    if action == "submit":
        res = submit_exercise(args.session_id, args.response)
        if "error" in res:
            print(res["error"])
            return 1
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print("PASSED" if res["passed"] else "TRY AGAIN")
            print(res["feedback"])
        return 0

    if action == "progress":
        res = progress()
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Streak: {res['streak_days']} days | "
                  f"Total completed: {res['total_completed']}")
            for tid, tp in res["tracks"].items():
                lvl = tp["level"] or "not diagnosed"
                print(f"  {tid:12} {lvl:13} "
                      f"{tp['completed_count']}/{tp['total_lessons']} lessons")
            print(f"\nNote: {res['note']}")
        return 0

    if action == "lab-tracks":
        res = lab_tracks()
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            for t in res:
                print(f"{t['track']:12} {', '.join(t['exercise_types'])}")
        return 0

    if action == "lab-exercise":
        res = lab_exercise(args.track, args.exercise_type)
        if "error" in res:
            print(res["error"])
            return 1
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Session: {res['session_id']} "
                  f"[{res['track']}/{res['exercise_type']}]")
            print(f"Brief: {res['brief']}")
            print(f"Task: {res['task']}")
            print(f"Good looks like: {res['good_looks_like']}")
            print(f"\nSubmit with: ai-skills lab-submit {res['session_id']} "
                  "--response \"...\"")
        return 0

    if action == "lab-submit":
        res = submit_lab_exercise(args.session_id, args.response)
        if "error" in res:
            print(res["error"])
            return 1
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            print("PASSED" if res["passed"] else "TRY AGAIN")
            print(res["feedback"])
        return 0

    print(f"Unknown action {action!r}.")
    return 1


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `ai-skills` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p = subparsers.add_parser(
        "ai-skills",
        help="AI-proficiency training: diagnose, learn, practice.",
    )
    p.add_argument(
        "action",
        choices=["tracks", "diagnose", "answer", "plan", "lesson",
                 "exercise", "submit", "progress",
                 "lab-tracks", "lab-exercise", "lab-submit"],
        help="What to do.",
    )
    p.add_argument("--track", default="",
                   help="Track id, e.g. engineer.")
    p.add_argument("--exercise-type", default="",
                   help="ethics|evaluation|failure_analysis (lab only).")
    p.add_argument("--lesson-id", default="", help="Lesson id.")
    p.add_argument("--session-id", default="", help="Exercise session id.")
    p.add_argument("--level", default=None,
                   help="foundations|practitioner|advanced (plan only).")
    p.add_argument("--answers", default="[]",
                   help='JSON list of diagnostic answers (answer only).')
    p.add_argument("--response", default="",
                   help="Exercise response text (submit only).")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    return {"ai-skills": cmd_ai_skills}
