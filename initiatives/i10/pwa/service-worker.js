/* Veto service worker — offline-safe read views + deferred actions.
 *
 * Strategy:
 *  - App shell (HTML/CSS/JS/manifest/icons): cache-first, versioned by
 *    CACHE_NAME. Old caches are purged on activate.
 *  - Read APIs (GET under /api/): network-first with cache fallback, a
 *    15-minute freshness bound, and a 200-entry cap with oldest-first
 *    eviction. Only 2xx responses are cached — never error pages.
 *  - Mutations (POST/PUT/PATCH/DELETE): when the network fails, the
 *    worker does NOT enqueue anything itself — the PAGE
 *    (deferred-queue.js) is the single owner of the deferred-action
 *    queue. Two offline outcomes:
 *      * Non-navigation mutations (fetch/XHR from a live page): the
 *        worker answers the synthetic 202 {queued:true}, namespaced with
 *        the `x-veto-offline-queue` response header, so the page can
 *        capture it in VetoQueue.submit() and enqueue it.
 *      * Navigation-mode mutations (form POSTs mid-navigation): the page
 *        cannot run submit() mid-navigation, so a synthetic 202 would
 *        claim the action was queued while the form data is irrecoverably
 *        dropped. The worker serves offline.html instead (honest).
 *    The old `veto:queue-mutation` postMessage fan-out was removed: it
 *    delivered the same mutation to every open tab and double-queued
 *    alongside the 202 path (Initiative 10, WS5 review B1).
 *  - Navigation while offline: serve offline.html.
 *
 * Versioning: CACHE_NAME embeds a content hash stamped by
 * `python -m initiatives.i10.pwa stamp` into sw-version.js at bundle
 * time. validate_bundle() fails CI when the stamp is stale, so a deploy
 * can never silently serve a stale shell.
 */

try {
  // Sets self.VETO_SW_VERSION. Served from the web root alongside this
  // worker (see integration_notes.md §2). Missing file => "dev".
  importScripts("/sw-version.js");
} catch (err) {
  /* stamp not served (pre-wiring) — install still works, cache is "dev" */
}

const SW_VERSION = (self.VETO_SW_VERSION || "dev").toString();
const CACHE_NAME = "veto-shell-" + SW_VERSION;
const API_CACHE = CACHE_NAME + "-api";

// Install succeeds if the CORE shell cached; a missing optional asset is
// logged, never fatal. (Previously cache.addAll() made one 404 — e.g.
// /app.css before the webui wiring lands — fail the whole install.)
const CORE_ASSETS = ["/", "/offline.html"];
const OPTIONAL_ASSETS = [
  "/manifest.webmanifest",
  "/app.css",
  "/app.js",
  "/deferred-queue.js",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

// Read-API freshness bound + size cap (F5).
const API_TTL_MS = 15 * 60 * 1000;
const API_MAX_ENTRIES = 200;
const CACHED_AT_HEADER = "x-veto-cached-at";

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      const core = await Promise.allSettled(
        CORE_ASSETS.map((url) => cache.add(url))
      );
      const coreFailures = CORE_ASSETS.filter((_, i) => core[i].status === "rejected");
      if (coreFailures.length > 0) {
        // Honest failure: without the core shell there is nothing to serve.
        throw new Error("SW install failed — core assets missing: " + coreFailures.join(", "));
      }
      const optional = await Promise.allSettled(
        OPTIONAL_ASSETS.map((url) => cache.add(url))
      );
      OPTIONAL_ASSETS.forEach((url, i) => {
        if (optional[i].status === "rejected") {
          console.warn("[veto-sw] optional asset not cached:", url);
        }
      });
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== CACHE_NAME && k !== API_CACHE)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

function isReadApi(url) {
  return url.pathname.startsWith("/api/");
}

function isMutation(request) {
  return ["POST", "PUT", "PATCH", "DELETE"].includes(request.method);
}

