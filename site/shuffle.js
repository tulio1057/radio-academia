// Escolha da próxima faixa. Função pura: mesmo estado + mesmo rng = mesma escolha.
//
// Regras, em ordem de prioridade (uma regra só é relaxada quando a anterior não deixa opção):
//   1. Continuidade: lista não vazia sempre devolve uma faixa.
//   2. Ciclo: nenhuma faixa repete até todas tocarem; no reinício do ciclo, a última faixa
//      tocada não abre o novo ciclo (se houver outra).
//   3. Nunca o mesmo artista duas vezes seguidas, inclusive na virada de ciclo.
//   4. Evitar artistas tocados nas últimas `artistWindow` faixas.
//
// Testes: node --test tests/

/**
 * @param {{id: string, artist: string}[]} tracks faixas disponíveis agora
 *   (catálogo inteiro online; só as que estão em cache offline)
 * @param {{cycle: string[], recent: {id: string, artist: string}[]}} [state]
 *   cycle: ids já tocados no ciclo atual; recent: últimas faixas, a mais recente no fim
 * @param {{artistWindow?: number, rng?: () => number}} [options]
 * @returns {{track: object | null, state: {cycle: string[], recent: object[]}}}
 */
export function nextTrack(tracks, state = emptyState(), { artistWindow = 3, rng = Math.random } = {}) {
  if (!tracks.length) return { track: null, state };

  const ids = new Set(tracks.map((t) => t.id));
  let cycle = state.cycle.filter((id) => ids.has(id));
  const recent = state.recent;
  const last = recent[recent.length - 1];

  // Regra 2
  let candidates = tracks.filter((t) => !cycle.includes(t.id));
  if (!candidates.length) {
    cycle = [];
    candidates = tracks.filter((t) => !last || t.id !== last.id);
    if (!candidates.length) candidates = tracks; // lista de uma faixa só
  }

  // Regra 3
  if (last) candidates = prefer(candidates, (t) => artistKey(t) !== artistKey(last));

  // Regra 4
  const windowArtists = new Set(recent.slice(-artistWindow).map(artistKey));
  candidates = prefer(candidates, (t) => !windowArtists.has(artistKey(t)));

  const track = candidates[Math.floor(rng() * candidates.length)];
  const keep = Math.max(artistWindow, 1);
  return {
    track,
    state: {
      cycle: [...cycle, track.id],
      recent: [...recent, { id: track.id, artist: track.artist }].slice(-keep),
    },
  };
}

export function emptyState() {
  return { cycle: [], recent: [] };
}

// Aplica o filtro; se ele eliminar todo mundo, a regra é relaxada e a lista volta inteira.
function prefer(list, predicate) {
  const filtered = list.filter(predicate);
  return filtered.length ? filtered : list;
}

function artistKey(track) {
  return track.artist.trim().toLowerCase();
}
