"""CLI de laboratório para ensaio de emissão e consulta de faturas Alby."""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import sys
import time

from alby_live_proxy import LiveGuardedServer
from fixture_outbox import Outbox, initialize

DEFAULT_DIR = Path("/opt/tcc-ln-lab/live_run")
DEFAULT_SECRET = Path("/opt/tcc-ln-lab/secrets/alby-access-token")
DEFAULT_SOCK = DEFAULT_DIR / "proxy.sock"
DEFAULT_OUTBOX = DEFAULT_DIR / "outbox.sqlite3"
DEFAULT_PID = DEFAULT_DIR / "proxy.pid"


def send_uds_request(sock_path: Path, request_bytes: bytes) -> tuple[int, dict]:
    """Envia requisição HTTP via socket UDS e retorna status code e payload JSON."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(35)
        s.connect(str(sock_path))
        s.sendall(request_bytes)
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

    if not res:
        raise RuntimeError("resposta vazia do proxy")

    header_part, _, body_part = res.partition(b"\r\n\r\n")
    first_line = header_part.split(b"\r\n")[0].decode("ascii", errors="replace")
    status_match = re.match(r"HTTP/\d\.\d (\d{3})", first_line)
    if not status_match:
        raise RuntimeError(f"resposta inválida: {first_line}")
    status_code = int(status_match.group(1))

    data = {}
    if body_part:
        try:
            data = json.loads(body_part.decode("utf-8"))
        except ValueError:
            data = {"raw": body_part.decode("ascii", errors="replace")}
    return status_code, data


def cmd_start_proxy(run_dir: Path, secret_path: Path, outbox_path: Path, sock_path: Path, pid_path: Path, allowed_uid: int = 0):
    """Inicia o proxy UDS/TLS em processo contido."""
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)

    if not outbox_path.exists():
        initialize(outbox_path)

    sock_path.unlink(missing_ok=True)
    server = LiveGuardedServer(sock_path, secret_path, outbox_path, allowed_uid=allowed_uid)
    pid_path.write_text(str(os.getpid()))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sock_path.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)


def cmd_issue(sock_path: Path, memo: str) -> dict:
    """Emite uma única fatura de 10 sats via UDS proxy."""
    if not re.fullmatch(r"tcc:[A-Za-z0-9_-]{1,64}", memo):
        raise ValueError(f"memo inválido: {memo}")
    body = json.dumps({"amount": 10, "memo": memo}, separators=(",", ":")).encode("ascii")
    req = (
        f"POST /invoices HTTP/1.1\r\n"
        f"Host: api.getalby.com\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii") + body
    status_code, data = send_uds_request(sock_path, req)
    if status_code != 200:
        raise RuntimeError(f"falha na emissão (HTTP {status_code}): {data}")
    return data


def cmd_check(sock_path: Path, payment_hash: str) -> dict:
    """Consulta o status de uma fatura por payment_hash."""
    if not re.fullmatch(r"[0-9a-f]{64}", payment_hash):
        raise ValueError(f"payment_hash inválido: {payment_hash}")
    req = (
        f"GET /invoices/{payment_hash} HTTP/1.1\r\n"
        f"Host: api.getalby.com\r\n\r\n"
    ).encode("ascii")
    status_code, data = send_uds_request(sock_path, req)
    if status_code != 200:
        raise RuntimeError(f"falha na consulta (HTTP {status_code}): {data}")
    return data


def main():
    parser = argparse.ArgumentParser(description="Runner controlado da API Alby via UDS Proxy")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--secret-path", type=Path, default=DEFAULT_SECRET)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start-proxy")
    
    issue_parser = sub.add_parser("issue")
    issue_parser.add_argument("--memo", default="tcc:tcc2-real-test")

    check_parser = sub.add_parser("check")
    check_parser.add_argument("payment_hash")

    sub.add_parser("status")

    args = parser.parse_args()
    sock_path = args.run_dir / "proxy.sock"
    outbox_path = args.run_dir / "outbox.sqlite3"
    pid_path = args.run_dir / "proxy.pid"

    if args.command == "start-proxy":
        cmd_start_proxy(args.run_dir, args.secret_path, outbox_path, sock_path, pid_path, allowed_uid=os.getuid())
    elif args.command == "issue":
        res = cmd_issue(sock_path, args.memo)
        print(json.dumps(res, indent=2))
    elif args.command == "check":
        res = cmd_check(sock_path, args.payment_hash)
        print(json.dumps(res, indent=2))
    elif args.command == "status":
        box = Outbox(outbox_path)
        print(json.dumps(box.orphan_summary(), indent=2))


if __name__ == "__main__":
    main()
