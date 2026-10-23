# Outbox Durável, Pontos de Falha e Reconciliação (21/10/2026)

**Escopo estrito:** Ensaio 100% offline no LXC lab 121 com LNbits tag `v1.6.2` (`9349da153c223b008f7d80453b53a54e09c8ed3d`), netns filha sem rota externa, credencial sintética (`synthetic-lnbits-uds-fixture`), servidor TLS upstream falso, invoice `bcrt` determinística assinada localmente (sem nó Lightning) e banco SQLite efêmero. O launcher de produção do sandbox `/opt/tcc-ln-lab/run.sh` permaneceu inalterado em `VoidWallet`. O token Alby real em `/opt/tcc-ln-lab/secrets/alby-access-token` (0600 root:root) permaneceu intocado (mtime 19:39 preservado). Não houve chamada à Alby real, emissão de fatura pagável em rede, uso de fundos reais, commit, push, deploy, e-mail institucional ou submissão no SIACOES.

---

## 1. Arquitetura da Outbox Transacional Isolada

A mitigação implementada em `lab/fixture_outbox.py` e integrada em `lab/alby_guarded_proxy.py` resolve a vulnerabilidade de cota volátil em memória identificada no ensaio anterior:

### Estrutura e Isolamento de Acesso
- **Localização:** Armazenamento em `/tmp/tcc-ln-fixture/private/outbox.sqlite3`, permissões `0600` pertencente a `root:root`, em diretório com permissão `0700`. O UID do LNbits (`tcc_ln_lab`, UID 999) **não possui permissão de leitura nem escrita** no banco de outbox.
- **Configuração SQLite:** `PRAGMA synchronous = FULL`, `PRAGMA busy_timeout = 5000` e journal mode `delete`. Inicialização exige arquivo novo e diretório estritamente privado (`st_mode & 0o077 == 0`).
- **Tabela `metadata`:**
  - `version INTEGER NOT NULL CHECK(version=1)`
  - `ceiling INTEGER NOT NULL CHECK(ceiling=10)`
- **Tabela `attempts`:**
  - `memo TEXT PRIMARY KEY`: regex estrita `tcc:[A-Za-z0-9_-]{1,64}`.
  - `amount INTEGER NOT NULL CHECK(amount BETWEEN 1 AND 10)`.
  - `state TEXT NOT NULL CHECK(state IN ('issuing', 'uncertain', 'recorded'))`.
  - `payment_hash TEXT UNIQUE CHECK(payment_hash IS NULL OR length(payment_hash)=64)`.

### Máquina de Estados e Garantias
1. **`reserve(memo, amount)` (Pré-Chamada Upstream):**
   - Executa `BEGIN IMMEDIATE`.
   - Verifica se o memo já foi utilizado (rejeição de repetição) e se `SUM(amount) + amount <= 10` (teto agregado por arquivo de ensaio).
   - Insere linha com estado transitório `issuing` e confirma com `db.commit()` e fsync antes de iniciar a conexão TLS com o upstream.
2. **`mark(memo, state, payment_hash)` (Pós-Validação Criptográfica):**
   - Transição `issuing -> recorded`: só é chamada após validação completa da fatura BOLT11 pelo proxy (moeda `bcrt`, hash coincidente, montante em msat exato e assinatura válida da chave da fixture). Grava `payment_hash` de 64 caracteres hexadecimais.
   - Transição `issuing -> uncertain`: acionada caso ocorra timeout, erro de rede, queda de conexão, divergência criptográfica ou resposta inválida.
   - Não permite transição a partir de `recorded` (imutabilidade de faturas confirmadas) nem retorno de `uncertain` para `issuing`.
