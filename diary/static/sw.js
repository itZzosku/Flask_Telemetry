/* Shooting diary service worker.
   - The page itself: network first, last good copy when offline (so the app opens at the range).
   - API reads: network first, last answer when offline, marked with X-Offline so the page can say so.
   - Icons, manifest, fonts and the PDF library: cache first.
   - Logging out clears everything cached. Writes are never cached; the page queues them itself.
   - Share target: a file shared to the installed app (e.g. from ShotView) is POSTed to chrono/share;
     it is kept in the SHARE cache and the velocity page opens and imports it. */
const VERSION = "v9";
const PAGE = "diary-page-" + VERSION, DATA = "diary-data", ASSETS = "diary-assets-" + VERSION, SHARE = "diary-share";
const BASE = new URL("./", self.registration.scope).pathname;            // "/diary/"
const ASSET_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com"];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil((async () => {
  const keep = [PAGE, DATA, ASSETS, SHARE];
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
    e.respondWith((async () => { await caches.delete(DATA); await caches.delete(PAGE); await caches.delete(SHARE); return fetch(req); })());
    return;
  }
  if (local && req.method === "POST" && url.pathname === BASE + "chrono/share") {
    e.respondWith((async () => {
      try {
        const form = await req.formData();
        const files = form.getAll("files").filter(f => f && typeof f === "object" && f.size);
        await caches.delete(SHARE);
        const cache = await caches.open(SHARE);
        let i = 0;
        for (const f of files.slice(0, 20)) {
          await cache.put(BASE + "__shared/" + (i++),
            new Response(f, {headers: {"X-Name": encodeURIComponent(f.name || "shared.xls"),
                                       "Content-Type": f.type || "application/octet-stream"}}));
        }
        return Response.redirect(BASE + "chrono?shared=" + i, 303);
      } catch (err) {
        return Response.redirect(BASE + "chrono?shared=error", 303);
      }
    })());
    return;
  }
  if (req.method !== "GET") return;

  if (local && req.mode === "navigate" && (url.pathname === BASE || url.pathname === BASE + "chrono" || url.pathname === BASE + "zero")) {
    e.respondWith(networkFirst(req, PAGE, url.pathname).catch(() =>
      new Response("<p style='font:16px system-ui;padding:24px'>Offline. Open the app once while online and it will work offline after that.</p>",
                   {headers: {"Content-Type": "text/html; charset=utf-8"}})));
  } else if (local && url.pathname.startsWith(BASE + "api/")) {
    e.respondWith(networkFirst(req, DATA, null, true).catch(() =>
      new Response(JSON.stringify({error: "offline"}), {status: 503, headers: {"Content-Type": "application/json"}})));
  } else if (local && url.pathname === BASE + "manifest.webmanifest") {
    e.respondWith(networkFirst(req, ASSETS));          // never a stale manifest (share target, name)
  } else if ((local && url.pathname.startsWith(BASE + "static/")) || ASSET_HOSTS.includes(url.hostname)) {
    e.respondWith(cacheFirst(req));
  }
});
