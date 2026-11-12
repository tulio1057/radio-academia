// Player da Rádio Academia (Fase 5).
//
// Fluxo de cada faixa (Decisão 3): fetch() com timeout → Cache API "audio-v1" → blob URL → <audio>.
// A próxima faixa é baixada enquanto a atual toca. No máximo dois blobs em memória
// (atual e próxima); todo blob URL descartado passa por URL.revokeObjectURL.
//
// Falhas:
//   - erro definitivo da faixa (HTTP, content-type, arquivo corrompido) → quarentena temporária;
//   - erro de rede ou timeout → só pula (a faixa não tem culpa);
//   - K falhas seguidas → pausa de 60 s e nova tentativa.
// Tudo é registrado no console com o prefixo [radio].

import { nextTrack, emptyState } from "./shuffle.js";

const CONFIG = {
  playlistUrl: "playlist.json",
  // O catálogo muda a cada deploy e o player fica aberto por dias.
  playlistRefreshMs: 6 * 60 * 60_000,
  playlistRetryMs: 60_000,
  audioCache: "audio-v1",
  fetchTimeoutMs: 45_000,
  // ponytail: limite por número de faixas; upgrade (Fase 6): limite em MB com LRU
  maxCachedTracks: 30,
  failuresBeforePause: 3,
  pauseAfterFailuresMs: 60_000,
  quarantineMs: 10 * 60_000,
  artistWindow: 3,
};

const log = (...args) => console.info("[radio]", ...args);
const warn = (...args) => console.warn("[radio]", ...args);

class TrackError extends Error {
  constructor(message, { quarantine }) {
    super(message);
    this.quarantine = quarantine;
  }
}

// ---------------------------------------------------------------------------
// Playlist
// ---------------------------------------------------------------------------

async function fetchPlaylist() {
  const res = await fetch(CONFIG.playlistUrl, { cache: "no-cache" });
  if (!res.ok) throw new Error(`playlist: HTTP ${res.status}`);
  // Uma página de login (ex.: Cloudflare Access) não pode ser lida como playlist.
  const type = res.headers.get("content-type") || "";
  if (!type.includes("json")) throw new Error(`playlist: content-type inesperado "${type}"`);
  const data = await res.json();
  if (!Array.isArray(data.tracks)) throw new Error("playlist: campo tracks ausente");
  const tracks = data.tracks.filter((t) => t && t.id && t.url && t.title && t.artist);
  if (tracks.length !== data.tracks.length) {
    warn(`playlist: ${data.tracks.length - tracks.length} faixa(s) incompleta(s) ignorada(s)`);
  }
  return tracks;
}

// ---------------------------------------------------------------------------
// Áudio: download, cache e blob
// ---------------------------------------------------------------------------

let cachePromise = null;

function openCache() {
  if (!("caches" in window)) return Promise.resolve(null); // fora de HTTPS/localhost
  cachePromise ??= caches.open(CONFIG.audioCache).catch((err) => {
    warn("Cache API indisponível:", err);
    return null;
  });
  return cachePromise;
}

async function download(track) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CONFIG.fetchTimeoutMs);
  try {
    // no-store: o Cache API é a única cópia guardada (evita a segunda cópia no cache HTTP)
    const res = await fetch(track.url, { signal: controller.signal, cache: "no-store" });
    if (!res.ok) throw new TrackError(`HTTP ${res.status}`, { quarantine: true });
    const type = res.headers.get("content-type") || "";
    if (!type.startsWith("audio/")) {
      throw new TrackError(`content-type inesperado "${type}"`, { quarantine: true });
    }
    return await res.blob(); // o timeout também cobre o corpo
  } catch (err) {
    if (err instanceof TrackError) throw err;
    const reason = controller.signal.aborted
      ? `tempo esgotado (${CONFIG.fetchTimeoutMs / 1000} s)`
      : `rede/CORS: ${err.message}`;
    throw new TrackError(reason, { quarantine: false });
  } finally {
    clearTimeout(timer);
  }
}

