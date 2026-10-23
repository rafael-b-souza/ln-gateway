"""Testes de integração do proxy UDS com TLS local simulado."""

import http.server
import importlib.util
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

LAB = Path(__file__).parents[1] / "lab"
sys.path.insert(0, str(LAB))
from alby_guarded_proxy import GuardedServer
from fixture_outbox import initialize

TOKEN = b"synthetic-proxy-only-token"


class FakeTLS(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/balance" and self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "https://evil.invalid/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = b'{"unit":"sat"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode
        invoice = Bolt11(currency="bcrt", amount_msat=MilliSatoshi(10000),
                         date=int(time.time()), tags=Tags())
        invoice.tags.add(TagChar.payment_hash, "a" * 64)
        invoice.tags.add(TagChar.payment_secret, "b" * 64)
        invoice.tags.add(TagChar.description, "test-only")
        invoice.tags.add(TagChar.expire_time, 3600)
        content = __import__("json").dumps({"payment_hash": "a" * 64,
                  "payment_request": encode(invoice, private_key="1" * 64)}).encode()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass


def cert(tmp_path, hostname="api.getalby.com"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    cert_ = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
             .public_key(key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
             .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
             .sign(key, hashes.SHA256()))
    ca = tmp_path / "ca.pem"
    keyfile = tmp_path / "key.pem"
    ca.write_bytes(cert_.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    return ca, keyfile


@pytest.fixture
def rig(tmp_path):
    ca, key = cert(tmp_path)
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTLS)
    upstream.seen = []
    upstream.redirect = False
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(ca), str(key))
    upstream.socket = ctx.wrap_socket(upstream.socket, server_side=True)
    tls_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    tls_thread.start()
    secret = tmp_path / "fixture-token"
    secret.write_bytes(TOKEN)
    secret.chmod(0o600)
    fd = os.open(secret, os.O_RDONLY | os.O_NOFOLLOW)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger = private / "outbox.sqlite3"
    initialize(ledger)
    sock = tmp_path / "proxy.sock"
    server = GuardedServer(sock, fd, ca, upstream.server_port, os.getuid(), ledger)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, upstream, sock, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(5)
        tls_thread.join(5)
        assert not thread.is_alive() and not tls_thread.is_alive()
        sock.unlink(missing_ok=True)


def send(sock, data):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(sock))
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        result = b""
        while True:
            try:
                part = client.recv(4096)
            except ConnectionResetError:
                break  # rejeição por framing com bytes extras pode fechar com RST.
            if not part:
                break
            result += part
        return result


def request(method, path, body=b"", headers=b""):
    """HTTP canônico do ensaio; payload duplicado é enviado por teste separado."""
    h = b"Host: api.getalby.com\r\n"
    if body:
        h += b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n"
    return method.encode() + b" " + path.encode() + b" HTTP/1.1\r\n" + h + headers + b"\r\n" + body


def test_proxy_permits_only_exact_routes_and_opt_in(rig):
    """Política executável de rota/corpo e host, sem passthrough CONNECT."""
    proxy, upstream, sock, _ = rig
    assert b"200 OK" in send(sock, request("GET", "/balance"))
    assert b"200 OK" in send(sock, request("GET", "/invoices"))
    assert b"200 OK" in send(sock, request("GET", "/invoices/incoming"))
    assert b"200 OK" in send(sock, request("GET", "/invoices/" + "a" * 64))
    post = request("POST", "/invoices", b'{"amount":10,"memo":"tcc:fixture"}')
    for raw in (post, request("POST", "/payments/bolt11", b'{}'),
                request("GET", "/invoices/incoming?items=1"),
                request("GET", "/balance/../payments/bolt11"),
                request("GET", "//evil.invalid/balance"),
                request("GET", "/balance", headers=b"Host: evil.invalid\r\n"),
                request("GET", "/balance", headers=b"Transfer-Encoding: chunked\r\n"),
                request("GET", "/balance", headers=b"Authorization: Bearer x\r\n"),
                request("POST", "/invoices", b'{"amount":10,"amount":1,"memo":"tcc:fixture"}'),
                request("POST", "/invoices", b'{"amount":11,"memo":"tcc:fixture"}'),
                request("GET", "/balance") + request("GET", "/balance")):
        assert b"403 Forbidden" in send(sock, raw)
    assert len(upstream.seen) == 4
    proxy.invoices_enabled = True  # só fixture; não existe launcher para servidor externo
    assert b"200 OK" in send(sock, post)
    assert upstream.seen[-1] == ("POST", "/invoices", "Bearer " + TOKEN.decode())
    assert b"403 Forbidden" in send(sock, post)  # limite agregado de processo: 10 sats.
    assert b"403 Forbidden" in send(sock, request("POST", "/payments/bolt11", b'{}'))
    assert len(upstream.seen) == 5


def test_wrong_uid_denied_without_upstream(rig):
    """Mesmo tendo caminho UDS, credencial só é anexada a peer UID autorizado."""
    proxy, upstream, sock, _ = rig
    proxy.allowed_uid = os.getuid() + 1
    assert b"200 OK" not in send(sock, request("GET", "/balance"))
    assert upstream.seen == []


