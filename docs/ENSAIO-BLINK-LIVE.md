# Relatório de Ensaio Real em Mainnet com a API Blink (LXC 121)

Data: 21 de outubro de 2026  
Status: **Concluído com Sucesso Total — Emissão, Liquidação na Lightning Network, Reconciliação e Débito Criptográfico Comprovados**.

---

## 1. Contexto e Autorização

Com autorização expressa do usuário e sob o teto rígido de risco de **10 satoshis**, realizou-se o ensaio de ponta a ponta na rede principal (mainnet) do Bitcoin/Lightning Network.
O objetivo foi demonstrar na prática o ciclo completo do Gateway de Autenticação, Custódia e Consumo de API (TCC 2):
1. Emissão contida de fatura Lightning de 10 sats.
2. Validação criptográfica independente da fatura recebida da rede.
3. Liquidação externa da fatura pelo pagador.
4. Reconciliação atômica e idempotente no banco transacional PostgreSQL.
5. Consumo autenticado via desafio-resposta com chave assimétrica Ed25519 (débito de 1 sat) e geração de recibo auditável.

---

## 2. Arquitetura de Contenção e Salvaguardas

Para garantir risco zero de vazamento de credenciais ou pagamentos indevidos:
1. **Proxy UDS/TLS Restrito (`lab/blink_live_proxy.py`)**:
   - Conexão TLS 1.3 restrita exclusivamente a `api.blink.sv:443` com validação de certificados da CA do sistema operacional (`ssl.CERT_REQUIRED`, verificação estrita de hostname).
   - Comunicação local condicionada a Unix Domain Socket privado (`0600`), com autenticação via `SO_PEERCRED` restrita ao UID 0 (`root`).
   - Bloqueio sumário (HTTP 403) de qualquer mutação ou rota de pagamento de saída (`lnInvoicePaymentSend` proibido).
2. **Contenção do Segredo**:
   - O token da Blink foi lido exclusivamente via descritor de arquivo em modo `0600` (`O_NOFOLLOW`) pelo processo root do proxy, nunca passando por linha de comando (`argv`), variáveis de ambiente (`env`) ou logs de execução.
3. **Outbox Transacional Durável (`lab/fixture_outbox.py`)**:
   - Teto cumulativo rígido de 10 sats gravado em SQLite com `journal_mode = DELETE` e `synchronous = FULL`.
   - Reserva antecipada obrigatória antes de qualquer chamada externa.
4. **Validador Criptográfico BOLT11 Independente**:
   - Decodificação e inspeção da fatura com a biblioteca `bolt11`: exigência de prefixo `bc` (mainnet), montante exato de 10.000 msat, conferência estrita de `payment_hash`, assinatura do payee válida e prazo de expiração compatível.

---

## 3. Registro Cronológico da Execução

### Etapa A: Leitura e Identificação das Carteiras
- **Consulta GraphQL**: `query Me { me { defaultAccount { wallets { id walletCurrency balance } } } }`
- **Resultado Obtido**:
  - Carteira BTC: ID `917bca6e-64d4-4f37-a262-1164df2a375f` (Saldo inicial: `0` sats).
  - Carteira USD: ID `d67fd372-92fd-413d-8495-88b516decf8e` (Saldo inicial: `0` sats).

### Etapa B: Emissão da Fatura de 10 Sats
- **Reserva na Outbox**: Cota de 10 sats consumida (`remaining_sats = 0`).
- **Mutation GraphQL**: `lnInvoiceCreateOnBehalfOfRecipient` para 10 sats, memo `tcc:tcc2-blink-01`.
- **Fatura Gerada**:
  - `payment_hash`: `4add08da0f7df3dff843b953744fa6da48a432f3f20c6dbfb66a88556c03e07a`
  - `payment_request`:
    ```text
    lnbc100n1p4djn09pp5ftws3ks00heal7zrh9fhgnaxmfy2gvhn7gxxm0akd2y92mqrupaqdquw33kxwn5vd3nyttzd35ku6edxqcscqzpuxqrwzqsp533kc574yzh4w4c8g76pxg8dtxevjn3c8x38eh9l5whr7pwqewrqq9qxpqysgqs0nh4ghzrs9dy7sl2g89sc37fwm2vw77edulqrqudrzk0c438a050gahxmxkxzge3k395gvsykyxpxxj7w3alrnm7havmesud4uzsesqzv8ukk
    ```
