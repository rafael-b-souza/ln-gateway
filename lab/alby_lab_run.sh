#!/bin/bash
# Execução temporária de diagnóstico read-only. Nunca roda LNbits ou POST.
set -Eeuo pipefail
NS=tcc-alby-readonly
BASE=/opt/tcc-ln-lab
cleanup() {
    set +e
    if [[ -n "${PROXY_PID:-}" ]]; then kill "$PROXY_PID" 2>/dev/null; wait "$PROXY_PID" 2>/dev/null; fi
    nft delete table inet tcc_alby_host 2>/dev/null
    ip netns del "$NS" 2>/dev/null
    ip link del tcc-a-host 2>/dev/null
    echo 'rollback: namespace, veth, nft host e proxy removidos; VoidWallet inalterado'
}
trap cleanup EXIT INT TERM
[[ $EUID == 0 ]] || exit 78
[[ ! -e /var/run/netns/$NS && ! -e /sys/class/net/tcc-a-host ]] || exit 78
[[ $(grep -o 'LNBITS_BACKEND_WALLET_CLASS=[A-Za-z]*' "$BASE/run.sh") == LNBITS_BACKEND_WALLET_CLASS=VoidWallet ]] || exit 78
if pgrep -u tcc_ln_lab -f lnbits >/dev/null; then exit 78; fi
ip netns add "$NS"
ip link add tcc-a-host type veth peer name tcc-a-peer
ip link set tcc-a-peer netns "$NS"
ip addr add 172.31.252.1/30 dev tcc-a-host
ip link set tcc-a-host up
ip netns exec "$NS" ip addr add 172.31.252.2/30 dev tcc-a-peer
ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" ip link set tcc-a-peer up
# Somente CONNECT TCP ao proxy; kernel bloqueia saída direta, IPv4 e IPv6.
ip netns exec "$NS" nft -f - <<'RULES'
table inet tcc_alby {
 chain output { type filter hook output priority -10; policy drop;
   ip daddr 172.31.252.1 tcp dport 18765 accept
 }
 chain input { type filter hook input priority -10; policy drop;
   iifname "tcc-a-peer" ip saddr 172.31.252.1 tcp sport 18765 ct state established accept
 }
}
RULES
# Interface host aceita apenas do peer -> proxy; não muda política global nem SSH.
nft -f - <<'RULES'
table inet tcc_alby_host {
 chain input { type filter hook input priority -10; policy accept;
   iifname "tcc-a-host" ip saddr 172.31.252.2 ip daddr 172.31.252.1 tcp dport 18765 accept
   iifname "tcc-a-host" drop
 }
 chain forward { type filter hook forward priority -10; policy accept; iifname "tcc-a-host" drop; }
}
RULES
# IMPORTANTE: a tabela host é removida no trap; não deve ser mantida em operação.
# Processo proxy sem credencial; hostname/SNI fixos no código; saída de debug ausente.
/usr/bin/python3 -u "$BASE/alby_connect_proxy.py" >/dev/null 2>&1 &
PROXY_PID=$!
sleep 1
kill -0 "$PROXY_PID"
# Testes negativos antes de leitura de token. Cliente não possui rota default.
ip netns exec "$NS" ip route
ip netns exec "$NS" nft list table inet tcc_alby | head -n 15
# Ambiente mínimo impede proxy herdado e logging de HTTP.
timeout 45 ip netns exec "$NS" env -i PATH=/usr/bin:/bin HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 \
  python3 -I "$BASE/alby_readonly_client.py"
