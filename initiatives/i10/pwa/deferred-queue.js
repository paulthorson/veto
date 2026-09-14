/* Veto deferred-action queue — offline-safe mutations.
 *
 * OWNERSHIP (Initiative 10, WS5 review B1): the PAGE is the single owner
 * of this queue. The service worker never enqueues — on a network failure
 * for a non-navigation mutation it only answers the synthetic 202
 * {queued:true}, namespaced with the `x-veto-offline-queue` response
 * header (navigation-mode form POSTs get offline.html instead, since a
 * page mid-navigation cannot run submit() to enqueue). So there is exactly
 * one enqueue path and no postMessage fan-out that could double-queue a
 * mutation.
 *
 * Every entry carries an idempotency key:
 *  - generated once per submit() call (or supplied by the caller),
 *  - sent as the `Idempotency-Key` header on the original attempt AND on
 *    every replay, so a replay-after-ambiguous-failure (request reached
 *    the server but the response was lost) cannot double-apply server
 *    side — the server dedups by key (see integration_notes.md §6).
 *  - enforced UNIQUE in IndexedDB (`by-key` index), so the same key is
 *    never queued twice, even across tabs racing the same action.
 *
 * Replay is never silent: `replay()` shows a "Veto will replay N queued
 * action(s)" confirmation first. The queue-count badge
 * ([data-veto-queue-count]) and the `veto:queue-changed` event keep the
 * UI honest about what is pending.
 *
 * Retry policy: 429 and 5xx and network errors are transient — bounded
 * retries (MAX_ATTEMPTS) with exponential backoff, then the entry moves
 * to the dead-letter list as retryable (not permanent). Other 4xx are
 * permanent and go straight to dead-letter. 409 is treated as
 * "already applied" and the entry is dropped.
 */