3. **Resiliência a Crash / SIGKILL:**
   - Ao abrir a outbox (`Outbox(path)`), o processo executa automaticamente:
     `UPDATE attempts SET state='uncertain' WHERE state='issuing'`
   - Se o processo do proxy for morto com `SIGKILL` enquanto uma tentativa estava em `issuing` (entre o commit da reserva e o recebimento da resposta), o registro é promovido imediatamente para `uncertain`.
   - A cota consumida **não é liberada** (`SUM(amount)` contabiliza itens em `uncertain`). O memo **não pode ser reutilizado**.

---

## 2. Evidências dos Pontos de Falha Executados no Lab (LXC 121)

Foram executados ensaios isolados para validar cada ponto de quebra, preservando os diretórios de evidência para auditoria.

### A. Falha ANTES do POST Fictício (`--fail-before`)
- **Cenário:** Falha simulada de conectividade TCP imediatamente após a reserva durável no outbox, antes de enviar qualquer byte HTTP ao upstream.
- **Comportamento observado:**
  1. Proxy reservou `tcc:fixture` (10 sats) em `outbox.sqlite3` como `issuing`.
  2. Tentativa de conexão ao upstream falhou com `ConnectionRefusedError`.
  3. Bloco `finally` do proxy detectou `reserved=True` e marcou a tentativa como `uncertain`.
  4. Proxy retornou HTTP 403 ao cliente UDS; LNbits retornou HTTP 520 ao probe.
  5. Probe tentou reenviar a requisição: proxy rejeitou sumariamente com 403 (cota esgotada e memo duplicado).
  6. LNbits SQLite (`apipayments`): **0 pagamentos gravados**.
  7. Upstream TLS: **0 requisições POST recebidas** (`tls.issued == 0`).
  8. Outbox: `[('tcc:fixture', 10, 'uncertain', None)]`. Cota consumida: 10 sats; restante: 0 sats.
- **Evidência arquivada:** `/tmp/tcc-ln-fixture-evidence-fail-before`.

### B. Falha DURANTE / DEPOIS do POST com Resposta Perdida (`--timeout`)
- **Cenário:** O upstream TLS falso processou o `POST /invoices` (registrou a criação lógica da fatura), mas a resposta foi perdida/abandonada antes de ser recebida pelo proxy.
- **Comportamento observado:**
  1. Proxy reservou `tcc:fixture` (10 sats) no outbox como `issuing`.
  2. Upstream TLS recebeu a requisição e computou a fatura sintética.
  3. Upstream simulou timeout de 15 segundos; proxy abortou a conexão por timeout.
  4. Proxy marcou a tentativa como `uncertain` (`payment_hash IS NULL`).
  5. LNbits não recebeu resposta e reportou falha fechada; **0 pagamentos pendentes gravados em `apipayments`**.
  6. Upstream TLS: **1 requisição POST recebida** (`tls.issued == 1`).
  7. Nova tentativa do cliente rejeitada pelo proxy pela cota retida.
  8. Outbox: `[('tcc:fixture', 10, 'uncertain', None)]`.
- **Evidência arquivada:** `/tmp/tcc-ln-fixture-evidence-timeout`.

### C. SIGKILL do Daemon LNbits e Reinício (`--kill-restart`)
- **Cenário:** Fatura de 10 sats emitida com sucesso (`recorded`), mas o daemon LNbits sofreu `SIGKILL` sem encerramento gracioso. Em seguida, o daemon foi reiniciado dentro da mesma netns usando a mesma base de dados.
- **Comportamento observado:**
  1. Daemon reiniciou dentro da netns sem rota externa.
  2. Probe consultou status da fatura: `status.status_code == 200` e `paid == False`.
  3. SQLite do LNbits manteve exatamente **1 pagamento pendente** de 10.000 msat.
  4. Nenhuma reemissão upstream foi gerada (`tls.issued == 1`).
  5. Nenhum crédito foi duplicado.
- **Evidência arquivada:** `/tmp/tcc-ln-fixture-evidence-kill-restart`.

