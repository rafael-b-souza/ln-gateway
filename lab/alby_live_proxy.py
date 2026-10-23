"""Proxy Unix domain socket com TLS para integração com API Alby."""

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

HOST = "api.getalby.com"
PORT = 443
MAX_BODY = 1024
MAX_REPLY = 32768


def load_secret_fd(path):
    """Lê token de autenticação a partir de arquivo protegido."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        content = os.read(fd, 256).strip()
        if not 32 <= len(content) <= 128 or not all(33 <= b <= 126 for b in content):
            raise ValueError("formato de segredo inválido")
        return content.decode("ascii")
    finally:
        os.close(fd)


class LiveTLSConnection(http.client.HTTPSConnection):
    """Conexão TLS validada para a API Alby."""

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


class LiveHandler(socketserver.StreamRequestHandler):
    """Handler de filtragem de requisições HTTP."""

    def handle(self):
        """Processa requisição validando credenciais de peer e conteúdo."""
        reserved = False
        memo = None
        try:
            _pid, uid, _gid = struct.unpack("3i", self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != self.server.allowed_uid:
                return self.deny(403)

            self.connection.settimeout(15)
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

            # ROTA 1: POST /invoices (apenas emissão exata de 10 sats)
            if method == "POST" and path == "/invoices":
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

                if (not isinstance(payload, dict) or set(payload) != {"amount", "memo"}
                        or payload["amount"] != 10 or not isinstance(payload["memo"], str)
                        or not re.fullmatch(r"tcc:[A-Za-z0-9_-]{1,64}", payload["memo"])):
                    return self.deny(400)

                memo = payload["memo"]
                # Reserva transacional na outbox antes de tocar a rede Alby
                if not self.server.outbox.reserve(memo, 10):
                    return self.deny(403)  # cota esgotada ou memo repetido
                reserved = True

                # Conexão TLS real com CA do sistema
                target_port = getattr(self.server, "target_port", PORT)
                connect_host = getattr(self.server, "connect_host", HOST)
                conn = LiveTLSConnection(HOST, target_port, timeout=30, context=self.server.ssl_context, connect_host=connect_host)
                try:
                    out_body = json.dumps({"amount": 10, "memo": memo}, separators=(",", ":")).encode("ascii")
                    conn.request("POST", "/invoices", body=out_body, headers={
                        "Host": HOST,
                        "Authorization": f"Bearer {self.server.secret}",
                        "Content-Type": "application/json",
                        "User-Agent": "tcc-ln-gateway",
                        "Accept": "application/json",
                    })
                    resp = conn.getresponse()
                    if resp.status != 200:
                        err_body = resp.read(1024).decode("utf-8", errors="replace")
                        sys.stderr.write(f"Alby upstream error {resp.status}: {err_body}\n")
                        sys.stderr.flush()
                        return self.deny(resp.status)
                    data = resp.read(MAX_REPLY + 1)
                    if len(data) > MAX_REPLY:
                        return self.deny(502)
                    res_json = json.loads(data)
                    payment_request = res_json.get("payment_request")
                    payment_hash = res_json.get("payment_hash")

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
                    reply_payload = json.dumps({
                        "payment_hash": payment_hash,
                        "payment_request": payment_request,
                        "amount": 10,
                        "memo": memo,
                    }).encode("utf-8")
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " +
                                            str(len(reply_payload)).encode() + b"\r\nConnection: close\r\n\r\n" +
                                            reply_payload)
                    return
                finally:
                    conn.close()

            # ROTA 2: GET /invoices/{payment_hash} (consulta de status da fatura)
            elif method == "GET" and re.fullmatch(r"/invoices/[0-9a-f]{64}", path):
                target_port = getattr(self.server, "target_port", PORT)
                connect_host = getattr(self.server, "connect_host", HOST)
                conn = LiveTLSConnection(HOST, target_port, timeout=15, context=self.server.ssl_context, connect_host=connect_host)
                try:
                    conn.request("GET", path, headers={
                        "Host": HOST,
                        "Authorization": f"Bearer {self.server.secret}",
                        "User-Agent": "tcc-ln-gateway",
                        "Accept": "application/json",
                    })
                    resp = conn.getresponse()
                    data = resp.read(MAX_REPLY + 1)
                    if len(data) > MAX_REPLY:
                        return self.deny(502)
                    self.connection.sendall(f"HTTP/1.1 {resp.status} {resp.reason}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
                    return
                finally:
                    conn.close()

            # Qualquer outro método ou rota (ex: POST /payments/bolt11): estritamente NEGADO
            else:
                return self.deny(403)

        except (OSError, ValueError, UnicodeError, ssl.SSLError, http.client.HTTPException,
                KeyError, TypeError) as exc:
            self.deny(503)
        finally:
            if reserved:
                try:
                    self.server.outbox.mark(memo, "uncertain")
                except ValueError:
                    pass  # recorded já comitado

    def deny(self, code=403):
        """Devolve erro genérico sem incluir tokens ou detalhes internos."""
        try:
            self.connection.sendall(f"HTTP/1.1 {code} Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError:
            pass


class LiveGuardedServer(socketserver.UnixStreamServer):
    """Servidor UDS restrito que conecta exclusivamente a api.getalby.com."""

    def __init__(self, sock_path, secret_path, outbox_path, allowed_uid=0):
        self.secret = load_secret_fd(secret_path)
        self.allowed_uid = allowed_uid
        self.outbox = Outbox(outbox_path)
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = True
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        super().__init__(str(sock_path), LiveHandler)
        os.chmod(sock_path, 0o600)
