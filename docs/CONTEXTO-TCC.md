# Contexto de implementação — TCC 2 de Rafael Bernardy de Souza

Este diretório é um projeto acadêmico isolado para continuar a proposta de TCC 1 anexada pelo aluno. A fonte primária imutável é `docs/TCC1-original.tex` (texto fornecido pelo aluno; SHA-256 `cd83a551fce1cb064fee3b3cdce6e9866595df3e2c46dd12f03caf99edef3457`). Não substitua a proposta original; crie novos arquivos para o trabalho e registre divergências entre proposta e protótipo.

## Tema e escopo
- Título da proposta: **Gateway de Autenticação, Custódia e Consumo de API** (API Gateway for Authentication, Custody, and Usage-Based Billing).
- Proposta: FastAPI; PostgreSQL/TimescaleDB; Docker; integração LNbits REST e LNURL-Pay/WebSocket quando aplicável; autenticação por chaves públicas com assinatura/challenge-response; contabilização e cobrança por requisição, bytes ou tokens; rastreio auditável dos débitos; avaliação quantitativa de latência, vazão, custo, segurança e limites. Leia o documento integral antes de decidir arquitetura ou alegar cumprimento de item.
- A proposta descreve hipóteses e resultados *esperados*, não resultados medidos. A estimativa de redução de custos de 60% é hipótese, não fato. Não fabrique experimentos, métricas, citações, comprovação ou documentação institucional.
- Priorize um MVP honesto e testável com contratos bem definidos, banco transacional, cobrança idempotente e integração LNbits simulada nos testes. Isolar extensões não concluídas, documentar precisamente o que falta. Não reduzir silenciosamente o escopo da proposta.

## Contexto acadêmico e prazos informados (2026/2)
- Aluno: Rafael Bernardy de Souza, RA 2538865, Engenharia de Software UTFPR Dois Vizinhos.
- Na tela do SIACOES, os nomes Newton Carlos Will (orientador) e Erinaldo da Silva Pereira (coorientador) parecem ter sido **importados do TCC 1**; não há confirmação de aceite da orientação em TCC 2. O aluno pretende contatá-los na noite de 21/10/2026, assim como o responsável pelo TCC, Marlon (`pratcc-coens-dv@utfpr.edu.br`), para confirmar orientação, escopo, formalização e canal de entrega.
- Prazos repassados em e-mail de 2026/2: entrega TCC 2 até 09/11, defesa até 30/11, versão final/atas até 04/12. Confirmar com orientadores e SIACOES; não afirmar que a entrega já está regularizada.
- O usuário quer iniciar a implementação hoje à tarde, com o objetivo de conversar com professores à noite. Entregar síntese clara e demonstrável do que foi realmente feito e pendências para essa conversa.

## Regras de execução
- Trabalhe apenas em `/home/bots/tcc-ln-gateway` para este projeto, sem mexer em serviços de produção do monorepo nem usar carteiras Lightning reais. Verifique estado Git antes de mudanças; preserve alterações preexistentes.
- **Não faça commit, push, deploy ou publicação** sem pedido explícito do usuário, mesmo se instruções herdadas disserem diferente. Não envie e-mails nem faça submissões no SIACOES.
- Para testes de pagamento, use doubles/fakes ou ambiente de testes isolado sem dinheiro real; não use credenciais privadas, `.env` de produção nem chave LNbits real. Se eventual integração externa for necessária, obtenha autorização explícita antes de operações com saldo real.
- Faça validação defensiva, autenticação resistente a replay, segregação de chaves, idempotência e correção de concorrência no débito. Não apresente um protótipo como serviço de custódia seguro para produção: explicite limitações e modelo de ameaça.
- Escreva documentação em português e testes automatizados reproduzíveis. Inclua instruções para executar localmente; não exponha rede pública por padrão. Não altere `docs/TCC1-original.tex`; novos textos acadêmicos devem ser separados e somente conter resultados efetivamente obtidos.
