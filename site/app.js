// Player da Rádio Academia (Fases 5 e 6).
//
// Fluxo de cada faixa (Decisão 3): fetch() com timeout → Cache API "audio-v1" → blob URL → <audio>.
// A próxima faixa é baixada enquanto a atual toca. No máximo dois blobs em memória
// (atual e próxima); todo blob URL descartado passa por URL.revokeObjectURL.
//
// Falhas:
//   - erro definitivo da faixa (HTTP, content-type, arquivo corrompido) → quarentena temporária;
//   - erro de rede ou timeout → modo offline: só faixas já baixadas, até a rede voltar;
//   - K falhas seguidas → pausa de 60 s e nova tentativa.
// Tudo é registrado no console com o prefixo [radio].

import { nextTrack, emptyState } from "./shuffle.js";

const CONFIG = {
  playlistUrl: "playlist.json",
  // O catálogo muda a cada deploy e o player fica aberto por dias.
  playlistRefreshMs: 6 * 60 * 60_000,
  playlistRetryMs: 60_000,
  audioCache: "audio-v1",
  // Limite do cache de áudio. Também nunca passa de metade da cota que o navegador oferece.
  audioCacheMb: 1000,
  fetchTimeoutMs: 45_000,
  failuresBeforePause: 3,
  pauseAfterFailuresMs: 60_000,
  quarantineMs: 10 * 60_000,
  artistWindow: 3,
  // Offline: de quanto em quanto tempo testar se a rede voltou.
  probeIntervalMs: 60_000,
  probeTimeoutMs: 10_000,
};

const log = (...args) => console.info("[radio]", ...args);
const warn = (...args) => console.warn("[radio]", ...args);
const mb = (bytes) => `${(bytes / 1e6).toFixed(1)} MB`;

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
// Download
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Cache de áudio: Cache API + índice de uso (LRU por MB)
// ---------------------------------------------------------------------------

// O Cache API não guarda "último uso"; o índice fica no localStorage: url → {bytes, used}.
// Se o localStorage falhar, a evicção cai para a ordem de inserção (used = 0).
const INDEX_KEY = "radio.audio-index";

