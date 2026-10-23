# TCC2 — resultados técnicos parciais (21/10/2026)

Este registro é independente de `TCC1-original.tex` e não é uma versão final institucional. Projeto do aluno Rafael Bernardy de Souza. O texto de TCC1 é uma proposta, inclusive metas p95/throughput/custo e hipótese de redução de ~60%: nenhuma dessas metas foi demonstrada.

## Artefato executado

FastAPI no loopback oferece registro administrativo de chave Ed25519, desafio ligado a operação/ID/conteúdo e expiração, depósito fictício com referência externa exclusiva, verificação da confirmação no fake com valor e identidade da carteira, crédito uma vez e lançamento auditável, consumo com débito único e recibo autenticado. SQLite e PostgreSQL 16 local exclusivo (porta 55432) têm SQL versionado v1-v4; teste de duas instâncias de app contra mesmo banco confirma replay e serialização. O adaptador REST de LNbits está separado e **não está habilitado para emitir faturas reais**. Sem backend externo, saldo real ou chave Lightning.

## Evidência reproduzível e interpretação

No cluster exclusivo do TCC (`docs/POSTGRESQL.md`), a suíte automatizada completa executou **76 testes aprovados** sem advertências ou pulos:
```bash
PGPASSFILE="$PWD/.postgres-run/pgpass" TCC_TEST_DATABASE_URL='postgresql+psycopg://tcc_ln_gateway_test@127.0.0.1:55432/tcc_ln_gateway_test' PYTHONPATH=src .venv/bin/python -m pytest -q
# 76 passed in 51.32s
```
Os testes incluem concorrência de débito e crédito (`with_for_update`), isolamento estrito entre contas, prevenção de repetição de nonces/desafios, serialização de depósitos concorrentes com chave única de `payment_hash`, outbox durável transacional com máquina de estados `issuing -> uncertain -> recorded`, proxy UDS com autenticação `SO_PEERCRED`, validação criptográfica BOLT11 de faturas, tarifação determinística por bytes ou tokens com rollback atômico de eventos de uso, validação de contrato LNURL-Pay LUD-06 com vinculação estrita de `description_hash` e checagem de esquema TimescaleDB.

### Ensaio Real em Mainnet com a Blink (21/10/2026)
Conforme documentado em `docs/ENSAIO-BLINK-LIVE.md`, foi executado ensaio real de ponta a ponta na Lightning Network principal com autorização explícita e teto de risco de 10 satoshis:
1. **Emissão**: Fatura BOLT11 de 10 sats gerada via GraphQL da Blink (`tcc:tcc2-blink-01`), com validação criptográfica prévia (rede `bc`, 10.000 msat, assinatura do payee e validade futura comprovadas).
   - `payment_hash`: `4add08da0f7df3dff843b953744fa6da48a432f3f20c6dbfb66a88556c03e07a`.
2. **Liquidação Externa**: O usuário efetuou o pagamento da fatura em sua carteira pessoal, confirmando a liquidação externa (`status: PAID`, `settled: true`).
3. **Reconciliação e Auditoria**: O script `scripts/reconcile_and_consume_live.py` atualizou o depósito para `credited`, elevando o saldo da conta `demo-live-user` para 10 sats e gerando entrada auditável em `credits`.
4. **Consumo Autenticado com Chave Ed25519**: Foi emitido desafio assinado criptograficamente pelo cliente para `/v1/consume`; o gateway debitou 1 sat (saldo final: 9 sats) e gerou recibo consultável em `/v1/receipt`.

## Limites e trabalho restante

O contrato e as salvaguardas da outbox transacional foram validados na prática. No entanto, em relação à proposta teórica original do TCC 1:
- A autenticação implementada utiliza Ed25519 (RFC 8032) com desafios específicos vinculados ao conteúdo e validade curta; não se utilizou BIP-340 (chaves Schnorr nativas do Bitcoin).
- A cobrança suporta três modos determinísticos (`request`, `bytes` e `tokens` via `tiktoken`), registrando métricas em `usage_events` (migração 005) na mesma transação atômica do débito. A tabela foi desenhada com chave primária temporal `(account_id, request_id, occurred_at)` compatível com hipertabelas TimescaleDB. O script `scripts/enable_timescale_usage.py` valida e isola a ativação no cluster de testes.
- O contrato LNURL-Pay LUD-06 foi implementado em `ln_gateway.lnurl_pay.OfflineLNURLPay` com validação de metadados, prevenção de SSRF e verificação de `description_hash` em fatura BOLT 11. A exposição com carteiras móveis externas reais exige domínio público HTTPS futuro.
- A comparação de custos entre processadores legados (ex: Stripe) e micropagamentos Lightning é mantida como hipótese teórica fundamentada na literatura, sem execução de experimentos comparativos de benchmark com tráfego massivo sob carga de produção (Locust/Grafana).
- Não há benchmark para comprovar a meta de redução de custos de 60% proposta no TCC 1, que deve ser tratada estritamente como hipótese acadêmica.

## Para discutir com orientadores

Newton/Erinaldo: aceitar arquitetura de autenticação segregada ou exigir BIP-340? Qual escopo mínimo e comparação econômica defensável dentro do prazo? Autorizar sandbox LNbits separado e plano de exposição apenas local? **Urgente:** prazo informado de entrega 09/11/2026 ainda depende de confirmação de orientação, canal SIACOES, formato exigido e eventuais adequações de escopo com Newton/Erinaldo e responsável pelo TCC. Não houve envio, publicação ou regularização via SIACOES.
