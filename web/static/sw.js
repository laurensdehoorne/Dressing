// Keeps the app shell available offline. Always tries the network first so updates arrive immediately;
// the API and photos are never cached here: they belong to the signed-in account.
const CACHE = "dressing-v2"; // activating a new version deletes the older caches
const SHELL = ["/", "/app.js", "/style.css", "/manifest.webmanifest", "/apple-touch-icon.png", "/icon-192.png",
  "/fonts/bricolage-grotesque.woff2", "/fonts/figtree.woff2"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname.startsWith("/photos/")) return;
  event.respondWith(
    fetch(event.request, { cache: "no-cache" })
      .then((response) => {
        if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((cache) => cache.put(event.request, copy)); }
        return response;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match("/")))
  );
});

// Web Push: "Hoe viel je outfit?" in the evening. The message is {title, body, url, tag?}.
self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch { msg = { body: event.data?.text() }; }
  event.waitUntil(self.registration.showNotification(msg.title || "Dressing", {
    body: msg.body || "", icon: "/icon-192.png",
    data: { url: msg.url || "/" }, // what a tap opens (e.g. /#gedragen=<id>)
    tag: msg.tag || "dressing", // a newer one replaces the older of its kind
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const app = open.find((c) => c.url.startsWith(self.location.origin));
    if (app) { // already open: bring it forward, and let it open what the notification is about
      await app.focus();
      if (new URL(url).hash) app.postMessage({ open: new URL(url).hash.slice(1) });
      return;
    }
    await self.clients.openWindow(url);
  })());
});
