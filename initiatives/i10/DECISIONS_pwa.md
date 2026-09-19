# Decision Record: WS5 fixer — PWA review fixes (Initiative 10)

Run ID: 2026-09-13-ws5-fixer
Lane: Develop & Deliver
Started: 2026-09-13
Human arbiter: anonymous operator

Constitutional format: genuine options (Rule 2, each phrased
"trades X for Y"), a `schedule_driven` flag (Rule 3), and an `ops_goal`
naming a metric and direction (Rule 4).

Honest framing: this is a local pre-launch library. Veto has no public
users yet, no production traffic, and no measured availability
statistics — every "metric" below is a test-enforced target, not an
observed number. Nothing here invents users, incidents, or launch dates.

---

## 1. Request

> You are a fixer for Veto Initiative 10, workstream WS5 (PWA). The
> independent blind re-review returned KICK_BACK with a Rule 1 blocker.
> Apply every must-fix finding below, add tests proving the fixes, run
> the i10 PWA/channel tests, and report changed files. Do NOT commit.

Solution shape assumed by the request: fix the listed findings in place;
no redesign of the PWA architecture.

## 2. Artifacts produced

| Artifact | Path |
|---|---|
| Rewritten service worker (single-owner, tolerant install, TTL/cap, ok-only caching) | `initiatives/i10/pwa/service-worker.js` |
| Rewritten deferred queue (idempotency keys, dedup, confirm-before-replay, bounded retries) | `initiatives/i10/pwa/deferred-queue.js` |
| Manifest fixes (id, maskable icon) | `initiatives/i10/pwa/manifest.webmanifest` |
| Offline page sync wiring | `initiatives/i10/pwa/offline.html` |
| Bundle validator: fingerprint/stamp, honest wiring gate, single-owner checks | `initiatives/i10/pwa.py` |
| Node runtime harness (69 assertions against the real JS) | `tests/i10_pwa_harness.mjs` |
| Fix-proving tests | `tests/test_i10_channels_pwa.py` (PWA classes) |
| PWA wiring requirements | `initiatives/i10/integration_notes.md` §6 |
| This decision record | `initiatives/i10/DECISIONS_pwa.md` |

## 3. Decisions

### D1: Single owner for enqueue — the page, not the worker (B1)

- **Chose.** The service worker answers `202 {queued:true}` on a failed
  mutation and enqueues nothing; `deferred-queue.js` is the single
  enqueue choke point (`enqueueRequest`), deduped by idempotency key on
  a UNIQUE IndexedDB index (cross-tab safe). The `veto:queue-mutation`
  postMessage fan-out was deleted outright.
- **Over.**
  - Keep both paths (SW postMessage + page 202 handling): trades away
    nothing and keeps the double-queue — rejected; it was the blocker.
  - SW owns the queue (worker writes IndexedDB directly): trades away
    page-side visibility and testability (the replay/confirm/dead-letter
    UI all live in the page) to get raw-fetch coverage without page
    cooperation — the page already funnels mutations through
    `VetoQueue.submit()`, and splitting queue writes across two agents
    re-creates the ownership ambiguity that caused B1.
  - Background Sync API for replay: trades away the user's chance to see
    and confirm a duplicate-risky replay to get fire-and-forget background
    sync — wrong trade for a job-application machine, where a silent
    duplicate submission is the worst outcome; also trades away
    cross-browser reliability (permission-gated, uneven support) for
    automation we do not need.
- **Because.** One writer, one key, one dedup point: with exactly one
  code path that can create a queue entry, "exactly once" becomes a
  property of the code rather than of careful coordination between two
  agents. The removed protocol cannot regress — `validate_bundle()`
  fails CI if `veto:queue-mutation` or `.postMessage(` reappears in the
  worker.
- **ops_goal.** Duplicate queue entries per offline mutation: hold at 0
  (direction: zero; enforced by the harness: one offline mutation →
  exactly one entry; duplicate delivery → one entry; cross-tab same key
  → one entry).
- **schedule_driven.** false. Correctness of the highest-stakes defect
  class drove this; no deadline pressure.
