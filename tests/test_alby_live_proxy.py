"""Testes offline de contenção de rotas e credenciais do proxy Alby."""

import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest
from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode

LAB = Path(__file__).parents[1] / "lab"
sys.path.insert(0, str(LAB))
from alby_live_proxy import LiveGuardedServer, HOST
from fixture_outbox import initialize


def send_raw(sock_path, data):
    """Envia requisição HTTP bruta sobre o socket UDS."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(3)
        s.connect(str(sock_path))
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        res = b""
        while True:
            try:
                part = s.recv(4096)
            except ConnectionResetError:
                break
            if not part:
                break
            res += part
        return res


def cert(tmp_path, hostname="api.getalby.com"):
    """Emite CA/certificado descartáveis para teste local."""
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
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


def test_live_proxy_full_flow_with_mock_upstream(tmp_path):
    """Testa emissão e consulta com upstream mock usando BOLT11 bc autêntico."""
    ca, key = cert(tmp_path)

    import http.server
    import ssl
    test_hash = "f" * 64

    class MockAlby(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/invoices"
            assert self.headers.get("Authorization") == "Bearer " + "a" * 48
            now = int(time.time())
            inv = Bolt11(currency="bc", amount_msat=MilliSatoshi(10000), date=now, tags=Tags())
            inv.tags.add(TagChar.payment_hash, test_hash)
            inv.tags.add(TagChar.payment_secret, "e" * 64)
            inv.tags.add(TagChar.description, "tcc:mock")
            inv.tags.add(TagChar.expire_time, 3600)
            encoded = encode(inv, private_key="1" * 64)
            resp = json.dumps({"payment_hash": test_hash, "payment_request": encoded}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def do_GET(self):
            assert self.path == f"/invoices/{test_hash}"
            assert self.headers.get("Authorization") == "Bearer " + "a" * 48
            resp = json.dumps({"payment_hash": test_hash, "settled": False, "state": "CREATED"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server_mock = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockAlby)
    ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx_server.load_cert_chain(str(ca), str(key))
    server_mock.socket = ctx_server.wrap_socket(server_mock.socket, server_side=True)
    mock_thread = threading.Thread(target=server_mock.serve_forever, daemon=True)
    mock_thread.start()

    token_file = tmp_path / "token"
    token_file.write_bytes(b"a" * 48)
    token_file.chmod(0o600)

    outbox_file = tmp_path / "outbox.sqlite3"
    initialize(outbox_file)

    sock_path = tmp_path / "proxy.sock"
    proxy = LiveGuardedServer(sock_path, token_file, outbox_file, allowed_uid=os.getuid())
    # Para teste offline, substitui contexto SSL com a CA descartável e porta mock
    proxy.ssl_context = ssl.create_default_context(cafile=str(ca))
    proxy.target_port = server_mock.server_port
    proxy.connect_host = "127.0.0.1"
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        # Emite fatura válida
        post_req = (
            b"POST /invoices HTTP/1.1\r\n"
            b"Host: api.getalby.com\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 35\r\n\r\n"
            b'{"amount":10,"memo":"tcc:test-ok"}'
        )
        res = send_raw(sock_path, post_req)
        assert b"200 OK" in res
        body = json.loads(res.split(b"\r\n\r\n")[1])
        assert body["payment_hash"] == test_hash
        assert body["payment_request"].startswith("lnbc100n")

        # Segunda emissão DEVE ser negada pelo teto rígido
        res2 = send_raw(sock_path, post_req)
        assert b"403 Forbidden" in res2

        # Consulta de status
        get_req = (f"GET /invoices/{test_hash} HTTP/1.1\r\nHost: api.getalby.com\r\n\r\n").encode("ascii")
        res_get = send_raw(sock_path, get_req)
        assert b"200 OK" in res_get
        status = json.loads(res_get.split(b"\r\n\r\n")[1])
        assert status["settled"] is False and status["state"] == "CREATED"
    finally:
        proxy.shutdown()
        proxy.server_close()
        server_mock.shutdown()
        server_mock.server_close()
        proxy_thread.join(3)
        mock_thread.join(3)
        sock_path.unlink(missing_ok=True)


def test_live_runner_commands(tmp_path):
    """Testa as funções de comando de alby_live_runner."""
    from alby_live_runner import cmd_issue, cmd_check, send_uds_request
    ca, key = cert(tmp_path)
    import http.server
    import ssl
    test_hash = "e" * 64

    class MockAlby(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            now = int(time.time())
            inv = Bolt11(currency="bc", amount_msat=MilliSatoshi(10000), date=now, tags=Tags())
            inv.tags.add(TagChar.payment_hash, test_hash)
            inv.tags.add(TagChar.payment_secret, "d" * 64)
            inv.tags.add(TagChar.description, "tcc:runner-test")
            inv.tags.add(TagChar.expire_time, 3600)
            encoded = encode(inv, private_key="1" * 64)
            resp = json.dumps({"payment_hash": test_hash, "payment_request": encoded}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def do_GET(self):
            resp = json.dumps({"payment_hash": test_hash, "settled": True, "state": "SETTLED"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server_mock = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockAlby)
    ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx_server.load_cert_chain(str(ca), str(key))
    server_mock.socket = ctx_server.wrap_socket(server_mock.socket, server_side=True)
    mock_thread = threading.Thread(target=server_mock.serve_forever, daemon=True)
    mock_thread.start()

    run_dir = tmp_path / "run"
    run_dir.mkdir(0o700)
    token_file = run_dir / "token"
    token_file.write_bytes(b"a" * 48)
    token_file.chmod(0o600)
    outbox_file = run_dir / "outbox.sqlite3"
    initialize(outbox_file)
    sock_path = run_dir / "proxy.sock"

    proxy = LiveGuardedServer(sock_path, token_file, outbox_file, allowed_uid=os.getuid())
    proxy.ssl_context = ssl.create_default_context(cafile=str(ca))
    proxy.target_port = server_mock.server_port
    proxy.connect_host = "127.0.0.1"
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()

    try:
        issue_res = cmd_issue(sock_path, "tcc:runner-test")
        assert issue_res["payment_hash"] == test_hash
        assert issue_res["amount"] == 10

        check_res = cmd_check(sock_path, test_hash)
        assert check_res["settled"] is True
        assert check_res["state"] == "SETTLED"
    finally:
        proxy.shutdown()
        proxy.server_close()
        server_mock.shutdown()
        server_mock.server_close()
        proxy_thread.join(3)
        mock_thread.join(3)
        sock_path.unlink(missing_ok=True)


def test_live_proxy_rejects_outbound_payments_and_invalid_amounts(tmp_path):
    """Garante que o proxy real recusa POST /payments/bolt11 e valores diferentes de 10 sats."""
    token_file = tmp_path / "token"
    token_file.write_bytes(b"a" * 48)
    token_file.chmod(0o600)

    outbox_file = tmp_path / "outbox.sqlite3"
    initialize(outbox_file)

    sock_path = tmp_path / "proxy.sock"
    server = LiveGuardedServer(sock_path, token_file, outbox_file, allowed_uid=os.getuid())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # 1. Tentar fazer pagamento de saída: DEVE SER 403
        pay_req = (
            b"POST /payments/bolt11 HTTP/1.1\r\n"
            b"Host: api.getalby.com\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 15\r\n\r\n"
            b'{"invoice":"x"}'
        )
        res = send_raw(sock_path, pay_req)
        assert b"403 Forbidden" in res

        # 2. Tentar emitir 11 sats: DEVE SER 400
        inv_11 = (
            b"POST /invoices HTTP/1.1\r\n"
            b"Host: api.getalby.com\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 35\r\n\r\n"
            b'{"amount":11,"memo":"tcc:test-11"}'
        )
        res = send_raw(sock_path, inv_11)
        assert b"400 Forbidden" in res

        # 3. Tentar emitir com memo fora do padrão: DEVE SER 400
        inv_bad_memo = (
            b"POST /invoices HTTP/1.1\r\n"
            b"Host: api.getalby.com\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 34\r\n\r\n"
            b'{"amount":10,"memo":"bad_memo"}'
        )
        res = send_raw(sock_path, inv_bad_memo)
        assert b"400 Forbidden" in res

        # 4. Outbox permanece intacta com 0 sats consumidos
        summary = server.outbox.orphan_summary()
        assert summary["consumed_sats"] == 0
        assert summary["remaining_sats"] == 10
    finally:
        server.shutdown()
        server.server_close()
        t.join(3)
        sock_path.unlink(missing_ok=True)
