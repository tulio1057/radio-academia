# CLAUDE.md — Rádio Academia (player PWA static-first)

Player web de música licenciada (Creative Commons) para recepção de academia.
Operado por recepcionista sem conhecimento técnico. Especificação completa em `docs/POCF.md` (ainda não está no repo; adicionar na Fase 0).
Este arquivo tem precedência sobre o POCF onde houver conflito (ver "Decisões arquiteturais").

---

## Regras invioláveis

- Sem backend, sem banco de dados, sem API própria, sem servidor em produção.
- Sem framework frontend. Apenas HTML5, CSS3, JavaScript vanilla e HTML Audio API.
- Sem painel administrativo. `catalog/tracks.csv` é a fonte única do catálogo.
- `site/playlist.json` e `site/creditos/index.html` são GERADOS pelo ingest. Nunca editar à mão.
- Nunca commitar credenciais. Config do rclone fica fora do repo. Nenhum token no JavaScript.
- Nunca usar dados fictícios como se fossem músicas licenciadas. Testes unitários usam CSV de fixture e arquivos temporários (não são apresentados como músicas); a aceitação das fases usa faixas CC reais.
- Nunca ignorar erros silenciosamente: toda falha é registrada em log ou no console.
- Não assumir reprodução ininterrupta garantida pelo navegador nem cache offline ilimitado.

## Filosofia de código

- Código mínimo que funciona (YAGNI). Ordem de preferência: não escrever → reusar → stdlib → API nativa da plataforma → dependência já instalada → código novo mínimo.
- Python: apenas stdlib (`csv`, `hashlib`, `subprocess`, `json`, `tomllib`, `logging`, `unittest`).
- Simplificações deliberadas marcadas com comentário: `ponytail: <limite atual> <caminho de upgrade>`.

## Forma de trabalho

- Trabalhar uma fase por vez, um arquivo por vez.
- Para cada arquivo: caminho, responsabilidade, código completo, como executar, como testar.
- Ao final de cada fase, reportar: o que foi implementado, o que foi testado, pendências.
- Não avançar de fase sem o critério de pronto atendido.

---

## Decisões arquiteturais (ajustes ao POCF)

1. **Ingestão local, sem GitHub Actions no MVP.** Originais de áudio não entram no Git. O ingest roda no Windows do desenvolvedor, gera `playlist.json` e créditos, e esses arquivos são commitados. A integração Git do Cloudflare Pages faz o deploy a cada push.
2. **Service worker escrito à mão, sem Workbox.** O SW cuida só de estáticos e `playlist.json`; não justifica a dependência.
3. **O player é o único dono do cache de MP3.** O player baixa cada faixa via `fetch()`, guarda no Cache API (cache `audio-v1`) e toca a partir de blob URL. O SW não intercepta nem guarda MP3. Evita os problemas de cache de respostas `Range`/206 e evita duas cópias com duas políticas de evicção. No máximo dois blobs em memória (faixa atual e próxima), sempre com `URL.revokeObjectURL` ao descartar. Custo aceito: a faixa só começa a tocar após o download completo (~4–5 MB); por isso a próxima é sempre pré-carregada.
4. **MP3 públicos no R2 durante o MVP, servidos pelo `r2.dev`** (não há domínio próprio). A Cloudflare indica o `r2.dev` só para desenvolvimento (limite de requisições, sem garantia, sem cache/Access); aceito porque é um player só, de baixo volume, com cache local das faixas. URL base do áudio num único lugar da config. `ponytail: r2.dev; upgrade: Custom Domain no bucket e trocar a URL base`. Cloudflare Access protege o Pages, não o bucket; áudio privado exigiria domínio próprio atrás do Access. Decisão consciente: o conteúdo é CC.
5. **Lista de licenças aceitas:** `CC0-1.0`, `CC-BY-3.0`, `CC-BY-4.0`. Rejeitar qualquer licença NC (academia é uso comercial), ND e SA no MVP (normalização e corte de silêncio podem ser considerados modificação da obra).
6. **ECAD:** licença CC não impede cobrança por execução pública se autor/intérprete for filiado a associação de gestão coletiva. Academias são alvo frequente de cobrança. Registrar em `docs/licencas.md`, alertar o cliente por escrito e preferir artistas que declarem publicamente não serem filiados (guardar a evidência em `source_url` ou em `docs/licencas.md`).
7. **Processamento conservador:** corte de silêncio só nas pontas (abaixo de -50 dB, mínimo ~1 s), nunca no meio. `loudnorm` em duas passagens, alvo configurável entre -14 e -16 LUFS, true peak -1,5 dBTP (margem para o overshoot do encoder MP3).
8. **Reprodução automática na recepção depende de configuração da máquina, não do código.** O Chrome bloqueia áudio sem gesto do usuário; o atalho de inicialização usa `--autoplay-policy=no-user-gesture-required`, ou a operação documenta "um clique em Play após ligar". Wake Lock só funciona com a aba visível; o que garante 8 h de funcionamento é o plano de energia do Windows sem suspensão.

