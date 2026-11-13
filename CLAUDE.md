# CLAUDE.md — Rádio (player pessoal PWA static-first)

Player web de música para uso pessoal do dono, em casa e nos aparelhos dele.
Começou como rádio de recepção de academia com catálogo Creative Commons (Fases 0–7 da
versão anterior); em 2026-11-12 virou player pessoal com áudio privado no Firebase (Decisão 10).
Histórico completo das decisões em `docs/decisoes.md`.

---

## Regras invioláveis

- Sem servidor próprio, sem API própria, sem código rodando em produção além do navegador.
  O único serviço com estado é o Firebase (Authentication + Cloud Storage), usado direto do navegador.
- Sem framework frontend. HTML5, CSS3, JavaScript vanilla e HTML Audio API.
  O SDK do Firebase Auth entra por CDN com versão fixa; nada além dele.
- Sem painel administrativo. `catalog/tracks.csv` é a fonte única do catálogo.
- Áudio e lista de músicas são privados: ficam só no bucket do Firebase, lidos com o login do
  dono. Nunca colocar MP3, `playlist.json` ou nomes de músicas pessoais em `site/`, no GitHub ou
  em qualquer arquivo público.
- Nunca commitar credenciais. A chave da conta de serviço e a config do rclone ficam fora do repo.
  A config web do Firebase (`apiKey`, `projectId`…) vai no JavaScript: identifica o projeto e não
  dá acesso; quem protege os dados são as regras de segurança do Storage.
- Uso pessoal, sem execução pública. O áudio vem de downloads que a própria fonte oferece
  (ex.: botão de download do Sua Música), baixados à mão. Nada de baixar de YouTube nem de
  automatizar download de sites.
- Testes usam CSV de fixture e arquivos temporários sintéticos; nunca apresentados como músicas.
- Nunca ignorar erros silenciosamente: toda falha é registrada em log ou no console.
- Não assumir reprodução ininterrupta garantida pelo navegador nem cache offline ilimitado.

## Filosofia de código

- Código mínimo que funciona (YAGNI). Ordem de preferência: não escrever → reusar → stdlib → API nativa da plataforma → dependência já instalada → código novo mínimo.
- Python: apenas stdlib (`csv`, `hashlib`, `subprocess`, `json`, `tomllib`, `logging`, `unittest`).
- Simplificações deliberadas marcadas com comentário: `ponytail: <limite atual> <caminho de upgrade>`.

## Forma de trabalho

- Trabalhar uma etapa por vez, um arquivo por vez.
- Para cada arquivo: caminho, responsabilidade, código completo, como executar, como testar.
- Ao final de cada etapa, reportar: o que foi implementado, o que foi testado, pendências.
- Não avançar de etapa sem o critério de pronto atendido.

---

## Decisões arquiteturais em vigor

Detalhes, alternativas e consequências em `docs/decisoes.md`.

1. **Ingestão local.** Originais de áudio não entram no Git. O ingest roda no Windows do dono, processa o áudio e envia MP3 e `playlist.json` ao bucket privado. O site (Cloudflare Pages) é publicado a cada push e não contém música.
2. **Service worker escrito à mão, sem Workbox.** Cuida só dos arquivos estáticos do site.
3. **O player é o único dono do cache de MP3.** Baixa cada faixa inteira (com o token do login), guarda no Cache API (`audio-v1`) e toca por blob URL. No máximo dois blobs em memória. A próxima faixa é sempre pré-carregada.
7. **Processamento conservador:** corte de silêncio só nas pontas (abaixo de -50 dB, mínimo ~1 s), `loudnorm` em duas passagens, alvo -14 a -16 LUFS, true peak -1,5 dBTP.
8. **Tocar sem clique depende da máquina:** flag `--autoplay-policy=no-user-gesture-required` no atalho do Chrome, ou um clique em ▶.
10. **Firebase para áudio privado.** Login com Google (Firebase Authentication); MP3 e `playlist.json` no Cloud Storage do Firebase, com regra que libera leitura só para o UID do dono. Plano Blaze com alerta de orçamento; uso dentro da faixa sem custo (5 GB guardados, 100 GB/mês de download, bucket nos EUA). Upload pelo rclone (backend `gcs`) com conta de serviço.

Substituídas pela Decisão 10 (mantidas no histórico): 4 (MP3 públicos no `r2.dev`), 5 (só licenças CC), 6 (ECAD), 9 (site público sem login).

---

## Estrutura do projeto

