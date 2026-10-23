# Relatório de Ensaio Controlado com a API Alby Real (LXC 121)

Data: 21 de outubro de 2026  
Status: **Executado com salvaguardas ativas e contenção confirmada; emissão recusada pelo upstream Alby por ausência de funding source**.

---

## 1. Objetivo e Autorização

Após aprovação explícita do usuário com teto rígido de perda de 10 satoshis, executou-se o ensaio de emissão pontual de uma única fatura de teste de 10 sats contra a API real da Alby (`api.getalby.com:443`), utilizando o token de laboratório presente em `/opt/tcc-ln-lab/secrets/alby-access-token` no LXC lab 121.

---

## 2. Salvaguardas Operacionais Implementadas

1. **Proxy UDS/TLS Restrito (`lab/alby_live_proxy.py`)**:
   - Conexão de saída exclusivamente para `api.getalby.com:443` com validação de certificados TLS via CA do sistema operacional (`ssl.CERT_REQUIRED`, verificação estrita de hostname).
   - Comunicação local via Unix Domain Socket (`proxy.sock`), com verificação de credenciais de processo (`SO_PEERCRED`) restrita ao UID 0 (`root`).
   - Rejeição absoluta e incondicional (HTTP 403) de qualquer rota de pagamento de saída, como `POST /payments/bolt11`.
2. **Contenção Absoluta de Segredo**:
   - O token de acesso foi lido diretamente via descritor de arquivo em modo `0600` com `O_NOFOLLOW` pelo processo root do proxy.
   - O token nunca foi exposto em variáveis de ambiente (`env`), linha de comando (`argv`), logs de execução ou repassado ao LNbits/Gateway.
3. **Outbox Transacional Durável (`lab/fixture_outbox.py`)**:
   - Teto cumulativo rígido de 10 sats gravado em banco SQLite em modo `journal_mode = DELETE` e `synchronous = FULL`.
   - Reserva prévia com `BEGIN IMMEDIATE` antes de qualquer tráfego na rede.
   - Quarentena obrigatória em estado `uncertain` em caso de erro upstream, cancelamento ou resposta diferente de 200 OK.
4. **Validador Criptográfico BOLT11 Independente**:
   - Pipeline de inspeção independente via biblioteca `bolt11` para garantir rede `bc` (mainnet), valor exato de 10.000 msat (10 sats), correspondência estrita com o `payment_hash`, assinatura válida do payee e prazo de expiração compatível antes de qualquer aceite.

---

## 3. Registro Cronológico da Execução

1. **Provisionamento do Ambiente**:
   - Criado diretório privado transitório `/opt/tcc-ln-lab/live_run` com permissões `0700` (`root:root`).
   - Inicializada a outbox durável `/opt/tcc-ln-lab/live_run/outbox.sqlite3`.
   - Iniciado o daemon do proxy `LiveGuardedServer` sob PID `32967`.
   - Verificação do status inicial da outbox:
     ```json
     {
       "recorded": [],
       "uncertain_quarantined": [],
       "consumed_sats": 0,
       "ceiling_sats": 10,
       "remaining_sats": 10
     }
     ```

2. **Disparo da Requisição de Emissão**:
   - Enviada requisição UDS com memo formatado `tcc:tcc2-real-test-01` e montante de 10 sats.
   - O proxy reservou 10 sats na outbox (`remaining_sats = 0`).
   - O proxy estabeleceu conexão TLS 1.3 com `api.getalby.com:443` e enviou a requisição `POST /invoices`.

3. **Resposta do Upstream da Alby**:
   - O servidor da Alby retornou código de status **HTTP 400**:
     ```json
     {
       "error": true,
       "code": 8,
       "message": "Please configure your funding source. https://getalby.com/node/embrace_albyhub"
     }
     ```
   - **Diagnóstico Técnico**: A conta da Alby associada ao token não possui uma fonte de fundos conectada. Com a migração da arquitetura da Alby para Alby Hub / NWC / LNDhub em 2024/2025, contas sem nó Lightning ou carteira de custódia vinculada em `https://getalby.com/node/embrace_albyhub` recusam a criação de faturas via API.

4. **Comportamento das Salvaguardas do Gateway**:
   - Como o status retornado foi diferente de 200 OK, o proxy acionou a transição de segurança no bloco `finally`, marcando a reserva na outbox como `uncertain`:
     ```json
     {
       "recorded": [],
       "uncertain_quarantined": [
         {
           "memo": "tcc:tcc2-real-test-01",
           "amount": 10
         }
       ],
       "consumed_sats": 10,
       "ceiling_sats": 10,
       "remaining_sats": 0
     }
     ```
   - A cota remanescente foi zerada, impedindo qualquer tentativa automática de reemissão.
   - Nenhuma fatura foi exposta, nenhum crédito espúrio foi gerado no PostgreSQL e nenhum satoshi foi perdido.

---

## 4. Higiene e Rollback Pós-Ensaio

Imediatamente após a conclusão do diagnóstico:
1. O processo `LiveGuardedServer` (PID `32967`) foi finalizado com `kill`.
2. O diretório temporário `/opt/tcc-ln-lab/live_run` (contendo socket e logs) foi removido.
3. Foi verificado que nenhum processo órfão do lab permaneceu em execução no LXC 121 (`ps aux`).
4. O launcher padrão `/opt/tcc-ln-lab/run.sh` permaneceu estritamente intacto com `LNBITS_BACKEND_WALLET_CLASS=VoidWallet` e isolamento total via `unshare --net`.
5. A base de dados PostgreSQL dedicada local (`127.0.0.1:55432`) permanece íntegra, com todos os 67 testes automatizados aprovados.