async function stampedCopy(response) {
  const headers = new Headers(response.headers);
  headers.set(CACHED_AT_HEADER, Date.now().toString());
  return new Response(await response.clone().blob(), {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function evictOldestApiEntries(cache) {
  const keys = await cache.keys();
  if (keys.length <= API_MAX_ENTRIES) return;
  const withTs = await Promise.all(
    keys.map(async (key) => {
      const entry = await cache.match(key);
      const ts = parseInt(
        (entry && entry.headers.get(CACHED_AT_HEADER)) || "0",
        10
      );
      return { key, ts };
    })
  );
  withTs.sort((a, b) => a.ts - b.ts);
  const overflow = withTs.slice(0, keys.length - API_MAX_ENTRIES);
  await Promise.all(overflow.map((e) => cache.delete(e.key)));
}

async function freshApiMatch(request) {
  const cache = await caches.open(API_CACHE);
  const cached = await cache.match(request);
  if (!cached) return undefined;
  const ts = parseInt(cached.headers.get(CACHED_AT_HEADER) || "0", 10);
  if (Date.now() - ts > API_TTL_MS) {
    await cache.delete(request); // stale: never serve, refetch when online
    return undefined;
  }
  return cached;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // third-party: passthrough

  // Mutations (including form-POST navigations): try network; on network
  // failure the worker does NOT enqueue (ENQUEUE OWNERSHIP: the page,
  // deferred-queue.js, is the single owner of the queue — the worker never
  // enqueues and never postMessages; the page generates the idempotency
  // key and replays it via the Idempotency-Key header).
  //   - Non-navigation mutations: answer the synthetic 202 {queued:true},
  //     namespaced with x-veto-offline-queue: 1 so submit() never mistakes
  //     a genuine server 202 for the worker's capture.
  //   - Navigation-mode mutations (form POSTs mid-navigation): the page
  //     cannot run submit() mid-navigation, so a synthetic 202 would be a
  //     false claim — serve offline.html (or a plain 503 if it is not
  //     cached) instead. The form data is not queued; the user is told
  //     the truth.
  if (isMutation(request)) {
    event.respondWith(
      (async () => {
        try {
          return await fetch(request.clone());
        } catch (err) {
          if (request.mode === "navigate") {
            return (await caches.match("/offline.html")) ||
              new Response("Offline — this action was not queued.", { status: 503 });
          }
          return new Response(JSON.stringify({ queued: true, reason: "offline" }), {
            status: 202,
            headers: {
              "Content-Type": "application/json",
              "x-veto-offline-queue": "1",
            },
          });
        }
      })()
    );
    return;
  }

  // Offline navigation fallback. GET navigations reach here; POST
  // navigation mutations are handled — and served offline.html while
  // offline — by the mutation branch above.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/offline.html")));
    return;
  }

  // Read APIs: GET only, network-first with fresh-cache fallback.
  // Only 2xx responses are cached (F4); entries carry a cache timestamp
  // for the TTL bound and oldest-first eviction (F5).
  if (request.method === "GET" && isReadApi(url)) {
    event.respondWith(
      (async () => {
        try {
          const response = await fetch(request);
          if (response.ok) {
            const cache = await caches.open(API_CACHE);
            await cache.put(request, await stampedCopy(response));
            await evictOldestApiEntries(cache);
          }
          return response;
        } catch (err) {
          return freshApiMatch(request);
        }
      })()
    );
    return;
  }

  // Shell assets: cache-first. Only 2xx responses are cached (F4).
  event.respondWith(
    caches.match(request).then(
      (cached) =>
        cached ||
        fetch(request).then(async (response) => {
          // GET-only caching: cache.put throws TypeError for non-GET
          // requests, and HEAD/OPTIONS can reach this branch.
          if (response.ok && request.method === "GET") {
            const cache = await caches.open(CACHE_NAME);
            await cache.put(request, response.clone());
          }
          return response;
        })
    )
  );
});
