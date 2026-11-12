// Testes do shuffle (Fase 5a).  Rodar:  node --test tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { nextTrack, emptyState } from "../site/shuffle.js";

// Gerador pseudoaleatório com semente (mulberry32): resultados reproduzíveis.
function seeded(seed) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function tracks(spec) {
  // spec: { A: 3, B: 2 } → A1, A2, A3, B1, B2
  return Object.entries(spec).flatMap(([artist, n]) =>
    Array.from({ length: n }, (_, i) => ({ id: `${artist}${i + 1}`, artist })),
  );
}

function play(list, count, { seed = 1, artistWindow = 3, state = emptyState() } = {}) {
  const rng = seeded(seed);
  const out = [];
  for (let i = 0; i < count; i++) {
    const r = nextTrack(list, state, { artistWindow, rng });
    out.push(r.track);
    state = r.state;
  }
  return out;
}

function eachSeed(fn, seeds = 200) {
  for (let seed = 1; seed <= seeds; seed++) fn(seed);
}

test("lista vazia devolve null sem quebrar", () => {
  const r = nextTrack([], emptyState());
  assert.equal(r.track, null);
  assert.deepEqual(r.state, emptyState());
});

test("lista de uma faixa repete a mesma faixa (continuidade vence)", () => {
  const seq = play(tracks({ A: 1 }), 5);
  assert.deepEqual(seq.map((t) => t.id), ["A1", "A1", "A1", "A1", "A1"]);
});

test("ciclo: todas tocam uma vez antes de qualquer repetição", () => {
  const list = tracks({ A: 3, B: 3, C: 2, D: 4 });
  eachSeed((seed) => {
    const seq = play(list, list.length * 3, { seed });
    for (let c = 0; c < 3; c++) {
      const ids = seq.slice(c * list.length, (c + 1) * list.length).map((t) => t.id);
      assert.equal(new Set(ids).size, list.length, `semente ${seed}, ciclo ${c}`);
    }
  });
});

test("virada de ciclo não repete a última faixa nem o último artista", () => {
  const list = tracks({ A: 2, B: 2, C: 2 });
  eachSeed((seed) => {
    const seq = play(list, list.length * 5, { seed });
    for (let i = list.length; i < seq.length; i += list.length) {
      assert.notEqual(seq[i].id, seq[i - 1].id, `semente ${seed}, posição ${i}`);
      assert.notEqual(seq[i].artist, seq[i - 1].artist, `semente ${seed}, posição ${i}`);
    }
  });
});

test("um artista só: toca tudo, sem travar (regra 3 relaxada)", () => {
  const list = tracks({ A: 4 });
  const seq = play(list, 8);
  assert.equal(new Set(seq.slice(0, 4).map((t) => t.id)).size, 4);
  assert.equal(new Set(seq.slice(4).map((t) => t.id)).size, 4);
  assert.notEqual(seq[4].id, seq[3].id);
});

test("dois artistas equilibrados alternam sempre", () => {
  const list = tracks({ A: 3, B: 3 });
  eachSeed((seed) => {
    const seq = play(list, 30, { seed });
    for (let i = 1; i < seq.length; i++) {
      assert.notEqual(seq[i].artist, seq[i - 1].artist, `semente ${seed}, posição ${i}`);
    }
  });
});

test("dois artistas desequilibrados: só repete artista quando o ciclo obriga", () => {
  const list = tracks({ A: 4, B: 1 });
  eachSeed((seed) => {
    let state = emptyState();
    const rng = seeded(seed);
    for (let i = 0; i < 25; i++) {
      const before = state;
      const r = nextTrack(list, state, { rng });
      const last = before.recent.at(-1);
      if (last && r.track.artist === last.artist) {
        // permitido só se nenhuma faixa restante do ciclo é de outro artista
        const remaining = list.filter((t) => !before.cycle.includes(t.id));
        const pool = remaining.length ? remaining : list.filter((t) => t.id !== last.id);
        assert.ok(pool.every((t) => t.artist === last.artist), `semente ${seed}, passo ${i}`);
      }
      state = r.state;
    }
  });
});

test("janela de artistas: com artistas suficientes, nenhum repete dentro da janela", () => {
  const list = tracks({ A: 2, B: 2, C: 2, D: 2, E: 2 });
  eachSeed((seed) => {
    const seq = play(list, 10, { seed, artistWindow: 3 }); // um ciclo
    // dentro de um ciclo, com 5 artistas e janela 3, há sempre candidato fora da janela
    // até sobrarem poucas faixas; conferimos as 6 primeiras escolhas
    for (let i = 1; i < 6; i++) {
      const window = seq.slice(Math.max(0, i - 3), i).map((t) => t.artist);
      assert.ok(!window.includes(seq[i].artist), `semente ${seed}, posição ${i}`);
    }
  });
});

test("faixas que saíram da lista (offline, quarentena) não quebram o ciclo", () => {
  const all = tracks({ A: 2, B: 2 });
  let { state } = nextTrack(all, emptyState(), { rng: seeded(3) });
  const fewer = all.filter((t) => t.id !== state.cycle[0]);
  const r = nextTrack(fewer, state, { rng: seeded(3) });
  assert.ok(fewer.some((t) => t.id === r.track.id));
});

test("artista comparado sem diferenciar maiúsculas e espaços", () => {
  const list = [
    { id: "x", artist: "Lee Rosevere" },
    { id: "y", artist: " lee rosevere " },
    { id: "z", artist: "Outro" },
  ];
  const state = { cycle: ["x"], recent: [{ id: "x", artist: "Lee Rosevere" }] };
  const r = nextTrack(list, state, { rng: () => 0 });
  assert.equal(r.track.id, "z");
});
