"""Habilita TimescaleDB SOMENTE no banco de teste dedicado; sem sudo nem servidor 5432.

Exige extensão previamente instalada no PostgreSQL 16 e aprovada para o cluster
isolado. Não altera roles, shared_preload_libraries nem ambientes externos.
"""

import os
from sqlalchemy import text
from sqlalchemy.engine import make_url
from ln_gateway.migrate import database_engine, migrate


def main():
    url = os.environ.get("TCC_TEST_DATABASE_URL", "")
    parsed = make_url(url) if url else None
    if (parsed is None or parsed.drivername != "postgresql+psycopg"
            or parsed.host != "127.0.0.1" or parsed.port != 55432
            or parsed.username != "tcc_ln_gateway_test" or parsed.database != "tcc_ln_gateway_test"
            or parsed.password is not None):
        raise SystemExit("Habilitação exclusiva para tcc_ln_gateway_test em 127.0.0.1:55432")
    engine = database_engine(url)
    migrate(engine)
    try:
        with engine.begin() as db:
            available = db.execute(text("SELECT 1 FROM pg_available_extensions WHERE name='timescaledb'")).scalar_one_or_none()
            if available != 1:
                raise RuntimeError("TimescaleDB não instalada no cluster dedicado; nada foi alterado")
            db.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            db.execute(text("SELECT create_hypertable('usage_events', 'occurred_at', "
                            "if_not_exists => TRUE, migrate_data => TRUE)"))
            value = db.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables "
                                    "WHERE hypertable_name = 'usage_events'")).scalar_one()
            print("TimescaleDB ativada para métricas no banco de teste:", value)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
