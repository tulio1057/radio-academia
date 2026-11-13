# Decisões de arquitetura

Registro das decisões do projeto, na ordem em que foram tomadas.
Onde houver conflito, vale este documento e o `CLAUDE.md`.

Cada decisão tem: **contexto**, **decisão**, **alternativas descartadas** e **consequências**.

Status: revisado pelo desenvolvedor; decisões novas são acrescentadas ao fim.

---

## 1. Ingestão local, sem GitHub Actions no MVP

**Contexto.** O ingest precisa dos arquivos de áudio originais, do FFmpeg e de credenciais do R2.
Os originais são grandes e não devem entrar no Git.

**Decisão.** O ingest roda no Windows do desenvolvedor. Ele gera `site/playlist.json` e
`site/creditos/index.html`, que são commitados. A integração Git do Cloudflare Pages publica
o site a cada push.

**Alternativas descartadas.**
- GitHub Actions: exigiria guardar os originais em outro lugar acessível ao CI e manter
  segredos do R2 no GitHub. Não se justifica com uma única pessoa editando o catálogo.

**Consequências.**
- Só quem tem os originais e o rclone configurado consegue adicionar músicas.
- Os arquivos gerados no repositório precisam ser determinísticos para o diff fazer sentido.
- Reavaliar quando houver mais de uma pessoa editando o catálogo.

---

## 2. Service worker escrito à mão, sem Workbox

**Contexto.** O SW só precisa guardar os arquivos fixos do site e o `playlist.json`.

**Decisão.** `site/sw.js` escrito à mão, com stale-while-revalidate e versão de cache.

**Alternativas descartadas.**
- Workbox: dependência e etapa de build para um problema de poucas dezenas de linhas.

**Consequências.**
- Atualizações de versão do cache são manuais: aumentar `VERSION` no `sw.js` a cada deploy que
  mude `index.html`, `app.js`, `shuffle.js`, `style.css` ou a lista de arquivos. Sem isso, o
  stale-while-revalidate ainda atualiza arquivo por arquivo, mas sem garantia de que todos
  mudem juntos.
- O player fica aberto por dias, então ele pede ao navegador para checar versão nova junto com
  a recarga da playlist (a cada 6 h). Versão nova instalada → a página recarrega sozinha na
  próxima troca de faixa. Depois do recarregamento a música só volta sozinha com a flag de
  autoplay (Decisão 8); sem ela, aparece "Clique em ▶".
- Reavaliar se o SW ficar difícil de manter.

---

## 3. O player é o único dono do cache de MP3

**Contexto.** O navegador pede áudio em partes (`Range`, resposta 206), e respostas parciais
não podem ser guardadas no Cache API de forma confiável. Além disso, pré-carregamento e modo
offline precisam do mesmo arquivo; dois mecanismos gerariam duas cópias e duas políticas de evicção.

**Decisão.** O player baixa cada faixa inteira com `fetch()`, guarda no Cache API (cache
`audio-v1`) e toca a partir de blob URL. O SW não intercepta nem guarda MP3. No máximo dois
blobs em memória (faixa atual e próxima), sempre liberados com `URL.revokeObjectURL`.

**Alternativas descartadas.**
- SW com cache-first de MP3: conflito com respostas 206 e duplicação com o pré-carregamento.
- `<audio src>` apontando direto para o R2: sem controle de timeout, sem offline.

**Consequências.**
- A faixa só começa a tocar depois do download completo (~4–5 MB a 128 kbps). Por isso a
  próxima faixa é sempre pré-carregada durante a atual.
- Falhas de rede são detectadas no `fetch()` (timeout com `AbortController`), não pelos
  eventos `stalled`/`waiting` do elemento de áudio.

---

## 4. MP3 públicos no R2, servidos pelo `r2.dev`

> **Substituída pela Decisão 10 em 2026-11-12** (player passou a ser pessoal, com áudio privado no Firebase). Mantida como histórico.

**Contexto.** O Cloudflare Access protege o site no Pages, mas não o bucket R2. Não há domínio
próprio disponível para o projeto. Segundo a documentação da Cloudflare, o `r2.dev` tem limite
de requisições (valor não publicado), é indicado para tráfego de desenvolvimento, não tem
garantia de confiabilidade e não oferece cache, WAF nem Access.

**Decisão.** No MVP os MP3 são públicos, servidos pelo Public Development URL do bucket
(`https://pub-<id>.r2.dev`). Decisão consciente, revista em 2026-11-11:
- o volume é de um player, uma faixa a cada poucos minutos, longe de qualquer limite;
- o player guarda as faixas no Cache API (Decisão 3) e continua tocando o que está em cache
  se o `r2.dev` falhar (Fase 6);