async function storeInCache(cache, track, blob, keep) {
  try {
    await cache.put(track.url, new Response(blob, { headers: { "content-type": blob.type } }));
  } catch (err) {
    // Cache cheio não impede tocar: o blob já está em memória.
    warn(`não foi possível guardar ${track.id} no cache:`, err.name, err.message);
    return;
  }
  const keys = await cache.keys(); // ordem de inserção: as mais antigas primeiro
  let excess = keys.length - CONFIG.maxCachedTracks;
  for (const req of keys) {
    if (excess <= 0) break;
    if (keep.has(req.url)) continue;
    await cache.delete(req);
    excess--;
  }
}

async function loadBlob(track, keep) {
  const cache = await openCache();
  if (cache) {
    const hit = await cache.match(track.url);
    if (hit) {
      log(`cache: ${track.id}`);
      return hit.blob();
    }
  }
  const started = performance.now();
  const blob = await download(track);
  log(`baixada ${track.id} (${(blob.size / 1e6).toFixed(1)} MB em ${((performance.now() - started) / 1000).toFixed(1)} s)`);
  if (cache) await storeInCache(cache, track, blob, keep);
  return blob;
}

async function forgetCached(track) {
  const cache = await openCache();
  if (cache && (await cache.delete(track.url))) log(`removida do cache: ${track.id}`);
}

// ---------------------------------------------------------------------------
// Player
// ---------------------------------------------------------------------------