---

## Estrutura do projeto

```text
catalog/tracks.csv           # catálogo (fonte única)
media/originals/             # gitignored — arquivos-fonte
media/processed/             # gitignored — MP3 normalizados
scripts/ingest.py            # valida → processa → upload → gera saídas
scripts/config.toml          # LUFS, bitrate, bucket, base URL, limites
logs/                        # gitignored — logs de ingest
site/                        # raiz publicada no Cloudflare Pages
  index.html
  app.js
  style.css
  sw.js
  manifest.json
  icons/
  playlist.json              # GERADO
  creditos/index.html        # GERADO
tests/                       # node --test (shuffle) e unittest (ingest)
docs/
  POCF.md                    # especificação original
  decisoes.md
  licencas.md
  deploy.md
  operacao.md
  testes.md
```

## Catálogo — `catalog/tracks.csv`

Campos: `id, title, artist, album, genre, license, license_url, source_url, attribution, filename, duration, status, date_added`

- `id`: único, `[a-z0-9-]+`
- `license`: deve estar na lista aceita
- `license_url`, `source_url`: obrigatórias, `https://`
- `filename`: sem `/`, `\` ou `..`; deve existir em `media/originals/`
- `status`: `active` | `inactive`
- `date_added`: `AAAA-MM-DD`
- `album` e `duration` opcionais. O ingest não reescreve o CSV (o Excel trava o arquivo aberto); a duração real vem do `ffprobe` no MP3 processado e vai para o `playlist.json`

---

## Plano de execução

### Fase 0 — Esqueleto e decisões
- `git init`, estrutura de pastas, `.gitignore`, `README.md`, `docs/POCF.md`, `docs/decisoes.md` (com as decisões acima e justificativas).
- Nenhum código de aplicação.
- **Pronto quando:** repo no GitHub e `decisoes.md` revisado pelo desenvolvedor.

### Fase 1 — Catálogo e validação
- Cabeçalho de `tracks.csv`.
- `ingest.py` só com validação: campos obrigatórios, lista de licenças, URLs https, filename seguro, id único, existência do arquivo, duplicata por SHA-256.
- Faixa inválida → log e pula; nunca aborta a ingestão inteira.
- Testes unitários em `tests/` com `unittest`, usando CSV de fixture e arquivos temporários (rodam num clone novo, sem áudio).
- **Pronto quando:** testes unitários passam; 3–4 faixas CC reais (Free Music Archive, ccMixter) validam manualmente, e casos inválidos são rejeitados com mensagem clara.

### Fase 2 — Processamento de áudio
- `ffprobe` para integridade da entrada.
- Ordem: corte de silêncio nas pontas → medição `loudnorm` → aplicação `loudnorm`. Medir sempre o áudio já cortado.
- `loudnorm` duas passagens com `linear=true`, alvo e true peak do `config.toml`. Forçar `-ar 44100` (o filtro devolve 192 kHz). Ler `normalization_type` na saída: se cair para `dynamic`, registrar aviso no log.
- Saída MP3 CBR 128–160 kbps, metadados preservados, `ffprobe` na saída.
- Idempotência: nome de saída `{id}-{hash8}.mp3`, hash do original + parâmetros. Existe → pula. Parâmetros mudaram → reprocessa.
- **Pronto quando:** faixas processadas medem o LUFS-alvo (±1) e segunda execução não reprocessa nada.

### Fase 3 — Infraestrutura mínima do R2
Pré-requisito do upload e do player. Registrar em `docs/deploy.md` (primeira parte):
- Instalar FFmpeg, rclone e Python via `winget`.
- Criar bucket R2 e token com escopo restrito ao bucket; remote do rclone configurado fora do repo.
- Public Development URL (`r2.dev`) ativado no bucket (Decisão 4).
- CORS: `GET` e `HEAD` liberados apenas para `http://localhost:<porta>` (desenvolvimento) e o domínio do Pages. Sem CORS o `fetch()` do player não consegue ler o corpo.
- **Pronto quando:** `rclone lsjson` lista o bucket, e um `fetch()` do console em `localhost` lê um MP3 de teste pelo `r2.dev`.

