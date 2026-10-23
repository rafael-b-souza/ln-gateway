"""Orquestra LNbits v1.6.2 isolado em namespace com proxy e TLS simulado.

Nenhuma rota de egress na filha; root guarda FD da fixture, UID 999 só UDS.
Não modifica launcher, SQLite existente, firewall ou segredo real.
"""

import http.server
import os
from pathlib import Path
import ssl
import socket
import subprocess
import sys
import threading
import time

# -I does not include script directory in sys.path; fixture code is root-owned.
sys.path.insert(0, "/opt/tcc-ln-lab")
from alby_guarded_proxy import GuardedServer
from fixture_outbox import initialize

BASE = Path("/tmp/tcc-ln-fixture")
SCRIPT = "/opt/tcc-ln-lab/fixture_lnbits_entry.py"


class FakeTLS(http.server.BaseHTTPRequestHandler):
    """Responde saldo fictício e registra apenas método/path, nunca Bearer."""

    def do_GET(self):
        """GET da wallet em processo separado prova encaminhamento autorizado."""
        self.server.seen.append(("GET", self.path, self.headers.get("Authorization") ==
                                 "Bearer synthetic-lnbits-uds-fixture"))
        content = b'{"unit":"sat","balance":0}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        """Recebe somente fatura sintética até cota; não há invoice de rede."""
        import json
        from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(("POST", self.path, self.headers.get("Authorization") ==
                                 "Bearer synthetic-lnbits-uds-fixture"))
        assert self.path == "/invoices" and payload == {"amount": 10, "memo": "tcc:fixture"}
        assert self.server.issued < 1
        self.server.issued += 1
        if self.server.drop_response:
            # Crash/timeout depois da criação lógica no fake, antes do retorno.
            # Não existe invoice de rede nem preimage transferida.
            import time
            time.sleep(15)
            return
        # A fatura é assinada localmente com fixture determinística e hash
        # sem preimage revelado: não há nó Lightning neste namespace.
        import time
        invoice = Bolt11(currency="bcrt", amount_msat=MilliSatoshi(10000), date=int(time.time()), tags=Tags())
        invoice.tags.add(TagChar.payment_hash, "a" * 64)
        invoice.tags.add(TagChar.payment_secret, "b" * 64)
        invoice.tags.add(TagChar.description, "fixture-no-node")
        invoice.tags.add(TagChar.expire_time, 3600)
        data = json.dumps({"payment_hash": "a" * 64,
                           "payment_request": encode(invoice, private_key="1" * 64)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        """Não gera logs de requisição/headers."""


def require_lab():
    """Confere versão e backend sem inspecionar conteúdo de segredo externo."""
    assert os.geteuid() == 0 and (not BASE.exists() or "--resume" in sys.argv)
    if BASE.exists():
        assert "--resume" in sys.argv and (BASE / "private/outbox.sqlite3").is_file()
        assert not (BASE / "proxy.sock").exists()
    assert "LNBITS_BACKEND_WALLET_CLASS=VoidWallet" in Path("/opt/tcc-ln-lab/run.sh").read_text()
    rev = subprocess.check_output(["git", "-C", "/opt/tcc-ln-lab/source", "rev-parse", "HEAD"],
                                  timeout=5).decode().strip()
    assert rev == "9349da153c223b008f7d80453b53a54e09c8ed3d"


def run():
    """Trap Python fecha socket/threads e remove somente fixture criada nesta execução."""
    require_lab()
    resume = "--resume" in sys.argv
    if not resume:
        BASE.mkdir(mode=0o711)
    private = BASE / "private"
    if not resume:
        private.mkdir(mode=0o700)
    try:
        key, cert = private / "key.pem", private / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-subj", "/CN=api.getalby.com", "-addext", "subjectAltName=DNS:api.getalby.com",
                        "-keyout", str(key), "-out", str(cert), "-days", "1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        tls = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTLS)
        tls.seen = []
        tls.issued = 0
        tls.drop_response = "--timeout" in sys.argv
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
        upstream = threading.Thread(target=tls.serve_forever, daemon=True)
        upstream.start()
        try:
            token = private / "synthetic"
            if not resume:
                token.write_bytes(b"synthetic-lnbits-uds-fixture")
                token.chmod(0o600)
            fd = os.open(token, os.O_RDONLY | os.O_NOFOLLOW)
            ledger_path = private / "outbox.sqlite3"
            if not resume:
                initialize(ledger_path)
            sock = BASE / "proxy.sock"
            proxy = GuardedServer(sock, fd, cert, tls.server_port, 999, ledger_path,
                                  invoices_enabled=True,
                                  fail_before_post="--fail-before" in sys.argv)
            # Socket UDS deve pertencer ao grupo tcc_ln_lab (990) com modo 0660
            os.chown(sock, 0, 990)
            os.chmod(sock, 0o660)
            worker = threading.Thread(target=proxy.serve_forever, daemon=True)
            worker.start()
            try:
                data = BASE / "data"
                if not resume:
                    data.mkdir(mode=0o700)
                    os.chown(data, 999, 990)
                env = {"HOME": "/nonexistent", "PATH": "/opt/tcc-ln-lab/venv/bin:/usr/bin:/bin",
                       "PYTHONDONTWRITEBYTECODE": "1"}
                script = ["unshare", "--net", "--", "sh", "-c",
                          "ip link set lo up; cd /opt/tcc-ln-lab/source; exec setpriv --reuid=tcc_ln_lab --regid=tcc_ln_lab "
                          "--init-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all "
                          "env -i HOME=/tmp/tcc-ln-fixture/data PATH=/opt/tcc-ln-lab/venv/bin:/usr/bin:/bin "
                          "LNBITS_DATA_FOLDER=/tmp/tcc-ln-fixture/data LNBITS_EXTENSIONS_PATH=/tmp/tcc-ln-fixture/data "
                          "LNBITS_BACKEND_WALLET_CLASS=VoidWallet LNBITS_ALLOWED_FUNDING_SOURCES=VoidWallet "
                          "LNBITS_EXTENSIONS_DEACTIVATE_ALL=true LNBITS_ADMIN_UI=true HOST=127.0.0.1 "
                          "PYTHONDONTWRITEBYTECODE=1 /opt/tcc-ln-lab/venv/bin/python -I " + SCRIPT]
                # Daemon uvicorn real roda como UID do lab dentro de netns sem egress;
                # script anterior permanece apenas como probe histórico de lifespan.
                script[-1] = script[-1].replace(SCRIPT, "/opt/tcc-ln-lab/fixture_lnbits_daemon.py")
                process = subprocess.Popen(script, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                try:
                    try:
                        proc_env = Path(f"/proc/{process.pid}/environ").read_bytes()
                        proc_args = Path(f"/proc/{process.pid}/cmdline").read_bytes()
                    except FileNotFoundError:
                        proc_env = proc_args = b""  # saiu cedo; returncode será validado.
                    assert b"synthetic-lnbits-uds-fixture" not in proc_env + proc_args
                    # Probe roda na MESMA namespace do daemon, sem expor porta no host.
                    # Aguarda existência de /proc/ns/net e testa sockets diretos em
                    # script sob UID sem credencial antes de qualquer POST fictício.
                    for _ in range(100):
                        if os.stat(f"/proc/{process.pid}/ns/net").st_ino != os.stat("/proc/self/ns/net").st_ino:
                            break
                        time.sleep(0.05)
                    else:
                        raise AssertionError("daemon não entrou na netns")
                    client = subprocess.run(["nsenter", "--net=" + f"/proc/{process.pid}/ns/net",
                                             "--", "setpriv", "--reuid=tcc_ln_lab", "--regid=tcc_ln_lab",
                                             "--init-groups", "/opt/tcc-ln-lab/venv/bin/python", "-I",
                                             "/opt/tcc-ln-lab/fixture_lnbits_api_probe.py"] +
                                            (["--resume"] if resume else
                                             ["--fail-before"] if "--fail-before" in sys.argv else
                                             ["--timeout"] if tls.drop_response else []),
                                            env={"PATH": "/opt/tcc-ln-lab/venv/bin:/usr/bin:/bin",
                                                 "HOME": "/tmp/tcc-ln-fixture/data",
                                                 "PYTHONDONTWRITEBYTECODE": "1"},
                                            capture_output=True, timeout=130)
                    if client.returncode:
                        process.kill()
                        server_out, server_err = process.communicate(timeout=8)
                        raise AssertionError("probe offline falhou: " + client.stderr[-320:].decode("ascii", "replace") +
                                             "; daemon: " + (server_out + server_err)[-440:].decode("ascii", "replace"))
                    if client.stdout:
                        print("PROBE STDOUT:", client.stdout.decode().strip())
                    if client.stderr:
                        print("PROBE STDERR:", client.stderr.decode().strip())
                    # Checa persistência apenas no diretório descartável, ainda
                    # antes do rollback; nada é lido do SQLite real do lab.
                    for file in data.rglob("*"):
                        if file.is_file() and file.stat().st_size < 10_000_000:
                            assert b"synthetic-lnbits-uds-fixture" not in file.read_bytes()
                    if "--kill-restart" in sys.argv:
                        # SIGKILL simula falha sem shutdown gracioso; reabre apenas
                        # DB efêmero e preserva cota do proxy neste processo.
                        process.kill()
                        process.communicate(timeout=10)
                        assert tls.issued == 1
                        process = subprocess.Popen(script, env=env, stdout=subprocess.PIPE,
                                                   stderr=subprocess.PIPE)
                        for _ in range(100):
                            if os.stat(f"/proc/{process.pid}/ns/net").st_ino != os.stat("/proc/self/ns/net").st_ino:
                                break
                            time.sleep(0.05)
                        else:
                            raise AssertionError("reinício fora da netns")
                        restart = subprocess.run(["nsenter", "--net=" + f"/proc/{process.pid}/ns/net", "--",
                                                  "setpriv", "--reuid=tcc_ln_lab", "--regid=tcc_ln_lab",
                                                  "--init-groups", "/opt/tcc-ln-lab/venv/bin/python", "-I",
                                                  "/opt/tcc-ln-lab/fixture_lnbits_api_probe.py", "--restart"],
                                                 env={"PATH": "/opt/tcc-ln-lab/venv/bin:/usr/bin:/bin",
                                                      "HOME": "/tmp/tcc-ln-fixture/data",
                                                      "PYTHONDONTWRITEBYTECODE": "1"},
                                                 capture_output=True, timeout=45)
                        assert restart.returncode == 0, restart.stderr[-240:]
                        assert tls.issued == 1
                    if "--proxy-restart" in sys.argv:
                        # Reinício do proxy sobre a mesma ledger; valida bloqueio de replay
                        proxy.shutdown()
                        proxy.server_close()
                        worker.join(5)
                        sock.unlink(missing_ok=True)
                        fd2 = os.open(token, os.O_RDONLY | os.O_NOFOLLOW)
                        proxy = GuardedServer(sock, fd2, cert, tls.server_port, 999, ledger_path,
                                              invoices_enabled=True)
                        os.chown(sock, 0, 990)
                        os.chmod(sock, 0o660)
                        worker = threading.Thread(target=proxy.serve_forever, daemon=True)
                        worker.start()
                        replay = subprocess.run(["nsenter", "--net=" + f"/proc/{process.pid}/ns/net", "--",
                                                 "setpriv", "--reuid=tcc_ln_lab", "--regid=tcc_ln_lab",
                                                 "--init-groups", "/opt/tcc-ln-lab/venv/bin/python", "-I",
                                                 "/opt/tcc-ln-lab/fixture_lnbits_api_probe.py", "--replay"],
                                                env={"PATH": "/opt/tcc-ln-lab/venv/bin:/usr/bin:/bin",
                                                     "HOME": "/tmp/tcc-ln-fixture/data",
                                                     "PYTHONDONTWRITEBYTECODE": "1"},
                                                capture_output=True, timeout=45)
                        assert replay.returncode == 0, replay.stderr[-240:]
                        assert tls.issued == 1
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=20)
                except BaseException:
                    process.kill()
                    process.communicate()
                    raise
                logs = stdout + stderr
                assert b"synthetic-lnbits-uds-fixture" not in Path("/proc/self/environ").read_bytes()
                assert b"synthetic-lnbits-uds-fixture" not in " ".join(script).encode()
                assert b"synthetic-lnbits-uds-fixture" not in logs and b"Bearer" not in logs
                if process.returncode not in (0, -15):
                    raise AssertionError("daemon fixture falhou; exit=" + str(process.returncode) +
                                         "; final=" + logs[-300:].decode("ascii", "replace"))
                from fixture_outbox import Outbox
                rows = Outbox(ledger_path).entries()
                assert len(rows) == 1 and rows[0][1] == 10
                if resume:
                    assert tls.issued == 0 and rows[0][2] in ("recorded", "uncertain")
                elif "--fail-before" in sys.argv:
                    assert tls.issued == 0 and rows[0][2] == "uncertain"
                else:
                    assert tls.issued == 1 and rows[0][2] == ("uncertain" if tls.drop_response else "recorded")
                assert all(call[2] is True for call in tls.seen), tls.seen
                assert not any(call[1] == "/payments/bolt11" for call in tls.seen)
                print("LAB: daemon LNbits + outbox durável; " +
                      ("retomada sem POST" if resume else
                       "falha pré-POST" if "--fail-before" in sys.argv else
                       "tentativa incerta" if tls.drop_response else
                       "proxy-restart verificado" if "--proxy-restart" in sys.argv else
                       "1 pending"))
            finally:
                proxy.shutdown()
                proxy.server_close()
                worker.join(5)
                sock.unlink(missing_ok=True)
        finally:
            tls.shutdown()
            tls.server_close()
            upstream.join(5)
    finally:
        # Evidência de outbox fica disponível a auditoria local. Nunca apagar
        # a ledger para "resetar" quota: operador deve arquivar/inspecionar.
        pass


if __name__ == "__main__":
    run()
