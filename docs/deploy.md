# Deploy

Passo a passo em PowerShell, no Windows do desenvolvedor.

- **Parte 1 (Fase 3):** ferramentas locais e armazenamento do áudio no Cloudflare R2.
- **Parte 2 (Fase 7):** Cloudflare Pages, Access, máquina da recepção e custos.

Nos comandos abaixo, troque:

| Marcador | Exemplo |
|---|---|
| `<BUCKET>` | `radio-academia-audio` |
| `<URL_AUDIO>` | `https://pub-xxxxxxxx.r2.dev` |
| `<ACCOUNT_ID>` | ID da conta Cloudflare (painel → R2 → "Account details") |

---

## Parte 1 — Ferramentas e R2

### 1.1 Ferramentas locais

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
winget install --id Rclone.Rclone -e
```

Feche e abra o PowerShell depois de instalar, para o `PATH` ser atualizado. Conferir:

```powershell
python --version
ffmpeg -version | Select-Object -First 1
rclone version | Select-Object -First 1
```

### 1.2 Bucket

Painel Cloudflare → **R2 Object Storage** → **Create bucket**.

- Nome: `<BUCKET>`.
- Location: automática. Storage class: Standard.

O R2 pede um meio de pagamento mesmo dentro da cota gratuita. Os limites e preços são
registrados na Parte 2, consultando a página de preços vigente.

O endereço público do áudio é configurado na etapa 1.5.

### 1.3 Token de API restrito ao bucket

Painel → **R2 Object Storage** → **Manage API tokens** → **Create API token**.

- Permissão: **Object Read & Write**.
- Escopo: **Apply to specific buckets only** → `<BUCKET>`.
- TTL: sem expiração, ou com expiração e lembrete para renovar.

Ao criar, o painel mostra **uma única vez** o *Access Key ID* e o *Secret Access Key*.
Não cole esses valores em arquivo do repositório, em chat, nem em linha de comando
(o PowerShell guarda o histórico em texto).

### 1.4 Remote do rclone (fora do repositório)

O rclone guarda a configuração em `%APPDATA%\rclone\rclone.conf`, fora do repositório.
Criar o remote **sem as chaves**:

```powershell
rclone config create r2 s3 provider=Cloudflare endpoint=https://<ACCOUNT_ID>.r2.cloudflarestorage.com acl=private no_check_bucket=true
```

`no_check_bucket=true` é necessário porque um token restrito a um bucket não pode
listar nem criar buckets.

Adicionar as chaves direto no arquivo:

```powershell
notepad "$env:APPDATA\rclone\rclone.conf"
```

Na seção `[r2]`, acrescente duas linhas e salve:

```ini
access_key_id = <cole o Access Key ID>
secret_access_key = <cole o Secret Access Key>
```

Conferir (deve responder `[]` com o bucket vazio, sem erro):

```powershell
rclone lsjson r2:<BUCKET>
```

### 1.5 Endereço público do áudio (`r2.dev`)

Bucket → **Settings** → **Public Development URL** → **Enable** → digite `allow` para confirmar.
Anote o endereço exibido (`<URL_AUDIO>`, formato `https://pub-<id>.r2.dev`).

O `r2.dev` é uma escolha consciente do MVP (Decisão 4): a Cloudflare o indica para
desenvolvimento e não garante desempenho. Upgrade para domínio próprio, quando necessário:

1. Ter um domínio na mesma conta Cloudflare.
2. Bucket → **Settings** → **Custom Domains** → **Add** → `audio.<dominio>`, TLS mínimo 1.2;
   esperar o status **Active**.
3. Trocar a URL base do áudio na configuração do ingest, rodar o ingest e fazer o deploy.
4. Desativar o Public Development URL.

### 1.6 CORS

Sem CORS, o `fetch()` do player não consegue ler o MP3 (Decisão 3).

Bucket → **Settings** → **CORS Policy** → **Add CORS policy** → aba JSON:

```json
[
  {
    "AllowedOrigins": ["http://localhost:8000"],
    "AllowedMethods": ["GET", "HEAD"],
    "MaxAgeSeconds": 86400
  }
]
```

Na Fase 7, acrescentar a origem do Pages (`https://<projeto>.pages.dev` e o domínio final do
site) em `AllowedOrigins`. Não usar `"*"`.

### 1.7 Verificação (critério de pronto da Fase 3)

1. Enviar um MP3 de teste num prefixo separado:

   ```powershell
   rclone copyto media\processed\<arquivo>.mp3 r2:<BUCKET>/teste/cors.mp3
   rclone lsjson r2:<BUCKET>/teste
   ```