class Player {
  constructor(audio, ui) {
    this.audio = audio;
    this.ui = ui;
    this.tracks = [];
    this.shuffle = emptyState();
    this.quarantine = new Map(); // id → instante em que volta a valer
    this.current = null; // { track, url }
    this.next = null; // { track, promise<url> }
    this.failures = 0;
    this.seq = 0; // invalida operações antigas quando o usuário pula de novo
    this.loading = false;
    this.retryTimer = null;
    this.wakeLock = null;

    audio.addEventListener("ended", () => this.advance());
    audio.addEventListener("error", () => this.onAudioError());
    audio.addEventListener("loadedmetadata", () => this.ui.progress(0, audio.duration));
    audio.addEventListener("timeupdate", () => this.ui.progress(audio.currentTime, audio.duration));
    audio.addEventListener("playing", () => {
      this.failures = 0;
      this.ui.playing(true);
      this.requestWakeLock();
    });
    audio.addEventListener("pause", () => this.ui.playing(false));
    audio.addEventListener("stalled", () => log("evento stalled"));
    audio.addEventListener("waiting", () => log("evento waiting"));
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !audio.paused) this.requestWakeLock();
    });
    this.setupMediaSession();
  }

  setTracks(tracks) {
    this.tracks = tracks;
    log(`playlist: ${tracks.length} faixa(s)`);
  }

  available() {
    const now = Date.now();
    for (const [id, until] of this.quarantine) if (until <= now) this.quarantine.delete(id);
    return this.tracks.filter((t) => !this.quarantine.has(t.id));
  }

  pick() {
    const r = nextTrack(this.available(), this.shuffle, { artistWindow: CONFIG.artistWindow });
    this.shuffle = r.state;
    return r.track;
  }

  urlsInUse() {
    return new Set([this.current?.track.url, this.next?.track.url].filter(Boolean));
  }

  prepare(track) {
    const keep = this.urlsInUse().add(track.url);
    const promise = loadBlob(track, keep).then((blob) => URL.createObjectURL(blob));
    promise.catch(() => {}); // a falha é tratada em advance(); evita aviso de promessa sem catch
    return { track, promise };
  }

  discard(item) {
    item?.promise.then((url) => URL.revokeObjectURL(url), () => {});
  }

  preloadNext() {
    if (this.next) return;
    const track = this.pick();
    if (track) {
      log(`pré-carregando ${track.id}`);
      this.next = this.prepare(track);
    }
  }

  stopCurrent() {
    const old = this.current;
    this.current = null; // antes de mexer no <audio>, para o handler de erro ignorar
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    if (old) URL.revokeObjectURL(old.url);
  }

  // Vai para a próxima faixa: usa a pré-carregada se houver, senão escolhe e baixa.
  async advance() {
    const seq = ++this.seq;
    clearTimeout(this.retryTimer);
    this.stopCurrent();
    this.loading = true;
    this.ui.busy(true);
    try {
      while (seq === this.seq) {
        let item = this.next;
        this.next = null;
        if (!item) {
          const track = this.pick();
          if (!track) {
            this.pauseAndRetry("Nenhuma faixa disponível.");
            return;
          }
          item = this.prepare(track);
        }
        this.ui.status(`Carregando “${item.track.title}”…`);

        let url;
        try {
          url = await item.promise;
        } catch (err) {
          if (seq !== this.seq) return;
          if (this.registerFailure(item.track, err)) return;
          continue;
        }
        if (seq !== this.seq) {
          URL.revokeObjectURL(url); // outro advance() assumiu
          return;
        }

        this.current = { track: item.track, url };
        this.audio.src = url;
        this.ui.track(item.track);
        this.updateMediaSession(item.track);
        log(`tocando ${item.track.id}`);
        await this.play();
        this.preloadNext();
        return;
      }
    } finally {
      if (seq === this.seq) {
        this.loading = false;
        this.ui.busy(false);
      }
    }
  }

  // Devolve true se o player entrou em pausa por falhas seguidas.
  registerFailure(track, err) {
    warn(`falha em ${track.id}: ${err.message}`);
    if (err.quarantine) {
      this.quarantine.set(track.id, Date.now() + CONFIG.quarantineMs);
      log(`quarentena de ${CONFIG.quarantineMs / 60_000} min: ${track.id}`);
    }
    this.failures++;
    if (this.failures >= CONFIG.failuresBeforePause) {
      this.failures = 0;
      this.pauseAndRetry(`${CONFIG.failuresBeforePause} falhas seguidas.`);
      return true;
    }
    this.ui.status(`Faixa indisponível, pulando…`);
    return false;
  }

  pauseAndRetry(reason) {
    const seconds = CONFIG.pauseAfterFailuresMs / 1000;
    warn(`${reason} Nova tentativa em ${seconds} s.`);
    this.ui.status(`${reason} Nova tentativa em ${seconds} s.`, true);
    this.ui.busy(false);
    this.discard(this.next);
    this.next = null;
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this.advance(), CONFIG.pauseAfterFailuresMs);
  }

  async onAudioError() {
    if (!this.current) return;
    const { track } = this.current;
    const code = this.audio.error?.code;
    this.stopCurrent();
    // code 3/4: decodificação ou formato → arquivo ruim; o cache não pode guardar a cópia ruim
    await forgetCached(track);
    const err = new TrackError(`erro no elemento de áudio (código ${code})`, { quarantine: true });
    if (!this.registerFailure(track, err)) this.advance();
  }

  async play() {
    try {
      await this.audio.play();
      this.ui.status("");
    } catch (err) {
      if (err.name === "NotAllowedError") {
        // Chrome sem --autoplay-policy=no-user-gesture-required (Decisão 8)
        log("autoplay bloqueado; aguardando clique");
        this.ui.status("Clique em ▶ para começar.", true);
      } else if (err.name !== "AbortError") {
        warn("play() falhou:", err);
      }
    }
  }

  // Botão play/pause.
  toggle() {
    if (this.current) {
      if (this.audio.paused) this.play();
      else this.audio.pause();
    } else if (!this.loading) {
      this.advance();
    }
  }

  skip() {
    if (!this.tracks.length) return;
    log("próxima (botão)");
    this.advance();
  }

  async requestWakeLock() {
    if (!("wakeLock" in navigator) || this.wakeLock || document.visibilityState !== "visible") return;
    try {
      this.wakeLock = await navigator.wakeLock.request("screen");
      this.wakeLock.addEventListener("release", () => (this.wakeLock = null));
      log("wake lock ativo");
    } catch (err) {
      warn("wake lock indisponível:", err.name, err.message);
    }
  }

  setupMediaSession() {
    if (!("mediaSession" in navigator)) return;
    const handlers = {
      play: () => this.toggle(),
      pause: () => this.audio.pause(),
      nexttrack: () => this.skip(),
    };
    for (const [action, handler] of Object.entries(handlers)) {
      try {
        navigator.mediaSession.setActionHandler(action, handler);
      } catch (err) {
        warn(`mediaSession ${action}:`, err.message);
      }
    }
  }

  updateMediaSession(track) {
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title,
      artist: track.artist,
      album: track.album || "",
    });
  }
}

