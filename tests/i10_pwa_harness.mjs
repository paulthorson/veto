#!/usr/bin/env node
/* Veto i10 PWA runtime harness (F7).
 *
 * Loads the REAL initiatives/i10/pwa/service-worker.js and
 * deferred-queue.js in Node with stubbed platform globals (caches,
 * IndexedDB, fetch, DOM) and drives their event handlers end to end.
 * This exercises the actual fetch-routing and queue/replay logic — not
 * just the static shape that pwa.validate_bundle() checks.
 *
 * Exit 0 = all assertions passed. Any failure prints and exits 1.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PWA = path.resolve(HERE, "..", "initiatives", "i10", "pwa") + path.sep;
const ORIGIN = "https://veto.local";

let passed = 0;
let failed = 0;
const failures = [];
function ok(cond, name, detail) {
  if (cond) { passed++; /* console.log("  ok -", name); */ }
  else { failed++; failures.push(`${name}${detail ? " :: " + detail : ""}`); }
}
async function rejects(promise, name, match) {
  try { await promise; ok(false, name, "expected rejection, resolved"); }
  catch (e) { ok(!match || String(e && e.message).includes(match), name, String(e && e.message)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ----------------------------- fakes ---------------------------------- */

class FakeHeaders {
  constructor(init) {
    this.map = new Map();
    if (init instanceof FakeHeaders) for (const [k, v] of init) this.map.set(k, v);
    else if (Array.isArray(init)) for (const [k, v] of init) this.map.set(String(k).toLowerCase(), v);
    else if (init) for (const k of Object.keys(init)) this.map.set(k.toLowerCase(), init[k]);
  }
  get(k) { const v = this.map.get(String(k).toLowerCase()); return v === undefined ? null : v; }
  set(k, v) { this.map.set(String(k).toLowerCase(), v); }
  *[Symbol.iterator]() { for (const e of this.map) yield e; }
}
class FakeResponse {
  constructor(body, init = {}) {
    this._body = body == null ? "" : String(body);
    this.status = init.status || 200;
    this.statusText = init.statusText || "";
    this.headers = init.headers instanceof FakeHeaders ? init.headers : new FakeHeaders(init.headers);
  }
  get ok() { return this.status >= 200 && this.status < 300; }
  async text() { return this._body; }
  async json() { return JSON.parse(this._body); }
  async blob() { return this._body; }
  clone() { return new FakeResponse(this._body, { status: this.status, statusText: this.statusText, headers: this.headers }); }
}
class FakeRequest {
  constructor(url, init = {}) {
    this.url = url; this.method = init.method || "GET"; this.mode = init.mode || "";
    this._body = init.body || ""; this.headers = new FakeHeaders(init.headers);
  }
  clone() { return new FakeRequest(this.url, { method: this.method, mode: this.mode, body: this._body, headers: this.headers }); }
  async text() { return this._body; }
}
function urlOf(u) { return typeof u === "string" ? u : u.url; }
function pathOf(u) { const s = urlOf(u); return s.startsWith("/") ? s : new URL(s).pathname; }

class FakeCache {
  constructor(fetchImpl) { this.map = new Map(); this.fetchImpl = fetchImpl; }
  // Mirror the real Cache API: relative keys resolve against the origin.
  _k(r) { const s = urlOf(r); return s.startsWith("/") ? ORIGIN + s : s; }
  async match(r) { return this.map.get(this._k(r)); }
  async put(r, res) { this.map.set(this._k(r), res); }
  async delete(r) { return this.map.delete(this._k(r)); }
  async keys() { return [...this.map.keys()].map((k) => ({ url: k })); }
  async add(u) {
    const res = await this.fetchImpl(typeof u === "string" && u.startsWith("/") ? ORIGIN + u : u);
    if (!res.ok) throw new TypeError(`cache.add failed for ${u}: HTTP ${res.status}`);
    await this.put(u, res);
  }
  get size() { return this.map.size; }
}
function makeCaches(fetchImpl) {
  const inner = new Map();
  return {
    async open(n) { if (!inner.has(n)) inner.set(n, new FakeCache(fetchImpl)); return inner.get(n); },
    async match(r) { for (const c of inner.values()) { const m = await c.match(r); if (m) return m; } return undefined; },
    async keys() { return [...inner.keys()]; },
    async delete(n) { return inner.delete(n); },
    _inner: inner,
  };
}

/* In-memory IndexedDB fake. Databases are shared per fake instance, so
 * two queue "tabs" built on the same instance share state (cross-tab). */
function domError(name, msg) { const e = new Error(msg || name); e.name = name; return e; }
function makeRequest() { return { result: undefined, error: undefined, onsuccess: null, onerror: null }; }
class FakeStore {
  constructor(name, keyPath, autoIncrement) {
    this.name = name; this.keyPath = keyPath; this.autoIncrement = !!autoIncrement;
    this.records = new Map(); this.nextId = 1; this.indexes = new Map();
  }
  get indexNames() { return { contains: (n) => this.indexes.has(n) }; }
  createIndex(n, kp, opts) { this.indexes.set(n, { keyPath: kp, unique: !!(opts && opts.unique) }); }
  index(n) {
    const idx = this.indexes.get(n);
    const store = this;
    return {
      get(key) {
        const req = makeRequest();
        queueMicrotask(() => {
          for (const v of store.records.values()) {
            if (v[idx.keyPath] === key) { req.result = v; req.onsuccess && req.onsuccess({ target: req }); return; }
          }
          req.result = undefined; req.onsuccess && req.onsuccess({ target: req });
        });
        return req;
      },
    };
  }
  _finish(tx, req, fn) {
    queueMicrotask(() => {
      try { fn(); req.onsuccess && req.onsuccess({ target: req }); }
      catch (e) { req.error = e; req.onerror && req.onerror({ target: req }); }
      queueMicrotask(() => {
        if (req.error) { tx.error = req.error; tx.onerror && tx.onerror({ target: tx }); }
        else { tx.oncomplete && tx.oncomplete({ target: tx }); }
      });
    });
  }
  put(v, tx) {
    const req = makeRequest(); const store = this;
    this._finish(tx, req, () => {
      let key = v[this.keyPath];
      if ((key === undefined || key === null) && this.autoIncrement) { key = this.nextId++; v[this.keyPath] = key; }
      for (const idx of this.indexes.values()) {
        if (!idx.unique) continue;
        const iv = v[idx.keyPath];
        for (const [rk, rv] of this.records) {
          if (rk !== key && rv[idx.keyPath] === iv) throw domError("ConstraintError", "unique index violation");
        }
      }
      this.records.set(key, v); req.result = key;
    });
    return req;
  }
  get(k, tx) { const req = makeRequest(); const s = this; this._finish(tx, req, () => { req.result = s.records.get(k); }); return req; }
  getAll(tx) { const req = makeRequest(); const s = this; this._finish(tx, req, () => { req.result = [...s.records.values()]; }); return req; }
  delete(k, tx) { const req = makeRequest(); const s = this; this._finish(tx, req, () => { s.records.delete(k); }); return req; }
}
function wrapStore(store, tx) {
  return {
    createIndex: (n, kp, o) => store.createIndex(n, kp, o),
    index: (n) => store.index(n),
    get indexNames() { return store.indexNames; },
    put: (v) => store.put(v, tx),
    get: (k) => store.get(k, tx),
    getAll: () => store.getAll(tx),
    delete: (k) => store.delete(k, tx),
  };
}
function makeFakeIndexedDB() {
  const databases = new Map();
  function wrapDb(db) {
    return {
      transaction: (names, mode) => {
        const tx = { error: undefined, oncomplete: null, onerror: null };
        tx.objectStore = (n) => wrapStore(db.stores.get(n), tx);
        return tx;
      },
      createObjectStore: (n, opts) => {
        const s = new FakeStore(n, opts && opts.keyPath, opts && opts.autoIncrement);
        db.stores.set(n, s); return wrapStore(s, { error: undefined });
      },
      get objectStoreNames() { return { contains: (n) => db.stores.has(n) }; },
    };
  }
  return {
    open(name, version) {
      const req = makeRequest();
      queueMicrotask(() => {
        let db = databases.get(name);
        if (!db) { db = { version: 0, stores: new Map() }; databases.set(name, db); }
        if (version > db.version) {
          const wdb = wrapDb(db);
          const upgradeTx = { objectStore: (n) => wrapStore(db.stores.get(n), { error: undefined }) };
          req.result = wdb;
          req.onupgradeneeded && req.onupgradeneeded({ target: { result: wdb, transaction: upgradeTx } });
          db.version = version;
        }
        req.result = wrapDb(db);
        req.onsuccess && req.onsuccess({ target: req });
      });
      return req;
    },
    _databases: databases,
  };
}

/* Minimal DOM for the queue's badge + confirm dialog. */
function makeElem(tag, hooks) {
  const el = {
    tag, children: [], attrs: {}, style: {}, parentNode: null,
    _handlers: {}, textContent: "",
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; if (hooks) hooks.created(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; },
    addEventListener(t, h) { (this._handlers[t] = this._handlers[t] || []).push(h); },
    click() { (this._handlers.click || []).forEach((h) => h({})); },
    focus() {},
  };
  return el;
}
function findDescendant(el, pred) {
  if (pred(el)) return el;
  for (const c of el.children) { const f = findDescendant(c, pred); if (f) return f; }
  return null;
}

/* ------------------------- service worker ------------------------------ */

function loadSW(fetchImpl) {
  const events = {};
  const selfStub = {
    VETO_SW_VERSION: undefined,
    location: { origin: ORIGIN },
    clients: {
      claim: async () => {},
      matchAll: () => { throw new Error("B1: clients.matchAll must not be called (no fan-out)"); },
    },
    skipWaiting: async () => {},
    addEventListener: (t, h) => { events[t] = h; },
  };
  const caches = makeCaches(fetchImpl);
  const warns = [];
  const src = fs.readFileSync(PWA + "service-worker.js", "utf8");
  const importScripts = () => {
    const s = fs.readFileSync(PWA + "sw-version.js", "utf8");
    new Function("self", s)(selfStub);
  };
  new Function(
    "self", "caches", "fetch", "Request", "Response", "Headers", "importScripts", "console",
    `"use strict";\n${src}`
  )(selfStub, caches, fetchImpl, FakeRequest, FakeResponse, FakeHeaders, importScripts,
    { warn: (...a) => warns.push(a.join(" ")), log() {}, error() {} });
  return { events, selfStub, caches, warns };
}
async function swFetch(events, req) {
  const e = { request: req, _p: null, respondWith(p) { this._p = p; } };
  const h = events.fetch;
  if (!h) return undefined;
  h(e);
  return e._p ? await e._p : undefined;
}
const R = (url, init) => new FakeRequest(url, init);

async function testSW() {
  console.log("# service-worker");
  const stamp = JSON.parse(JSON.stringify(
    (fs.readFileSync(PWA + "sw-version.js", "utf8").match(/"([0-9a-f]+)"/) || [])[1] || ""));
  // 1: tolerant install — optional 404s do not fail install
  {
    const fetchImpl = async (u) => {
      const p = pathOf(u);
      if (p === "/" || p === "/offline.html") return new FakeResponse("<html></html>", { status: 200 });
      return new FakeResponse("nope", { status: 404 });
    };
    const { events, caches, warns } = loadSW(fetchImpl);
    const waits = [];
    await events.install({ waitUntil(p) { waits.push(p); } });
    let threw = null;
    try { await Promise.all(waits); } catch (e) { threw = e; }
    ok(!threw, "sw: install succeeds when optional assets 404", threw && threw.message);
    const names = await caches.keys();
    ok(names.some((n) => n.startsWith("veto-shell-") && !n.endsWith("-api")), "sw: shell cache created");
    ok(names.some((n) => n.includes(stamp)), `sw: CACHE_NAME embeds stamped version ${stamp}`, names.join(","));
    const shell = (await caches.open(names.find((n) => n.startsWith("veto-shell-") && !n.endsWith("-api"))));
    ok(await shell.match(ORIGIN + "/offline.html"), "sw: core /offline.html cached");
    ok(warns.some((w) => w.includes("/app.css")), "sw: missing optional asset logged, not fatal", warns.join(" | "));
  }
  // 2: honest failure when a CORE asset is missing
  {
    const fetchImpl = async (u) => {
      const p = pathOf(u);
      if (p === "/") return new FakeResponse("<html></html>", { status: 200 });
      return new FakeResponse("nope", { status: 404 }); // /offline.html missing
    };
    const { events } = loadSW(fetchImpl);
    const waits = [];
    await events.install({ waitUntil(p) { waits.push(p); } });
    await rejects(Promise.all(waits), "sw: install fails when core /offline.html missing", "/offline.html");
  }
  // shared online/offline fetch stub for routing tests
  const apiState = { jobs: [{ id: 1 }], flaky: 0 };
  const fetchImpl = async (u, init = {}) => {
    const req = u instanceof FakeRequest ? u : new FakeRequest(urlOf(u), init);
    const p = new URL(req.url).pathname;
    if (req.method === "POST" && p === "/api/apply") throw new TypeError("offline");
    if (p === "/api/jobs") return new FakeResponse(JSON.stringify(apiState.jobs), { status: 200, headers: { "content-type": "application/json" } });
    if (p === "/api/boom") return new FakeResponse("err", { status: 500 });
    if (p === "/api/echo" && req.method === "POST") return new FakeResponse("ok", { status: 200 });
    if (p === "/style.css") return new FakeResponse("x", { status: 404 });
    if (p === "/good.css") return new FakeResponse("body{}", { status: 200, headers: { "content-type": "text/css" } });
    return new FakeResponse("net", { status: 200 });
  };
  const { events, caches } = loadSW(fetchImpl);
  {
    const waits = [];
    await events.install({ waitUntil(p) { waits.push(p); } });
    await Promise.all(waits);
  }
  // 4: offline mutation -> single 202, no fan-out
  {
    const res = await swFetch(events, R(ORIGIN + "/api/apply", { method: "POST", body: "{}" }));
    ok(res && res.status === 202, "sw: offline mutation -> 202");
    ok(res && (await res.json()).queued === true, "sw: 202 body {queued:true}");
  }
  // 5: form-POST navigation offline -> offline page (NOT a false 202 {queued:true};
  // the page cannot run submit() mid-navigation, so a 202 would be a lie)
  {
    const res = await swFetch(events, R(ORIGIN + "/api/apply", { method: "POST", mode: "navigate", body: "a=1" }));
    ok(res && res.status !== 202, "sw: offline form-POST navigation must not return false 202 {queued:true}");
    ok(res && res.status === 200, "sw: offline form-POST navigation serves the offline page");
  }
  // 6: GET navigation offline -> offline.html (installed while online, then offline)
  {
    let offline = false;
    const fetchImpl = async (u) => {
      if (offline) throw new TypeError("offline");
      const p = pathOf(u);
      if (p === "/" || p === "/offline.html") return new FakeResponse("<html>offline page</html>", { status: 200 });
      return new FakeResponse("x", { status: 404 });
    };
    const { events } = loadSW(fetchImpl);
    const waits = [];
    await events.install({ waitUntil(p) { waits.push(p); } });
    await Promise.all(waits);
    offline = true;
    const res = await swFetch(events, R(ORIGIN + "/dashboard", { mode: "navigate" }));
    ok(res && (await res.text()).includes("offline page"), "sw: offline GET navigation serves offline.html");
  }
  // 7: read API caches 200 with timestamp; never caches 500
  {
    const res = await swFetch(events, R(ORIGIN + "/api/jobs"));
    ok(res && res.status === 200, "sw: read API passes through 200");
    const apiCacheName = (await caches.keys()).find((n) => n.endsWith("-api"));
    const apiCache = await caches.open(apiCacheName);
    const cached = await apiCache.match(ORIGIN + "/api/jobs");
    ok(!!cached, "sw: 200 read-API response cached");
    ok(cached && /^\d+$/.test(cached.headers.get("x-veto-cached-at") || ""), "sw: cached entry carries timestamp");
    await swFetch(events, R(ORIGIN + "/api/boom"));
    ok(!(await apiCache.match(ORIGIN + "/api/boom")), "sw: 500 read-API response NOT cached (F4)");
  }
  // 8: stale cache treated as miss when offline
  {
    let offline = false;
    const fetchImpl = async (u) => {
      if (offline) throw new TypeError("offline");
      const p = pathOf(u);
      if (p === "/" || p === "/offline.html") return new FakeResponse("<html></html>", { status: 200 });
      return new FakeResponse("x", { status: 404 });
    };
    const { events: ev3, caches: c3 } = loadSW(fetchImpl);
    const waits = [];
    await ev3.install({ waitUntil(p) { waits.push(p); } });
    await Promise.all(waits);
    offline = true;
    const apiCache = await c3.open(`veto-shell-${stamp}-api`);
    const stale = new FakeResponse("[]", { status: 200 });
    stale.headers.set("x-veto-cached-at", String(Date.now() - 16 * 60 * 1000));
    await apiCache.put(ORIGIN + "/api/jobs", stale);
    const res = await swFetch(ev3, R(ORIGIN + "/api/jobs"));
    ok(res === undefined, "sw: stale (>TTL) cache entry treated as miss offline");
    const fresh = new FakeResponse("[]", { status: 200 });
    fresh.headers.set("x-veto-cached-at", String(Date.now()));
    await apiCache.put(ORIGIN + "/api/jobs", fresh);
    const res2 = await swFetch(ev3, R(ORIGIN + "/api/jobs"));
    ok(res2 && res2.status === 200, "sw: fresh cache entry served offline");
  }
  // 9: API cache eviction caps entries
  {
    const okFetch = async () => new FakeResponse("[]", { status: 200 });
    const { events: ev4, caches: c4 } = loadSW(okFetch);
    const waits = [];
    await ev4.install({ waitUntil(p) { waits.push(p); } });
    await Promise.all(waits).catch(() => {});
    for (let i = 0; i < 201; i++) await swFetch(ev4, R(ORIGIN + "/api/n" + i));
    const apiCache = await c4.open((await c4.keys()).find((n) => n.endsWith("-api")));
    ok(apiCache.size <= 200, `sw: API cache capped (size=${apiCache.size})`);
    ok(!(await apiCache.match(ORIGIN + "/api/n0")), "sw: oldest API entry evicted first");
    ok(await apiCache.match(ORIGIN + "/api/n200"), "sw: newest API entry retained");
  }
  // 10: read-API branch is GET-only (F10)
  {
    const res = await swFetch(events, R(ORIGIN + "/api/echo", { method: "POST", body: "{}" }));
    ok(res && res.status === 200, "sw: POST to /api passes through network");
    const apiCache = await caches.open((await caches.keys()).find((n) => n.endsWith("-api")));
    ok(!(await apiCache.match(ORIGIN + "/api/echo")), "sw: POST response not written to API cache");
  }
  // 11: shell branch caches only ok (F4)
  {
    await swFetch(events, R(ORIGIN + "/style.css"));
    const shellName = (await caches.keys()).find((n) => n.startsWith("veto-shell-") && !n.endsWith("-api"));
    const shell = await caches.open(shellName);
    ok(!(await shell.match(ORIGIN + "/style.css")), "sw: 404 shell asset NOT cached");
    await swFetch(events, R(ORIGIN + "/good.css"));
    ok(await shell.match(ORIGIN + "/good.css"), "sw: 200 shell asset cached");
  }
  // 12: activate purges old versioned caches
  {
    const { events: ev5, caches: c5 } = loadSW(fetchImpl);
    await c5.open("veto-shell-oldhash");
    const waits = [];
    await ev5.install({ waitUntil(p) { waits.push(p); } });
    await Promise.all(waits).catch(() => {});
    const act = [];
    await ev5.activate({ waitUntil(p) { act.push(p); } });
    await Promise.all(act);
    const names = await c5.keys();
    ok(!names.includes("veto-shell-oldhash"), "sw: activate purges stale versioned caches", names.join(","));
  }
}

/* ------------------------- deferred queue ------------------------------ */

function loadQueue(fetchImpl, idb, opts = {}) {
  const created = [];
  const dispatched = [];
  const bodyEl = makeElem("body");
  const documentStub = {
    body: opts.body === undefined ? bodyEl : opts.body,
    createElement: (t) => { const el = makeElem(t); created.push(el); return el; },
    querySelectorAll: () => [],
    addEventListener() {},
    dispatchEvent(e) { dispatched.push(e); },
  };
  const windowStub = { addEventListener() {}, confirm: () => false };
  let keyCtr = 0;
  const cryptoStub = { randomUUID: () => `test-key-${++keyCtr}` };
  class CustomEventStub { constructor(type, init) { this.type = type; this.detail = (init && init.detail) || {}; } }
  const src = fs.readFileSync(PWA + "deferred-queue.js", "utf8");
  new Function(
    "window", "document", "navigator", "indexedDB", "fetch", "crypto", "CustomEvent", "console",
    `"use strict";\n${src}`
  )(windowStub, documentStub, {}, idb, fetchImpl, cryptoStub, CustomEventStub, { log() {}, warn() {}, error() {} });
  return { Q: windowStub.VetoQueue, created, dispatched, documentStub, bodyEl };
}
function findDialog(created) {
  return created.find((el) => el.attrs && el.attrs["data-veto-replay-dialog"] !== undefined);
}
function clickDialogButton(created, startsWith) {
  const dlg = findDialog(created);
  if (!dlg) return false;
  const btn = findDescendant(dlg, (el) => el.tag === "button" && (el.textContent || "").startsWith(startsWith));
  if (!btn) return false;
  btn.click();
  return true;
}

async function testQueue() {
  console.log("# deferred-queue");
  const netFail = async () => { throw new TypeError("offline"); };

  // 13: one offline mutation -> exactly one queue entry, with idempotency key
  {
    const { Q } = loadQueue(netFail, makeFakeIndexedDB());
    const r = await Q.submit("POST", "/api/apply", { job: 1 });
    ok(r.queued === true && typeof r.idempotencyKey === "string", "queue: offline submit returns {queued:true} + key");
    const pend = await Q.pending();
    ok(pend.length === 1, `queue: exactly one entry queued (got ${pend.length})`);
    ok(pend[0].idempotencyKey === r.idempotencyKey, "queue: entry key matches submit key");
    ok(pend[0].ambiguous === true, "queue: offline entry flagged ambiguous");
  }
  // 14: duplicate enqueue with same key -> one entry (dedup)
  {
    const { Q } = loadQueue(netFail, makeFakeIndexedDB());
    const k = "dup-key-1";
    const a = await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: k });
    const b = await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: k });
    ok(a.deduplicated === false && b.deduplicated === true, "queue: second enqueue with same key deduplicated");
    ok((await Q.pending()).length === 1, "queue: duplicate delivery -> still one entry");
  }
  // 15: cross-tab race on the same key -> one entry
  {
    const idb = makeFakeIndexedDB();
    const t1 = loadQueue(netFail, idb);
    const t2 = loadQueue(netFail, idb);
    const k = "xtab-key-1";
    const [r1, r2] = await Promise.all([
      t1.Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: k }),
      t2.Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: k }),
    ]);
    const n = (await t1.Q.pending()).length;
    ok(n === 1, `queue: cross-tab same key -> one entry (got ${n})`, JSON.stringify([r1.deduplicated, r2.deduplicated]));
  }
  // 16: SW 202 path -> exactly one entry; repeated submit with same key -> still one.
  // The worker's synthetic 202 carries x-veto-offline-queue: 1; a genuine
  // server 202 (same body, no header) must NOT be treated as worker-captured.
  {
    const calls = [];
    const fetch202 = async (url, init) => {
      calls.push({ url, init });
      return new FakeResponse(JSON.stringify({ queued: true }), { status: 202, headers: { "content-type": "application/json", "x-veto-offline-queue": "1" } });
    };
    const { Q } = loadQueue(fetch202, makeFakeIndexedDB());
    const k = "sw202-key-1";
    await Q.submit("POST", "/api/apply", { job: 2 }, { idempotencyKey: k });
    await Q.submit("POST", "/api/apply", { job: 2 }, { idempotencyKey: k });
    ok((await Q.pending()).length === 1, `queue: 202 path enqueues exactly once (got ${(await Q.pending()).length})`);
    ok(calls[0].init.headers["Idempotency-Key"] === k, "queue: original attempt sends Idempotency-Key header");
  }
  // 16b: genuine server 202 without the worker header is returned untouched, not enqueued
  {
    const fetchGenuine202 = async (url, init) => {
      return new FakeResponse(JSON.stringify({ queued: true }), { status: 202, headers: { "content-type": "application/json" } });
    };
    const { Q } = loadQueue(fetchGenuine202, makeFakeIndexedDB());
    const r = await Q.submit("POST", "/api/apply", { job: 3 }, { idempotencyKey: "gen202-key-1" });
    ok((await Q.pending()).length === 0, "queue: genuine server 202 (no worker header) is not enqueued");
    ok(r && r.status === 202, "queue: genuine server 202 is returned to the caller");
  }
  // 17: replay requires confirmation — no fetch before confirm; cancel keeps queue
  {
    const calls = [];
    const rec = async (url, init) => { calls.push({ url, init }); return new FakeResponse("{}", { status: 200 }); };
    const { Q, created } = loadQueue(rec, makeFakeIndexedDB());
    await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: "c-key-1" });
    const p = Q.replay();
    await sleep(30);
    ok(calls.length === 0, "queue: replay makes no network call before confirmation");
    const dlg = findDialog(created);
    ok(!!dlg && dlg.attrs["data-veto-replay-dialog"] === "1", "queue: 'will replay N actions' dialog shown with count");
    ok(clickDialogButton(created, "Cancel"), "queue: cancel button clickable");
    const res = await p;
    ok(res.cancelled === true && calls.length === 0, "queue: cancel -> no replay, entry kept");
    ok((await Q.pending()).length === 1, "queue: cancelled replay keeps the entry");
  }
  // 18: confirmed replay sends Idempotency-Key; 2xx drops the entry
  {
    const calls = [];
    const rec = async (url, init) => { calls.push({ url, init }); return new FakeResponse("{}", { status: 200 }); };
    const { Q, created } = loadQueue(rec, makeFakeIndexedDB());
    await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: "rk-1" });
    const p = Q.replay();
    await sleep(30);
    ok(clickDialogButton(created, "Replay"), "queue: replay confirm clickable");
    const res = await p;
    ok(res.replayed === 1 && res.outcomes[0].outcome === "applied", "queue: replay applied the entry");
    ok(calls[0].init.headers["Idempotency-Key"] === "rk-1", "queue: replay sends Idempotency-Key header");
    ok((await Q.pending()).length === 0, "queue: applied entry removed");
  }
  // 19: 409 (already applied) drops the entry without dead-lettering
  {
    const rec = async () => new FakeResponse("conflict", { status: 409 });
    const { Q, created } = loadQueue(rec, makeFakeIndexedDB());
    await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: "k409" });
    const p = Q.replay({ confirmed: true });
    const res = await p;
    ok(res.outcomes[0].outcome === "applied", "queue: 409 treated as already-applied");
    ok((await Q.pending()).length === 0 && (await Q.deadLetter()).length === 0, "queue: 409 entry dropped, not dead-lettered");
  }
  // 20: 429 is transient — bounded retries with backoff, then retryable dead-letter
  {
    const rec = async () => new FakeResponse("slow", { status: 429 });
    const { Q } = loadQueue(rec, makeFakeIndexedDB());
    ok(Q.MAX_ATTEMPTS === 5, "queue: MAX_ATTEMPTS is 5 (bounded)");
    await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: "k429" });
    let last;
    for (let i = 0; i < Q.MAX_ATTEMPTS; i++) {
      const r = await Q.replay({ confirmed: true });
      last = r.outcomes[0];
      const pend = await Q.pending();
      if (pend.length) pend[0].nextRetryAt = 0; // fast-forward backoff for the test
    }
    ok(last.outcome === "dead-letter-retryable", `queue: 429 exhausts to retryable dead-letter (got ${last.outcome})`);
    const dead = await Q.deadLetter();
    ok(dead.length === 1 && dead[0].permanent === false, "queue: 429 dead-letter is retryable, not permanent");
    ok((await Q.pending()).length === 0, "queue: exhausted entry leaves the queue");
    // retryDead re-queues it
    await Q.retryDead(dead[0].id);
    ok((await Q.pending()).length === 1, "queue: retryDead re-queues");
    const deadId = (await Q.deadLetter())[0]?.id;
    if (deadId !== undefined) { await Q.discardDead(deadId); }
  }
  // 21: plain 400 -> permanent dead-letter
  {
    const rec = async () => new FakeResponse("bad", { status: 400 });
    const { Q } = loadQueue(rec, makeFakeIndexedDB());
    await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: "k400" });
    const res = await Q.replay({ confirmed: true });
    ok(res.outcomes[0].outcome === "dead-letter-permanent", "queue: 400 -> permanent dead-letter");
    const dead = await Q.deadLetter();
    ok(dead.length === 1 && dead[0].permanent === true && dead[0].status === 400, "queue: dead-letter records status + permanent");
  }
  // 22: network error during replay -> entry kept, attempts incremented
  {
    const rec = async () => { throw new TypeError("offline again"); };
    const { Q } = loadQueue(rec, makeFakeIndexedDB());
    await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: "koff" });
    const res = await Q.replay({ confirmed: true });
    ok(res.outcomes[0].outcome === "offline", "queue: replay network error -> offline outcome, round stops");
    const pend = await Q.pending();
    ok(pend.length === 1 && pend[0].attempts === 1 && pend[0].nextRetryAt > Date.now(), "queue: failed entry kept with backoff");
  }
  // 23: updateBadge before <body> exists does not throw (F8)
  {
    const { Q } = loadQueue(netFail, makeFakeIndexedDB(), { body: null });
    let threw = null;
    try { await Q.updateBadge(); } catch (e) { threw = e; }
    ok(!threw, "queue: updateBadge with no <body> does not throw", threw && threw.message);
  }
  // 24: badge reflects pending count when body exists
  {
    const { Q } = loadQueue(netFail, makeFakeIndexedDB());
    await Q.enqueueRequest({ method: "POST", url: "/api/apply", body: "{}", idempotencyKey: "kb1" });
    ok((await Q.pendingCount()) === 1, "queue: pendingCount reflects queue");
  }
  // 25: submit() loud-rejection contract (F8) — positive allowlist
  {
    const { Q } = loadQueue(netFail, makeFakeIndexedDB());
    const rejectsType = async (promise, name) => {
      try { await promise; ok(false, name, "expected TypeError, resolved"); }
      catch (e) { ok(e && e.name === "TypeError", name, "rejected with " + (e && e.name) + ": " + (e && e.message)); }
    };
    await rejectsType(Q.submit("POST", "/api/apply", new FormData()), "queue: FormData body -> TypeError");
    await rejectsType(Q.submit("POST", "/api/apply", new Blob(["x"])), "queue: Blob body -> TypeError");
    await rejectsType(Q.submit("POST", "/api/apply", new ArrayBuffer(8)), "queue: ArrayBuffer body -> TypeError");
    await rejectsType(Q.submit("POST", "/api/apply", new URLSearchParams("a=1")), "queue: URLSearchParams body -> TypeError");
    await rejectsType(Q.submit("POST", "/api/apply", new Uint8Array([1, 2])), "queue: Uint8Array body -> TypeError");
    await rejectsType(Q.submit("POST", "/api/apply", { nested: { file: new Blob(["x"]) } }), "queue: Blob nested in plain object -> TypeError");
    await rejectsType(Q.submit("POST", "/api/apply", new Map([["a", 1]])), "queue: Map body -> TypeError");
    // positive controls: allowed types submit fine
    const r1 = await Q.submit("POST", "/api/apply", { job: 1, tags: ["a", null], ok: true });
    ok(r1.queued === true && typeof r1.idempotencyKey === "string", "queue: plain-object body accepted");
    const r2 = await Q.submit("POST", "/api/apply", '{"job":2}');
    ok(r2.queued === true, "queue: string body accepted");
    const r3 = await Q.submit("POST", "/api/apply", null);
    ok(r3.queued === true, "queue: null body accepted");
    const r4 = await Q.submit("POST", "/api/apply", 42);
    ok(r4.queued === true, "queue: number body accepted");
  }
}

/* ------------------------------- main ---------------------------------- */

const section = process.argv[2];
try {
  if (!section || section === "sw") await testSW();
  if (!section || section === "queue") await testQueue();
} catch (e) {
  failed++;
  failures.push("harness crashed: " + (e && e.stack || e));
}
console.log(`\n${passed} passed, ${failed} failed`);
if (failures.length) { console.log("FAILURES:"); for (const f of failures) console.log(" - " + f); }
process.exit(failed ? 1 : 0);
