"""Outbox SQLite para persistência e controle de tentativas de emissão."""

import os
from pathlib import Path
import re
import sqlite3

SCHEMA = """CREATE TABLE metadata(version INTEGER NOT NULL CHECK(version=1), ceiling INTEGER NOT NULL CHECK(ceiling=10));
CREATE TABLE attempts(memo TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount BETWEEN 1 AND 10),
 state TEXT NOT NULL CHECK(state IN ('issuing','uncertain','recorded')),
 payment_hash TEXT UNIQUE CHECK(payment_hash IS NULL OR length(payment_hash)=64));"""


def _connect(path):
    """Abre conexão SQLite em modo leitura e escrita."""
    db = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=5, isolation_level=None)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        db.close()
        raise ValueError("journal inesperado")
    return db


def initialize(path):
    """Inicializa banco SQLite da outbox."""
    path = Path(path)
    if path.exists() or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
        raise ValueError("arquivo já existente ou diretório não privado")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with _connect(path) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO metadata VALUES(1,10)")
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class Outbox:
    """Gerenciador de tentativas de emissão com cota limitada."""

    def __init__(self, path):
        """Abre outbox e transiciona reservas incompletas para quarentena."""
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid() \
                or path.stat().st_mode & 0o077:
            raise ValueError("ledger ausente/insegura")
        self.path = path
        with _connect(path) as db:
            if (db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or db.execute("SELECT version, ceiling FROM metadata").fetchall() != [(1, 10)]
                    or {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} !=
                    {"metadata", "attempts"}):
                raise ValueError("schema/ledger inválida")
            # Crash entre commit de reserva e resposta -> incerto.
            db.execute("UPDATE attempts SET state='uncertain' WHERE state='issuing'")

    def reserve(self, memo, amount):
        """Reserva cota para emissão de fatura."""
        if not isinstance(memo, str) or not re.fullmatch(r"tcc:[A-Za-z0-9_-]{1,64}", memo) \
                or type(amount) is not int or not 1 <= amount <= 10:
            return False
        with _connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if (db.execute("SELECT 1 FROM attempts WHERE memo=?", (memo,)).fetchone()
                    or db.execute("SELECT COALESCE(SUM(amount),0) FROM attempts").fetchone()[0] + amount > 10):
                db.rollback()
                return False
            db.execute("INSERT INTO attempts(memo,amount,state) VALUES(?,?,'issuing')", (memo, amount))
            db.commit()
        return True

    def mark(self, memo, state, payment_hash=None):
        """Atualiza estado final da tentativa de emissão."""
        if state not in ("uncertain", "recorded"):
            raise ValueError("estado proibido")
        if state == "recorded" and (not isinstance(payment_hash, str)
                                     or not re.fullmatch(r"[0-9a-f]{64}", payment_hash)):
            raise ValueError("hash inválido")
        with _connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM attempts WHERE memo=?", (memo,)).fetchone()
            if row != ("issuing",):
                db.rollback()
                raise ValueError("transição inesperada")
            db.execute("UPDATE attempts SET state=?,payment_hash=? WHERE memo=?",
                       (state, payment_hash, memo))
            db.commit()

    def entries(self):
        """Retorna lista de tentativas registradas."""
        with _connect(self.path) as db:
            return db.execute("SELECT memo,amount,state,payment_hash FROM attempts ORDER BY memo").fetchall()

    def orphan_summary(self):
        """Retorna resumo de faturas gravadas, em quarentena e saldo restante."""
        with _connect(self.path) as db:
            recorded = db.execute("SELECT memo,amount,payment_hash FROM attempts WHERE state='recorded'").fetchall()
            uncertain = db.execute("SELECT memo,amount FROM attempts WHERE state='uncertain'").fetchall()
            consumed = db.execute("SELECT COALESCE(SUM(amount),0) FROM attempts").fetchone()[0]
            ceiling = db.execute("SELECT ceiling FROM metadata").fetchone()[0]
            return {
                "recorded": [{"memo": r[0], "amount": r[1], "payment_hash": r[2]} for r in recorded],
                "uncertain_quarantined": [{"memo": u[0], "amount": u[1]} for u in uncertain],
                "consumed_sats": consumed,
                "ceiling_sats": ceiling,
                "remaining_sats": max(0, ceiling - consumed),
            }
