// Service worker da Rádio Academia (Fase 6, Decisão 2).
//
// Cuida só dos arquivos do site e do playlist.json, com stale-while-revalidate:
// responde do cache na hora e atualiza o cache em segundo plano.
// NÃO toca em MP3: o player é o único dono do cache de áudio (Decisão 3).
//
// Ao alterar index.html, app.js, shuffle.js, style.css ou a lista PRECACHE, aumente VERSION.
// Isso instala um service worker novo com todos os arquivos da mesma versão juntos;
// o player recarrega sozinho na troca de faixa.

const VERSION = 2;
const CACHE = `static-v${VERSION}`;

const PRECACHE = [
  "./",
  "index.html",
  "app.js",
  "shuffle.js",
  "style.css",
  "manifest.json",
  "playlist.json",
  "creditos/",
  "icons/icon-192.png",
  "icons/icon-512.png",
];

// Tipo esperado por extensão. Resposta com outro tipo não é guardada: protege contra a página
// de login do Cloudflare Access ser guardada no lugar de um arquivo.
const EXPECTED_TYPES = [
  [/(\/|\.html)$/, "text/html"],
  [/\.js$/, "javascript"],
  [/\.css$/, "text/css"],
  [/\.json$/, "json"],
  [/\.png$/, "image/png"],
];

function expectedType(url) {
  const path = new URL(url).pathname;
  return EXPECTED_TYPES.find(([re]) => re.test(path))?.[1] ?? null;
}

function cacheable(url, res) {
  const expected = expectedType(url);
  const type = res.headers.get("content-type") || "";
  return Boolean(
    expected && res.ok && res.type === "basic" && !res.redirected && type.includes(expected),
  );
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // Tudo ou nada: se um arquivo falhar, a instalação falha e a versão anterior continua.
      const responses = await Promise.all(
        PRECACHE.map(async (path) => {
          const url = new URL(path, self.registration.scope).href;
          const res = await fetch(url, { cache: "reload" });
          if (!cacheable(url, res)) {
            throw new Error(`precache recusou ${path}: ${res.status} ${res.headers.get("content-type")}`);
          }
          return [url, res];
        }),
      );
      await Promise.all(responses.map(([url, res]) => cache.put(url, res)));
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        // Só caches do SW; "audio-v1" é do player e nunca é apagado aqui.
        if (name.startsWith("static-") && name !== CACHE) await caches.delete(name);
      }
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // MP3 no R2 e qualquer outro domínio
  if (!expectedType(req.url)) return;
  event.respondWith(staleWhileRevalidate(event, req));
});

async function staleWhileRevalidate(event, req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req, { ignoreSearch: true });
  const network = fetch(req).then(async (res) => {
    if (cacheable(req.url, res)) {
      await cache.put(req.url.split("?")[0], res.clone());
    } else {
      console.warn("[sw] resposta não guardada:", req.url, res.status, res.headers.get("content-type"));
    }
    return res;
  });
  if (cached) {
    event.waitUntil(network.catch((err) => console.warn("[sw] revalidação falhou:", req.url, err.message)));
    return cached;
  }
  return network;
}
