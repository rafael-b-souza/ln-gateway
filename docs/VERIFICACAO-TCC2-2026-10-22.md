# Verificação da minuta TCC 2 — 22/10/2026

**Escopo:** conferência de fontes primárias, relação das afirmações técnicas com código e evidências, compilação e requisitos de entrega. Isto **não** é homologação pela UTFPR ou parecer da orientação.

## Fontes bibliográficas conferidas

As quatro entradas na seção *Referências* de `TCC2-relatorio.tex` foram abertas em suas origens, e cada citação foi associada a uma afirmação específica:

| Fonte verificada | Afirmação apoiada | Limite |
| --- | --- | --- |
| Poon e Dryja, *The Bitcoin Lightning Network: Scalable Off-Chain Instant Payments*, versão preliminar 0.5.9.2, 14 jan. 2016, https://lightning.network/lightning-network-paper.pdf | Descrição original da Lightning Network | Não prova custo ou desempenho do protótipo |
| Josefsson e Liusvaara, RFC 8032, https://www.rfc-editor.org/rfc/rfc8032 | Definição de Ed25519/EdDSA | Não atesta a segurança desta aplicação |
| Lightning Network, BOLT 11, https://github.com/lightning/bolts/blob/master/11-payment-encoding.md | Campos, valor, assinatura e expiração de faturas Lightning | Não atesta liquidação: essa informação vem de consulta ao provedor |
| Fiatjaf e Akumaigorodski, LUD-06: payRequest base spec, https://github.com/lnurl/luds/blob/luds/06.md | Contrato LNURL-Pay, especificação de metadados e tag `description_hash` em BOLT 11 | Validação de contrato de laboratório; não é serviço público exposto |

A bibliografia é **manual** porque a classe `normas-utf-tex` v2.0 / `abntcite` antigo exigem `\abntrefinfo` para gerar a formatação autor-data correta; a saída foi conferida na compilação do PDF. As especificações BOLT 11 e LUD-06 não possuem registro de DOI ou ano formal de publicação nas fontes primárias dos repositórios, tendo sido referenciadas pelos seus respectivos repositórios oficiais com a data de consulta. A formatação final deve ser revisada na migração para o modelo institucional da UTFPR.

## Evidências internas verificadas

- `src/ln_gateway/app.py` contém nonce de 60 segundos, mensagem canônica, verificação Ed25519, consumo condicional do desafio, lock de conta, rota de débito idempotente com suporte a modalidades `request`, `bytes` e `tokens`, registro atômico de métricas de uso em `usage_events` e limitação do emissor HTTP a `FakeInvoices`; `src/ln_gateway/storage.py` define as chaves únicas e tabelas de lançamentos. O gateway de demonstração **não** emite faturas reais automaticamente.
- `src/ln_gateway/billing.py` implementa cotação determinística com teto seguro (`quote`), usando o codificador BPE `cl100k_base` (`tiktoken`) para tokens e contagem exata de bytes para payloads UTF-8.
- `src/ln_gateway/lnurl_pay.py` valida o contrato LUD-06 (`OfflineLNURLPay`), confere conformidade de metadados, bloqueia URLs de callback com risco de SSRF e exige vínculo do hash de metadados na tag `h` (`description_hash`) da fatura BOLT 11 emitida.
- `src/ln_gateway/migrations/005_*.sql` e `scripts/enable_timescale_usage.py` preparam a tabela `usage_events` com chave primária temporal `(account_id, request_id, occurred_at)` e testam a habilitação de hipertabela TimescaleDB exclusivamente na instância isolada de testes.
- `docs/ENSAIO-BLINK-LIVE.md` e `scripts/reconcile_and_consume_live.py` registram o pagamento pontual de 10 satoshis, o crédito no banco dedicado e o débito de 1 satoshi; não confundir esse script de laboratório com integração em produção.
- A suíte foi reexecutada nesta verificação **somente** no banco dedicado `127.0.0.1:55432` (`tcc_ln_gateway_test`): **76 passed in 51.32s**. Nenhum pagamento externo foi iniciado.
- Não há ensaio quantitativo de latência, vazão ou custo; o valor de 60% no TCC 1 segue como hipótese, não resultado.

## Modelo e entrega institucional

A página oficial [Orientações para entrega — UTFPR](https://www.utfpr.edu.br/bibliotecas/trabalhos-academicos/orientacoes-para-entrega), consultada em 22/10/2026, exige para o trabalho acadêmico: **capa, folha de rosto, folha de aprovação sem assinaturas, resumo, abstract, sumário, corpo e referências**. Pede título em inglês e licença Creative Commons na folha de rosto, consistência de metadados com os sistemas institucionais e arquivo final em **PDF/A**. Disponibiliza modelo LaTeX **versão 11.0 (abr. 2025)** para uso offline e online. O arquivo offline foi baixado apenas em `/tmp/opencode/tcc-refs/utfpr-modelo.zip` para inspeção; seu pacote identifica um modelo UTFPR-CP v2.6 e recomenda TeX Live 2021. Não se deve presumir que substitua sem adaptação o template exigido pelo curso de Dois Vizinhos.

A minuta atual usa a classe externa `normas-utf-tex` v2.0 (2011), já usada no TCC 1. **Não está conforme para depósito final:** falta a folha de aprovação (banca/data ainda não confirmadas); a folha de rosto da classe antiga não imprime o título inglês e a licença CC; a licença depende de escolha/anuência do aluno e orientação; o PDF gerado com Tectonic é PDF comum, **não PDF/A validado**. Foi incluído `abstract` e uma lista de referências, mas a adequação de formatação ABNT e template continua pendente. A citação automática “11 f.” da classe antiga não equivale necessariamente à contagem institucional de folhas.

**Próximos passos com a orientação:** confirmar o aceite formal da orientação, banca e prazos, além da licença aplicável, template homologado pelo curso e procedimentos de entrega no SIACOES e biblioteca. Em seguida, migrar a minuta para o modelo acordado, incluir a folha de aprovação preliminar e a licença institucional e validar o formato PDF/A antes de qualquer submissão formal.
