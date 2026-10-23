"""Smoke test do proxy Alby com TLS local em ambiente de teste."""

import http.server
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading

from alby_guarded_proxy import GuardedServer


class Fake(http.server.BaseHTTPRequestHandler):
    """Servidor TLS de teste para responder payloads simulados."""

    def do_GET(self):
        self.server.calls.append(self.path)
        content = b'{"unit":"sat"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


def send(path, request):
    """Envia requisição bruta sobre socket Unix."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        result = bytearray()
        while chunk := client.recv(4096):
            result.extend(chunk)
        return bytes(result)


def check():
    """Prova deny e rollback por descarte da netns; sem caminhos reais hardcoded."""
    assert os.geteuid() == 0 and not Path("/proc/net/route").read_text().count("00000000\t00000000")
    for address in ("1.1.1.1", "10.10.10.102", "169.254.169.254"):
        try:
            socket.create_connection((address, 443), timeout=0.3)
        except OSError:
            pass
        else:
            raise AssertionError("egress direto não bloqueado")
    with tempfile.TemporaryDirectory(prefix="tcc-guard-smoke-") as tmp:
        directory = Path(tmp)
        key, cert = directory / "key.pem", directory / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-subj", "/CN=api.getalby.com", "-addext", "subjectAltName=DNS:api.getalby.com",
                        "-keyout", str(key), "-out", str(cert), "-days", "1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        tls = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        tls.calls = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        tls.socket = context.wrap_socket(tls.socket, server_side=True)
        thread = threading.Thread(target=tls.serve_forever, daemon=True)
        thread.start()
        secret = directory / "synthetic"
        secret.write_bytes(b"synthetic-smoke-never-real-token")
        secret.chmod(0o600)
        fd = os.open(secret, os.O_RDONLY | os.O_NOFOLLOW)
        directory.chmod(0o711)  # somente atravessar, sem listar fixture/token.
        path = directory / "local.sock"
        proxy = GuardedServer(path, fd, cert, tls.server_port, os.getuid())
        worker = threading.Thread(target=proxy.serve_forever, daemon=True)
        worker.start()
        try:
            # Mesmo UID diferente com caminho conhecido não acessa socket nem arquivo.
            def unprivileged():
                """Executa probe sob UID/GID de ninguém sem carregar credencial."""
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)
            probe = subprocess.run(["/usr/bin/python3", "-I", "-c",
                                    "import socket,sys,pathlib; "
                                    "s=socket.socket(socket.AF_UNIX); "
                                    "s.connect(sys.argv[1])", str(path)],
                                   preexec_fn=unprivileged, capture_output=True, timeout=5)
            assert probe.returncode != 0 and b"synthetic-smoke-never-real-token" not in probe.stderr
            readable = subprocess.run(["/usr/bin/python3", "-I", "-c",
                                       "import pathlib,sys;pathlib.Path(sys.argv[1]).read_bytes()",
                                       str(secret)], preexec_fn=unprivileged,
                                      capture_output=True, timeout=5)
            assert readable.returncode != 0 and b"synthetic-smoke-never-real-token" not in readable.stderr
            def req(method, target):
                """Forma canônica para verificar deny por rota em bytes HTTP."""
                return f"{method} {target} HTTP/1.1\r\nHost: api.getalby.com\r\n\r\n".encode()
            assert b"200 OK" in send(path, req("GET", "/balance"))
            assert b"403 Forbidden" in send(path, req("POST", "/payments/bolt11"))
            assert b"403 Forbidden" in send(path, req("GET", "/balance?redirect=1"))
            assert b"403 Forbidden" in send(path, req("POST", "/invoices"))
            assert tls.calls == ["/balance"]
            print("LAB offline: FD sintético + TLS fixture/UDS; payments negado; egress direto negado")
        finally:
            proxy.shutdown()
            proxy.server_close()
            tls.shutdown()
            tls.server_close()
            worker.join(5)
            thread.join(5)
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    check()