- **Confidence.** high. Proven by 69 runtime harness assertions plus the
  static single-owner invariant in CI.
- **Unknown at decision time.** Whether the future server will persist
  idempotency keys durably across restarts — the client contract is
  specified (integration_notes.md §6c); server-side durability is the
  wiring sweep's to verify.

### D2: Idempotency keys on every entry, replayed via `Idempotency-Key` (B1)

- **Chose.** Every queue entry carries a key (caller-supplied or
  generated per `submit()`), sent as the `Idempotency-Key` header on the
  original attempt AND every replay; the server must dedup by key
  (§6c). Entries are flagged `ambiguous: true` unless the original
  attempt definitively succeeded, and replay always confirms first.
- **Over.**
  - Replay without keys, relying on "the network failed so the server
    never saw it": trades away safety against the ambiguous case
    (request processed, response lost) to get a simpler server contract
    — rejected; the ambiguous case is exactly how duplicate job
    applications happen.
  - User confirmation INSTEAD of keys: trades away machine-enforced
    safety to get a simpler protocol — rejected; confirmation is kept
    as well (defense in depth), but humans cannot reliably judge whether
    a lost response was processed.
- **Because.** This is a job-application machine: a duplicate submission
  is a real-world, user-visible harm, not a cosmetic glitch. Keys make
  replay idempotent even when the failure was ambiguous; confirmation
  makes the replay visible. Either alone is insufficient.
- **ops_goal.** Replays sent without an `Idempotency-Key` header: hold at
  0 (direction: zero; harness asserts the header on every replay).
- **schedule_driven.** false.
- **Confidence.** high (client side). Server-side key handling is a
  wiring-sweep requirement, explicitly documented, not assumed.
- **Unknown at decision time.** Server key-retention window (how long a
  repeated key returns the stored result).

### D3: Confirm-before-replay, no silent auto-replay (B1)

- **Chose.** `replay()` shows a "Veto will replay N queued action(s)"
  dialog before sending anything; the `online` event only refreshes the
  badge and fires `veto:queue-changed` — the webui renders its own
  "Review & sync" prompt from that event.
- **Over.**
  - Silent auto-replay on `online`: trades away user awareness to get
    zero-friction sync — rejected; a replay that double-applies must
    never happen without the user seeing it first.
  - postMessage handoff from worker to page for replay triggering:
    trades away nothing — rejected with D1; the protocol is gone.
- **Because.** The review required a user-visible "will replay N
  actions" surface; making confirmation the default path (with an
  explicit `{confirmed:true}` escape hatch for webui-owned confirm UI)
  keeps the invariant "no mutation replays without a human seeing the
  count" while letting the webui own the visual design.
- **ops_goal.** Replays initiated without a preceding user-visible
  confirmation: hold at 0 (direction: zero; harness asserts zero
  network calls before the dialog resolves).
