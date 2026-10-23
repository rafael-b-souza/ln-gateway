# TCC 2 — pontos para conversar com a orientação

**Aluno:** Rafael Bernardy de Souza, RA 2538865 — Engenharia de Software, UTFPR Dois Vizinhos  
**Referência:** proposta original em `TCC1-original.tex`  
**Situação da orientação:** os nomes Newton Carlos Will e Erinaldo da Silva Pereira constam no material do TCC 1; o aceite para TCC 2 ainda precisa ser confirmado. Confirmar também com Marlon (`pratcc-coens-dv@utfpr.edu.br`) o cadastro no SIACOES e a forma de entrega.

## O que mostrar

O gateway FastAPI autentica por chave pública Ed25519. O cliente assina um desafio de 60 segundos vinculado à conta, à rota, ao ID da operação e ao hash do conteúdo. O banco consome cada nonce uma vez. Um segundo desafio para o mesmo ID e conteúdo recupera o débito anterior sem cobrar novamente; com conteúdo diferente, a requisição é recusada.

No PostgreSQL local dedicado (`127.0.0.1:55432`), atualização condicional do saldo, lançamento contábil e eventos de uso ocorrem na mesma transação; o fluxo também roda em SQLite local com testes de concorrência e reversão. A API pública de depósitos opera com faturas simuladas para demonstrar o ciclo de crédito sem risco financeiro. Os componentes de integração externa (adaptador LNbits REST, validação BOLT11, contrato LNURL-Pay LUD-06 e outbox com proxies Unix) foram mantidos em módulos de bancada de laboratório isolados.

O ensaio prévio com a Blink (10 satoshis) comprovou o funcionamento do fluxo de ponta a ponta em ambiente controlado; para a demonstração na reunião, utiliza-se exclusivamente o fluxo simulado sem movimentação de saldo real.

## Demonstração sem dinheiro

Na raiz do projeto, com a instância PostgreSQL dedicada iniciada e as dependências em `.venv`:

```bash
PGPASSFILE="$PWD/.postgres-run/pgpass" \
TCC_TEST_DATABASE_URL='postgresql+psycopg://tcc_ln_gateway_test@127.0.0.1:55432/tcc_ln_gateway_test' \
PYTHONPATH=src .venv/bin/python -m pytest -q
# Verificado em 22/10/2026: 76 passed

PYTHONPATH=src .venv/bin/python -m ln_gateway.demo_flow
# money_moved: false
```

A suíte reúne testes unitários em SQLite, validações offline de contratos de rede e testes transacionais no PostgreSQL. A variável de banco é necessária para executar os 10 testes de concorrência e rollback no PostgreSQL. Os dados detalhados do ensaio pontual na rede principal constam em `docs/ENSAIO-BLINK-LIVE.md`.

## Divergências e perguntas para a reunião

- O TCC 1 previa BIP-340/Schnorr, TimescaleDB, LNURL-Pay/WebSocket, cobrança por tokens ou bytes, contêineres e avaliação de latência, vazão e custo. O protótipo implementou Ed25519, tarifação por requisição, bytes e tokens (`tiktoken` BPE), particionamento de eventos analíticos compatível com TimescaleDB e validação de contrato LNURL-Pay LUD-06 com faturas assinadas BOLT 11. Permanecem pendentes: avaliação de latência e vazão sob carga, contêineres e WebSocket em tempo real.
- A hipótese de 60% de redução de custos **não foi testada**. Qual baseline econômico e qual carga de teste fazem sentido para a banca?
- O caso de emissão com resultado desconhecido permanece em quarentena na outbox e exige reconciliação assistida. Como a orientação recomenda estruturar essa restrição técnica no relatório final para delimitar com precisão o que foi validado e o que depende de intervenção operacional humana?
- Confirmar o aceite formal da orientação no SIACOES e alinhar os prazos de 2026/2 (entrega em 09/11 e defesas até 30/11). Apresentar a minuta em LaTeX para receber apontamentos de conteúdo e validar o modelo da biblioteca UTFPR a ser adotado.