(function () {
  "use strict";

  const DB_NAME = "veto-deferred";
  const DB_VERSION = 2;
  const STORE = "queue";
  const DEAD = "dead-letter";
  const KEY_INDEX = "by-key";

  const MAX_ATTEMPTS = 5;
  const BACKOFF_BASE_MS = 1000;
  const BACKOFF_MAX_MS = 60000;

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (event) => {
        const db = req.result;
        let store;
        if (!db.objectStoreNames.contains(STORE)) {
          store = db.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
        } else {
          store = event.target.transaction.objectStore(STORE);
        }
        if (!store.indexNames.contains(KEY_INDEX)) {
          store.createIndex(KEY_INDEX, "idempotencyKey", { unique: true });
        }
        if (!db.objectStoreNames.contains(DEAD)) {
          db.createObjectStore(DEAD, { keyPath: "id", autoIncrement: true });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function put(store, entry) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      const req = tx.objectStore(store).put(entry);
      req.onsuccess = () => resolve(req.result);
      tx.oncomplete = () => resolve(req.result);
      tx.onerror = () => reject(tx.error);
    });
  }

  async function all(store) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const req = db.transaction(store, "readonly").objectStore(store).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async function getByKey(key) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).index(KEY_INDEX).get(key);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }

  async function getById(store, id) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const req = db.transaction(store, "readonly").objectStore(store).get(id);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }

  async function del(store, id) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).delete(id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  }

  function newIdempotencyKey() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return crypto.randomUUID();
    }
    return "key-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  }

  function isConstraintError(err) {
    return err && (err.name === "ConstraintError" || /constraint/i.test(err.message || ""));
  }

  /**
   * THE single enqueue choke point. Same idempotency key is never queued
   * twice — the unique index makes this hold across tabs racing the same
   * action (the loser gets {deduplicated:true}).
   */
  async function enqueueRequest({ method, url, body, idempotencyKey }) {
    const key = idempotencyKey || newIdempotencyKey();
    if (await getByKey(key)) {
      return { queued: true, deduplicated: true, idempotencyKey: key };
    }
    const entry = {
      method,
      url,
      body: body == null ? null : body,
      idempotencyKey: key,
      enqueuedAt: new Date().toISOString(),
      attempts: 0,
      nextRetryAt: 0,
      // true unless the original attempt definitively succeeded: the
      // request may have reached the server while the response was lost.
      // Replay therefore always confirms first and always sends the key.
      ambiguous: true,
      lastError: null,
    };
    try {
      const id = await put(STORE, entry);
      await queueChanged();
      return { queued: true, deduplicated: false, id, idempotencyKey: key };
    } catch (err) {
      if (isConstraintError(err)) {
        // Lost a cross-tab race: the other tab queued this key first.
        return { queued: true, deduplicated: true, idempotencyKey: key };
      }
      throw err;
    }
  }

  function isPlainObject(value) {
    if (value === null || typeof value !== "object") return false;
    const proto = Object.getPrototypeOf(value);
    return proto === Object.prototype || proto === null;
  }

  function describeBody(value) {
    if (value === null) return "null";
    const t = typeof value;
    if (t === "object" || t === "function") {
      try {
        const ctor = value.constructor;
        if (ctor && typeof ctor.name === "string" && ctor.name) return "instance of " + ctor.name;
      } catch (err) {
        /* fall through to the toString tag */
      }
      return Object.prototype.toString.call(value);
    }
    return t;
  }

  function rejectBody(value) {
    throw new TypeError(
      "VetoQueue.submit() accepts JSON-only bodies — string, null, number, " +
      "boolean, plain object, or array (checked recursively) — but received " +
      describeBody(value) + ". FormData, Blob, File, ArrayBuffer, typed " +
      "arrays, DataView, URLSearchParams, Map, Set, class instances, " +
      "functions, and other binary/structured values would silently mangle " +
      "to \"{}\" or fail at JSON.stringify: serialize to JSON explicitly " +
      "before submitting."
    );
  }

  // F8: positive allowlist for submit() bodies (see the BODY CONTRACT on
  // submit()). A denylist (FormData/Blob only) cannot hold: any
  // structured/binary value not named — ArrayBuffer, URLSearchParams,
  // typed arrays — would slip through and get silently mangled to "{}".
  function assertJsonOnly(body, seen) {
    if (typeof body === "string" || typeof body === "number" || typeof body === "boolean" || body == null) {
      return;
    }
    if (typeof body !== "object") rejectBody(body); // function, symbol, bigint
    seen = seen || [];
    if (seen.indexOf(body) !== -1) rejectBody(body); // circular structure
    seen.push(body);
    if (Array.isArray(body)) {
      for (const item of body) assertJsonOnly(item, seen);
      seen.pop();
      return;
    }
    if (isPlainObject(body)) {
      const keys = Object.keys(body);
      for (const key of keys) assertJsonOnly(body[key], seen);
      seen.pop();
      return;
    }
    rejectBody(body);
  }

  /**
   * Attempt a mutation, queuing it on network failure (exactly once, by
   * idempotency key).
   *
   * BODY CONTRACT (F8): JSON ONLY — enforced with a POSITIVE ALLOWLIST,
   * checked recursively. `body` may be exactly one of:
   *   - a string,
   *   - null or undefined (serialized as null),
   *   - a number or boolean (note: NaN/Infinity are accepted as numbers
   *     but JSON.stringify coerces them to null — callers needing exact
   *     round-trips should normalize first),
   *   - a plain object ({...} — prototype Object.prototype or null),
   *   - an array,
   * where every value nested inside a plain object / array must itself
   * satisfy the same contract.
   *
   * EVERYTHING else is REJECTED with a TypeError: FormData, Blob, File,
   * ArrayBuffer, typed arrays (Uint8Array, …), DataView,
   * URLSearchParams, Map, Set, class instances, functions, symbols,
   * bigint, circular structures, and any other binary/structured value —
   * including such values nested inside an otherwise-plain object.
   * Rationale: the queue serializes the payload as a string and replays
   * it with `Content-Type: application/json`, so a JSON.stringify'd
   * FormData ("{}") would silently drop the user's data while looking
   * queued. Loud rejection beats silently dropped payloads. Callers must
   * serialize to JSON explicitly before submitting (the worker
   * advertises 202 handoff for form POSTs, but it can only hand off
   * what it can replay).
   */
  async function submit(method, url, body, opts) {
    opts = opts || {};
    assertJsonOnly(body); // F8: JSON-only — binary/structured bodies rejected LOUDLY
    const payload = typeof body === "string" ? body : JSON.stringify(body == null ? null : body);
    const idempotencyKey = opts.idempotencyKey || newIdempotencyKey();
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
        body: payload,
      });
    } catch (err) {
      // Network failure: the request may or may not have reached the
      // server. Queue it (ambiguous) — exactly once, by key.
      const out = await enqueueRequest({ method, url, body: payload, idempotencyKey });
      return { queued: true, idempotencyKey, deduplicated: out.deduplicated };
    }
    if (res.status === 202) {
      const data = await res.json().catch(() => ({}));
      // Only the WORKER's synthetic 202 counts as a capture: it carries
      // the x-veto-offline-queue header (F4). A genuine server 202 +
      // {"queued": true} is returned to the caller untouched — never
      // double-applied into the queue.
      if (data && data.queued && res.headers && res.headers.get("x-veto-offline-queue")) {
        // Service worker captured an offline mutation: it did NOT enqueue
        // (page owns the queue) — we enqueue here, exactly once, by key.
        const out = await enqueueRequest({ method, url, body: payload, idempotencyKey });
        return { queued: true, idempotencyKey, deduplicated: out.deduplicated };
      }
    }
    return res;
  }

  function backoffMs(attempts) {
    return Math.min(BACKOFF_BASE_MS * Math.pow(2, attempts), BACKOFF_MAX_MS);
  }

  function isTransientStatus(status) {
    return status === 429 || (status >= 500 && status < 600);
  }

  async function deadLetter(entry, info) {
    await del(STORE, entry.id);
    await put(DEAD, {
      ...entry,
      id: undefined,
      failedAt: new Date().toISOString(),
      status: info.status == null ? null : info.status,
      error: info.error || null,
      permanent: !!info.permanent,
    });
    await queueChanged();
  }

  async function replayEntry(entry) {
    const res = await fetch(entry.url, {
      method: entry.method,
      headers: { "Content-Type": "application/json", "Idempotency-Key": entry.idempotencyKey },
      body: entry.body,
    });
    if (res.ok || res.status === 409) {
      // 2xx: applied. 409: the idempotency-aware server reports this key
      // was already applied — drop the entry, do not re-apply.
      await del(STORE, entry.id);
      await queueChanged();
      return { outcome: "applied", id: entry.id };
    }
    if (isTransientStatus(res.status)) {
      // 429 / 5xx: transient — bounded retries with backoff, then the
      // dead-letter list as RETRYABLE (never silently permanent).
      entry.attempts += 1;
      entry.lastError = "HTTP " + res.status;
      if (entry.attempts >= MAX_ATTEMPTS) {
        await deadLetter(entry, { status: res.status, permanent: false });
        return { outcome: "dead-letter-retryable", id: entry.id };
      }
      entry.nextRetryAt = Date.now() + backoffMs(entry.attempts);
      await put(STORE, entry);
      await queueChanged();
      return { outcome: "retry-later", id: entry.id };
    }
    // Other 4xx: permanent rejection — dead-letter for user inspection.
    await deadLetter(entry, { status: res.status, permanent: true });
    return { outcome: "dead-letter-permanent", id: entry.id };
  }

  /**
   * Replay queued mutations. Shows a "will replay N actions" confirmation
   * first unless {confirmed:true} (the caller already confirmed via its
   * own UI — the integration contract still requires a user-visible
   * confirm step somewhere before this runs).
   */
  async function replay(opts) {
    opts = opts || {};
    const now = Date.now();
    const due = (await all(STORE)).filter(
      (e) => e.attempts < MAX_ATTEMPTS && (e.nextRetryAt || 0) <= now
    );
    if (due.length === 0) {
      return { replayed: 0, outcomes: [] };
    }
    if (!opts.confirmed) {
      const ok = await confirmReplay(due.length);
      if (!ok) return { replayed: 0, cancelled: true, outcomes: [] };
    }
    const outcomes = [];
    for (const entry of due) {
      try {
        outcomes.push(await replayEntry(entry));
      } catch (err) {
        // Still offline (or IndexedDB hiccup): stop this round, keep the
        // rest queued with backoff for the next round. Bounded retries with
        // backoff, EXACTLY like the transient-HTTP path in replayEntry():
        // when attempts reaches MAX_ATTEMPTS the entry dead-letters as
        // RETRYABLE — otherwise the `due` filter would exclude it forever
        // (attempts >= MAX_ATTEMPTS), wedged in the queue, badge-counted,
        // and unrecoverable except manual IndexedDB surgery.
        entry.attempts += 1;
        entry.lastError = String((err && err.message) || err);
        if (entry.attempts >= MAX_ATTEMPTS) {
          try {
            await deadLetter(entry, { status: null, permanent: false });
          } catch (dlErr) {
            /* keep going; entry state already best-effort */
          }
          outcomes.push({ outcome: "dead-letter-retryable", id: entry.id });
          break;
        }
        entry.nextRetryAt = Date.now() + backoffMs(entry.attempts);
        try {
          await put(STORE, entry);
        } catch (putErr) {
          /* keep going; entry state already best-effort */
        }
        outcomes.push({ outcome: "offline", id: entry.id });
        break;
      }
    }
    await queueChanged();
    return { replayed: outcomes.length, outcomes };
  }

  function confirmReplay(count) {
    const label = "Veto will replay " + count + " queued action" + (count === 1 ? "" : "s") +
      " now that you're back online. Duplicates are prevented with idempotency keys.";
    try {
      if (typeof document === "undefined" || !document.body) {
        return Promise.resolve(window.confirm(label));
      }
      return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.setAttribute("data-veto-replay-dialog", String(count));
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.style.cssText =
          "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;" +
          "justify-content:center;background:rgba(0,0,0,.6)";
        const box = document.createElement("div");
        box.style.cssText =
          "background:#0d1117;color:#e6edf3;max-width:24rem;padding:1.5rem;" +
          "border-radius:.5rem;border:1px solid #30363d;font-family:system-ui,sans-serif";
        const p = document.createElement("p");
        p.textContent = label;
        const row = document.createElement("div");
        row.style.cssText = "display:flex;gap:.75rem;justify-content:flex-end;margin-top:1rem";
        const cancel = document.createElement("button");
        cancel.textContent = "Cancel";
        const go = document.createElement("button");
        go.textContent = "Replay " + count + " action" + (count === 1 ? "" : "s");
        go.style.cssText = "background:#238636;color:#fff;border:0;border-radius:.375rem;padding:.5rem 1rem";
        const done = (value) => {
          if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
          resolve(value);
        };
        cancel.addEventListener("click", () => done(false));
        go.addEventListener("click", () => done(true));
        row.appendChild(cancel);
        row.appendChild(go);
        box.appendChild(p);
        box.appendChild(row);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        if (go.focus) go.focus();
      });
    } catch (err) {
      return Promise.resolve(window.confirm(label));
    }
  }

  async function pending() {
    return all(STORE);
  }

  async function deadLetterList() {
    return all(DEAD);
  }

  async function retryDead(id) {
    const entry = await getById(DEAD, id);
    if (!entry) return null;
    await del(DEAD, id);
    const { id: _drop, failedAt: _f, status: _s, error: _e, permanent: _p, ...rest } = entry;
    const out = await enqueueRequest({
      ...rest,
      attempts: 0,
      nextRetryAt: 0,
      lastError: null,
    });
    await queueChanged();
    return out;
  }

  async function discardDead(id) {
    await del(DEAD, id);
    await queueChanged();
  }

  async function pendingCount() {
    return (await all(STORE)).length;
  }

  // F8: never throw before <body> exists (message/event timing) or when
  // there is no DOM at all.
  async function updateBadge() {
    try {
      if (typeof document === "undefined" || !document.body) return;
      const count = await pendingCount();
      const els = document.querySelectorAll("[data-veto-queue-count]");
      for (const el of els) {
        el.textContent = String(count);
        el.hidden = count === 0;
      }
      document.body.classList.toggle("veto-has-queued", count > 0);
    } catch (err) {
      /* badge is advisory — never break queueing */
    }
  }

  async function queueChanged() {
    await updateBadge();
    try {
      if (typeof document !== "undefined" && document.dispatchEvent) {
        const count = await pendingCount();
        document.dispatchEvent(
          new CustomEvent("veto:queue-changed", { detail: { pending: count } })
        );
      }
    } catch (err) {
      /* advisory */
    }
  }

  window.VetoQueue = {
    submit,
    enqueueRequest,
    replay,
    confirmReplay,
    pending,
    pendingCount,
    deadLetter: deadLetterList,
    retryDead,
    discardDead,
    updateBadge,
    MAX_ATTEMPTS,
  };

  // Connectivity returns: update the badge and notify the UI. Replay is
  // NOT automatic — the user confirms first (see replay()).
  window.addEventListener("online", () => {
    queueChanged();
  });
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("DOMContentLoaded", () => {
      updateBadge();
    });
  }
})();