- cache de borda e Access no áudio não agregam: o cache é do player e o conteúdo é CC.

A URL base do áudio fica num único lugar da configuração do ingest.
`ponytail: r2.dev sem garantia de produção; upgrade: domínio próprio no bucket (Custom Domain), trocar a URL base, rodar o ingest e desativar o r2.dev`.

**Alternativas descartadas.**
- Domínio próprio já no MVP (ex.: `audio.<dominio>`): custo e configuração sem necessidade
  comprovada para uma única recepção.
- Domínio do bucket atrás do Access: complexidade sem exigência do cliente.

**Consequências.**
- Qualquer pessoa com a URL de um MP3 consegue baixá-lo.
- A Cloudflare pode mudar as regras do `r2.dev` sem aviso; a troca para domínio próprio não
  exige reenviar arquivos.
- Comprar domínio quando: os logs do player mostrarem falhas vindas do `r2.dev`, houver mais
  de uma unidade, ou se quiser endereço próprio para o site.
- Reavaliar se o cliente exigir áudio privado (exige domínio próprio atrás do Access).

---

## 5. Licenças aceitas: CC0-1.0, CC-BY-3.0, CC-BY-4.0

> **Substituída pela Decisão 10 em 2026-11-12** (player passou a ser pessoal, com áudio privado no Firebase). Mantida como histórico.

**Contexto.** A academia faz uso comercial da música. O ingest modifica o áudio
(normalização de volume e corte de silêncio nas pontas).

**Decisão.** Aceitar apenas `CC0-1.0`, `CC-BY-3.0` e `CC-BY-4.0`.

**Alternativas descartadas.**
- NC (NãoComercial): incompatível com uso em estabelecimento comercial.
- ND (SemDerivações): a normalização pode ser considerada modificação da obra.
- SA (CompartilhaIgual): exigiria licenciar a versão processada sob os mesmos termos;
  evitado por cautela no MVP.

**Consequências.**
- Catálogo disponível menor.
- Toda faixa BY exige atribuição visível: no player e na página de créditos.

---

## 6. ECAD

> **Substituída pela Decisão 10 em 2026-11-12** (player passou a ser pessoal, com áudio privado no Firebase). Mantida como histórico.

**Contexto.** Uma licença CC não impede a cobrança por execução pública quando o autor ou
intérprete é filiado a uma associação de gestão coletiva. Academias são alvo frequente
de cobrança do ECAD.

**Decisão.** Registrar o risco em `docs/licencas.md`, alertar o cliente por escrito e
preferir artistas que declarem publicamente não serem filiados, guardando a evidência.

**Alternativas descartadas.**
- Ignorar o risco por ser conteúdo CC: a licença CC não substitui a gestão coletiva.

**Consequências.**
- A responsabilidade final pela execução pública é do cliente, e isso precisa estar claro.
- A curadoria de faixas leva mais tempo.

---

## 7. Processamento de áudio conservador

**Contexto.** Faixas de fontes diferentes têm volumes muito diferentes. Processamento
agressivo pode alterar a obra e piorar o som.

**Decisão.**
- Corte de silêncio só nas pontas (abaixo de -50 dB, mínimo ~1 s), nunca no meio.
- `loudnorm` em duas passagens (medir → aplicar, modo linear), alvo configurável entre
  -14 e -16 LUFS, true peak -1,5 dBTP.
- Saída MP3 CBR 128–160 kbps a 44,1 kHz.

**Alternativas descartadas.**
- `loudnorm` de uma passagem: usa compressão dinâmica, altera mais o som.
- Corte de silêncio no meio: pode quebrar pausas que fazem parte da música.

**Consequências.**
- Se o alvo não puder ser atingido em modo linear, o FFmpeg cai para o modo dinâmico;
  o ingest registra esse caso no log.
- A margem de -1,5 dBTP evita distorção causada pelo encoder MP3.

---

## 8. Reprodução automática depende da máquina, não do código

**Contexto.** O Chrome bloqueia áudio sem um gesto do usuário. O Wake Lock só funciona com
a aba visível. A recepcionista não deve precisar configurar nada.

**Decisão.**
- O atalho de inicialização do Windows abre o Chrome com
  `--app=URL --autoplay-policy=no-user-gesture-required`.
- O plano de energia do Windows fica sem suspensão no horário de funcionamento.
- Se a flag de autoplay não puder ser usada, a operação documenta "um clique em Play ao ligar".

