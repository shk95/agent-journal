"use strict";

const shellCache = "agent-journal-shell-v1";
const shellPaths = ["/", "/app.js", "/vi.js", "/notes.js", "/style.css", "/theme.js"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(shellCache)
    .then(cache => cache.addAll(shellPaths.map(path => new Request(path, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !shellPaths.includes(url.pathname))
    return;
  const path = url.pathname;
  event.respondWith(caches.open(shellCache).then(async cache => {
    try {
      const response = await fetch(event.request);
      if (response.ok) await cache.put(path, response.clone()).catch(() => {});
      return response;
    } catch (error) {
      const offline = await cache.match(path);
      if (offline) return offline;
      throw error;
    }
  }));
});
