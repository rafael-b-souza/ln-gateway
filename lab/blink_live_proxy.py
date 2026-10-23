"""Proxy Unix domain socket com TLS para integração com API Blink GraphQL."""

import http.client
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import ssl
import struct
import sys
import time

from bolt11 import decode as bolt11_decode
from fixture_outbox import Outbox, initialize

HOST = "api.blink.sv"
PORT = 443
ENDPOINT = "/graphql"
MAX_BODY = 4096
MAX_REPLY = 65536


def load_secret_fd(path):
    """Lê token de autenticação a partir de arquivo protegido."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        content = os.read(fd, 256).strip()
        if not 16 <= len(content) <= 128 or not all(33 <= b <= 126 for b in content):
            raise ValueError("formato de segredo inválido")
        return content.decode("ascii")
    finally:
        os.close(fd)


class BlinkTLSConnection(http.client.HTTPSConnection):
    """Conexão TLS validada para a API Blink."""

    def __init__(self, host, port, timeout, context, connect_host=None):
        super().__init__(host, port, timeout=timeout, context=context)
        self.connect_host = connect_host or host

    def connect(self):
        raw = socket.create_connection((self.connect_host, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class BlinkHandler(socketserver.StreamRequestHandler):
    """Handler de filtragem de requisições GraphQL."""

    def handle(self):
        """Processa requisição validando credenciais de peer e conteúdo."""
        reserved = False
        memo = None
        try:
            _pid, uid, _gid = struct.unpack("3i", self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != self.server.allowed_uid:
                return self.deny(403)

            self.connection.settimeout(20)
            head = b""
            while b"\r\n\r\n" not in head and len(head) <= 4096:
                part = self.connection.recv(1)
                if not part:
                    return
                head += part

            if len(head) > 4096 or not head.endswith(b"\r\n\r\n"):
                return self.deny(400)

            lines = head[:-4].split(b"\r\n")
            if not re.fullmatch(rb"(GET|POST) /[A-Za-z0-9/_-]* HTTP/1\.1", lines[0]):
                return self.deny(400)

            method, path, _ = lines[0].decode("ascii").split(" ")
            headers = {}
            for line in lines[1:]:
                if not re.fullmatch(rb"[A-Za-z0-9-]+: [\x20-\x7e]+", line):
                    return self.deny(400)
                name, value = line.split(b": ", 1)
                headers[name.decode("ascii").lower()] = value.decode("ascii")

            if headers.get("host") != HOST:
                return self.deny(403)

            # Proibição explícita de qualquer pagamento de saída
            if "payment" in path.lower() or "send" in path.lower():
                return self.deny(403)

            # ROTA 1: GET /v1/wallets (Leitura de carteiras e saldo BTC)
            if method == "GET" and path == "/v1/wallets":
                query_payload = {
                    "query": "query Me { me { defaultAccount { wallets { id walletCurrency balance } } } }"
                }
                status, data = self._call_blink_graphql(query_payload)
                if status != 200:
                    return self.deny(status)
                wallets = (data.get("data", {}).get("me", {})
                           .get("defaultAccount", {}).get("wallets", []))
                cleaned_wallets = []
                for w in wallets:
                    cleaned_wallets.append({
                        "id": w.get("id"),
                        "currency": w.get("walletCurrency"),
                        "balance_sats": w.get("balance"),
                    })
                return self.respond_json(200, {"wallets": cleaned_wallets})

            # ROTA 2: POST /v1/invoices (Emissão pontual de 10 sats)
            elif method == "POST" and path == "/v1/invoices":
                length = headers.get("content-length", "")
                if not re.fullmatch(r"[1-9][0-9]{0,3}", length) or not 1 <= int(length) <= MAX_BODY:
                    return self.deny(400)
                if headers.get("content-type") != "application/json":
                    return self.deny(415)

                raw_body = self.rfile.read(int(length))
                try:
                    payload = json.loads(raw_body)
                except (ValueError, TypeError):
                    return self.deny(400)

                if (not isinstance(payload, dict) or set(payload) != {"amount", "memo", "wallet_id"}
                        or payload["amount"] != 10 or not isinstance(payload["memo"], str)
                        or not re.fullmatch(r"tcc:[A-Za-z0-9_-]{1,64}", payload["memo"])
                        or not isinstance(payload["wallet_id"], str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload["wallet_id"])):
                    return self.deny(400)

                memo = payload["memo"]
                wallet_id = payload["wallet_id"]

                # Reserva transacional na outbox antes de tocar a rede
                if not self.server.outbox.reserve(memo, 10):
                    return self.deny(403)  # cota esgotada ou memo repetido
                reserved = True

                # Mutation GraphQL para criação de fatura
                mutation_payload = {
                    "query": (
                        "mutation LnInvoiceCreateOnBehalfOfRecipient($input: LnInvoiceCreateOnBehalfOfRecipientInput!) {"
                        "  lnInvoiceCreateOnBehalfOfRecipient(input: $input) {"
                        "    invoice {"
                        "      paymentRequest"
                        "      paymentHash"
                        "      satoshis"
                        "    }"
                        "    errors {"
                        "      message"
                        "    }"
                        "  }"
                        "}"
                    ),
                    "variables": {
                        "input": {
                            "amount": 10,
                            "recipientWalletId": wallet_id,
                            "memo": memo,
                        }
                    }
                }
                status, data = self._call_blink_graphql(mutation_payload)
                if status != 200:
                    return self.deny(status)

                res_mutation = data.get("data", {}).get("lnInvoiceCreateOnBehalfOfRecipient", {})
                errors = res_mutation.get("errors", [])
                if errors:
                    err_msg = errors[0].get("message", "erro na criacao da fatura")
                    sys.stderr.write(f"Blink GraphQL error: {err_msg}\n")
                    sys.stderr.flush()
                    return self.deny(400)

                invoice_obj = res_mutation.get("invoice", {})
                payment_request = invoice_obj.get("paymentRequest")
                payment_hash = invoice_obj.get("paymentHash")
                satoshis = invoice_obj.get("satoshis")

                if not payment_request or not payment_hash or satoshis != 10:
                    return self.deny(502)

                # Validação Criptográfica Independente da BOLT11
                inv = bolt11_decode(payment_request)
                now = int(time.time())
                if (inv.currency != "bc"
                        or inv.payment_hash != payment_hash
                        or int(inv.amount_msat) != 10000
                        or not inv.signature
                        or not inv.payee
                        or not inv.signature.verify(inv.payee)
                        or inv.date > now + 120
                        or inv.date + inv.expiry <= now + 60):
                    return self.deny(502)

                # Registra sucesso verificado na outbox
                self.server.outbox.mark(memo, "recorded", payment_hash)
                return self.respond_json(200, {
                    "payment_hash": payment_hash,
                    "payment_request": payment_request,
                    "amount": 10,
                    "memo": memo,
                })

            # ROTA 3: GET /v1/invoices/{wallet_id}/{payment_hash} (Consulta de liquidação)
            elif method == "GET" and re.fullmatch(r"/v1/invoices/[A-Za-z0-9_-]{1,64}/[0-9a-f]{64}", path):
                _, _, _, wallet_id, payment_hash = path.split("/")
                query_payload = {
                    "query": (
                        "query InvoiceByPaymentHash($walletId: WalletId!, $paymentHash: PaymentHash!) {"
                        "  me {"
                        "    defaultAccount {"
                        "      walletById(walletId: $walletId) {"
                        "        invoiceByPaymentHash(paymentHash: $paymentHash) {"
                        "          ... on LnInvoice {"
                        "            paymentStatus"
                        "          }"
                        "        }"
                        "      }"
                        "    }"
                        "  }"
                        "}"
                    ),
                    "variables": {
                        "walletId": wallet_id,
                        "paymentHash": payment_hash,
                    }
                }
                status, data = self._call_blink_graphql(query_payload)
                if status != 200:
                    return self.deny(status)

                invoice_data = (data.get("data", {}).get("me", {})
                                .get("defaultAccount", {}).get("walletById", {})
                                .get("invoiceByPaymentHash", {}))
                payment_status = invoice_data.get("paymentStatus") if invoice_data else None
                settled = (payment_status == "PAID")
                return self.respond_json(200, {
                    "payment_hash": payment_hash,
                    "status": payment_status or "UNKNOWN",
                    "settled": settled,
                })

            else:
                return self.deny(403)

        except (OSError, ValueError, UnicodeError, ssl.SSLError, http.client.HTTPException,
                KeyError, TypeError) as exc:
            sys.stderr.write(f"Proxy exception: {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()
            self.deny(503)
        finally:
            if reserved:
                try:
                    self.server.outbox.mark(memo, "uncertain")
                except ValueError:
                    pass

    def _call_blink_graphql(self, payload: dict) -> tuple[int, dict]:
        """Encaminha requisição GraphQL aprovada para a API Blink com validação de certificado TLS."""
        target_port = getattr(self.server, "target_port", PORT)
        connect_host = getattr(self.server, "connect_host", HOST)
        conn = BlinkTLSConnection(HOST, target_port, timeout=25,
                                  context=self.server.ssl_context, connect_host=connect_host)
        try:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            conn.request("POST", ENDPOINT, body=body, headers={
                "Host": HOST,
                "X-API-KEY": self.server.secret,
                "Content-Type": "application/json",
                "User-Agent": "tcc-ln-gateway",
                "Accept": "application/json",
            })
            resp = conn.getresponse()
            raw = resp.read(MAX_REPLY + 1)
            if len(raw) > MAX_REPLY:
                return 502, {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                data = {}
            return resp.status, data
        finally:
            conn.close()

    def respond_json(self, status: int, data: dict):
        """Formata e devolve resposta JSON ao cliente UDS."""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.connection.sendall(
            f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )

    def deny(self, code=403):
        """Devolve erro genérico sem incluir tokens, faturas ou detalhes de exceção."""
        try:
            self.connection.sendall(f"HTTP/1.1 {code} Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError:
            pass


class BlinkGuardedServer(socketserver.UnixStreamServer):
    """Inicializa socket Unix privado para comunicação local com o proxy Blink."""

    def __init__(self, sock_path, secret_path, outbox_path, allowed_uid=0):
        self.secret = load_secret_fd(secret_path)
        self.allowed_uid = allowed_uid
        self.outbox = Outbox(outbox_path)
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = True
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        super().__init__(str(sock_path), BlinkHandler)
        os.chmod(sock_path, 0o600)
