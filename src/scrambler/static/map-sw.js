/* Scrambler map tile service worker
 * Provides cache-first tile serving for offline / hybrid map mode.
 * Registered at root scope so it can intercept cross-origin tile requests.
 */
const CACHE_NAME = 'scrambler-tiles-v1';

// Match standard slippy-map tile URLs: /z/x/y.ext
function isTileUrl(urlStr) {
  try {
    const u = new URL(urlStr);
    return /\/\d+\/\d+\/\d+\.(png|jpg|jpeg|webp|avif|pbf|mvt)(\?.*)?$/i.test(u.pathname);
  } catch {
    return false;
  }
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', e => {
  if (!isTileUrl(e.request.url)) return;
  e.respondWith(
    caches.open(CACHE_NAME).then(cache =>
      cache.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(resp => {
          if (resp && (resp.ok || resp.type === 'opaque')) {
            cache.put(e.request, resp.clone());
          }
          return resp;
        }).catch(() => new Response('', { status: 503, statusText: 'Offline' }));
      })
    )
  );
});

self.addEventListener('message', e => {
  if (!e.data) return;
  const port = e.ports && e.ports[0];

  if (e.data.type === 'cache-tiles') {
    const urls = e.data.urls || [];
    if (!urls.length) { port && port.postMessage({ done: 0, total: 0, finished: true }); return; }
    caches.open(CACHE_NAME).then(cache => {
      let done = 0;
      const total = urls.length;
      Promise.all(urls.map(url =>
        cache.match(url).then(cached => {
          if (cached) {
            done++;
            port && port.postMessage({ done, total });
            return;
          }
          return fetch(url, { mode: 'no-cors' })
            .then(resp => { cache.put(url, resp); })
            .catch(() => {})
            .finally(() => { done++; port && port.postMessage({ done, total }); });
        })
      )).then(() => port && port.postMessage({ done: total, total, finished: true }));
    });

  } else if (e.data.type === 'cache-size') {
    caches.open(CACHE_NAME)
      .then(c => c.keys())
      .then(keys => port && port.postMessage({ count: keys.length }));

  } else if (e.data.type === 'clear-cache') {
    caches.delete(CACHE_NAME)
      .then(() => port && port.postMessage({ ok: true }));
  }
});
