# Outcome Event Contract — `outcome-min-v0`

**Status: ACTIVE** (Week-0 minimum event, roadmap Decision 02)
**Schema version:** `outcome-min-v0` · **Module:** `outcomes.py` · **Migration:** `outcome_migration.py`

This is the stable contract other workstreams build against. Field names,
storage format, and the migration guarantees below are frozen for Q4.

## Event schema

One JSON object per line in `outcomes.jsonl` (append-only; see below).

| Field | Type | Required | Description |
|---|---|---|---|
| `event_id` | string | yes | uuid4 hex. Identity anchor; never reused. |
| `application_id` | string | yes | The application's `job_id`. |
| `event_type` | string | yes | One of the 11 canonical types (below). |
| `occurred_at` | string | yes | ISO-8601 UTC timestamp of when the thing happened (not when recorded). |
| `source` | string | yes | Capture channel, e.g. `server:apply_to_job`, `server:browser_apply`, `apply_queue`, `lifecycle:update_stage`, `manual`, `csv-import`. |
| `role` | string | yes | Job title at capture time (`""` if unknown). |
| `provenance` | object | yes | Who/what produced the event, e.g. `{"actor": "user", "method": "ats_apply_auto", "board": "greenhouse"}`. |
| `schema_version` | string | yes | Always `"outcome-min-v0"`. |
| `recorded_at` | string | writer-set | When the event was appended. |
| `corrects` | string | no | `event_id` this event supersedes (reversible correction). |
| `migrated_from` | string | no | Set on canonical events: the min-v0 `event_id`. |
| `migration_id` | string | no | Set on canonical events: the migration batch. |

### Canonical event types (11)

`discovered`, `shortlisted`, `applied`, `replied`, `screened`,
`interviewed`, `offered`, `accepted`, `rejected`, `withdrawn`, `stale`

Week-0 auto-capture emits `applied` only (every confirmed submission
after enablement). The other ten types are valid from day one and arrive
via guided manual updates (`outcomes update`), the `outcome_record_event`
MCP tool, or `outcomes record`.

### Legacy stage → event type mapping

`lifecycle.update_stage` mirrors stage changes automatically:

| Legacy stage | Canonical event |
|---|---|
| `applied` | `applied` |
| `interviewing` | `interviewed` |
| `offer` | `offered` |
| `rejected` | `rejected` |
| `withdrawn` | `withdrawn` |
| `ghosted` | `stale` |

## Storage format

- **File:** `outcomes.jsonl`, next to `applications.json` (override with `--events`).
- **Env override:** `VETO_OUTCOMES_FILE` forces every default path
  (store, CLI, MCP tools, lifecycle mirroring) to a given file. The
  test suite sets this so fake events never pollute the real store.
- **Append-only:** writers only append lines. The file is never rewritten,
  truncated, or reordered by the capture path.
- **Tolerant readers:** blank lines and corrupt lines are skipped with a
  warning, never fatal.
- **Enablement marker:** `outcomes_meta.json` records `capture_enabled_at`
  on the first-ever append. Coverage "after enablement" is measured
  against this timestamp.

## Duplicate detection

Identity key = SHA-256 over canonical JSON of
`[schema_version, application_id, event_type, occurred_at, source, corrects]`.
An append whose key (or `event_id`) already exists is **rejected**, never
double-appended. `corrects` is part of the key so a correction is a
distinct observation, not a duplicate of the event it supersedes.
`record_event` returns
`{"appended": false, "duplicate_of": "<event_id>"}` in that case.

## Corrections (reversible, provenanced)

Corrections are new events, never edits: `outcomes.correct_event`
appends an event with `corrects: <original event_id>` and provenance
naming the actor and reason. Only `role`, `occurred_at`, `source`, and
`provenance` are correctable — identity fields are immutable. Readers
exclude superseded events by default (`include_superseded=True` keeps
the full audit trail).

## Canonical migration contract

When the full canonical outcome model lands (`outcome-v1`):

1. **Identity preserved** — canonical events keep the min-v0 `event_id`.
2. **Time preserved** — `occurred_at` is copied verbatim, never re-stamped.
3. **Lossless** — `verify_migration` proves every min-v0 event has exactly
   one canonical counterpart with identical id and time.
4. **Rollback recorded** — `rollback_migration` appends a rollback record
   to `outcomes_migration.jsonl`; readers exclude rolled-back migrations
   by default. The min-v0 store is never modified, so any migration can
   be re-run after rollback.
5. **Corrections carry through** — `corrects` pointers migrate unchanged.

## Coverage instrumentation (Q4 exit gate)

`outcomes.coverage_report` / `analytics.outcome_coverage` report:

- `cohort_coverage` — post-enablement submissions with an `applied` event
  (Week-0 promise: 100%).
- `semantic_coverage` — distinct canonical types observed ÷ 11 (Week-0
  auto-capture alone = 1/11 ≈ 9.1%).
- `state_change_coverage` — observed post-enablement stage-history
  transitions with a matching outcome event ÷ all such transitions.
  **This is the Q4 exit-gate metric: ≥ 95% required.** `below_gate: true`
  flags a material bias per the roadmap. `insufficient_data: true` when
  there are no events or no applications — the 1.0 coverage values are
  vacuous then and must not be claimed as gate evidence.

`outcomes missing_outcome_prompts` / `analytics.outcome_prompts` list
applications whose outcome is stale or missing, most-stale first, with
suggested next event types — the feed for the guided update flow.

## API surface

**Python** (`import outcomes`):
`record_event`, `record_application_event`, `correct_event`,
`load_events`, `events_for_application`, `timeline`,
`coverage_report`, `missing_outcome_prompts`, `import_csv`,
`guided_update`, `capture_enabled_at`, `default_events_path`.

**Migration** (`import outcome_migration`):
`migrate_to_canonical`, `rollback_migration`, `verify_migration`,
`load_canonical_events`, `migration_records`.

**CLI** (`veto outcomes …`): `record`, `update` (guided interactive),
`timeline`, `coverage`, `prompts`, `import-csv`.

**MCP tools** (`outcomes.register_tools`):
`outcome_record_event`, `outcome_coverage`, `outcome_prompts`,
`outcome_timeline`.

**Analytics** (`analytics.py`, event-query side):
`outcome_coverage`, `outcome_timeline`, `outcome_prompts`; the
`analytics` report now includes `event_coverage` and
`recent_outcome_events`.
