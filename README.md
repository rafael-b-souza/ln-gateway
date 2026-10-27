# ln-gateway — L402 / HTTP 402 Payment Required Gateway for Autonomous AI Agents and Metered APIs

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://python.org)
[![Protocol](https://img.shields.io/badge/protocol-L402%20%2F%20HTTP%20402-orange.svg)](https://docs.lightning.engineering/the-lightning-network/l402)
[![Lightning Network](https://img.shields.io/badge/lightning-BOLT11%20%7C%20LNURL--Pay-yellow.svg)](https://github.com/lightning/bolts)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-80%20passed-brightgreen.svg)](#suíte-de-testes)

**ln-gateway** é uma plataforma e gateway de alta performance que implementa a especificação **L402 (HTTP 402 Payment Required)** para habilitar a economia autônoma de Agentes de Inteligência Artificial (AI Agents) e a tarifação granular de APIs em tempo real através da **Bitcoin Lightning Network**.

O projeto resolve o gargalo de pagamentos máquina-para-máquina (M2M): agentes de software não possuem cartões de crédito ou contas bancárias legadas, mas conseguem transacionar microssatoshis instantaneamente via Lightning Network com liquidação criptográfica determinística.

---

## ⚡ Visão Geral da Arquitetura L402

O protocolo L402 combina três pilares fundamentais:
1. **HTTP 402 Payment Required**: Código de status HTTP nativo da web para paywalls granulares.
2. **Lightning Network (BOLT11 / LNbits)**: Micropagamentos instantâneos com liquidação em satoshis e garantia criptográfica de quitação via pré-imagem SHA-256 (`preimage`).
3. **Autenticação Ed25519 & Tokens HMAC**: Tokens de autorização assinados e desafios criptográficos de uso único (*nonce* efêmero com TTL) imunes a ataques de *replay*.

```
   ┌─────────────────────────────────────────────────────────────┐
   │                     AUTONOMOUS AI AGENT                     │
   │  (Ed25519 Keypair + Autonomous Lightning Wallet Client)     │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
         1. POST /v1/ai/infer     │ 3. HTTP 402 Payment Required
            (Sem credenciais)     │    (Invoice BOLT11 + Ed25519 Challenge)
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                    LN-GATEWAY (L402 CORE)                   │
   │  ┌───────────────────────┐       ┌───────────────────────┐  │
   │  │   L402 Paywall Engine │       │ Challenge & Nonce Mgr │  │
   │  └───────────┬───────────┘       └───────────┬───────────┘  │
   │              │                               │              │
   │  ┌───────────▼───────────┐       ┌───────────▼───────────┐  │
   │  │ Metered Billing (BPE) │       │ ACID Database Ledger  │  │
   │  │  (Request/Byte/Tokens)│       │ (PostgreSQL/Timescale)│  │
   │  └───────────────────────┘       └───────────────────────┘  │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  │ 2. Issue BOLT11 Invoice
                                  │ 4. Verify Payment Settlement
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                  LIGHTNING NETWORK BACKEND                  │
   │         (LNbits REST / LND / CLN / Synthetic Engine)        │
   └─────────────────────────────────────────────────────────────┘
```

---

## 🔄 Diagramas de Sequência em ASCII

### 1. Fluxo L402 Completo para Inferência de Agentes de IA

```text
+----------+                 +------------+                 +----------------+
| AI AGENT |                 | LN-GATEWAY |                 | LIGHTNING NODE |
+----+-----+                 +-----+------+                 +-------+--------+
     |                             |                                |
     | 1. POST /v1/ai/infer        |                                |
     |    (sem token / auth)       |                                |
     |---------------------------->|                                |
     |                             | 2. Cria tentativa & gera       |
     |                             |    fatura BOLT11 assinada      |
     |                             |------------------------------->|
     |                             |<-------------------------------|
     |                             |                                |
     |                             | 3. Cria desafio efêmero        |
     |                             |    (Ed25519 canonical message) |
     |                             |                                |
     | 4. HTTP 402 Payment Required|                                |
     |    WWW-Authenticate: L402   |                                |
     |    {invoice, payment_hash,  |                                |
     |     challenge_nonce}        |                                |
     |<----------------------------|                                |
     |                             |                                |
     | 5. Paga fatura BOLT11       |                                |
     |------------------------------------------------------------->|
     |                             |                                |
     | 6. Retorna Pré-imagem       |                                |
     |    (SHA256 preimage == hash)|                                |
     |<-------------------------------------------------------------|
     |                             |                                |
     | 7. Assina desafio com       |                                |
     |    chave privada Ed25519    |                                |
     |                             |                                |
     | 8. POST /v1/l402/token      |                                |
     |    {preimage, signature,    |                                |
     |     payment_hash, nonce}    |                                |
     |---------------------------->|                                |
     |                             | 9. Valida SHA256(preimage),    |
     |                             |    confere status LN pago,     |
     |                             |    verifica assinatura Ed25519,|
     |                             |    credita conta atômica e     |
     |                             |    emite Token HMAC-SHA256     |
     |                             |                                |
     | 10. HTTP 200 OK             |                                |
     |     {token, expires_in}     |                                |
     |<----------------------------|                                |
     |                             |                                |
     | 11. POST /v1/ai/infer       |                                |
     |     Authorization: L402     |                                |
     |     <token>:<preimage>      |                                |
     |---------------------------->|                                |
     |                             | 12. Valida token & preimage,   |
     |                             |     debita saldo pré-pago,     |
     |                             |     registra evento de uso     |
     |                             |     e executa inferência AI    |
     |                             |                                |
     | 13. HTTP 200 OK             |                                |
     |     {model, response,       |                                |
     |      charged_sat}           |                                |
     |<----------------------------|                                |
```

### 2. Tarifação Granular e Idempotência Contábil

```text
+----------+                 +------------+                 +----------------+
| AI AGENT |                 | LN-GATEWAY |                 | DATABASE (DB)  |
+----+-----+                 +-----+------+                 +-------+--------+
     |                             |                                |
     | POST /v1/consume            |                                |
     | (request_id, nonce, sig)    |                                |
     |---------------------------->|                                |
     |                             | SELECT account FOR UPDATE      |
     |                             |------------------------------->|
     |                             |<-------------------------------|
     |                             |                                |
     |                             | IF request_id já existe:       |
     |                             |   -> Replay Idempotente (200)  |
     |                             |                                |
     |                             | IF balance >= price:           |
     |                             |   UPDATE balance = balance - P |
     |                             |   INSERT debit record          |
     |                             |   INSERT usage_events (tokens) |
     |                             |------------------------------->|
     |                             |<-------------------------------|
     |                             | COMMIT                         |
     | HTTP 200 OK                 |                                |
     | {balance_sat, replayed}     |                                |
     |<----------------------------|                                |
```

---

## 📖 Documentação Interativa Swagger / OpenAPI

O **ln-gateway** disponibiliza documentação OpenAPI 3.0 interativa e completa:

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
- **Esquema OpenAPI JSON**: `http://localhost:8000/openapi.json`

### Catálogo de Endpoints da API

| Método | Endpoint | Autenticação / Protocolo | Descrição |
|---|---|---|---|
| `POST` | `/v1/ai/infer` | **L402 / HTTP 402** | Rota de inferência de IA sob paywall. Retorna `402` com fatura BOLT11 e desafio quando não autenticada; retorna `200` com o resultado do modelo quando fornecido token L402 válido. |
| `POST` | `/v1/l402/token` | **Ed25519 + Preimage** | Valida pré-imagem Lightning e assinatura Ed25519 sobre desafio canônico, emitindo token L402 assinado (HMAC-SHA256). |
| `POST` | `/v1/l402/settle` | Nenhuma (Lab / Demo) | Simula liquidação de fatura em ambiente de testes e retorna a pré-imagem criptográfica correspondente. |
| `POST` | `/v1/challenges` | Nenhuma | Emite desafio efêmero canônico (TTL de 60s) vinculado a uma rota e hash do corpo. |
| `POST` | `/v1/consume` | **Ed25519 Challenge-Response** | Consumo tarifado direto da API com débito idempotente e medição por requisição, bytes ou tokens. |
| `POST` | `/v1/deposits` | **Ed25519 Challenge-Response** | Solicita emissão de fatura Lightning para recarga de saldo pré-pago. |
| `POST` | `/v1/deposits/reconcile`| **Ed25519 Challenge-Response** | Consulta status no provedor Lightning e credita depósito confirmado de forma atômica. |
| `POST` | `/v1/deposits/status` | **Ed25519 Challenge-Response** | Consulta trilha de auditoria persistida de um depósito. |
| `POST` | `/v1/receipt` | **Ed25519 Challenge-Response** | Retorna comprovante criptográfico de lançamento de débito contábil. |
| `GET` | `/health` | Pública | Verificação de integridade e modo de operação do gateway. |

---

## 🐳 Demonstração Completa via Docker Compose

O repositório inclui um ambiente de demonstração pronto para execução com um único comando (`docker-compose.demo.yml`):

```sh
docker compose -f docker-compose.demo.yml up --build
```

### O que o ambiente executa:
1. **Container `gateway`**: Inicia o serviço FastAPI com suporte a L402, banco transacional e emissor Lightning sintético.
2. **Container `agent`**: Agente autônomo de IA (`scripts/demo_l402_agent.py`) que:
   - Gera dinamicamente um par de chaves Ed25519.
   - Solicita `/v1/ai/infer` sem credenciais.
   - Captura e inspeciona o retorno **HTTP 402 Payment Required** e o cabeçalho `WWW-Authenticate: L402`.
   - Liquida a fatura BOLT11 instantaneamente e obtém a pré-imagem.
   - Assina o desafio criptográfico e obtém o token L402 (`/v1/l402/token`).
   - Executa a inferência de IA fornecendo `Authorization: L402 <token>:<preimage>`.
   - Valida idempotência e rejeição contra ataques de repetição (*anti-replay*).

### Execução da demonstração em terminal local (sem Docker):

Em um primeiro terminal:
```sh
.venv/bin/uvicorn ln_gateway.main:app --host 127.0.0.1 --port 8000
```

Em um segundo terminal:
```sh
python3 scripts/demo_l402_agent.py --gateway-url http://127.0.0.1:8000
```

---

## 💻 Instalação e Execução Local

```sh
# Clonar e criar ambiente virtual Python 3.12
git clone https://github.com/rafael-b-souza/ln-gateway.git
cd ln-gateway
python3 -m venv .venv
source .venv/bin/activate

# Instalar dependências em modo de desenvolvimento
pip install -e '.[test]'
```

### Modos de Tarifação Configuráveis

O gateway suporta três modelos de bilhetagem via variáveis de ambiente:
- `GATEWAY_BILLING_MODE=request`: Valor fixo em satoshis por requisição atendida.
- `GATEWAY_BILLING_MODE=bytes`: Tarifa calculada proporcionalmente ao volume de dados (blocos de 1024 bytes).
- `GATEWAY_BILLING_MODE=tokens`: Tarifa computada dinamicamente via contagem de tokens BPE (`tiktoken` modelo `cl100k_base`, padrão GPT-4).

### Suíte de Testes (100% de Aprovação)

```sh
# Executar todos os testes com SQLite em memória
PYTHONPATH=src pytest -v

# Executar suíte completa incluindo cluster PostgreSQL dedicado (80 testes)
PGPASSFILE="$PWD/.postgres-run/pgpass" \
GATEWAY_TEST_DATABASE_URL='postgresql+psycopg://ln_gateway_test@127.0.0.1:55432/ln_gateway_test' \
PYTHONPATH=src pytest -v
```

Resultado atual: **80 passed** (sem falhas e sem regressões).

---

## 🔗 Integração com SatsAI

Este gateway atua como infraestrutura central de pagamento e custódia para aplicações de Inteligência Artificial consumidoras, incluindo o projeto **[SatsAI](https://github.com/rafael-b-souza/satsai)**:
- Enquanto o **SatsAI** provê a interface interativa de chat para usuários humanos (React + Tailwind + QR Code Lightning),
- O **ln-gateway** opera na camada de infraestrutura atendendo Agentes de IA autônomos via chamadas de API M2M sem fricção, micropagamentos automatizados e conciliação idempotente.

---

## 📜 Licença

Distribuído sob a licença MIT. Consulte `LICENSE` para mais detalhes.