- **Validação BOLT11**: Aprovada sem ressalvas (rede `bc`, 10.000 msat, assinatura conferida).
- **Registro Preventivo**: Gravado em `deposits` com status `pending` na conta `demo-live-user`.

### Etapa C: Pagamento e Liquidação na Lightning Network
- A fatura foi apresentada ao usuário, que efetuou o pagamento a partir de sua carteira Lightning pessoal.
- A consulta `InvoiceByPaymentHash` na Blink confirmou a liquidação:
  ```json
  {
    "payment_hash": "4add08da0f7df3dff843b953744fa6da48a432f3f20c6dbfb66a88556c03e07a",
    "status": "PAID",
    "settled": true
  }
  ```

### Etapa D: Reconciliação Transacional no PostgreSQL (Porta 55432)
O script `scripts/reconcile_and_consume_live.py` executou a reconciliação atômica sob lock:
1. `Deposit(account_id='demo-live-user', request_id='tcc2-blink-01')` atualizado de `pending` para `credited` em `2026-10-21 23:53:38 UTC`.
2. `Account.balance` incrementado em 10 sats (saldo: `10` sats).
3. Registro de auditoria em `credits` gravado com `payment_hash`, `amount = 10` e `balance_after = 10`.

### Etapa E: Consumo Autenticado com Chave Ed25519 e Auditoria
1. O cliente solicitou desafio de autenticação em `/v1/challenges` para a rota `/v1/consume`.
2. O cliente assinou a mensagem canônica contendo nonce, path, account_id, request_id e hash do payload utilizando sua chave privada local Ed25519 (`demo_live_client.demo-key`).
3. Chamada `POST /v1/consume` enviada:
   - Resposta: `HTTP 200 OK`
   - `request_id`: `consume-live-01`
   - `charged_sat`: `1` sat
   - `balance_sat`: `9` sats
   - `replayed`: `false`
   - `echo`: `ensaio-blink-tcc2-ok`
4. Consulta ao recibo `/v1/receipt`:
   - Verificado lançamento idempotente em `debits` comprovando o débito de 1 sat e o saldo resultante de 9 sats.

---

## 4. Evidências Gravadas no Banco PostgreSQL Dedicado

```text
Table: accounts
 account_id     | public_key                                                       | balance
 demo-live-user | c21dbb049520c5603f7df8f95814211f972ceebf06cf8d0322797a30fc7e6692 |       9

Table: deposits
 account_id     | request_id    | amount | status   | credited_at                   | payment_hash
 demo-live-user | tcc2-blink-01 |     10 | credited | 2026-10-21 23:53:38.007233+00 | 4add08da0f7df3...

Table: credits
 payment_hash      | account_id     | request_id    | amount | balance_after | created_at
 4add08da0f7df3... | demo-live-user | tcc2-blink-01 |     10 |            10 | 2026-10-21 23:53:38.012822+00

Table: debits
 account_id     | request_id      | amount | balance_after | created_at
 demo-live-user | consume-live-01 |      1 |             9 | 2026-10-21 23:53:38.129132+00
```

---

## 5. Higiene e Encerramento Operacional

- Processo proxy encerrado com sucesso; nenhum daemon remanescente no LXC 121.
- Diretórios temporários de execução removidos.
- Arquivos de credenciais restritos a permissões `0600 root:root`.
- Launcher padrão `/opt/tcc-ln-lab/run.sh` preservado em `VoidWallet` e namespace isolado (`unshare --net`).
