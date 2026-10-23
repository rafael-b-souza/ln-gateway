# Evidência Técnica do Sandbox LNbits v1.6.2 (LXC 121 / lab)

Data de execução: 21/10/2026.  
Escopo: Validação controlada de sandbox isolado, **sem fundos, sem rede externa e sem emissão de fatura real**.

---

## 1. Ambiente e Isolamento Comprovado

1. **Host Dedicado**:
   - Hostname `lab`, LXC `10.10.10.121`, Ubuntu 24.04 LTS (kernel Linux 6.8).
   - O LXC dev (`10.10.10.120`) e os bots em produção (`10.10.10.102`) não receberam nenhum serviço ou dependência do LNbits.
   - O PostgreSQL do sistema (`127.0.0.1:5432`) não foi acessado. Todos os testes transacionais usam a porta dedicada `55432`.

2. **Diretório e Usuário de Execução**:
   - Diretório exclusivo: `/opt/tcc-ln-lab/`.
   - Venv dedicado em Python 3.12 com código-fonte clonado diretamente da tag oficial `v1.6.2` (commit `9349da153c223b008f7d80453b53a54e09c8ed3d`).
   - Usuário de sistema sem privilégios: `tcc_ln_lab` (UID 999), sem shell (`/usr/sbin/nologin`).
   - `setpriv` removeu todas as capabilities do processo (`--bounding-set=-all --inh-caps=-all --ambient-caps=-all`).

3. **Isolamento Criptográfico e de Rede (Network Namespace Privado)**:
   - O processo do LNbits foi lançado com `unshare --net`, criando um network namespace privativo (`net:[4026534304]`), totalmente separado do namespace do host (`net:[4026533692]`).
   - No namespace do LNbits, apenas a interface de loopback (`lo`) foi habilitada. Não existe rota default, gateway ou interface externa.
   - **Comprovação de Egress Blocked**: tentativa de conexão socket para IP público (`1.1.1.1:443`) retornou `errno 101` (`Network is unreachable`).
   - O processo LNbits escuta estritamente em `127.0.0.1:5000` **dentro** do namespace isolado; conexões a partir do host para essa porta falham diretamente (`URLError`).
   - A porta SSH (`22`) do host `10.10.10.121` foi preservada sem interferência.

---

## 2. Configuração do Backend e Proibição de Faturas Reais

1. **Funding Source**:
   - Configurado explicitamente como `VoidWallet` (`LNBITS_BACKEND_WALLET_CLASS=VoidWallet` e `LNBITS_ALLOWED_FUNDING_SOURCES=VoidWallet`).
   - `VoidWallet` é uma implementação nula interna do LNbits que rejeita criação e pagamento de faturas (`create_invoice` e `pay_invoice` retornam `ok=False`), retornando saldo `0`.
   - O `FakeWallet` não foi utilizado para emissão, pois a inspeção do código comprovou que ele gera invoices BOLT11 com prefixo `bc` (mainnet) que poderiam ser confundidas com faturas pagáveis.

2. **Desativação de Superfície de Ataque e Extensões**:
   - `LNBITS_ALLOW_NEW_ACCOUNTS=false`: proíbe cadastro público de usuários.
   - `LNBITS_EXTENSIONS_DEACTIVATE_ALL=true`: desativa todas as extensões.
   - `LNBITS_EXTENSIONS_MANIFESTS=[]` e `LNBITS_WASM_EXTENSIONS_MANIFESTS=[]`: impede download de manifestos remotos.
   - `FORWARDED_ALLOW_IPS=127.0.0.1`.

---

## 3. Resultados Reais das Chamadas à API do Sandbox

Com o sandbox ativo no namespace isolado, os endpoints foram consultados via chamadas internas autenticadas:

1. **`GET /api/v1/health`**:
   - Retorno: HTTP 200 `OK`.
   - Payload: `{"server_time": ..., "up_time": "..."}`.

2. **Inicialização do Administrador e Carteira Vazia**:
   - Inicialização local através do endpoint `PUT /api/v1/auth/first_install` com credencial aleatória temporária gerada no sandbox.
   - Uma única carteira descartável vazia (`LNbits wallet`) foi gerada localmente no SQLite (`/opt/tcc-ln-lab/data/database.sqlite3`).
   - Contagem no banco: `wallets = 1`, `apipayments = 0`.
   - Nenhuma chave externa, nó Lightning ou saldo foi importado.

3. **`GET /api/v1/wallet` (Autenticado com Invoice Key `X-Api-Key`)**:
   - Retorno: HTTP 200 `OK`.
   - Payload real verificado: `{'name': 'LNbits wallet', 'balance': 0}`.
   - Saldo estritamente zero (`0 msat`).

4. **`GET /api/v1/payments` (Histórico de Pagamentos)**:
   - Retorno: HTTP 200 `OK`.
   - Payload real verificado: `[]` (contagem de pagamentos = 0).

5. **`GET /api/v1/payments/{payment_hash}` com Hash Desconhecido**:
   - Consulta de hash inexistente (`0` * 64) tanto com cabeçalho `X-Api-Key` quanto sem cabeçalho.
   - Retorno real verificado: HTTP 404 `Not Found` (`detail: "Payment does not exist."`).

6. **Teste de Persistência e Reinício**:
   - O processo sandbox foi encerrado e reiniciado mantendo o volume de dados.
   - Após reinício, o status HTTP 200 foi revalidado em `/api/v1/health` e `/api/v1/wallet` (mantendo `balance: 0` e `apipayments = 0`).
   - O sandbox foi finalizado após a bateria de validações.

---

## 4. O que Ficou Comprovado vs O que NÃO Foi Feito

### Comprovado
- Instalação limpa, reproduzível e isolada da tag LNbits `v1.6.2` no LXC laboratório `10.10.10.121`.
- Bloqueio estrito de tráfego de saída do processo (egress code 101) via network namespace dedicado.
- Contrato HTTP verificado na prática com o servidor real rodando: formato de `/api/v1/health`, formato de chave de leitura e resposta de saldo `0` em `/api/v1/wallet`, e comportamento 404 em faturas inexistentes em `/api/v1/payments/{hash}`.
- Suíte transacional do gateway (PostgreSQL 16 porta 55432 no LXC dev) com **33 testes passando sem falhas**.

### NÃO Feito (Limites Estritos e Pendências)
- **Nenhum fundo foi movimentado**: não houve uso de satoshis nem carteira de produção.
- **Nenhuma fatura BOLT11 pagável foi emitida**: com o `VoidWallet`, a criação de invoices é rejeitada na fonte, garantindo segurança contra emissão acidental.
- **Não há conexão remota ativa do Gateway para o LNbits**: o gateway continua usando o port isolado `FakeInvoices` em memória. A máquina de estados durável (`issuing` / `uncertain`) permanece desacoplada do LNbits real até a resolução formal dos requisitos abaixo.

---

## 5. Roteiro Obrigatório Antes de Qualquer Teste com Saldo Real

Para que qualquer operação com fundos reais venha a ser sequer cogitada no futuro, as seguintes etapas devem ser cumpridas:
1. Validador de decodificação BOLT11 independente integrado ao gateway (verificação de assinatura, hash de pagamento, satoshis e expiração sem confiar cegamente no envelope de retorno).
2. Protocolo de outbox / auditoria transacional para faturas órfãs decorrentes de perda de resposta entre gateway e nó.
3. Definição formal de teto financeiro máximo (ex: 100 sats) e autorização prévia, explícita e específica do usuário.