### D. Reinício do Processo PROXY e Bloqueio de Replay (`--proxy-restart`)
- **Cenário:** Fatura emitida com sucesso (`recorded`). O processo do proxy UDS foi finalizado (`SIGTERM`/fechamento do socket) e um novo processo de proxy foi instanciado apontando para o **mesmo arquivo `outbox.sqlite3`**.
- **Comportamento observado:**
  1. Novo proxy inicializou, validou integridade do SQLite e carregou a ledger durável.
  2. Client dentro da netns enviou requisição de replay (`POST /api/v1/payments` com o mesmo memo `tcc:fixture`).
  3. Proxy 2 interceptou a requisição, consultou a outbox durável, identificou memo já existente e cota esgotada (10/10) e retornou HTTP 403 Forbidden.
  4. Upstream TLS: **nenhuma nova requisição POST recebida** (`tls.issued == 1` mantido).
  5. Outbox: manteve exatamente 1 registro `recorded` com hash e 10 sats consumidos.
- **Evidência arquivada:** `/tmp/tcc-ln-fixture-evidence-proxy-restart`.

---

## 3. Matriz Consolidada de Evidências em Disco

Inspeção direta dos bancos de dados arquivados em `/tmp/tcc-ln-fixture-evidence-*`:

| Ensaio | Outbox `attempts` (memo, amount, state, payment_hash) | LNbits `apipayments` (amount_msat, status) | Upstream POSTs |
|---|---|---|---|
| `fail-before` | `('tcc:fixture', 10, 'uncertain', NULL)` | Nenhum registro (0) | 0 |
| `timeout` | `('tcc:fixture', 10, 'uncertain', NULL)` | Nenhum registro (0) | 1 |
| `normal` | `('tcc:fixture', 10, 'recorded', 'a'*64)` | `(10000, 'pending')` | 1 |
| `proxy-restart` | `('tcc:fixture', 10, 'recorded', 'a'*64)` | `(10000, 'pending')` | 1 |
| `kill-restart` | `('tcc:fixture', 10, 'recorded', 'a'*64)` | `(10000, 'pending')` | 1 |

---

## 4. Procedimento Formal de Reconciliação e Quarentena

Para garantir que nenhuma fatura órfã cause perda financeira ou duplo crédito em caso de falha de processo, o sistema define duas rotinas operacionais:

### 1. Faturas em Estado `recorded` (Hash Conhecido)
- **Condição:** O proxy recebeu a fatura do upstream e validou os dados BOLT11 antes da interrupção. O `payment_hash` está registrado na outbox.
- **Procedimento:**
  1. Consultar o backend upstream via rota read-only `GET /invoices/<payment_hash>`.
  2. Se a fatura estiver liquidada (`settled` / `paid`):
     - Conferir se o montante liquidado corresponde exatamente ao valor da fatura.
     - Creditar a conta no gateway através de transação transacional com chave de idempotência exclusiva no `payment_hash` (tabela `deposits` com constraint `UNIQUE(payment_hash)`).
  3. Se a fatura estiver pendente ou expirada:
     - Manter o status pendente até a expiração (TTL de 3600 segundos). Não creditar.

### 2. Faturas em Estado `uncertain` (Quarentena Explícita)
- **Condição:** Houve falha antes do recebimento/validação do hash (falha pré-POST, timeout durante POST, resposta corrompida ou crash do proxy em `issuing`). O `payment_hash` é `NULL`.
- **Regras Mandatórias de Segurança:**
  1. **Proibição de Reemissão Automática:** O sistema recusa terminantemente qualquer nova tentativa de emissão com o mesmo memo ou que exceda o teto restante.
  2. **Retenção de Cota:** A cota de 10 sats consumida pela tentativa em `uncertain` **não é devolvida ao teto disponível**. Isso impede que sucessivas falhas de rede causem emissão repetida no provedor além do limite acordado.
  3. **Isolamento de Quarentena:**
     - Uma fatura em `uncertain` sem hash conhecido **não pode ser reconciliada automaticamente por busca textual de memo**, pois a API externa pode ter sofrido truncamento, colisão ou atraso de sincronização.
     - Operador humano deve auditar o painel/logs da API do provedor.
     - Se o provedor confirmar que a requisição não foi criada, o operador pode arquivar o arquivo de ledger e iniciar uma nova rodada de ensaios com um novo banco.
     - Em hipótese alguma o software executa rollback automático de cota sem intervenção explicita do operador.