def test_tls_name_redirect_and_rollback(rig):
    """Redirecionamento nunca é seguido; certificado de outro host falha fechado."""
    proxy, upstream, sock, temp = rig
    upstream.redirect = True
    assert b"403 Forbidden" in send(sock, request("GET", "/balance"))
    assert len(upstream.seen) == 1
    other_ca, other_key = cert(temp, "evil.invalid")
    # Inspeciona falha de hostname mesmo que o certificado esteja na trust store.
    proxy.ssl_context = ssl.create_default_context(cafile=str(other_ca))
    assert b"403 Forbidden" in send(sock, request("GET", "/balance"))
    assert len(upstream.seen) == 1


def test_secret_not_in_response_or_logs(rig, capsys):
    """Cliente local nunca recebe header Bearer nem resposta de exceção sensível."""
    _, _, sock, _ = rig
    body = send(sock, request("GET", "/balance"))
    assert TOKEN not in body
    assert TOKEN not in capsys.readouterr().out.encode() + capsys.readouterr().err.encode()
    # PR_SET_DUMPABLE=0 pode negar até a leitura de /proc/self pelo próprio UID.
    try:
        proc = Path("/proc/self/environ").read_bytes() + Path("/proc/self/cmdline").read_bytes()
    except PermissionError:
        proc = b""
    assert TOKEN not in proc


def test_failure_before_post_records_uncertain_and_retains_ceiling(rig):
    """Falha de conexão antes do POST marca uncertain e preserva teto na ledger."""
    proxy, upstream, sock, tmp_path = rig
    proxy.invoices_enabled = True
    # Aponta para porta fechada para simular falha de conexão/DNS/TLS antes do POST
    proxy.fixture_port = 65530
    post = request("POST", "/invoices", b'{"amount":10,"memo":"tcc:fail-before"}')
    res = send(sock, post)
    assert b"403 Forbidden" in res
    assert len(upstream.seen) == 0  # nada chegou ao upstream

    summary = proxy.outbox.orphan_summary()
    assert summary["uncertain_quarantined"] == [{"memo": "tcc:fail-before", "amount": 10}]
    assert summary["consumed_sats"] == 10
    assert summary["remaining_sats"] == 0

    # Nova tentativa com memo diferente é negada pelo esgotamento da cota
    assert b"403 Forbidden" in send(sock, request("POST", "/invoices", b'{"amount":1,"memo":"tcc:retry"}'))


def test_proxy_restart_preserves_durable_ledger_and_blocks_replay(tmp_path):
    """Reinício do proxy sobre a mesma ledger mantém registros e recusa replay."""
    ca, key = cert(tmp_path)
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTLS)
    upstream.seen = []
    upstream.redirect = False
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(ca), str(key))
    upstream.socket = ctx.wrap_socket(upstream.socket, server_side=True)
    tls_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    tls_thread.start()

    secret = tmp_path / "token"
    secret.write_bytes(TOKEN)
    secret.chmod(0o600)
    fd = os.open(secret, os.O_RDONLY | os.O_NOFOLLOW)

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger = private / "outbox.sqlite3"
    initialize(ledger)

    sock1 = tmp_path / "proxy1.sock"
    server1 = GuardedServer(sock1, fd, ca, upstream.server_port, os.getuid(), ledger, invoices_enabled=True)
    t1 = threading.Thread(target=server1.serve_forever, daemon=True)
    t1.start()
    try:
        post = request("POST", "/invoices", b'{"amount":10,"memo":"tcc:durable-test"}')
        res1 = send(sock1, post)
        assert b"200 OK" in res1
    finally:
        server1.shutdown()
        server1.server_close()
        t1.join(5)
        sock1.unlink(missing_ok=True)

    # Segundo proxy é iniciado sobre a MESMA ledger (simulando restart/crash)
    fd2 = os.open(secret, os.O_RDONLY | os.O_NOFOLLOW)
    sock2 = tmp_path / "proxy2.sock"
    server2 = GuardedServer(sock2, fd2, ca, upstream.server_port, os.getuid(), ledger, invoices_enabled=True)
    t2 = threading.Thread(target=server2.serve_forever, daemon=True)
    t2.start()
    try:
        summary = server2.outbox.orphan_summary()
        assert len(summary["recorded"]) == 1
        assert summary["recorded"][0]["memo"] == "tcc:durable-test"
        assert summary["consumed_sats"] == 10
        assert summary["remaining_sats"] == 0

        # Tentativa de replay do mesmo memo após restart do proxy é negada
        assert b"403 Forbidden" in send(sock2, post)
        # Nenhuma nova chamada POST chegou ao upstream
        assert len([c for c in upstream.seen if c[0] == "POST"]) == 1
    finally:
        server2.shutdown()
        server2.server_close()
        t2.join(5)
        upstream.shutdown()
        upstream.server_close()
        tls_thread.join(5)
        sock2.unlink(missing_ok=True)

