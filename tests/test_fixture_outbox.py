"""Testes da outbox SQLite para persistência e recuperação de tentativas."""

import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time

import pytest

LAB = Path(__file__).parents[1] / "lab"
sys.path.insert(0, str(LAB))
from fixture_outbox import Outbox, initialize


def test_initialize_permissions_and_schema(tmp_path):
    """Valida permissões restritas e schema na inicialização."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    db_path = private / "outbox.sqlite3"
    initialize(db_path)
    assert db_path.is_file()
    assert (db_path.stat().st_mode & 0o777) == 0o600

    # Não permite reinicializar sobre arquivo existente
    with pytest.raises(ValueError, match="já existente"):
        initialize(db_path)

    # Não permite inicializar em diretório aberto a group/other
    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o755)
    with pytest.raises(ValueError, match="não privado"):
        initialize(public / "outbox.sqlite3")


def test_outbox_startup_crash_recovery(tmp_path):
    """Ao reabrir o banco, tentativas issuing pendentes passam para uncertain."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    db_path = private / "outbox.sqlite3"
    initialize(db_path)

    outbox1 = Outbox(db_path)
    assert outbox1.reserve("tcc:test-crash-1", 4) is True
    # Deixado deliberadamente em 'issuing' como se processo sofresse SIGKILL
    del outbox1

    outbox2 = Outbox(db_path)
    entries = outbox2.entries()
    assert len(entries) == 1
    assert entries[0] == ("tcc:test-crash-1", 4, "uncertain", None)
    # Cota consumida de 4 sats permanece; restante é 6
    summary = outbox2.orphan_summary()
    assert summary["consumed_sats"] == 4
    assert summary["remaining_sats"] == 6
    assert summary["uncertain_quarantined"] == [{"memo": "tcc:test-crash-1", "amount": 4}]


def test_outbox_ceiling_and_memo_validation(tmp_path):
    """Cota agregada máxima de 10 sats e validação estrita de memo."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    db_path = private / "outbox.sqlite3"
    initialize(db_path)

    outbox = Outbox(db_path)
    # Validação de formato de memo
    assert outbox.reserve("invalido", 5) is False
    assert outbox.reserve("tcc:", 5) is False
    assert outbox.reserve("tcc:with spaces", 5) is False
    assert outbox.reserve("tcc:memo", 0) is False
    assert outbox.reserve("tcc:memo", 11) is False
    assert outbox.reserve("tcc:memo", 5.5) is False

    # Reservas válidas somando até 10
    assert outbox.reserve("tcc:batch-1", 6) is True
    assert outbox.reserve("tcc:batch-2", 4) is True
    # Excede o teto de 10 sats
    assert outbox.reserve("tcc:batch-3", 1) is False
    # Memo duplicado é rejeitado
    assert outbox.reserve("tcc:batch-1", 1) is False


def test_outbox_mark_state_transitions(tmp_path):
    """Transições estritas: issuing -> recorded (com hash) ou issuing -> uncertain."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    db_path = private / "outbox.sqlite3"
    initialize(db_path)

    outbox = Outbox(db_path)
    assert outbox.reserve("tcc:item-rec", 5) is True
    # Recorded sem hash válido deve falhar
    with pytest.raises(ValueError, match="hash inválido"):
        outbox.mark("tcc:item-rec", "recorded", "not-a-hash")
    with pytest.raises(ValueError, match="hash inválido"):
        outbox.mark("tcc:item-rec", "recorded", "a" * 63)

    valid_hash = "c" * 64
    outbox.mark("tcc:item-rec", "recorded", valid_hash)

    # Transição a partir de recorded é proibida
    with pytest.raises(ValueError, match="transição inesperada"):
        outbox.mark("tcc:item-rec", "uncertain")

    # Item incerto
    assert outbox.reserve("tcc:item-unc", 3) is True
    outbox.mark("tcc:item-unc", "uncertain")
    with pytest.raises(ValueError, match="transição inesperada"):
        outbox.mark("tcc:item-unc", "recorded", "d" * 64)

    summary = outbox.orphan_summary()
    assert summary["recorded"] == [{"memo": "tcc:item-rec", "amount": 5, "payment_hash": valid_hash}]
    assert summary["uncertain_quarantined"] == [{"memo": "tcc:item-unc", "amount": 3}]
    assert summary["consumed_sats"] == 8
    assert summary["remaining_sats"] == 2


def _worker_reserve_and_hang(db_path, memo, amount, sync_event):
    """Processo filho que reserva e trava aguardando SIGKILL."""
    outbox = Outbox(db_path)
    res = outbox.reserve(memo, amount)
    if res:
        sync_event.set()
        time.sleep(30)


def test_outbox_sigkill_recovery(tmp_path):
    """SIGKILL durante emissão mantém a tentativa persistida para quarentena após reinício."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    db_path = private / "outbox.sqlite3"
    initialize(db_path)

    sync_event = multiprocessing.Event()
    proc = multiprocessing.Process(target=_worker_reserve_and_hang,
                                   args=(str(db_path), "tcc:sigkill-item", 7, sync_event))
    proc.start()
    try:
        assert sync_event.wait(timeout=5)
        # Processo garantiu a reserva em disco; mata imediatamente com SIGKILL
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=3)
    finally:
        if proc.is_alive():
            proc.kill()

    # Reabre com novo processo/instância
    recovered = Outbox(db_path)
    summary = recovered.orphan_summary()
    assert summary["uncertain_quarantined"] == [{"memo": "tcc:sigkill-item", "amount": 7}]
    assert summary["consumed_sats"] == 7
    assert summary["remaining_sats"] == 3
    # Tentativa de reutilizar memo do processo morto é negada
    assert recovered.reserve("tcc:sigkill-item", 1) is False
    # Tentativa de ultrapassar teto de 10 (7 + 4 = 11) é negada
    assert recovered.reserve("tcc:other-item", 4) is False
    # Tentativa dentro do restante (3) é permitida
    assert recovered.reserve("tcc:other-item", 3) is True