### Fase 4 — Upload e arquivos gerados
- Upload com `rclone copy --checksum --header-upload "Cache-Control: public, max-age=31536000, immutable"` (o cabeçalho é gravado no upload; os nomes são imutáveis pelo hash). Verificação com `rclone check` ou `lsjson`.
- Gerar `site/playlist.json` (só `status=active` com upload confirmado) e `site/creditos/index.html`.
- Saídas determinísticas: ordem estável (por `id`), chaves ordenadas, sem data ou hora de geração dentro dos arquivos.
- Arquivos antigos no R2 após mudança de parâmetros não são apagados: `ponytail: órfãos acumulam no bucket; upgrade: comando de limpeza comparando bucket × playlist.json`.
- Log em `logs/ingest-AAAAMMDD.log`.
- **Pronto quando:** duas execuções seguidas → zero uploads e `git diff` vazio na segunda.

### Fase 5 — Player
**5a. Shuffle (função pura, testada com `node --test`):**
- Recebe a lista de faixas disponíveis (catálogo inteiro online, só as em cache offline) e o histórico recente.
- Regras em ordem de prioridade; uma regra só é relaxada quando a anterior não puder ser cumprida:
  1. Continuidade: sempre devolver uma faixa se a lista não estiver vazia.
  2. Ciclo sem repetir faixa até esgotar a lista; reinício de ciclo quando necessário.
  3. Evitar o mesmo artista consecutivo, inclusive na virada de ciclo.
  4. Janela de N faixas sem repetir artista.
- Testes cobrem: um artista só, dois artistas, lista de 1 faixa, lista vazia, virada de ciclo.

**5b. Reprodução:**
- `fetch()` com timeout via `AbortController` → Cache API → blob URL; pré-carrega a próxima faixa durante a atual.
- Validar a resposta antes de guardar: `response.ok` e `content-type` de áudio.
- Falhas de rede são detectadas no `fetch()` (timeout, status, CORS). No elemento de áudio, tratar `error` (arquivo corrompido/decodificação), `ended`, `loadedmetadata`; `stalled`/`waiting` só para log.
- Faixa com falha entra em quarentena temporária.
- K falhas seguidas → pausa de 60 s e nova tentativa (sem loop infinito).
- Cache de áudio com limite simples desde já (manter no máximo M faixas, apagando as mais antigas); o LRU por MB vem na Fase 6.
- Wake Lock após primeiro clique; reativa em `visibilitychange`; ausência de suporte não quebra nada.
- Media Session API (título/artista no SO).

