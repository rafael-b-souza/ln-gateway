"""Testes offline de filtragem GraphQL e autenticação UDS do proxy Blink."""

import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time

import pytest
from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode

LAB = Path(__file__).parents[1] / "lab"
sys.path.insert(0, str(LAB))
from blink_live_proxy import BlinkGuardedServer, HOST
from blink_live_runner import cmd_wallets, cmd_issue, cmd_check, send_uds_request
from fixture_outbox import initialize


def cert(tmp_path, hostname="api.blink.sv"):
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


def test_blink_proxy_rejects_outbound_payments_and_invalid_amounts(tmp_path):
    """Proxy Blink recusa pagamentos de saída e faturas acima do teto de 10 sats."""
    token_file = tmp_path / "token"
    token_file.write_bytes(b"b" * 48)
    token_file.chmod(0o600)

    outbox_file = tmp_path / "outbox.sqlite3"
    initialize(outbox_file)

    sock_path = tmp_path / "proxy.sock"
    server = BlinkGuardedServer(sock_path, token_file, outbox_file, allowed_uid=os.getuid())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # 1. Tentar fazer pagamento de saída: DEVE SER 403
        pay_req = (
            b"POST /v1/payments HTTP/1.1\r\n"
            b"Host: api.blink.sv\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 15\r\n\r\n"
            b'{"invoice":"x"}'
        )
        res = send_raw(sock_path, pay_req)
        assert b"403 Forbidden" in res

        # 2. Tentar emitir 15 sats: DEVE SER 400
        inv_15 = (
            b"POST /v1/invoices HTTP/1.1\r\n"
            b"Host: api.blink.sv\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 60\r\n\r\n"
            b'{"amount":15,"memo":"tcc:test-15","wallet_id":"test-wallet-id"}'
        )
        res = send_raw(sock_path, inv_15)
        assert b"400 Forbidden" in res

        # 3. Tentar emitir com memo inválido: DEVE SER 400
        inv_bad_memo = (
            b"POST /v1/invoices HTTP/1.1\r\n"
            b"Host: api.blink.sv\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 58\r\n\r\n"
            b'{"amount":10,"memo":"bad_memo","wallet_id":"test-wallet-id"}'
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


def test_blink_proxy_full_flow_with_mock_upstream(tmp_path):
    """Testa leitura de carteiras, emissão e status com upstream GraphQL mock."""
    ca, key = cert(tmp_path)
    import http.server
    test_hash = "d" * 64
    test_wallet_id = "test-btc-wallet"

    class MockBlink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/graphql"
            assert self.headers.get("X-API-KEY") == "b" * 48
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            query = body.get("query", "")

            # Query Me
            if "query Me" in query:
                resp = json.dumps({
                    "data": {
                        "me": {
                            "defaultAccount": {
                                "wallets": [
                                    {"id": test_wallet_id, "walletCurrency": "BTC", "balance": 1500},
                                    {"id": "test-usd-wallet", "walletCurrency": "USD", "balance": 100}
                                ]
                            }
                        }
                    }
                }).encode()
            # Mutation invoice
            elif "mutation LnInvoiceCreateOnBehalfOfRecipient" in query:
                now = int(time.time())
                inv = Bolt11(currency="bc", amount_msat=MilliSatoshi(10000), date=now, tags=Tags())
                inv.tags.add(TagChar.payment_hash, test_hash)
                inv.tags.add(TagChar.payment_secret, "c" * 64)
                inv.tags.add(TagChar.description, "tcc:blink-test")
                inv.tags.add(TagChar.expire_time, 3600)
                encoded = encode(inv, private_key="1" * 64)
                resp = json.dumps({
                    "data": {
                        "lnInvoiceCreateOnBehalfOfRecipient": {
                            "invoice": {
                                "paymentRequest": encoded,
                                "paymentHash": test_hash,
                                "satoshis": 10
                            },
                            "errors": []
                        }
                    }
                }).encode()
            # Query InvoiceByPaymentHash
            elif "query InvoiceByPaymentHash" in query:
                resp = json.dumps({
                    "data": {
                        "me": {
                            "defaultAccount": {
                                "walletById": {
                                    "invoiceByPaymentHash": {
                                        "paymentStatus": "PAID"
                                    }
                                }
                            }
                        }
                    }
                }).encode()
            else:
                resp = json.dumps({"errors": [{"message": "unknown query"}]}).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server_mock = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockBlink)
    ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx_server.load_cert_chain(str(ca), str(key))
    server_mock.socket = ctx_server.wrap_socket(server_mock.socket, server_side=True)
    mock_thread = threading.Thread(target=server_mock.serve_forever, daemon=True)
    mock_thread.start()

    run_dir = tmp_path / "run"
    run_dir.mkdir(0o700)
    token_file = run_dir / "token"
    token_file.write_bytes(b"b" * 48)
    token_file.chmod(0o600)
    outbox_file = run_dir / "outbox.sqlite3"
    initialize(outbox_file)
    sock_path = run_dir / "proxy.sock"

    proxy = BlinkGuardedServer(sock_path, token_file, outbox_file, allowed_uid=os.getuid())
    proxy.ssl_context = ssl.create_default_context(cafile=str(ca))
    proxy.target_port = server_mock.server_port
    proxy.connect_host = "127.0.0.1"
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()

    try:
        # 1. Leitura de carteiras
        wallets_res = cmd_wallets(sock_path)
        assert len(wallets_res["wallets"]) == 2
        btc_wallet = [w for w in wallets_res["wallets"] if w["currency"] == "BTC"][0]
        assert btc_wallet["id"] == test_wallet_id
        assert btc_wallet["balance_sats"] == 1500

        # 2. Emissão de fatura de 10 sats
        issue_res = cmd_issue(sock_path, test_wallet_id, "tcc:blink-test")
        assert issue_res["payment_hash"] == test_hash
        assert issue_res["amount"] == 10
        assert issue_res["payment_request"].startswith("lnbc100n")

        # 3. Tentar segunda emissão: DEVE SER 403 (teto esgotado)
        with pytest.raises(RuntimeError) as exc_info:
            cmd_issue(sock_path, test_wallet_id, "tcc:blink-test-2")
        assert "403" in str(exc_info.value)

        # 4. Checar status da fatura
        check_res = cmd_check(sock_path, test_wallet_id, test_hash)
        assert check_res["status"] == "PAID"
        assert check_res["settled"] is True
    finally:
        proxy.shutdown()
        proxy.server_close()
        server_mock.shutdown()
        server_mock.server_close()
        proxy_thread.join(3)
        mock_thread.join(3)
        sock_path.unlink(missing_ok=True)
