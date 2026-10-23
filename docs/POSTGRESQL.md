# PostgreSQL dedicado ao laboratório TCC2 (sem produção)

**Estado em 21/10/2026:** foi criada uma **instância PostgreSQL 16 isolada** sob `.postgres-data/` neste diretório, executada como `bots`, somente em `127.0.0.1:55432`; o servidor de sistema na porta 5432 e seus bancos **não foram modificados**. Foram criados os bancos UTF-8 `tcc_ln_gateway` e `tcc_ln_gateway_test`, com os respectivos papéis LOGIN sem privilégios administrativos. O banco de demo já tem migrações v1/v2/v3/v4; os testes no banco de teste validaram concorrência. Nada usa fundos reais. Esta instância é de desenvolvimento, **não é gerenciada por systemd**: após reinício do LXC, será necessário iniciá-la manualmente. Os dados são locais e ocupam espaço do disco deste LXC (que tinha ~4 GB disponíveis no provisionamento); não use para testes de carga nem LNbits de produção.

## Diretórios e controle

`.postgres-data/` contém o cluster e `.postgres-run/` contém o socket, logs e `pgpass`. Ambos têm modo 0700 e são ignorados pelo Git. `pgpass` tem modo 0600; **não publique, copie ao repositório ou envie o conteúdo dessa senha**. O socket local no diretório 0700 permite manutenção apenas ao usuário `bots`; a autenticação TCP em localhost usa SCRAM. Evite executar scripts não confiáveis como `bots` neste LXC, pois teriam acesso aos arquivos locais. Não há backup/alta disponibilidade configurados.

## Início e encerramento (dentro de `/home/bots/tcc-ln-gateway`)

```sh
/usr/lib/postgresql/16/bin/pg_ctl -D "$PWD/.postgres-data" -l "$PWD/.postgres-run/postgres.log" -o "-h 127.0.0.1 -p 55432 -k $PWD/.postgres-run -c listen_addresses=127.0.0.1 -c unix_socket_permissions=0700" start
pg_isready -h 127.0.0.1 -p 55432
# Para parar depois dos testes, com segurança:
/usr/lib/postgresql/16/bin/pg_ctl -D "$PWD/.postgres-data" stop -m fast
```

Não rode `initdb` novamente sobre o cluster criado. Se a porta 55432 estiver ocupada, investigue; não aponte para o PostgreSQL de sistema (5432) sem a configuração separada e autorizada.

## Acesso do projeto e testes

```sh
export PGPASSFILE="$PWD/.postgres-run/pgpass"
export GATEWAY_DATABASE_URL='postgresql+psycopg://tcc_ln_gateway@127.0.0.1:55432/tcc_ln_gateway'
export TCC_TEST_DATABASE_URL='postgresql+psycopg://tcc_ln_gateway_test@127.0.0.1:55432/tcc_ln_gateway_test'
PYTHONPATH=src python3 -m ln_gateway.migrate
PYTHONPATH=src python3 -m pytest -q
```

Migrações v1/v2/v3/v4 são transacionais sob advisory lock; o teste PG só aceita o banco `_test` e limpa as contas que cria. No primeiro teste real em 21/10/2026: **14 passed in 2.20s**, sem pulos. Sem as variáveis acima a aplicação continua usando SQLite para demo; `TCC_TEST_DATABASE_URL` ausente pula o teste PG. Não armazene credenciais em URLs, histórico do shell, arquivos rastreados ou e-mails. Para a demo PostgreSQL, execute os comandos do README com as variáveis `PGPASSFILE` e `GATEWAY_DATABASE_URL` definidas no terminal correspondente. Ao sair do shell, as variáveis deixam de valer; o cluster permanece ativo até parar ou reiniciar o LXC.

Esta instância separada foi escolhida porque `bots` não tem papel PostgreSQL nem sudo sobre o servidor de sistema existente; criar papéis ali sem permissão não era possível. **Não foi criado LXC novo nem alterado nenhum banco dos bots.**