const store = {
  cache: null,
  index: new Map(),
  limitBytes: CONFIG.audioCacheMb * 1e6,

  async init() {
    if (!("caches" in window)) {
      warn("Cache API indisponível (é preciso HTTPS ou localhost): sem cache de áudio nem offline");
      return;
    }
    try {
      this.cache = await caches.open(CONFIG.audioCache);
    } catch (err) {
      warn("não foi possível abrir o cache de áudio:", err);
      return;
    }
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(INDEX_KEY) || "{}");
    } catch (err) {
      warn("índice do cache ilegível; recomeçando:", err.message);
    }
    // O cache é a verdade; o índice só complementa.
    for (const req of await this.cache.keys()) {
      const entry = saved[req.url];
      let bytes = entry?.bytes;
      if (!bytes) {
        const res = await this.cache.match(req);
        bytes = Number(res?.headers.get("x-bytes")) || (await res.blob()).size;
      }
      this.index.set(req.url, { bytes, used: entry?.used ?? 0 });
    }
    this.save();

    if (navigator.storage?.estimate) {
      const { usage, quota } = await navigator.storage.estimate();
      this.limitBytes = Math.min(CONFIG.audioCacheMb * 1e6, quota / 2);
      log(`armazenamento: ${mb(usage)} usados de ${mb(quota)}; limite do áudio ${mb(this.limitBytes)}`);
    }
    if (navigator.storage?.persist) {
      // Pede ao navegador para não apagar o cache sob pressão de espaço.
      const persisted = await navigator.storage.persist().catch(() => false);
      log(`armazenamento persistente: ${persisted ? "sim" : "não concedido"}`);
    }
    log(`cache de áudio: ${this.index.size} faixa(s), ${mb(this.totalBytes())}`);
  },

  save() {
    try {
      localStorage.setItem(INDEX_KEY, JSON.stringify(Object.fromEntries(this.index)));
    } catch (err) {
      warn("não foi possível salvar o índice do cache:", err.message);
    }
  },

  has(url) {
    return this.index.has(url);
  },

  totalBytes() {
    let total = 0;
    for (const { bytes } of this.index.values()) total += bytes;
    return total;
  },

  async get(url) {
    if (!this.cache || !this.index.has(url)) return null;
    const res = await this.cache.match(url);
    if (!res) {
      this.index.delete(url); // sumiu do cache (navegador limpou)
      this.save();
      return null;
    }
    this.index.get(url).used = Date.now();
    this.save();
    return res.blob();
  },

  // Remove as menos usadas recentemente até sobrar espaço para `needed` bytes.
  async evict(needed, keep) {
    const candidates = [...this.index]
      .filter(([url]) => !keep.has(url))
      .sort((a, b) => a[1].used - b[1].used);
    let total = this.totalBytes();
    for (const [url, { bytes }] of candidates) {
      if (total + needed <= this.limitBytes) break;
      await this.cache.delete(url);
      this.index.delete(url);
      total -= bytes;
      log(`cache cheio: removida ${url.split("/").pop()} (${mb(bytes)})`);
    }
    this.save();
    return total + needed <= this.limitBytes;
  },

  // Guarda a faixa. Devolve false se não coube (a reprodução segue com o blob em memória).
  async put(url, blob, keep) {
    if (!this.cache) return false;
    if (!(await this.evict(blob.size, keep))) {
      warn(`sem espaço no cache para ${url.split("/").pop()} (${mb(blob.size)})`);
      return false;
    }
    const response = () =>
      new Response(blob, { headers: { "content-type": blob.type, "x-bytes": String(blob.size) } });
    try {
      await this.cache.put(url, response());
    } catch (err) {
      if (err.name !== "QuotaExceededError") {
        warn("falha ao guardar no cache:", err.name, err.message);
        return false;
      }
      // O navegador tem menos espaço do que a estimativa dizia: libera o dobro e tenta uma vez.
      warn("QuotaExceededError; liberando espaço");
      this.limitBytes = Math.max(this.totalBytes() - blob.size, 0);
      await this.evict(blob.size, keep);
      try {
        await this.cache.put(url, response());
      } catch (retryErr) {
        warn("cache cheio mesmo após liberar espaço:", retryErr.name);
        return false;
      }
    }
    this.index.set(url, { bytes: blob.size, used: Date.now() });
    this.save();
    return true;
  },

  async delete(url) {
    if (!this.cache) return;
    await this.cache.delete(url);
    if (this.index.delete(url)) {
      log(`removida do cache: ${url.split("/").pop()}`);
      this.save();
    }
  },
};

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
    this.offline = !navigator.onLine;
    this.probeTimer = null;
    this.reloadPending = false;

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
    window.addEventListener("offline", () => this.setOffline(true, "navegador sem conexão"));
    window.addEventListener("online", () => this.probe()); // "online" só diz que há rede local
    if (this.offline) this.setOffline(true, "navegador sem conexão");
    this.setupMediaSession();
  }

  setTracks(tracks) {
    this.tracks = tracks;
    log(`playlist: ${tracks.length} faixa(s)`);
    this.ui.storage();
  }

  // Offline: o shuffle só recebe faixas já baixadas; nunca promete faixa que não está aqui.
  available() {
    const now = Date.now();
    for (const [id, until] of this.quarantine) if (until <= now) this.quarantine.delete(id);
    return this.tracks.filter(
      (t) => !this.quarantine.has(t.id) && (!this.offline || store.has(t.url)),
    );
  }

  setOffline(offline, reason) {
    if (offline === this.offline && (offline ? this.probeTimer : true)) return;
    this.offline = offline;
    this.ui.offline(offline);
    clearInterval(this.probeTimer);
    this.probeTimer = null;
    if (offline) {
      warn(`modo offline (${reason}): só faixas baixadas`);
      this.probeTimer = setInterval(() => this.probe(), CONFIG.probeIntervalMs);
      // A próxima pré-carregada pode não estar baixada; ela será escolhida de novo.
      if (this.next && !store.has(this.next.track.url)) {
        this.discard(this.next);
        this.next = null;
      }
    } else {
      log(`rede de volta (${reason}): catálogo completo`);
    }
  }

  // Testa a rede pedindo só o cabeçalho de um MP3 (sem baixar). Qualquer resposta HTTP,
  // mesmo 404, prova que a rede e o bucket respondem.
  async probe() {
    const track = this.tracks[0];
    if (!track) return;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CONFIG.probeTimeoutMs);
    try {
      await fetch(track.url, { method: "HEAD", cache: "no-store", signal: controller.signal });
      this.setOffline(false, "teste de rede ok");
    } catch {
      // continua offline; o próximo teste acontece no intervalo
    } finally {
      clearTimeout(timer);
    }
  }

  pick() {
    const r = nextTrack(this.available(), this.shuffle, { artistWindow: CONFIG.artistWindow });
    this.shuffle = r.state;
    return r.track;
  }

  urlsInUse() {
    return new Set([this.current?.track.url, this.next?.track.url].filter(Boolean));
  }

  async loadBlob(track) {
    const cached = await store.get(track.url);
    if (cached) {
      log(`cache: ${track.id}`);
      return cached;
    }
    const started = performance.now();
    const blob = await download(track);
    log(`baixada ${track.id} (${mb(blob.size)} em ${((performance.now() - started) / 1000).toFixed(1)} s)`);
    if (this.offline) this.setOffline(false, "download concluído");
    await store.put(track.url, blob, this.urlsInUse().add(track.url));
    this.ui.storage();
    return blob;
  }

  prepare(track) {
    const promise = this.loadBlob(track).then((blob) => URL.createObjectURL(blob));
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
    if (this.reloadPending) {
      // Versão nova do site instalada pelo service worker: troca entre uma faixa e outra.
      log("versão nova do site; recarregando");
      location.reload();
      return;
    }
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
            this.pauseAndRetry(
              this.offline && this.tracks.length
                ? "Sem internet e nenhuma música baixada."
                : "Nenhuma música disponível.",
            );
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
    } else {
      this.setOffline(true, err.message);
    }
    this.failures++;
    if (this.failures >= CONFIG.failuresBeforePause) {
      this.failures = 0;
      this.pauseAndRetry(`${CONFIG.failuresBeforePause} falhas seguidas.`);
      return true;
    }
    this.ui.status("Música indisponível, pulando…");
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
    await store.delete(track.url);
    this.ui.storage();
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
      artwork: [{ src: "icons/icon-512.png", sizes: "512x512", type: "image/png" }],
    });
  }
}

