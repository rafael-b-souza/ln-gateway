#!/bin/sh
# Preparação deliberadamente não ativável: o token só pode ser usado após nova autorização,
# egress verificado e revisão do risco de variáveis de ambiente da tag v1.6.2.
set -eu
FILE=/opt/tcc-ln-lab/secrets/alby-access-token
if [ "$(id -u)" -ne 0 ]; then
    echo 'BLOQUEADO: somente root pode verificar metadados do caminho reservado.' >&2
    exit 78
fi
if [ -e "$FILE" ]; then
    # stat consulta metadados APENAS; nunca abre nem revela o conteúdo.
    [ ! -L "$FILE" ] || { echo 'BLOQUEADO: link simbólico.' >&2; exit 78; }
    [ -f "$FILE" ] || { echo 'BLOQUEADO: não é arquivo regular.' >&2; exit 78; }
    [ "$(stat -c '%u:%a' /opt/tcc-ln-lab/secrets)" = '0:700' ] || {
        echo 'BLOQUEADO: diretório de segredo não é root 0700.' >&2; exit 78;
    }
    [ "$(stat -c '%u:%a' "$FILE")" = '0:600' ] || {
        echo 'BLOQUEADO: arquivo de segredo não é root 0600.' >&2; exit 78;
    }
fi
# Não há rota nem proxy de Alby provados; este script nunca executa LNbits com Alby.
echo 'BLOQUEADO: staging apenas; backend permanece VoidWallet. Credencial não lida.' >&2
exit 78