// ---------------------------------------------------------------------------
// Interface
// ---------------------------------------------------------------------------

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function createUi() {
  const $ = (id) => document.getElementById(id);
  const el = {
    play: $("play"), next: $("next"), title: $("title"), artist: $("artist"),
    status: $("status"), fill: $("fill"), bar: $("bar"), elapsed: $("elapsed"),
    duration: $("duration"), attribution: $("attribution"), license: $("license"),
    source: $("source"), credit: $("credit"),
  };
  let knownDuration = 0;
  return {
    el,
    track(track) {
      el.title.textContent = track.title;
      el.artist.textContent = track.artist;
      el.attribution.textContent = track.attribution;
      el.license.href = track.license_url;
      el.license.textContent = track.license.replace(/-/g, " "); // CC-BY-4.0 → CC BY 4.0
      el.source.href = track.source_url;
      el.credit.hidden = false;
      knownDuration = track.duration || 0;
      this.progress(0, knownDuration);
      document.title = `${track.title} — ${track.artist}`;
    },
    progress(current, duration) {
      const total = Number.isFinite(duration) && duration > 0 ? duration : knownDuration;
      const ratio = total ? Math.min(current / total, 1) : 0;
      el.fill.style.transform = `scaleX(${ratio})`;
      el.bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
      el.elapsed.textContent = formatTime(current);
      el.duration.textContent = formatTime(total);
    },
    playing(isPlaying) {
      el.play.classList.toggle("is-playing", isPlaying);
      el.play.setAttribute("aria-label", isPlaying ? "Pausar" : "Tocar");
    },
    busy(isBusy) {
      document.body.classList.toggle("is-busy", isBusy);
    },
    status(text, important = false) {
      el.status.textContent = text;
      el.status.classList.toggle("is-important", important && Boolean(text));
    },
  };
}

// ---------------------------------------------------------------------------
// Início
// ---------------------------------------------------------------------------

async function main() {
  const ui = createUi();
  const player = new Player(document.getElementById("audio"), ui);
  window.radio = { player, config: CONFIG }; // para diagnóstico pelo console (F12)

  ui.el.play.addEventListener("click", () => player.toggle());
  ui.el.next.addEventListener("click", () => player.skip());

  async function refresh() {
    try {
      const tracks = await fetchPlaylist();
      player.setTracks(tracks);
      return true;
    } catch (err) {
      warn(err.message);
      return false;
    }
  }

  ui.status("Carregando a lista de músicas…");
  while (!(await refresh())) {
    ui.status(`Lista de músicas indisponível. Nova tentativa em ${CONFIG.playlistRetryMs / 1000} s.`, true);
    await new Promise((r) => setTimeout(r, CONFIG.playlistRetryMs));
  }
  setInterval(refresh, CONFIG.playlistRefreshMs);
  player.advance(); // lista vazia cai em "Nenhuma faixa disponível" e tenta de novo em 60 s
}

main();
