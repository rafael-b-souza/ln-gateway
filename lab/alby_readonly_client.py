"""Cliente de leitura para API Alby com validação TLS."""

import http.client
import json
import os
import socket
import ssl
import stat
import sys
from pathlib import Path

HOST = "api.getalby.com"
PROXY = ("172.31.252.1", 18765)
FILE = Path("/opt/tcc-ln-lab/secrets/alby-access-token")


def tunnel(host):
    """Estabelece túnel HTTP CONNECT com o proxy."""
    sock = socket.create_connection(PROXY, timeout=8)
    try:
        sock.sendall((f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n").encode("ascii"))
        raw = b""
        while b"\r\n\r\n" not in raw and len(raw) < 256:
            raw += sock.recv(256 - len(raw))
        if raw != b"HTTP/1.1 200 Connection Established\r\n\r\n":
            raise RuntimeError("CONNECT negado")
        return sock
    except Exception:
        sock.close()
        raise


def request(path, token):
    """Executa requisição GET autenticada via TLS."""
    assert path in ("/balance", "/invoices/incoming?items=1&page=1")
    conn = tunnel(HOST)
    tls = ssl.create_default_context().wrap_socket(conn, server_hostname=HOST)
    tls.settimeout(10)
    try:
        req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\n"
               f"Authorization: Bearer {token}\r\nAccept: application/json\r\n"
               "Connection: close\r\n\r\n")
        tls.sendall(req.encode("ascii"))
        response = http.client.HTTPResponse(tls)
        response.begin()
        body = response.read(65537)
        if len(body) > 65536:
            raise RuntimeError("resposta grande")
        if response.status != 200:
            return response.status, None
        if not response.getheader("Content-Type", "").lower().startswith("application/json"):
            raise RuntimeError("tipo de resposta inesperado")
        return response.status, json.loads(body)
    finally:
        tls.close()


def preflight():
    """Valida restrições de conectividade e túnel."""
    for ip in ("1.1.1.1", "10.10.10.102", "10.10.10.121", "192.168.3.1",
               "169.254.169.254", "127.0.0.1"):
        with socket.socket() as s:
            s.settimeout(2)
            if s.connect_ex((ip, 443)) == 0:
                raise RuntimeError("egress direto não negado")
    with socket.socket(socket.AF_INET6) as s:
        s.settimeout(2)
        if s.connect_ex(("2606:4700:4700::1111", 443)) == 0:
            raise RuntimeError("egress IPv6 não negado")
    for host in ("example.com", "10.10.10.102", "192.168.3.1", "1.1.1.1"):
        try:
            tunnel(host)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("proxy deixou CONNECT indevido passar")
    # Mesmo se CONNECT for autorizado, SNI de outro domínio no IP compartilhado
    # precisa ser encerrado pelo proxy antes de abrir conexão externa.
    sock = tunnel(HOST)
    try:
        try:
            ssl.create_default_context().wrap_socket(sock, server_hostname="example.com")
        except (ssl.SSLError, OSError, TimeoutError):
            pass
        else:
            raise RuntimeError("proxy deixou SNI indevido passar")
    finally:
        sock.close()
    print("preflight: direct IPv4/IPv6, RFC1918 e CONNECT/SNI alternativos negados", flush=True)


def main():
    """Abre segredo apenas após preflight; nunca publica body, token ou identidade."""
    if os.geteuid() != 0:
        raise RuntimeError("UID inesperado")
    preflight()
    ds, fs = FILE.parent.lstat(), FILE.lstat()
    if (not stat.S_ISDIR(ds.st_mode) or ds.st_uid != 0 or ds.st_gid != 0
            or stat.S_IMODE(ds.st_mode) != 0o700 or not stat.S_ISREG(fs.st_mode)
            or fs.st_uid != 0 or fs.st_gid != 0 or stat.S_IMODE(fs.st_mode) != 0o600
            or not 12 <= fs.st_size <= 2048):
        raise RuntimeError("metadata da credencial inválida")
    print("metadata: root:root 0700/0600; sem env/argv", flush=True)
    fd = os.open(FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        token = os.read(fd, 2049).decode("ascii")
    finally:
        os.close(fd)
    if not token or token.strip() != token or not all(33 <= ord(c) <= 126 for c in token):
        raise RuntimeError("credencial inválida")
    # Consulta read-only e sem retorno de campos pessoais. Se 200 com saldo não
    # zero, interromper antes de consultar invoices.
    code, obj = request("/balance", token)
    print(f"GET /balance: HTTP {code}", flush=True)
    if code != 200 or not isinstance(obj, dict) or obj.get("unit") != "sat":
        return
    val = obj.get("balance")
    if type(val) is not int or val < 0:
        raise RuntimeError("valor inválido")
    print("saldo: 0 sat" if val == 0 else "saldo: não zero; interrompido", flush=True)
    if val != 0:
        return
    code, obj = request("/invoices/incoming?items=1&page=1", token)
    print(f"GET /invoices/incoming: HTTP {code}", flush=True)
    if code == 200 and isinstance(obj, list):
        print("contrato lista: JSON array (itens não exibidos)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("diagnóstico interrompido sem exposição de payload/segredo", flush=True)
        sys.exit(2)