// ---------------------------------------------------------------------------
// Baixar para offline
// ---------------------------------------------------------------------------

// Baixa, uma por vez, as faixas que ainda não estão no cache, até o limite de espaço.
// Para sem apagar faixas já baixadas (evita trocar uma faixa offline por outra).
async function downloadForOffline(player, ui, control) {
  const missing = player.tracks.filter((t) => !store.has(t.url) && !player.quarantine.has(t.id));
  let done = 0;
  let failed = 0;
  for (const track of missing) {
    if (control.cancelled) break;
    const sizes = [...store.index.values()].map((e) => e.bytes);
    const estimate = sizes.length ? Math.max(...sizes) : 6e6;
    if (store.totalBytes() + estimate > store.limitBytes) {
      ui.offlineProgress(`Limite de ${mb(store.limitBytes)} atingido.`);
      log("baixar para offline: limite de espaço atingido");
      return;
    }
    ui.offlineProgress(`Baixando ${done + failed + 1} de ${missing.length}…`);
    try {
      const blob = await download(track);
      if (!(await store.put(track.url, blob, player.urlsInUse()))) break;
      done++;
    } catch (err) {
      failed++;
      warn(`baixar para offline: ${track.id}: ${err.message}`);
      if (!err.quarantine) {
        ui.offlineProgress("Sem conexão. Tente de novo mais tarde.");
        return;
      }
    }
  }
  const tail = failed ? `, ${failed} com falha` : "";
  ui.offlineProgress(control.cancelled ? "Download cancelado." : `Pronto: ${done} baixada(s)${tail}.`);
  log(`baixar para offline: ${done} baixadas, ${failed} falhas`);
}

