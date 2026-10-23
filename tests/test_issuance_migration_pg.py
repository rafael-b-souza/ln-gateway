"""Testes de migração de esquema v4 e comportamento de rollback em PostgreSQL."""

import os
import secrets

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ln_gateway.migrate import migrate
from ln_gateway.storage import make_sessions


def test_pg_v4_idempotent_and_rollback():
    """Valida idempotência das migrações e atomicidade em falha de transação."""
    url = os.getenv("TCC_TEST_DATABASE_URL")
    if not url:
        pytest.skip("cluster exclusivo não configurado")
    parsed = make_url(url)
    assert (parsed.drivername, parsed.host, parsed.port, parsed.database, parsed.username,
            parsed.password) == ("postgresql+psycopg", "127.0.0.1", 55432,
                                 "tcc_ln_gateway_test", "tcc_ln_gateway_test", None)
    sessions = make_sessions(url)
    engine = sessions.kw["bind"]
    account = "rollback_" + secrets.token_hex(8)
    try:
        migrate(engine)
        migrate(engine)
        with engine.connect() as db:
            assert db.execute(text("SELECT version FROM schema_version ORDER BY version")).scalars().all() == [1, 2, 3, 4, 5]
            assert db.execute(text("SELECT to_regclass('invoice_attempts')")).scalar_one() == "invoice_attempts"
        with pytest.raises(RuntimeError):
            with engine.begin() as db:
                db.execute(text("INSERT INTO accounts(id, public_key, balance) VALUES (:id, :pub, 0)"),
                           {"id": account, "pub": secrets.token_hex(32)})
                db.execute(text("INSERT INTO invoice_attempts(account_id, request_id, amount, wallet_id, state, created_at) "
                                "VALUES (:id, 'aborted', 10, 'fake-demo-wallet', 'issuing', now())"),
                           {"id": account})
                raise RuntimeError("falha antes de commit")
        with engine.connect() as db:
            assert db.execute(text("SELECT count(*) FROM accounts WHERE id=:id"), {"id": account}).scalar_one() == 0
            assert db.execute(text("SELECT count(*) FROM invoice_attempts WHERE account_id=:id"),
                              {"id": account}).scalar_one() == 0
    finally:
        engine.dispose()
