# ln-gateway — Gateway de autenticação e tarifação de APIs via Lightning Network

Protótipo experimental desenvolvido para o Trabalho de Conclusão de Curso 2 (Engenharia de Software — UTFPR Dois Vizinhos), voltado ao estudo de autenticação por chave pública e tarifação de APIs com créditos denominados em satoshis (Lightning Network). O projeto tem finalidade estritamente acadêmica, investigando consistência transacional e idempotência em arquiteturas de microsserviços. A proposta integral está registrada em `docs/TCC1-original.tex` e permanece inalterada.

A aplicação demonstra autenticação Ed25519 com desafios de uso único e lançamentos contábeis idempotentes. O endpoint protegido exemplifica o processamento com um eco do conteúdo recebido. A emissão de depósitos pela API pública opera com faturas simuladas (`FakeInvoices`), enquanto adaptadores de rede (LNbits REST, validação BOLT11, validador LNURL-Pay e proxies com outbox) constituem módulos de bancada de laboratório para ensaios controlados. Um ensaio pontual prévio com 10 satoshis na rede Blink está documentado em `docs/ENSAIO-BLINK-LIVE.md`; nenhum pagamento real é necessário para executar este repositório.

## Executar localmente

No diretório do projeto, executando em ambiente local com chaves e instâncias de teste:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m ln_gateway.demo_flow
```

O último comando usa banco temporário e faturas simuladas; a saída inclui `money_moved: false`. Sem `TCC_TEST_DATABASE_URL`, os testes que dependem do cluster PostgreSQL dedicado são ignorados. Para rodar todos os **76 testes**, use a instância exclusiva local em `127.0.0.1:55432`, conforme `docs/POSTGRESQL.md`:

```sh
PGPASSFILE="$PWD/.postgres-run/pgpass" \
TCC_TEST_DATABASE_URL='postgresql+psycopg://tcc_ln_gateway_test@127.0.0.1:55432/tcc_ln_gateway_test' \
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Verificado em 22/10/2026: `76 passed`. O PostgreSQL do sistema na porta 5432 não faz parte dos testes.

## Testar o consumo por HTTP

Para uma demonstração nova em SQLite, rode no primeiro terminal:

```sh
.venv/bin/python -m ln_gateway.keygen alice
.venv/bin/python -m ln_gateway.register alice "$(< alice.pub)" --demo-balance 5
.venv/bin/uvicorn ln_gateway.main:app --host 127.0.0.1 --port 8000
```

No segundo terminal, no mesmo diretório:

```sh
.venv/bin/python -m ln_gateway.client_demo alice pedido1 'olá' --key-file alice.demo-key
.venv/bin/python -m ln_gateway.client_demo alice pedido1 'olá' --key-file alice.demo-key
.venv/bin/python -m ln_gateway.client_demo alice pedido1 '' --key-file alice.demo-key --receipt
.venv/bin/python -m ln_gateway.client_demo alice pedido2 'outro' --key-file alice.demo-key
```

A primeira chamada debita 1 crédito; a repetição usa outro desafio e retorna `replayed: true`, sem novo débito. O recibo consulta o lançamento sem cobrar. O arquivo `alice.demo-key` armazena a chave privada gerada para a demonstração local. Para testar novos consumos ou simular conflitos de idempotência, utilize identificadores de requisição distintos.

Para testar depósito sem movimentação financeira, a API aceita os parâmetros `--deposit`, `--reconcile` e `--deposit-status` no cliente. A fatura fica pendente porque a rota pública não realiza marcação de pagamento: a suíte `tests/test_deposits.py` exercita a confirmação controlando o emissor simulado no escopo do teste.

## Tarifação por bytes/tokens, LNURL-Pay e TimescaleDB

O protótipo oferece tarifação configurável via variáveis de ambiente `GATEWAY_BILLING_MODE` (`request`, `bytes` ou `tokens`) e `GATEWAY_UNIT_PRICE_SAT`:
- **`request`**: valor fixo por requisição autenticada.
- **`bytes`**: tarifa calculada por blocos de 1024 bytes trafegados.
- **`tokens`**: contagem determinística via tokenizador BPE `cl100k_base` (`tiktoken`).

A cada consumo, um evento de tarifação é gravado na tabela `usage_events` na mesma transação em que ocorre o débito de saldo. A tabela foi modelada com chave primária composta (`account_id`, `request_id`, `occurred_at`), viabilizando o particionamento temporal como hipertabela no TimescaleDB. O script `scripts/enable_timescale_usage.py` valida essa conversão no PostgreSQL de teste. O módulo `ln_gateway.lnurl_pay.OfflineLNURLPay` valida o contrato LNURL-Pay (LUD-06), conferindo o hash SHA-256 dos metadados contra a fatura BOLT11 e aplicando verificações contra SSRF nas URLs de callback.

### Documentação e relatórios
- `docs/TCC2-relatorio.tex`: minuta acadêmica para o relatório de TCC 2.
- `docs/RESUMO-PROFESSORES.md`: pauta e evidências para alinhamento com os orientadores.
- `docs/TCC2-PROGRESSO.md` e `docs/OUTBOX-FALHAS-RECONCILIACAO.md`: detalhamento dos ensaios de concorrência e injeção de falhas.