// ---------------------------------------------------------------------------
// Interface
// ---------------------------------------------------------------------------

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function createUi(getTracks) {
  const $ = (id) => document.getElementById(id);
  const el = {
    play: $("play"), next: $("next"), title: $("title"), artist: $("artist"),
    status: $("status"), fill: $("fill"), bar: $("bar"), elapsed: $("elapsed"),
    duration: $("duration"), attribution: $("attribution"), license: $("license"),
    source: $("source"), credit: $("credit"), net: $("net"), download: $("download"),
    storage: $("storage"),
  };
  let knownDuration = 0;
  let messageTimer = null;
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
    offline(isOffline) {
      el.net.hidden = !isOffline;
    },
    storage() {
      if (messageTimer) return; // uma mensagem do download está na tela
      const tracks = getTracks();
      const here = tracks.filter((t) => store.has(t.url)).length;
      el.storage.textContent = store.cache
        ? `${here} de ${tracks.length} músicas no computador (${mb(store.totalBytes())})`
        : "";
    },
    // Mensagem do "Baixar para offline". Com `final`, volta ao resumo depois de alguns segundos.
    offlineProgress(text, final = !text.startsWith("Baixando")) {
      clearTimeout(messageTimer);
      el.storage.textContent = text;
      messageTimer = setTimeout(() => {
        messageTimer = null;
        this.storage();
      }, final ? 8_000 : 10 * 60_000);
    },
  };
}

// ---------------------------------------------------------------------------
// Início
// ---------------------------------------------------------------------------

function registerServiceWorker(player) {
  if (!("serviceWorker" in navigator)) {
    warn("service worker indisponível: sem modo offline do site");
    return null;
  }
  const hadController = Boolean(navigator.serviceWorker.controller);
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    // Na primeira instalação não há o que recarregar; nas seguintes, troca na próxima faixa.
    if (hadController) player.reloadPending = true;
  });
  return navigator.serviceWorker.register("sw.js").catch((err) => {
    warn("registro do service worker falhou:", err);
    return null;
  });
}

async function main() {
  let player;
  const ui = createUi(() => player?.tracks ?? []);
  player = new Player(document.getElementById("audio"), ui);
  window.radio = { player, store, config: CONFIG }; // para diagnóstico pelo console (F12)

  ui.el.play.addEventListener("click", () => player.toggle());
  ui.el.next.addEventListener("click", () => player.skip());

  let offlineJob = null;
  ui.el.download.addEventListener("click", async () => {
    if (offlineJob) {
      offlineJob.cancelled = true;
      return;
    }
    offlineJob = { cancelled: false };
    ui.el.download.textContent = "Cancelar download";
    try {
      await downloadForOffline(player, ui, offlineJob);
    } finally {
      offlineJob = null;
      ui.el.download.textContent = "Baixar para offline";
    }
  });

  const registration = registerServiceWorker(player);
  await store.init();

  async function refresh() {
    try {
      player.setTracks(await fetchPlaylist());
      (await registration)?.update().catch((err) => warn("verificação de versão falhou:", err.message));
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
  player.advance(); // lista vazia cai em "Nenhuma música disponível" e tenta de novo em 60 s
}

main();