2. Servir o site localmente:

   ```powershell
   python -m http.server 8000 -d site
   ```

3. Abrir `http://localhost:8000` no Chrome, abrir o console (F12) e rodar:

   ```js
   const r = await fetch("<URL_AUDIO>/teste/cors.mp3");
   console.log(r.status, r.headers.get("content-type"), (await r.blob()).size);
   ```

   Esperado: `200 audio/mpeg <tamanho em bytes>`. Erro de CORS no console significa que a
   política da etapa 1.6 não está ativa ou a origem não confere (porta inclusive).

4. Apagar o arquivo de teste:

   ```powershell
   rclone deletefile r2:<BUCKET>/teste/cors.mp3
   ```

### Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `AccessDenied` no `rclone lsjson` | token sem acesso a este bucket, ou chaves coladas erradas |
| `rclone` tenta criar o bucket e falha | falta `no_check_bucket = true` na seção `[r2]` |
| `fetch()` falha com erro de CORS | origem diferente da cadastrada (`localhost` ≠ `127.0.0.1`, porta diferente) |
| `fetch()` responde 429 | limite de requisições do `r2.dev` (Decisão 4); se recorrente, migrar para domínio próprio |

---

## Parte 2 — Publicação e máquina da recepção

Marcadores novos:

| Marcador | Exemplo |
|---|---|
| `<PROJETO>` | `radio-academia` (vira `https://radio-academia.pages.dev`) |
| `<URL_SITE>` | `https://radio-academia.pages.dev` |

### 2.1 Cloudflare Pages ligado ao GitHub

Painel → **Workers & Pages** → **Create application** → **Pages** → **Connect to Git**.

1. Se a conta do GitHub não aparecer: **Add GitHub account** → **Install & Authorize**,
   escolhendo **Only select repositories** → `radio-academia`.
2. Selecione o repositório e **Begin setup**:
   - Project name: `<PROJETO>`. Se o nome estiver ocupado, o painel sugere outro;
     anote o endereço final (`<URL_SITE>`).
   - Production branch: `main`.
   - Framework preset: **None**.
   - Build command: vazio (não há etapa de build).
   - Build output directory: `site`.
3. **Save and Deploy**. Cada push na `main` publica de novo.

O arquivo `site/_headers` faz o Pages servir `sw.js`, `playlist.json` e `manifest.json` com
`Cache-Control: no-cache`, para que nunca fiquem presos num cache antigo.

### 2.2 CORS do bucket com o endereço do site

Bucket → **Settings** → **CORS Policy** → editar a regra da etapa 1.6:

```json
[
  {
    "AllowedOrigins": ["http://localhost:8000", "<URL_SITE>"],
    "AllowedMethods": ["GET", "HEAD"],
    "MaxAgeSeconds": 86400
  }
]
```

Sem barra no fim do endereço. Endereços de prévia (`https://<hash>.<PROJETO>.pages.dev`)
não precisam tocar áudio e ficam de fora.

### 2.3 Cloudflare Access no site

O Access exige o Zero Trust ativado na conta. Na primeira vez o painel pede um nome de
equipe (team name) e a escolha de um plano; escolha o **Free** e confira no próprio painel
o limite de usuários e se ele pede meio de pagamento.

O botão do Pages protege só as prévias. Para proteger também `<URL_SITE>`:

1. **Workers & Pages** → `<PROJETO>` → **Settings** → **General** → **Enable access policy**.
2. Clique em **Manage** na política criada. Em **Access** → **Applications**, abra a
   aplicação do projeto → **Configure**.
3. Em **Public hostname**, apague o `*` do campo **Subdomain** (fica só `<PROJETO>.pages.dev`).
   Se o painel reclamar de nome repetido, renomeie a aplicação. Salve.
4. Ainda nessa aplicação:
   - **Session Duration**: **1 month** (o player fica aberto por dias).
   - **Policies**: edite a regra de **Allow** e inclua, em **Emails**, o seu e-mail e o
     e-mail que a recepção vai usar para entrar.
   - **Login methods**: **One-time PIN** (o Access manda um código para o e-mail).
5. Volte em **Settings** → **General** do projeto e clique de novo em **Enable access policy**,
   para recriar a proteção das prévias.
6. Em **Access** → **Applications** devem existir duas aplicações: `<PROJETO>.pages.dev` e
   `*.<PROJETO>.pages.dev`.

Comportamento esperado:

