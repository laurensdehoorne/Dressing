// Keeps the app shell available offline. Always tries the network first so updates arrive immediately;
// the API and photos are never cached here: they belong to the signed-in account.
const CACHE = "dressing-v1"; // activating a new version deletes the older caches
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
