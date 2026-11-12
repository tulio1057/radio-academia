# Deploy

Passo a passo em PowerShell, no Windows do desenvolvedor.

- **Parte 1 (Fase 3):** ferramentas locais e armazenamento do áudio no Cloudflare R2.
- **Parte 2 (Fase 7):** Cloudflare Pages, Access e máquina da recepção. Ainda não escrita.

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