- Abrir `<URL_SITE>` numa janela anônima mostra a tela de login do Access.
- Os MP3 continuam públicos no `r2.dev` (Decisão 4). O Access protege só o site.
- Se a sessão expirar com o player aberto, ele segue tocando com os arquivos que o service
  worker já guardou; as atualizações de catálogo voltam depois de um novo login.

### 2.4 Cada atualização do catálogo ou do site

```powershell
python scripts/ingest.py
git add catalog site
git commit -m "Catálogo: <o que mudou>"
git push
```

Se `index.html`, `app.js`, `shuffle.js` ou `style.css` mudaram, aumente `VERSION` em
`site/sw.js` no mesmo commit (Decisão 2). O player aberto na recepção busca a versão nova a
cada 6 h e recarrega na troca de música.

### 2.5 Máquina da recepção

Feito uma vez, no Windows da recepção, com o usuário que fica logado nela.

**Atalho na inicialização.** O Chrome usa um perfil só do player, para que as flags valham
mesmo se outro Chrome já estiver aberto. Troque `<URL_SITE>` e rode no PowerShell:

```powershell
$url    = "<URL_SITE>"
$chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") |
          Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw "Chrome não encontrado" }
$perfil = "$env:LOCALAPPDATA\RadioAcademia\Chrome"
$flags  = "--app=$url --user-data-dir=`"$perfil`" --autoplay-policy=no-user-gesture-required --no-first-run"
$shell  = New-Object -ComObject WScript.Shell
foreach ($pasta in @([Environment]::GetFolderPath("Startup"), [Environment]::GetFolderPath("Desktop"))) {
  $atalho = $shell.CreateShortcut("$pasta\Rádio Academia.lnk")
  $atalho.TargetPath = $chrome
  $atalho.Arguments  = $flags
  $atalho.Save()
}
```

Isso cria o atalho na pasta de inicialização (abre ao entrar no Windows) e outro na área de
trabalho (para abrir à mão).

**Primeiro acesso.** Abra pelo atalho da área de trabalho, entre com o e-mail no Access e
clique em **Baixar para offline**. Feche e abra pelo atalho de novo: deve tocar sem clique.
Se aparecer "Clique em ▶", a flag de autoplay não foi aceita; nesse caso a operação diária
fica "um clique em ▶ ao ligar" (Decisão 8).

**Energia.** Sem suspensão e sem desligar a tela na tomada (rode no PowerShell):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

Se a recepção desligar a máquina à noite, nada mais é preciso. Se ela ficar ligada direto,
ajuste em **Configurações** → **Windows Update** → **Horário ativo** para cobrir o horário
de funcionamento, evitando reinício no meio do expediente.

**Entrada no Windows.** O atalho só roda depois que alguém entra no Windows. Se a máquina
liga sem ninguém por perto, decidir com o responsável pela máquina entre entrar à mão ao
ligar ou configurar o login automático do Windows (trade-off de segurança dele, não do player).

### 2.6 Custos

Consultado em 2026-11-12 nas páginas de preços e limites da Cloudflare. Conferir de novo
antes de mudanças grandes no catálogo.

| Serviço | Grátis por mês | Acima disso |
|---|---|---|
| R2 armazenamento (Standard) | 10 GB-mês | US$ 0,015 / GB-mês |
| R2 operações classe A (escrita, listagem) | 1 milhão | US$ 4,50 / milhão |
| R2 operações classe B (leitura) | 10 milhões | US$ 0,36 / milhão |
| R2 saída de dados (inclusive `r2.dev`) | sem cobrança | sem cobrança |
| Pages | 500 builds; até 20.000 arquivos; 25 MiB por arquivo | — |
| Access (Zero Trust Free) | conferir limite de usuários no painel | — |

Estimativa do MVP: 100 faixas × ~4,5 MB ≈ 0,45 GB armazenados. Uma recepção tocando 12 h por
dia faz da ordem de 15 leituras por hora (faixa + teste de rede eventual), cerca de 6 mil
por mês, e o cache local reduz ainda mais. O ingest faz poucas operações de escrita por
execução. Tudo bem dentro da faixa grátis; o R2 exige meio de pagamento cadastrado mesmo assim.

### 2.7 Verificação (critério de pronto da Fase 7)

1. `<URL_SITE>` numa janela anônima pede login do Access.
2. Depois do login, o player toca. No console (F12), as linhas `[radio] baixada ...` mostram
   o download pelo `r2.dev`, sem erro de CORS.
3. Na máquina de teste, reiniciar o Windows: o player abre sozinho em janela própria e toca
   sem clique.
