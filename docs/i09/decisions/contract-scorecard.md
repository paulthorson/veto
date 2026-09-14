# Provider-contract + reliability-scorecard decisions (Initiative 09, 2026-09-13)

Constitutional format: genuine options (Rule 2, each phrased "trades X
for Y"), a `schedule_driven` flag (Rule 3), and an `ops_goal` naming a
metric and direction (Rule 4).

Honest framing: this is a local pre-launch library. Veto has no public
users yet, no production traffic, and no measured operational statistics
— every "metric" below is a test-enforced target, not an observed
number. Nothing here invents users, incidents, partner agreements, or
launch dates.

## D1: adapter vs parallel definition for the 03→09 health contract

- **Chosen: adapter over Initiative 03's contract
  (`providers/health_contract.py` as a read/write adapter over the
  in-tree `provider_health.py`), not a parallel definition.**
  - Writes delegate to 03's `record_fetch` / `record_captcha` /
    `set_budget` / `cooldown` so there is exactly one write path. Reads
    map `provider_status()` rows onto `HealthSnapshot` in exactly one
    function (`snapshot_from_status`): if 03 renames a field, one function
    needs updating. Anything 03's contract does not recognize (absent or
    unrecognized `status`, unrecognized `captcha_state`) maps to an
    explicit `unknown` state that can never render healthy and is never
    written to 03's store (writing it would invent a fetch that never
    happened).
  - Trades away definitional independence (i09 cannot evolve the health
    schema on its own — every new field waits on 03's contract, and a 03
    restructure breaks the import until `_provider_health_module` is
    repointed) to get a single source of truth for health data, zero
    write-path divergence (03's budget counters, cooldowns, and recovery
    streaks stay authoritative), and one explicit mapping function that a
    reviewer can audit in full.
- **Surviving option B: parallel i09-owned definition.**
  - Trades away the single source of truth (two health schemas, two
    write paths, and a standing sync burden every time 03 changes
    semantics) to get independence from 03's release schedule and a
    schema i09 can extend freely (per-provider capability metadata, UI
    labels) without negotiating with another team. Survives as the
    fallback position: the `FileHealthStore` fallback already mirrors
    03's day-unit streak math, so if 03's contract is ever withdrawn or
    restructured beyond adaptation, i09 can promote the fallback store to
    the primary with its semantics intact.

- `schedule_driven`: false — correctness-driven (a blind engineer review
  KICK_BACK rework), not calendar-driven; no launch date was traded.
- `ops_goal`: "share of scoreboard rows whose rendered status contradicts
  the underlying snapshot's tri-state (ok/error/unknown) — keep at zero
  (test-pinned: drift fixtures assert unknown renders as 'unknown', never
  green)".

## D2: fallback vs fail-hard when 03's contract is unavailable or broken

- **Chosen: loud fallback to the local JSONL `FileHealthStore`.**
  - If `provider_health` is missing (ImportError) OR broken
    (SyntaxError, changed packaging — the named failure mode), the
    scorecard keeps working on an i09-owned store with 03-mirroring
    semantics (calendar-day streaks, carried-forward counters, unknown
    snapshots never inventing writes). The fallback switch is LOUD
    (stderr + error log), because the two stores have different
    semantics: 03's live contract vs i09's day-unit approximation, and
    03's budget counters are not shared. Read-path API drift
    (`load_store`/`provider_status` raising) degrades to loud "no data"
    per provider instead of a dashboard-crashing traceback.
  - Trades away strictness (the dashboard keeps rendering on degraded
    data, so an operator could act on a scoreboard that no longer
    reflects 03's live state) to get availability (the terminal dashboard
    never crashes for a health-subsystem failure) — with the loudness
    compensating for the lost strictness: every degradation path logs at
    error level, so the fallback is visible, not silent.
- **Surviving option B: fail hard (raise, refuse to render).**
  - Trades away dashboard availability (a broken health module takes
    down the whole terminal dashboard, including unrelated panels) to
    get a guarantee that no operator ever sees a scoreboard built from
    non-authoritative data. Survives as the posture for the write path:
    `ProviderHealthAdapter.record` raises `RuntimeError` when neither
    03's module nor a fallback store is configured — refusing to write
    beats writing into the void, and "unknown" snapshots are refused on
    ALL write paths (inventing a fetch is fabricated data).

- `schedule_driven`: false — same correctness-driven rework as D1.
- `ops_goal`: "share of fallback-mode scoreboard renders that go
  unlogged — keep at zero (test-pinned: every fallback and every
  read-path drift asserts an error-level log or stderr emission)".

## D3: tri-state rendering (ok / error / unknown) in the operator column

- **Chosen: render the tri-state verbatim; "unknown" is an explicit row
  status, never collapsed into "error", and never rendered as healthy.**
  - An absent or unrecognized `status` from 03 is schema drift, not a
    failure: collapsing it into "error" would send an operator chasing a
    failure that was never observed, and rendering it as "ok" would be a
    lie. Unknown rows also render "—" in the budget column (an unknown
    observation carries no budget data; "0/N" would be invented). The
    terminal footer states plainly that "no data" and "unknown" rows are
    unknowns, not healthy.
  - Trades away a simpler two-state operator column (ok/error is easier
    to scan and to threshold alerts on) to get honesty about the
    evidence: the board distinguishes "we observed a failure" from "we
    know nothing", which is the difference between an actionable alert
    and a wild-goose chase.
- **Surviving option B: collapse unknown into error.**
  - Trades away the evidence distinction (every drift or gap becomes an
    "error" row, which trains operators to ignore the error state —
    alert fatigue by construction) to get a two-state column that naive
    automation can threshold without understanding drift.

- `schedule_driven`: false.
- `ops_goal`: "share of schema-drift rows (absent/unrecognized 03
  status) rendered as anything other than 'unknown' — keep at zero
  (test-pinned)".

## D4: atomic compaction for the fallback store

- **Chosen: temp file + `os.replace()` on compaction.**
  - The append-only JSONL store is bounded by compaction (latest line
    per provider once past `FALLBACK_MAX_LINES`). The old code rewrote
    the file in place: a crash mid-write truncated the store to zero
    bytes and wiped all history. The rewrite now goes to a temp sibling
    and is atomically replaced, so a crash leaves either the old file
    or the new one — never an empty file.
  - Trades away write-path simplicity (temp-file dance, same-directory
    fsync assumptions) to get crash safety for the only durable health
    history i09 owns.
- **Surviving option B: in-place rewrite.**
  - Trades away crash safety to get simpler code. Rejected: the fallback
    store is the store of last resort — it must not be the store most
    likely to lose data.

- `schedule_driven`: false.
- `ops_goal`: "compaction runs that leave the store empty or truncated —
  keep at zero (test-pinned: a simulated mid-compact `os.replace` failure
  asserts the original file survives byte-identical)".