- **schedule_driven.** false.
- **Confidence.** high.
- **Unknown at decision time.** Final visual design of the webui's
  Review & sync prompt (webui team's call, contract fixed here).

### D4: Tolerant install + honest shippability gate (F1)

- **Chose.** Install caches per-asset with `allSettled`: it fails only
  if a CORE asset (`/`, `/offline.html`) is missing; optional assets
  (`/app.css`, `/app.js`, icons…) log a warning. Separately,
  `validate_bundle()` now FAILS while the webui.py serving wiring is
  absent (`webui-wiring` check) — honest red, not green — because the
  bundle cannot install as-shipped without it.
- **Over.**
  - Keep `cache.addAll` all-or-nothing: trades away deployability during
    the (normal) window where the shell and the wiring land separately
    to get a simpler install — rejected; one 404 must not brick the PWA.
  - Keep the old whitelist ("app.css/app.js are served by webui, trust
    us") and stay green: trades away honesty to get a green CI badge —
    rejected; CI green on an undeployable bundle is how this shipped.
  - Block the bundle on wiring by deleting the bundle until the sweep:
    trades away reviewability of the bundle itself to get a trivially
    honest gate — rejected; the bundle is reviewable now, gated honestly.
- **Because.** Install-time tolerance and CI-time honesty are different
  problems: the worker must survive a partially-deployed shell, while CI
  must never claim "shippable" for something that cannot install.
- **ops_goal.** `validate_bundle()` passing on a bundle whose install
  would fail as-shipped: hold at 0 (direction: zero; the webui-wiring
  check plus the temp-dir stale/wiring tests enforce it).
- **schedule_driven.** false.
- **Confidence.** high.
- **Unknown at decision time.** Exact webui.py route shapes (the sweep
  may adjust `WEBUI_WIRING_MARKERS` if the registration snippet lives in
  a template rather than webui.py).

### D5: Hand-rolled worker, not Workbox

- **Chose.** Keep the hand-rolled service worker (~200 lines, zero
  dependencies, no build step).
- **Over.**
  - Adopt Workbox: trades away auditability and the zero-build local-
    first story to get battle-tested precaching/strategies — the strategies
    we need (cache-first shell, TTL-bounded network-first reads) are
    small enough to own, and every line is covered by the harness.
- **Because.** A vendored framework would move the B1 ownership logic
  into configuration surface we don't control; the hand-rolled worker's
  fetch routing is directly asserted by 11 harness tests.
- **ops_goal.** Third-party JS in the PWA bundle: hold at 0
  (direction: zero; keeps the offline shell self-contained).
- **schedule_driven.** false.
- **Confidence.** medium-high. The harness is the backstop; Workbox
  remains the fallback if routing needs outgrow it.
- **Unknown at decision time.** Whether future PWA needs (push, periodic
  sync) will force a framework anyway.

### D6: Cache freshness bounds + content-hash versioning (F5/F6)

- **Chose.** Read-API cache: 15-minute TTL (stale entries are never
  served), 200-entry cap with oldest-first eviction, only 2xx cached.
  `CACHE_NAME` embeds the bundle content hash, stamped by
  `python -m initiatives.i10.pwa stamp`; CI fails on a stale stamp;
  old versioned caches purge on `activate`.
- **Over.**
  - Manual `CACHE_NAME` bump with no CI tie: trades away deploy safety
    to get one less build step — rejected; silent stale shells were the
    finding.
  - Unbounded API cache with manual invalidation: trades away memory and
    freshness guarantees to get simpler code — rejected; an unbounded
    cache on a phone is a slow leak.
- **Because.** Versioning by content hash makes "which shell is
  deployed" a computable fact instead of a human-remembered bump, and
  the stamp check makes forgetting it a CI failure rather than a
  production incident.
- **ops_goal.** Stale shell served after a bundle deploy: hold at 0
  (direction: zero; versioned cache name + activate purge + stamp-fresh
  CI gate). Read-API entries served past TTL: hold at 0 (direction:
  zero; harness asserts stale-as-miss).
- **schedule_driven.** false.
- **Confidence.** high.
- **Unknown at decision time.** Whether 15 min / 200 entries fit real
  dashboard usage — tunable constants, documented in the worker.

### D7: Node runtime harness for the fetch/queue logic (F7)

- **Chose.** A Node harness (`tests/i10_pwa_harness.mjs`) that loads the
  REAL `service-worker.js` and `deferred-queue.js` with stubbed platform
  globals and drives install/fetch/queue/replay end to end, run from
  pytest via `TestPwaJsHarness`.
- **Over.**
  - Static-only validation: trades away any proof the routing logic
    works to get zero test infrastructure — rejected; that was the
    finding.
  - Full headless-browser test in CI: trades away CI speed and
    hermeticity to get maximum fidelity — deferred, not rejected: the
    Q3 exit-gate matrix (integration_notes.md §6f) routes the
    real-device install/offline/replay test to the human gate, which is
    where fidelity actually matters.
- **Because.** Node is available in this environment, the harness runs in
  ~2s, and it asserts the exact properties the review cared about
  (single-enqueue, dedup, idempotency header, confirm gate, bounded
  retries). A browser test would prove more but cost a browser in CI;
  the human gate covers the last mile.
- **ops_goal.** Harness assertions passing: 69/69 (direction: hold;
  regressions fail the pytest suite).
- **schedule_driven.** false.
- **Confidence.** high for logic; the harness stubs the platform, so
  real-device behavior remains the documented human gate.
- **Unknown at decision time.** None material — node availability is
  verified in-repo (`TestPwaJsHarness` skips honestly if absent).

### D8: Fixer round 2 (2026-09-13) — blind-review KICK_BACK fixes

- **Chose.** Five changes, no redesign:
  - **F1 (Rule 1 blocker):** the worker no longer answers the synthetic
    202 for navigation-mode mutations on network failure. A page
    mid-navigation cannot run `VetoQueue.submit()`, so the 202 claimed a
    queueing that never happened and the form data was dropped while the
    user was told it was queued. Navigation-mode POSTs on network failure
    now get `offline.html` (honest). Header comment updated to say so.
    *Over:* keeping the 202 for all mutations (trades away honesty for a
    uniform mutation path — rejected; uniform here is a lie).
  - **F2 (major):** `replay()`'s network-error catch block now mirrors
    `replayEntry()`'s transient-HTTP path: on reaching MAX_ATTEMPTS the
    entry dead-letters as retryable instead of wedging in the queue
    forever (the `due` filter would have excluded it while the badge
    still counted it).
    *Over:* leaving the wedge (trades away recoverability for one fewer
    branch — rejected; "badge-counted but unreplayable" is the worst
    queue state).
  - **F4:** the worker's synthetic 202 carries `x-veto-offline-queue: 1`;
    `submit()` treats a 202 as worker-captured ONLY when that header is
    present. A genuine server 202 + `{"queued": true}` is returned to the
    caller untouched — never double-applied into the queue.
  - **F5:** `WEBUI_WIRING_MARKERS` gains `icons/`, `icon-192`, `icon-512`
    — the install prompt needs the 192/512 icons, so the gate can no
    longer report shippable while installability can't fire.
  - **F6:** `validate_bundle()` parses CORE_ASSETS/OPTIONAL_ASSETS via a
    defensive helper; a worker missing those constants fails the
    `sw-cache-list` check with a clear message instead of raising
    IndexError.
  - **F8:** `VetoQueue.submit()` is JSON-ONLY, loudly. FormData/Blob
    bodies are rejected with a TypeError: the queue serializes the
    payload as a string and replays it with
    `Content-Type: application/json`, so a `JSON.stringify`'d FormData
    would serialize to `"{}"` and silently drop the user's data while
    looking queued. The docstring states the contract; callers must
    serialize to JSON explicitly before submitting.
  - **F7 (nit):** `_strip_js_comments`' `//` regex was LEFT alone. It can
    strip `//` inside string literals (future `https://` URLs), but a
    character-walking fix risks false negatives in the B1 single-owner
    check (worker fan-out regressing silently) — the risk is future-only
    in files that currently contain no `//` inside strings, so changing
    it is not clean and safe.
- **Because.** Every fix narrows the gap between what the worker/queue
  CLAIM and what they DO — the same honesty theme as the round-1 wiring
  gate.
- **ops_goal.** Synthetic 202s claimed for mutations that were never
  queued: hold at 0 (direction: zero). Queue entries wedged
  unrecoverably: hold at 0.
- **schedule_driven.** false.
- **Unknown at decision time.** Resolved in round 3: the harness
  assertions named here were already updated — #5 now asserts the offline
  form-POST navigation serves offline.html (never a false 202), and
  #16/#16b assert the worker-namespaced 202 enqueues exactly once while a
  genuine server 202 (no `x-veto-offline-queue` header) is returned
  untouched.

## 4. Standing ops_goal — offline availability (Rule 4)
**Metric: offline availability** — the share of dashboard read views
that render with zero network, and the integrity of deferred mutations.

- Direction: **increase** availability; **hold at zero** the failure
  modes (duplicate queue entries per offline mutation; replays without
  `Idempotency-Key`; replays without user-visible confirmation; stale
  shell served post-deploy; error responses cached as valid).
- Current status: no production traffic exists; all targets are
  test-enforced (69 harness assertions + `validate_bundle` CI checks),
  not observed measurements.
- The Q3 exit-gate matrix must add a real-device measurement before the
  PWA is declared shippable (integration_notes.md §6f).
