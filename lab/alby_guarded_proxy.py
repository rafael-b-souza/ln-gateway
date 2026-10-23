"""Proxy HTTP sobre socket Unix com validação estrita de requisições."""

import http.client
import json
import os
import re
import socket
import socketserver
import ssl
import struct

from alby_method_policy import allowed
from alby_secret_fixture import load_synthetic

HOST = "api.getalby.com"
MAX_BODY = 1024
MAX_REPLY = 32768


class FixtureTLSConnection(http.client.HTTPSConnection):
    """Conexão TLS direcionada a porta local com verificação de SNI."""

    def __init__(self, port, context):
        super().__init__(HOST, 443, timeout=10, context=context)
        self.fixture_port = port

    def connect(self):
        """Conecta no socket de loopback e aplica camada TLS."""
        raw = socket.create_connection(("127.0.0.1", self.fixture_port), timeout=10)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=HOST)
        except BaseException:
            raw.close()
            raise


class StrictHandler(socketserver.StreamRequestHandler):
    """Handler de requisição HTTP estrita sobre socket Unix."""

    def handle(self):
        """Processa requisição validando permissões de peer e formato HTTP."""
        try:
            _pid, uid, _gid = struct.unpack("3i", self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != self.server.allowed_uid:
                return
            self.connection.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head and len(head) <= 4096:
                part = self.connection.recv(1)
                if not part:
                    return
                head += part
            if len(head) > 4096 or not head.endswith(b"\r\n\r\n"):
                return self.deny()
            lines = head[:-4].split(b"\r\n")
            if len(lines) > 12 or not re.fullmatch(rb"(GET|POST) /[A-Za-z0-9/_-]* HTTP/1\.1", lines[0]):
                return self.deny()
            method, path, _ = lines[0].decode("ascii").split(" ")
            headers = {}
            for line in lines[1:]:
                if not re.fullmatch(rb"[A-Za-z0-9-]+: [\x20-\x7e]+", line):
                    return self.deny()
                name, value = line.split(b": ", 1)
                name = name.decode("ascii").lower()
                if name in headers:
                    return self.deny()
                headers[name] = value.decode("ascii")
            if headers.get("host") != HOST or any(key not in ("host", "content-type", "content-length") for key in headers):
                return self.deny()
            if method == "GET":
                if "content-length" in headers or "content-type" in headers:
                    return self.deny()
                body = None
            else:
                length = headers.get("content-length", "")
                if not re.fullmatch(r"[1-9][0-9]{0,3}", length) or not 1 <= int(length) <= MAX_BODY:
                    return self.deny()
                if headers.get("content-type") != "application/json":
                    return self.deny()
                data = self.rfile.read(int(length))
                try:
                    def unique(pairs):
                        """Rejeita chaves JSON repetidas para impedir divergência de parsers."""
                        obj = {}
                        for key, value in pairs:
                            if key in obj:
                                raise ValueError("duplicado")
                            obj[key] = value
                        return obj
                    body = json.loads(data, object_pairs_hook=unique)
                    if data != json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode():
                        return self.deny()
                except (ValueError, TypeError):
                    return self.deny()
            # Um único frame: bytes extras/segunda requisição nunca alcançam o upstream.
            self.connection.settimeout(0.05)
            try:
                if self.connection.recv(1, socket.MSG_PEEK):
                    return self.deny()
            except socket.timeout:
                pass
            if not allowed(method, path, body, invoices_enabled=self.server.invoices_enabled):
                return self.deny()
            # Reserva durável antes de POST; não repetir mesmo após timeout/crash.
            memo = body["memo"] if method == "POST" else None
            reserved = False
            if memo is not None:
                if not self.server.outbox.reserve(memo, body["amount"]):
                    return self.deny()
                reserved = True
            target_port = 65530 if (method == "POST" and self.server.fail_before_post) else self.server.fixture_port
            conn = FixtureTLSConnection(target_port, self.server.ssl_context)
            try:
                forwarded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
                # Token permanece apenas na memória do proxy, nunca passa ao cliente local.
                conn.request(method, path, body=forwarded,
                             headers={"Authorization": "Bearer " + self.server.secret.decode("ascii"),
                                      "Content-Type": "application/json"} if forwarded else
                             {"Authorization": "Bearer " + self.server.secret.decode("ascii")})
                response = conn.getresponse()
                if response.status != 200 or response.getheader("Location") or response.getheader("Transfer-Encoding"):
                    return self.deny()
                content = response.read(MAX_REPLY + 1)
                if len(content) > MAX_REPLY or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json":
                    return self.deny()
                if memo is not None:
                    # Fornecedor falso tem contrato BOLT11 bcrt; valor/hash/assinatura
                    # devem ser comprovados antes de registrar referência.
                    from bolt11 import decode
                    data = json.loads(content)
                    invoice = decode(data["payment_request"])
                    if (invoice.currency != "bcrt" or invoice.payment_hash != data["payment_hash"]
                            or int(invoice.amount_msat) != body["amount"] * 1000
                            or not invoice.signature.verify(invoice.payee)):
                        return self.deny()
                    self.server.outbox.mark(memo, "recorded", data["payment_hash"])
                self.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " +
                                        str(len(content)).encode() + b"\r\nConnection: close\r\n\r\n" + content)
            finally:
                conn.close()
        except (OSError, ValueError, UnicodeError, ssl.SSLError, http.client.HTTPException,
                KeyError, TypeError):
            self.deny()
        finally:
            # Após exceção ou resposta não verificável, marcar como incerta sem
            # liberar cota. Em crash/SIGKILL a abertura seguinte faz o mesmo.
            if "reserved" in locals() and reserved:
                try:
                    self.server.outbox.mark(memo, "uncertain")
                except ValueError:
                    pass  # recorded já comitado; nunca regravar como incerto.

    def deny(self):
        """Envia resposta HTTP 403 Forbidden e fecha conexão."""
        try:
            self.connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass


class GuardedServer(socketserver.UnixStreamServer):
    """Servidor Unix domain socket com controle de acesso e outbox."""

    def __init__(self, path, secret_fd, ca_file, fixture_port, allowed_uid, outbox_path,
                 invoices_enabled=False, fail_before_post=False):
        """Inicializa servidor com contexto TLS e outbox."""
        if not 1 <= fixture_port <= 65535 or type(allowed_uid) is not int:
            raise ValueError("fixture inválida")
        self.secret = load_synthetic(secret_fd)
        if not self.secret.startswith(b"synthetic-"):
            raise ValueError("credencial real proibida neste ensaio")
        self.fixture_port = fixture_port
        self.allowed_uid = allowed_uid
        self.invoices_enabled = invoices_enabled
        self.fail_before_post = fail_before_post
        from fixture_outbox import Outbox
        self.outbox = Outbox(outbox_path)
        self.ssl_context = ssl.create_default_context(cafile=str(ca_file))
        self.ssl_context.check_hostname = True
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        super().__init__(str(path), StrictHandler)
        os.chmod(path, 0o660)


def main():
    """Ponto de entrada desabilitado para execução direta."""
    raise SystemExit("somente fixtures; não há launcher para Alby real")


if __name__ == "__main__":
    main()
