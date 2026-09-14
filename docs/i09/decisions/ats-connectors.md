# ATS connectors decisions (Initiative 09, 2026-09-13)

Constitutional format: genuine options (Rule 2, each phrased "trades X
for Y"), a `schedule_driven` flag (Rule 3), and an `ops_goal` naming a
metric and direction (Rule 4).

Honest framing: this is a local pre-launch library. Veto has no public
users yet, no production traffic, and no measured operational statistics
— every "metric" below is a test-enforced target, not an observed
number. Nothing here invents users, incidents, partner agreements, or
launch dates.

## D1: enrichment posture for the ATS connectors (Greenhouse/Lever/Ashby)

- **Chosen: read-only enrichment + honest capability declaration +
  browser-assisted fill-only flow.**
  - `get_details` on each board pulls only the vendor's official public
    API (no auth, no scraping of gated surfaces) and surfaces the
    application surface it can actually see: question counts where the
    API answers `?questions=true` (Greenhouse), 0 with an explicit
    "does not expose" note where it does not (Lever, Ashby). Every
    connector's `readiness_note` states plainly that direct API
    submission is not permitted to job seekers, and
    `ats_apply.describe_apply_path` names the browser flow as the
    recommended path, with the human performing the actual submit.
  - Trades away one-click auto-apply convenience (the user must still
    complete and submit applications in the browser) to get ToS-safe
    read-only enrichment, an honestly-declared capability surface, and
    zero fabricated fields — when a vendor omits a value, the field is
    `""`, never invented.
- **Surviving option B: direct API submission where the board's terms
  permit it.**
  - Trades away the uniform fill-only architecture (per-board submit
    paths, per-board key handling, and a permanently larger ToS-review
    burden — plus employer-issued API keys where the platform restricts
    POST to employers, e.g. Greenhouse's Job Board API key and Lever's
    API-key POST) to get true one-click submission on any board that
    opens a job-seeker submission path. Survives as the direction to take
    if a vendor publishes a job-seeker-facing submission API — today none
    of the three does, so `can_apply_direct` returns False for all of
    them and the option stays dormant rather than being implemented on
    hope.
- **Surviving option C: deeper HTML scraping of the hosted job pages
  (jobs.lever.co, boards.greenhouse.io) to extract application question
  lists the public APIs withhold.**
  - Trades away contract stability and honesty (scrapers break on
    markup changes, hosted pages can serve different content to bots
    than to humans, and the line between "public page" and gated
    application surface is thinner than an API contract) to get fuller
    question extraction for Lever and Ashby without waiting on vendor
    API changes. Survives only as a fallback if a vendor's API stops
    answering a field the enrichment genuinely needs — and even then it
    would ship with a stated fragility caveat, not as a quiet upgrade.

- `schedule_driven`: false — this work was correctness-driven (a blind
  review KICK_BACK rework), not calendar-driven; no launch date was
  traded.
- `ops_goal`: "share of get_details calls that raise instead of
  returning the declared error/empty-field shapes — keep at/near zero
  (test-pinned: malformed payloads, missing postings, and hostile
  vendor timestamps all return shapes, never raise)."

## D2: hostile vendor timestamp handling (Lever createdAt)

`datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)` on a hostile
`createdAt` (e.g. `10**22`) raises `OverflowError: timestamp out of
range for platform time_t`, which the original `(TypeError, ValueError,
OSError)` guard did not catch — the exception escaped `get_details`
uncaptured, breaking the never-raise contract from D1.

- **Chosen: widen the guard to
  `(TypeError, ValueError, OSError, OverflowError)` and fall back to
  `posted_date = ""`.**
  - Trades away a more "informative" failure (letting the traceback
    surface to the caller) to get the invariant D1 promises: no vendor
    payload can crash detail retrieval. The OverflowError case is pinned
    in `tests/test_ats_deepening.py` (`test_bad_created_at_falls_back_to
    _empty_posted_date`).
- **Surviving option B: propagate the exception (loud failure).**
  - This strategy trades away per-call isolation — one hostile payload
    breaks its own detail call instead of degrading to `posted_date ""`
    — to get loud anomaly signaling: malformed vendor timestamps would
    surface as errors an operator or monitor could alert on, instead of
    silently degrading to an empty field. It loses to the chosen option
    because D1's ops_goal (detail calls never raise) is the headline
    contract the conformance suite enforces; a loud failure wins only if
    Veto ever gains an anomaly-monitoring pipeline that needs the
    signal, and none exists today.
- **Surviving option C: pre-validate `createdAt` against platform
  time_t bounds before converting, instead of widening the exception
  guard.**
  - This strategy trades away simplicity — range-validation logic that
    must track platform time_t limits (and any future exception type a
    new platform's `fromtimestamp` could raise) — to get the same
    never-raise outcome without relying on the breadth of the exception
    list. It survives as hardening if the guard list ever proves
    incomplete on a new platform; until then the wider guard is the
    simpler, equivalent invariant.
- `schedule_driven`: false. `ops_goal`: same as D1 — vendor-shaped
  hostile inputs never raise.

## D3: Greenhouse employment_type wiring

`get_details` hardcoded `"employment_type": ""` while `custom_fields`
already extracted the `"Employment Type"` metadata name — the value
(`"Full-time"` in the fixture) was dropped on the floor.

- **Chosen: read the value from the first metadata entry whose name
  matches "employment type" case-insensitively, first hit wins; `""`
  when absent.**
  - Trades away a few lines of extraction logic to get no fabricated
    data and no dropped data: the field now reflects what the vendor
    actually said, or stays honestly empty.
- **Surviving option B: keep `employment_type` hardcoded `""` and rely
  on the metadata text already surfaced in `requirements`.**
  - This strategy trades away a structured, filterable field —
    downstream matching can no longer filter or rank on employment type
    — to get zero new extraction logic and zero risk of misreading a
    vendor-specific metadata label as a canonical employment type. It
    stays dormant because the extraction is cheap and honest (first
    case-insensitive name match, `""` when absent), so dropping
    available vendor data buys almost nothing.
- **Surviving option C: normalize the value into a canonical vocabulary
  (`full-time` / `part-time` / `contract`) instead of passing the raw
  vendor string through.**
  - This strategy trades away fidelity to the vendor's wording —
    "Full-time", "FULL TIME", and "Full Time" would all collapse to one
    label — to get a normalized field downstream filters can match
    without fuzzy logic. It was not taken because the normalization
    rules would have to be invented per vendor, and inventing canonical
    labels is exactly what the no-fabrication constraint forbids; the
    raw vendor string is the honest value.
- `schedule_driven`: false. `ops_goal`: "employment_type empty on a
  detail response whose metadata names an Employment Type — zero
  (test-pinned)."

## D4: Ashby POST-fallback trigger — docs vs code (round-4 rework)

The module docstring and this decision record said the POST fallback
fires "when GET fails or returns empty", but the code only fires it when
GET returns None — an empty-but-successful GET (`{"jobs": []}`) returns
immediately. The fix had two candidate directions.

- **Chosen: correct the docs to say "when GET fails (returns None)".**
  - Trades away the simplicity of a one-phrase doc edit in the other
    direction (no code change would be needed to just reword docs
    either way, but extending the code would add request volume) to get
    operationally sound behavior: an empty-but-successful GET is the
    vendor *answering* — retrying it as a POST doubles request volume on
    every legitimately empty board and asks the fallback endpoint to
    "fix" a non-failure. A failed GET (None) is the only signal the
    fallback is for. Docs, module docstring, and code now agree, pinned
    by `AshbyPostFallbackTests` in `tests/test_ats_deepening.py` (empty
    GET issues no POST; failed GET retries once via POST).
- **Surviving option B: extend the code to treat an empty-list GET as a
  POST trigger.**
  - Trades away request discipline — every board with zero live jobs
    costs two requests instead of one, permanently — to get maximal
    resilience against a hypothetical vendor mode where GET "succeeds"
    with an empty body while POST would have returned jobs. It loses
    because that failure mode has never been observed; the POST fallback
    exists for observed intermittent GET rejections, and burning an
    extra request on every empty board is a concrete recurring cost for
    a speculative gain. It survives if empty-GET/full-POST divergence is
    ever observed live.
- `schedule_driven`: false. `ops_goal`: "POST fallback issued per board
  when the vendor answered empty — zero (test-pinned)."

## Standing constraints (non-negotiable, all three boards)

- **No invented data.** A field the vendor does not supply is `""` (or
  `0`, or `[]`) — posted dates, question counts, and employment types
  are never fabricated, never estimated, never carried over from
  another source. `readiness_note` says what the connector cannot see.
- **Never raise on transport or vendor data.** `providers._common.fetch_json`
  returns `None` — never raises — on transport errors, HTTP error
  statuses, and malformed JSON (its whole call body sits inside one
  `try/except Exception`). The contract is pinned by
  `FetchJsonNeverRaisesTests` in `tests/test_ats_deepening.py` (404, 500,
  timeout, connection error, malformed JSON), and every `get_details`
  validates payload shape before use so a degraded vendor response
  yields the documented error shape, never an exception.
- **Fill-only automation.** Tooling may populate application fields in
  the browser flow; it never invents answers the user did not provide.
- **Human submits.** No apply/submit write path exists: no submission
  POST, no API-key plumbing for apply endpoints. One narrow exception:
  Ashby's `get_details`/`_fetch_board` uses a POST with an empty JSON
  body as a *read-only fallback* on Ashby's public posting endpoint when
  GET fails (returns None) — an empty-but-successful GET (`{"jobs":
  []}`) is NOT retried: the vendor answered, it just has no jobs. The
  fallback issues no mutation and hits the same public read endpoint. Read-only enrichment stays read-only — this
  record's options (B and C) describe dormant or fallback directions,
  not shipped behavior.

## D5: unguarded `or []` on nested vendor containers (round-5 MAJOR rework)

Round 4 claimed a "full re-audit" of the N1 defect class (unguarded
iteration over hostile vendor containers) but normalized only the
top-level `jobs`/`postings` containers. A blind reviewer found the same
defect one level down, in three places: guarding the ITEMS
(`isinstance(d, dict)`) does not guard the CONTAINER — a hostile truthy
non-iterable (e.g. `departments: 42`, `lists: True`) makes
`x = raw.get(k) or []` a no-op and iterating it raises TypeError,
violating the standing constraint "Never raise on transport or vendor
data".

- **Chosen: normalize every nested-container fetch with
  `_coerce_list` (isinstance(list) else []) — same idiom as the
  top-level container fix.** Sites fixed:
  - greenhouse `_parse_job`: `departments` (killed the entire
    multi-board search when truthy non-iterable).
  - greenhouse `get_details`: `departments` and `offices` (crashed the
    detail call; truthy non-iterable in the comprehensions).
  - lever `get_details`: `lists` (crashed the requirements join).
  - The same audit grepped every `or []` and every iteration over
    vendor-fetched values in all three providers. Ashby has no
    unguarded nested-container iteration: its only container (`jobs` in
    `_fetch_board`) is already `isinstance(list)`-guarded (N1 round 4),
    and `department` is a scalar coerced to str, never iterated raw —
    test-pinned as ignored when a hostile `departments` key arrives.
    All other nested containers were already guarded: greenhouse
    `jobs_list` (search), `questions`, `metadata` (isinstance-guarded
    in get_details), lever search `postings`, lever `categories`
    (`_coerce_categories`).
  - Each fix adds a `_coerce_list` helper to the provider module with
    the same comment style as the other coercion helpers, guaranteeing
    list output so the existing item-level isinstance guards and
    str-coercions keep working unchanged.
- **Surviving option B: accept truthy iterables (tuples, strings) as
  containers.** Trades away vendor-reality fidelity to get a marginally
  more permissive contract. It loses because `fetch_json` returns only
  JSON values — vendors never emit tuples — and a truthy string
  container iterates characters, which the item-level isinstance(dict)
  guards already skip gracefully (pinned by test). Normalizing to
  `[]` for anything non-list is the simpler, predictable rule.
- `schedule_driven`: false. `ops_goal`: "exception raised from iterating
  a vendor-supplied nested container — zero (test-pinned)."
  Pinned by `HostileNestedContainerTests` in
  `tests/test_ats_deepening.py`: numeric/truthy-non-iterable
  departments/offices/lists (42, True) on Greenhouse search and
  get_details and Lever get_details degrade to empty collections with
  no raise and validate clean; Ashby's audit-clean status is pinned by
  a hostile `departments: 42` key being ignored on both paths.
