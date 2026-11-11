# Rádio Academia

Player web de música Creative Commons para a recepção de uma academia.
Site estático (PWA) publicado no Cloudflare Pages; áudio no Cloudflare R2.
Sem backend, sem banco de dados, sem framework.

## Como funciona

1. O catálogo vive em `catalog/tracks.csv` (fonte única).
2. Os arquivos de áudio originais ficam em `media/originals/` (fora do Git).
3. `scripts/ingest.py` valida o catálogo, normaliza o áudio, envia ao R2 e gera
   `site/playlist.json` e `site/creditos/index.html`.
4. Os arquivos gerados são commitados; cada push publica o site no Cloudflare Pages.
5. Na recepção, o Chrome abre o player em janela própria ao ligar a máquina.

## Estrutura

```text
catalog/    catálogo de faixas (CSV)
media/      áudio original e processado (não versionado)
scripts/    ingest e configuração
logs/       logs do ingest (não versionado)
site/       raiz publicada no Cloudflare Pages
tests/      testes (node --test e unittest)
docs/       decisões, licenças, deploy, operação e testes
```

## Documentação

- [`docs/decisoes.md`](docs/decisoes.md) — decisões de arquitetura e por quê
- [`CLAUDE.md`](CLAUDE.md) — regras do projeto e plano de execução por fases

Os demais documentos (`licencas.md`, `deploy.md`, `operacao.md`, `testes.md`)
são escritos nas fases correspondentes.

## Requisitos (desenvolvimento)

Windows com Python 3.11+, FFmpeg, rclone e Node.js 20+ (só para testes).
Instruções de instalação em `docs/deploy.md` (Fase 3).