```text
catalog/tracks.csv           # catálogo (fonte única)
media/originals/             # gitignored — arquivos baixados pelo dono
media/processed/             # gitignored — MP3 normalizados
scripts/ingest.py            # valida → processa → envia ao Firebase → gera playlist privada
scripts/config.toml          # LUFS, bitrate, destino do rclone, bucket (sem segredos)
logs/                        # gitignored — logs de ingest
firebase/                    # regras de segurança e CORS do bucket (sem segredos)
site/                        # site publicado no Cloudflare Pages (sem música)
  index.html  app.js  shuffle.js  style.css  sw.js  manifest.json  _headers  icons/
tests/                       # node --test (shuffle) e unittest (ingest)
docs/
  decisoes.md  deploy.md  operacao.md  testes.md
```

## Catálogo — `catalog/tracks.csv`

Campos: `id, title, artist, album, genre, source_url, filename, duration, status, date_added`

- `id`: único, `[a-z0-9-]+`
- `source_url`: opcional; se preenchida, `https://` (de onde o arquivo foi baixado)
- `filename`: sem `/`, `\`, `..` ou `:`; deve existir em `media/originals/`
- `status`: `active` | `inactive`
- `date_added`: `AAAA-MM-DD`
- `album`, `source_url` e `duration` opcionais. O ingest não reescreve o CSV; a duração real vem do `ffprobe` e vai para o `playlist.json`

---

## Plano de execução

Fases 0–6 da versão anterior estão concluídas e continuam valendo: validação, processamento de
áudio, upload idempotente, player (shuffle, cache, falhas), PWA e offline. A Fase 7 publicou o
site no Cloudflare Pages (`https://radio-academia.pages.dev`). As etapas abaixo fazem a mudança
para o player pessoal.

### Etapa 1 — Regras e decisões
- Este arquivo reescrito; Decisão 10 em `docs/decisoes.md`; Decisões 4, 5, 6 e 9 marcadas como substituídas.
- **Pronto quando:** documentos revisados e commitados.

### Etapa 2 — Projeto Firebase (painel, feito pelo dono)
`docs/deploy.md`, parte Firebase:
- Projeto no plano Blaze com alerta de orçamento.
- Authentication com provedor Google; domínio `radio-academia.pages.dev` e `localhost` autorizados.
- Bucket do Cloud Storage numa região com faixa sem custo (us-central1, us-east1 ou us-west1).
- Regras em `firebase/storage.rules`: leitura só para o UID do dono; escrita negada pelo navegador.
- CORS do bucket (`firebase/cors.json`): `GET` para o site e para `localhost:8000`.
- Conta de serviço com permissão só no bucket, chave JSON fora do repo; remote `gcs` do rclone.
- **Pronto quando:** `rclone lsjson` lista o bucket; login com Google funciona em `localhost`; um `fetch()` autenticado lê um arquivo de teste e um `fetch()` sem login recebe 403.

### Etapa 3 — Ingest para o Firebase
- CSV simplificado (campos acima); validação sem regras de licença.
- Upload com `rclone copy --checksum` para o bucket; confirmação com `rclone check`.
- `playlist.json` gerado em `media/` (fora do Git) e enviado ao bucket; sai de `site/`.
- Página de créditos removida.
- **Pronto quando:** testes passam; duas execuções seguidas → zero uploads na segunda; nenhum nome de música em arquivo versionado.

### Etapa 4 — Player com login
- Tela "Entrar com Google" (Firebase Auth, SDK por CDN com versão fixa). Sessão persistente.
- Lista e faixas baixadas com o token do login; cache, offline, shuffle e tratamento de falhas mantidos.
- Token expirado é renovado; sem login, nada de música é pedido.
- **Pronto quando:** logado, toca do bucket; deslogado, nenhuma requisição de áudio; outra conta Google recebe 403; offline continua tocando o que está em cache.

### Etapa 5 — Limpeza e documentação
- Apagar as faixas do R2, desativar o `r2.dev` e remover a regra de CORS (com confirmação).
- `docs/operacao.md`: adicionar música (download no site de origem → `media/originals/` → linha no CSV → ingest), remover, recuperar o player.
- `docs/testes.md`: checklist manual no Chrome (login, troca de faixa, offline, cache cheio, token expirado, instalação).
- **Pronto quando:** R2 sem arquivos e documentos revisados.

---

## Fora do escopo (adicionar só com motivo concreto)

| Item | Adicionar quando |
|---|---|
| Firestore | a lista de músicas precisar ser editada fora do ingest |
| Upload pelo navegador | adicionar música sem o PC de casa |
| Mais de um usuário | outra pessoa precisar acessar |
| Limpeza de órfãos no bucket | perto do limite de 5 GB |
| Playwright / testes de navegador | o checklist manual ficar longo demais |
