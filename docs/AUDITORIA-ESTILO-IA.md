# Auditoria editorial e técnica — 22/10/2026

## Escopo e cuidado metodológico

Foi feita revisão local de `docs/TCC2-relatorio.tex`, `docs/RESUMO-PROFESSORES.md`, `README.md` e docstrings/comentários em `src/ln_gateway/`, `lab/` e `tests/`. A fonte `docs/TCC1-original.tex` foi lida como proposta, **não editada**; seu SHA-256 segue `cd83a551fce1cb064fee3b3cdce6e9866595df3e2c46dd12f03caf99edef3457`. A auditoria busca legibilidade e precisão, não tornar texto indetectável nem atribuir autoria humana a uma redação assistida. Não foram enviados arquivos a detectores externos: além de não comprovarem autoria, a submissão de uma minuta acadêmica a terceiros exigiria decisão do autor.

## Como funcionam os detectores citados

| Ferramenta | O que informa publicamente | Limite de interpretação |
|---|---|---|
| **Turnitin AI Writing** | Extrai prosa elegível, divide em trechos sobrepostos e usa classificador treinado em texto humano e gerado para estimar a fração classificada como IA. O fornecedor diz que o modelo atual não usa regras explícitas de *perplexity* ou *burstiness*. | Não é teste de plágio nem prova de autoria. A documentação alerta para falso positivo, exige ao menos 300 palavras elegíveis e não mostra percentual específico abaixo de 20%. A FAQ atual lista inglês, espanhol, japonês, árabe, neerlandês, alemão e turco; **português não é suportado** para esse indicador, portanto não se deve atribuir pontuação Turnitin à minuta. [FAQ](https://www.turnitin.com/products/features/ai-writing-detection/faq), [atualizações](https://guides.turnitin.com/hc/en-us/articles/28294949544717-AI-writing-detection-model). |
| **GPTZero** | Classificação estatística com modelos de aprendizado profundo e indicações por documento/trecho. As primeiras versões usavam perplexidade e variação do texto; o fornecedor esclarece que desde 2023 a arquitetura não depende diretamente dessas duas métricas. | O próprio serviço recomenda não usar a pontuação como veredito. Textos curtos, mistos, técnicos e fora do inglês reduzem a confiança. [Explicação](https://gptzero.me/news/how-ai-detectors-work/), [limites](https://gptzero.me/how-it-works). |
| **Copyleaks** | Modelo proprietário para classificação de texto humano/gerado, com níveis configuráveis de sensibilidade, testes em amostras separadas do treino e suporte anunciado a português. Oferece análise de código e detecção de similaridade como recursos distintos. | Taxas de acerto divulgadas são relativas aos conjuntos avaliados pelo fornecedor; não se transportam automaticamente para LaTeX acadêmico ou código deste projeto. Similaridade de código não identifica autoria por si só. [Metodologia](https://copyleaks.com/ai-detector/testing-methodology), [produto](https://copyleaks.com/ai-detector). |
| **ZeroGPT (`zerogpt.com`)** | Descreve o DeepAnalyse como avaliação em estágios (documento, frases, padrões locais) treinada em coleções humanas e sintéticas; destaca frases sinalizadas e porcentagem estimada. | Implementação e pesos são proprietários. As taxas divulgadas pelo próprio serviço não constituem validação independente nesta amostra. Não confundir o domínio com serviços homônimos. [Descrição](https://www.zerogpt.com/deep-analyse). |

**Perplexidade** é a surpresa média de um modelo de linguagem ao prever os tokens de um texto; depende do modelo, idioma e domínio. **Burstiness** é um nome usado para variações locais de probabilidade, estrutura e/ou extensão de frases, conforme a definição de cada estudo. Nenhuma das duas mede intenção ou autoria. Prosa técnica repetitiva pode ser humana; texto gerado pode ser editado. Em código, estruturas padronizadas por frameworks e testes tornam sinais estilísticos ainda menos informativos. Não há limiar universal ou nota calculada aqui.

## Diagnóstico no repositório e alterações

O relatório anterior usava qualificadores absolutos sem base suficiente: “imunidade a ataques de repetição”, “garantindo ... persistam fisicamente”, “impede qualquer possibilidade” e “plena viabilidade prática”. A redação também apresentava a outbox de laboratório e o gateway HTTP como se fossem uma integração custodial contínua. A lista extensa de propriedades sob títulos enfáticos e conclusões sem dados quantitativos dificultava separar ensaio pontual de hipótese. Erros de precisão importam mais que uma possível classificação por detector.

| Antes (trecho ou padrão encontrado) | Depois / motivo |
|---|---|
| Resumo: “assegurando imunidade a ataques de repetição” e “a integridade financeira é garantida” | O resumo agora identifica nonce de uso único, débito idempotente e os limites de teste. Proteção sob condições verificadas não é imunidade universal. |
| “A execução demonstrou a plena viabilidade prática da arquitetura proposta” após uma única fatura | O ensaio Blink é descrito como **uma execução pontual** de 10 sats, sem extrapolar para custódia, desempenho ou custo. |
| `issuing → uncertain → recorded` apresentado como fluxo padrão | Os estados são alternativas após `issuing`; uma tentativa `uncertain` não vira `recorded` automaticamente. A falta de `payment_hash` exige análise humana. |
| README inicial reunia muitos links num único período e não diferenciava API fake de laboratório | README abre com o limite operacional, dá comandos reproduzíveis e aponta os ensaios separados. |
| Resumo aos professores prometia “69 testes no PostgreSQL”, “zero perda de saldo” e demonstrava consulta a registro presumido no banco | Informa que os 69 casos misturam SQLite, offline e PostgreSQL; oferece demo sem dinheiro e não depende de registros persistentes anteriores. |
| Docstrings genéricas (“Garante ...”, “Exige ...”, “Nunca ...”) em `app.py`, `issuance.py`, `fixture_outbox.py` e testes | Nos pontos editados, descrevem invariantes concretas: nonce por operação, proibição de reenvio após timeout, isolamento do fake e rollback. Mantiveram-se comentários de segurança que explicam decisões reais. |

A revisão evitou trocar palavras por sinônimos sem ganho de significado ou inserir variação artificial de períodos para “burlar” classificadores. Em escrita técnica brasileira, termos como *chave composta*, *lançamento*, *reconciliação*, *bloqueio de linha*, *estado incerto* e *cota de ensaio* ajudam quando descrevem exatamente o código. Transições repetidas (“além disso”, “em suma”, “cabe ressaltar”), parágrafos uniformes, enumerações em toda seção, jargão ornamental e docstrings que só repetem o nome da função são sinais para **revisão humana**, não provas de geração por LLM. Não convém inventar coloquialismos em uma monografia.

## Pontos ainda pendentes da minuta

- O template `normas-utf-tex` e as entradas bibliográficas não estão neste diretório; `pdflatex` não está disponível aqui. A minuta não foi compilada nem certificada como conforme às normas da UTFPR. Fontes e citações devem ser verificadas com orientação antes da entrega.
- Nomes de orientação vieram do TCC 1; não há confirmação formal para TCC 2. Os prazos do semestre requerem conferência no SIACOES.
- A proposta TCC 1 prevê LNURL-Pay/WebSocket, TimescaleDB, Docker, tarifação por bytes/tokens, latência, vazão e análise comparativa de custos. Permanecem pendentes; a hipótese de 60% **não é um resultado medido**.
- O sistema não comprova custódia segura: não há transação distribuída entre gateway e outbox de laboratório, há casos `uncertain` sem recuperação automática, e testes não substituem revisão de segurança em implantação real.

## Validação local

Com `PGPASSFILE="$PWD/.postgres-run/pgpass"`, `TCC_TEST_DATABASE_URL='postgresql+psycopg://tcc_ln_gateway_test@127.0.0.1:55432/tcc_ln_gateway_test'` e `PYTHONPATH=src`, executou-se `.venv/bin/python -m pytest -q` em 22/10/2026: **69 passed, 0 skipped**. Sem as variáveis PostgreSQL, houve **59 passed, 10 skipped**. O `pytest` genérico do ambiente global não tinha `bolt11`/pacote instalados; usar a `.venv` do projeto. Nenhuma chamada Lightning real foi feita nesta auditoria.
