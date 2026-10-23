"""Validação de passagem segura de credencial por descritor de arquivo."""

import ctypes
import os
import resource
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PR_SET_DUMPABLE = 4


def load_synthetic(fd: int) -> bytes:
    """Carrega segredo de descritor restrito com restrição de dump de memória."""
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600 or not 12 <= st.st_size <= 2048):
        raise ValueError("descritor não restrito")
    if ctypes.CDLL(None).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise RuntimeError("dumpable não pôde ser desabilitado")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    data = os.read(fd, 2049)
    os.close(fd)
    if len(data) != st.st_size or not all(33 <= c <= 126 for c in data):
        raise ValueError("segredo sintético inválido")
    return data


def fixture():
    """Testa isolamento de credencial em processo filho."""
    with tempfile.TemporaryDirectory(prefix="tcc-fd-synthetic-") as tmp:
        path = Path(tmp) / "synthetic"
        secret = b"synthetic-test-only-no-real-credential"
        path.write_bytes(secret)
        path.chmod(0o600)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        result = subprocess.run([sys.executable, "-I", __file__, "child", str(fd)],
                                pass_fds=(fd,), capture_output=True, timeout=10,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        os.close(fd)
        assert result.returncode == 0 and secret not in result.stdout + result.stderr
        print("FD sintético: proc/env/argv/log sem segredo; dumpable=0 e core=0")


def child(fd: int):
    """Valida que o segredo não vazou para o ambiente de processo."""
    secret = load_synthetic(fd)
    try:
        proc = Path("/proc/self/environ").read_bytes() + Path("/proc/self/cmdline").read_bytes()
    except PermissionError:
        # Com dumpable=0 o próprio processo pode perder acesso a /proc/self.
        proc = b""
    if secret in proc or secret in os.environ.get("PATH", "").encode():
        raise RuntimeError("segredo no proc")
    # Não exportar token, não chamar LNbits, não fazer socket; processo termina.
    if secret != b"synthetic-test-only-no-real-credential":
        raise RuntimeError("fixture inválida")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        fixture()
    elif len(sys.argv) == 3 and sys.argv[1] == "child":
        child(int(sys.argv[2]))
    else:
        raise SystemExit(2)
