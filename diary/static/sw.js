/* Ampumapäiväkirja service worker.
   - The page itself: network first, last good copy when offline (so the app opens at the range).
   - API reads: network first, last answer when offline, marked with X-Offline so the page can say so.
   - Icons, manifest, fonts and the PDF library: cache first.
   - Logging out clears everything cached. Writes are never cached; the page queues them itself. */
const VERSION = "v1";
const PAGE = "diary-page-" + VERSION, DATA = "diary-data", ASSETS = "diary-assets-" + VERSION;
const BASE = new URL("./", self.registration.scope).pathname;            // "/diary/"
const ASSET_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com"];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil((async () => {
  const keep = [PAGE, DATA, ASSETS];
  for (const k of await caches.keys()) if (k.startsWith("diary-") && !keep.includes(k)) await caches.delete(k);
  await self.clients.claim();
})()));

async function networkFirst(req, cacheName, key, mark) {
  try {
    const res = await fetch(req);
    if (res.ok && !res.redirected) (await caches.open(cacheName)).put(key || req, res.clone());
    return res;
  } catch (err) {
    const hit = await caches.match(key || req, {cacheName});
    if (!hit) throw err;
    if (!mark) return hit;
    const h = new Headers(hit.headers); h.set("X-Offline", "1");
    return new Response(await hit.blob(), {status: hit.status, statusText: hit.statusText, headers: h});
  }
}
async function cacheFirst(req) {
  const hit = await caches.match(req, {cacheName: ASSETS});
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === "opaque") (await caches.open(ASSETS)).put(req, res.clone());
  return res;
}

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  const local = url.origin === self.location.origin;

  if (local && req.method === "POST" && url.pathname === BASE + "logout") {
    e.respondWith((async () => { await caches.delete(DATA); await caches.delete(PAGE); return fetch(req); })());
    return;
  }
  if (req.method !== "GET") return;

  if (local && req.mode === "navigate" && url.pathname === BASE) {
    e.respondWith(networkFirst(req, PAGE, BASE).catch(() =>
      new Response("<p style='font:16px system-ui;padding:24px'>Ei yhteyttä. Avaa sovellus kerran verkossa, niin se toimii jatkossa myös ilman yhteyttä.</p>",
                   {headers: {"Content-Type": "text/html; charset=utf-8"}})));
  } else if (local && url.pathname.startsWith(BASE + "api/")) {
    e.respondWith(networkFirst(req, DATA, null, true).catch(() =>
      new Response(JSON.stringify({error: "offline"}), {status: 503, headers: {"Content-Type": "application/json"}})));
  } else if ((local && (url.pathname.startsWith(BASE + "static/") || url.pathname === BASE + "manifest.webmanifest"))
             || ASSET_HOSTS.includes(url.hostname)) {
    e.respondWith(cacheFirst(req));
  }
});