---

## 5. Rollback e Higiene Operacional no Host de Testes

- **Ambiente Lab (LXC 121):**
  - Todas as instâncias temporárias ativas de Uvicorn/daemon foram terminadas (`pgrep` confirma 0 processos de `tcc_ln_lab`).
  - `/tmp/tcc-ln-fixture` ativo foi removido; apenas diretórios de evidência arquivados com permissão restrita `root:root` foram mantidos para auditoria.
  - Namespaces de rede limpos (`ip netns list` vazio).
  - Tabela nftables inalterada (`table inet filter`).
  - Arquivo de launcher `/opt/tcc-ln-lab/run.sh` verificado intacto com `LNBITS_BACKEND_WALLET_CLASS=VoidWallet`.
  - Arquivo de segredo real `/opt/tcc-ln-lab/secrets/alby-access-token` intocado (permissão `0600 root:root`, data e hora inalteradas).
- **Ambiente Dev (LXC 120):**
  - Suíte completa de testes no PostgreSQL 16 dedicado (`127.0.0.1:55432`): **64 testes aprovados** (`tests/test_alby_guarded_proxy.py`, `tests/test_fixture_outbox.py`, `tests/test_issuance_migration_pg.py`, etc.).
  - SHA-256 do texto de proposta original `docs/TCC1-original.tex` conferido: `cd83a551fce1cb064fee3b3cdce6e9866595df3e2c46dd12f03caf99edef3457`.

---

## 6. Barreiras Remanescentes e Decisão de Segurança

A implementação da outbox transacional durável e a execução dos pontos de falha demonstram que o proxy UDS não duplica chamadas nem perde o teto sob SIGKILL. **Entretanto, o fluxo real continua estritamente BLOQUEADO pelas seguintes barreiras objetivas:**

1. **Vínculo Transacional Ponta a Ponta Inexistente:**
   - A outbox durável foi demonstrada entre o proxy e o fornecedor simulado, e o LNbits foi demonstrado com seu SQLite interno.
   - O API Gateway principal (`src/ln_gateway/`) e o LNbits ainda operam em processos e bancos separados sem uma transação de duas fases (2PC) ou outbox unificada vinculando a sessão do usuário com o checking_id/payment_hash do LNbits.
2. **Dependência de Classe Upstream Insegura:**
   - A classe oficial `AlbyWallet` upstream do LNbits continua exigindo o Bearer token via `settings.alby_access_token`, com chamadas diretas sem allowlist e sem timeout seguro (`timeout=None` em pagamentos). O ensaio utilizou o adaptador customizado em memória `FixtureUDSWallet`; para um ambiente seguro, o LNbits precisaria usar um conector vendorizado que fale exclusivamente via UDS com o proxy de controle.
3. **Ausência de Egress Firewall Persistente no Host Lab:**
   - O isolamento de rede foi garantido via `unshare --net` na execução da fixture. Em modo de serviço contínuo (daemon), a regra de nftables/cgroup restringindo o tráfego do UID `tcc_ln_lab` estritamente ao socket UDS local e bloqueando todo e qualquer egress TCP/UDP ainda precisa ser instalada e testada em nível de sistema operacional.
4. **Necessidade de Autorização Específica:**
   - O escopo `invoices:create` registrado na chave externa não autoriza operações reais com fundos.
   - Qualquer emissão de fatura real de 10 sats (mesmo que para fins de teste) exigirá autorização formal com identificação do pagador, janela de tempo restrita e confirmação expressa do operador.
