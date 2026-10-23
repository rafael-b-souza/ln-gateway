"""Proxy HTTP CONNECT para validação de SNI e restrição de destinos."""

import ipaddress
import socket
import sys
from selectors import DefaultSelector, EVENT_READ

HOST = "api.getalby.com"
BIND = "172.31.252.1"
PEER = "172.31.252.2"
PORT = 18765


def recv_header(conn):
    """Limita CONNECT a um frame HTTP ASCII pequeno sem corpo."""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 2048:
        chunk = conn.recv(2048 - len(data))
        if not chunk:
            break
        data += chunk
    return data


def sni_hello(record):
    """Lê apenas SNI do primeiro registro TLS ClientHello; não aceita fragmentação."""
    if len(record) < 5 or record[0] != 22 or record[1:3] != b"\x03\x01" and record[1:3] != b"\x03\x03":
        return None
    if len(record) != 5 + int.from_bytes(record[3:5], "big") or len(record) > 8192:
        return None
    h = memoryview(record)[5:]
    if len(h) < 4 or h[0] != 1 or int.from_bytes(h[1:4], "big") != len(h) - 4:
        return None
    p = 4 + 2 + 32
    if p >= len(h):
        return None
    p += 1 + h[p]  # session id
    if p + 2 > len(h):
        return None
    p += 2 + int.from_bytes(h[p:p+2], "big")
    if p + 1 > len(h):
        return None
    p += 1 + h[p]
    if p + 2 > len(h):
        return None
    end = p + 2 + int.from_bytes(h[p:p+2], "big")
    p += 2
    if end != len(h):
        return None
    while p + 4 <= end:
        typ = int.from_bytes(h[p:p+2], "big")
        size = int.from_bytes(h[p+2:p+4], "big")
        p += 4
        if p + size > end:
            return None
        if typ == 0:
            q = p + 2
            if p + size < q or q + 3 > p + size:
                return None
            names_len = int.from_bytes(h[p:p+2], "big")
            if names_len + 2 != size or h[q] != 0:
                return None
            n = int.from_bytes(h[q+1:q+3], "big")
            if q + 3 + n != p + size:
                return None
            return bytes(h[q+3:q+3+n]).decode("ascii", "ignore")
        p += size
    return None


def handle(conn):
    """CONNECT único, TLS SNI validado antes de abrir socket externo."""
    conn.settimeout(8)
    if conn.getpeername()[0] != PEER:
        return
    header = recv_header(conn)
    if header != b"CONNECT api.getalby.com:443 HTTP/1.1\r\nHost: api.getalby.com:443\r\n\r\n":
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return
    conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    first = conn.recv(5, socket.MSG_WAITALL)
    if len(first) != 5 or first[0] != 22:
        return
    size = int.from_bytes(first[3:5], "big")
    if size < 50 or size > 8192:
        return
    hello = first + conn.recv(size, socket.MSG_WAITALL)
    if sni_hello(hello) != HOST:
        return
    # Só agora resolve destino; rejeita IP privado mesmo com DNS host comprometido.
    ips = [x[4][0] for x in socket.getaddrinfo(HOST, 443, family=socket.AF_INET,
                                                type=socket.SOCK_STREAM)
           if ipaddress.ip_address(x[4][0]).is_global]
    if not ips:
        return
    with socket.create_connection((ips[0], 443), timeout=8) as upstream:
        upstream.sendall(hello)
        conn.settimeout(None)
        upstream.settimeout(None)
        with DefaultSelector() as selector:
            selector.register(conn, EVENT_READ, upstream)
            selector.register(upstream, EVENT_READ, conn)
            while True:
                for key, _ in selector.select(timeout=15):
                    chunk = key.fileobj.recv(65536)
                    if not chunk:
                        return
                    key.data.sendall(chunk)


def main():
    """Servidor sem threads com tempo curto; sessões independentes sem armazenamento."""
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((BIND, PORT))
        server.listen(8)
        # Sem logs de sockets ou requisições.
        while True:
            conn, _ = server.accept()
            with conn:
                try:
                    handle(conn)
                except (OSError, ValueError, UnicodeError):
                    pass


if __name__ == "__main__":
    main()