**Alternativas descartadas.**
- Confiar só no Wake Lock: não impede a suspensão do Windows nem funciona em segundo plano.

**Consequências.**
- A configuração da máquina da recepção faz parte do deploy (`docs/deploy.md`).
- O teste de 8 h roda numa máquina configurada da mesma forma.

---

## 9. Site público, sem Cloudflare Access

> **Substituída pela Decisão 10 em 2026-11-12** (player passou a ser pessoal, com áudio privado no Firebase). Mantida como histórico.

**Contexto.** O plano original protegia o site com Cloudflare Access. O cliente informou em
2026-11-12 que não precisa de login. O áudio já é público (Decisão 4), a playlist e os
créditos só listam músicas CC, e o player não tem nenhum dado pessoal nem ação sensível.

**Decisão.** `https://radio-academia.pages.dev` fica aberto, sem Access.

**Alternativas descartadas.**
- Access com sessão de 1 mês: exige Zero Trust, cadastro de e-mails e novo login a cada mês
  na recepção, para proteger conteúdo que já é público.

**Consequências.**
- Qualquer pessoa com o link abre o player. Não há custo extra: os MP3 tocam do cache local e
  o R2 não cobra saída de dados.
- Sem tela de login, a recepção nunca fica presa num login expirado.
- O código continua compatível com Access (manifesto com `use-credentials`, service worker que
  não guarda redirecionamentos). Reativar: `docs/deploy.md`, etapa 2.3.

---

## 10. Player pessoal com áudio privado no Firebase

**Contexto.** Em 2026-11-12 o dono informou que a rádio deixou de ser para a academia e passou
a ser um player pessoal, para ouvir em casa e em vários aparelhos, com músicas baixadas pelo
botão de download que a própria fonte oferece (ex.: Sua Música). Essas músicas não têm licença
Creative Commons, então não podem ficar em lugar público: o `r2.dev` é sempre público (o
Cloudflare Access não o protege) e o site não tem login. O dono escolheu o Firebase.

Consultado em 2026-11-12 na documentação do Firebase:
- Cloud Storage exige o plano Blaze (pagamento por uso) desde 2026-02-03.
- Sem custo no Blaze, para buckets `*.firebasestorage.app` em us-central1, us-east1 ou us-west1:
  5 GB-mês guardados e 100 GB/mês baixados.
- Authentication: até 50 mil usuários ativos por mês sem custo.
- Firestore limita documentos a 1 MiB: não serve para guardar MP3.

**Decisão.**
- Login com Google pelo Firebase Authentication, no próprio site (sem Cloudflare Access).
- MP3 e `playlist.json` num bucket do Cloud Storage do Firebase. Regra de segurança: leitura só
  para o UID do dono; escrita pelo navegador negada (só o ingest escreve).
- O ingest envia pelo rclone (backend `gcs`) com uma conta de serviço restrita ao bucket; a
  chave JSON fica fora do repositório.
- `playlist.json` deixa de ser publicado em `site/` e de ir para o Git: nomes de músicas pessoais
  não ficam em nenhum arquivo público.
- O player baixa a lista e as faixas com o token do login (`Authorization: Firebase <token>`) e
  segue a Decisão 3: download inteiro → Cache API → blob URL.
- Catálogo sem campos de licença; página de créditos removida.
- Alerta de orçamento no Google Cloud para avisar se o uso sair da faixa sem custo.

**Alternativas descartadas.**
- Baixar de YouTube (por biblioteca ou conversor): viola os termos do YouTube e copia música sem
  autorização de quem tem os direitos.
- Player do YouTube incorporado: permitido, mas tem anúncios, exige o vídeo visível e não
  funciona offline; o dono não quis.
- Cloudflare R2 com domínio próprio atrás do Access: menos mudança de código e 10 GB sem custo,
  mas exige comprar domínio; o dono preferiu o Firebase.
- Pasta local no PC: grátis e totalmente privada, mas só funciona num computador.

**Consequências.**
- Primeiro serviço com estado no projeto e primeira dependência JavaScript (SDK do Firebase
  Auth, por CDN com versão fixa).
- Cartão cadastrado no Google Cloud; acima de 5 GB (~1.100 músicas de 4,5 MB) há cobrança.
- O site no Pages continua público, mas sem login não mostra nem pede nenhuma música.
- O uso é pessoal: tocar essas músicas em público (ex.: na academia) seria execução pública
  sem licença e voltaria a envolver o ECAD.
- O R2 e o `r2.dev` são desativados na Etapa 5.