**5c. Interface:**
- Botão grande play/pause, botão próxima, título, artista, progresso/duração, licença e atribuição, link para créditos.
- Legível de longe, feedback visual nas ações, sem menus.

- **Pronto quando:** testes do shuffle passam; apagar um MP3 do R2 faz o player pular a faixa; rede lenta no DevTools faz o `fetch()` estourar o timeout e o player passa para a próxima faixa; um MP3 corrompido dispara `error` e é pulado.

### Fase 6 — PWA e offline
- `sw.js`: estáticos e `playlist.json` com stale-while-revalidate. Não toca em MP3 (Decisão 3).
- Versão de cache no `sw.js` para manter `index.html`, `app.js` e `style.css` coerentes entre deploys.
- Nunca guardar resposta que não seja `ok` com o `content-type` esperado. Isso protege contra o redirect de login do Cloudflare Access ser guardado como `playlist.json`.
- Cache de áudio do player: limite configurável em MB, evicção LRU, tratamento de cache cheio (`QuotaExceededError`).
- `navigator.storage.estimate()` e `navigator.storage.persist()`.
- `manifest.json` com nome, ícones, `display: standalone`, cores.
- Botão "Baixar para offline": baixa seleção até o limite, mostra progresso.
- Offline: o shuffle recebe só as faixas em cache; nunca prometer faixa não baixada.
- Ao voltar a conexão: retomar catálogo completo.
- **Pronto quando:** com DevTools offline, o player continua tocando faixas em cache e ignora as demais; app instala pelo Chrome em janela própria.

### Fase 7 — Deploy e máquina da recepção
`docs/deploy.md` (segunda parte), passo a passo em PowerShell:
- Conectar Pages ao repo, output dir `site/`. `sw.js` servido sem cache longo (`_headers`).
- Atualizar o CORS do bucket com o domínio final do Pages.
- Cloudflare Access no Pages, com duração de sessão longa (o player fica aberto por dias). Documentar que o áudio no domínio do bucket continua público.
- Atalho do Chrome com `--app=URL --autoplay-policy=no-user-gesture-required` na pasta de inicialização do Windows da recepção (Decisão 8).
- Plano de energia do Windows: sem suspensão e sem desligar a tela no horário de funcionamento.
- Custos: consultar a página de preços atual da Cloudflare para R2 e Pages e registrar os limites no documento (não assumir gratuidade).
- **Pronto quando:** primeiro deploy no ar, tocando a partir do R2, abrindo e tocando sozinho no boot da máquina de teste.

### Fase 8 — Testes e documentação operacional
- `docs/testes.md`: checklist manual em Chrome e Edge no Windows (reprodução contínua, faixa indisponível, arquivo corrompido, erro de rede, troca de faixa, repetição de artista, offline, cache cheio, Wake Lock, instalação PWA, reinício da máquina, sessão do Access expirada).
- `docs/operacao.md` para quem não programa: adicionar, atualizar e remover música; corrigir arquivo; atualizar créditos; rodar ingest; fazer deploy; recuperar o sistema (player travado, cache corrompido, Cloudflare fora do ar, tela de login do Access).
- **Pronto quando:** teste de resistência de 8 h seguidas sem intervenção numa máquina parecida com a da recepção.

---

## Fora do MVP (adicionar só com motivo concreto)

| Item | Adicionar quando |
|---|---|
| GitHub Actions | mais de uma pessoa editando o catálogo |
| Workbox | o SW manual ficar difícil de manter |
| Domínio próprio no bucket R2 | falhas vindas do `r2.dev`, mais de uma unidade ou endereço próprio desejado |
| Access no domínio do bucket R2 | cliente exigir áudio privado (requer domínio próprio) |
| Limpeza de órfãos no R2 | custo de armazenamento relevante |
| Playwright / testes de navegador | deploy em várias unidades |
