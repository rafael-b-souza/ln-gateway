"""Migrações SQL versionadas do gateway."""

from importlib.resources import files

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def database_engine(url: str):
    """Cria engine SQLAlchemy validando restrições de ambiente."""
    if url.startswith("sqlite:///"):
        return create_engine(url, connect_args={"timeout": 10})
    if url.startswith("postgresql+psycopg://"):
        parsed = make_url(url)
        if parsed.host != "127.0.0.1" or parsed.port != 55432 \
                or parsed.database not in ("ln_gateway", "ln_gateway_test", "tcc_ln_gateway", "tcc_ln_gateway_test") \
                or parsed.username != parsed.database or parsed.password is not None:
            raise ValueError("PostgreSQL somente 127.0.0.1, porta local autorizada, papel/banco dedicados e senha fora da URL")
        return create_engine(url, connect_args={"connect_timeout": 10,
            "options": "-c statement_timeout=10000 -c lock_timeout=5000"})
    raise ValueError("Somente SQLite local e PostgreSQL/psycopg são aceitos")


def migrate(engine):
    """Executa migrações SQL pendentes em ordem sequencial."""
    dialect = engine.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise ValueError("dialeto inválido")
    with engine.begin() as connection:
        if dialect == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(18482291)"))
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"))
        versions = connection.execute(text("SELECT version FROM schema_version ORDER BY version")).scalars().all()
        from sqlalchemy import inspect
        if versions not in ([], [1], [1, 2], [1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5]):
            raise RuntimeError("Versão de schema desconhecida")
        tables = set(inspect(connection).get_table_names())
        expected_v1 = {"accounts", "challenges", "debits"}
        expected_v2 = expected_v1 | {"deposits", "credits"}
        if versions == [] and tables - {"schema_version"}:
            raise RuntimeError("Banco não vazio sem versão: não migrar schema desconhecido")
        if versions == [1] and not expected_v1.issubset(tables):
            raise RuntimeError("Schema v1 incompleto")
        if versions == [1] and tables & {"deposits", "credits"}:
            raise RuntimeError("Schema v2 parcial sem versão")
        if versions in ([1, 2], [1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5]):
            if not expected_v2.issubset(tables):
                raise RuntimeError("Schema v2 incompleto")
            columns = {c["name"] for c in inspect(connection).get_columns("deposits")}
            if versions in ([1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5]):
                if "wallet_id" not in columns:
                    raise RuntimeError("Schema v3 incompleto")
                if len(versions) >= 4:
                    if "invoice_attempts" not in tables:
                        raise RuntimeError("Schema v4 incompleto")
                    if len(versions) == 5:
                        if "usage_events" not in tables:
                            raise RuntimeError("Schema v5 incompleto")
                        return
                    if "usage_events" in tables:
                        raise RuntimeError("Schema v5 parcial sem versão")
                elif "invoice_attempts" in tables:
                    raise RuntimeError("Schema v4 parcial sem versão")
            if versions == [1, 2] and "wallet_id" in columns:
                raise RuntimeError("Schema v3 parcial sem versão")
        if len(versions) < 4 and "usage_events" in tables:
            raise RuntimeError("Schema v5 parcial sem versão")
        start = len(versions) + 1
        for version in range(start, 6):
            script = files("ln_gateway.migrations").joinpath(f"{version:03d}_{dialect}.sql").read_text()
            for statement in script.split(";\n"):
                if statement.strip():
                    connection.execute(text(statement.strip().rstrip(";")))
            connection.execute(text("INSERT INTO schema_version (version) VALUES (:version)"), {"version": version})


def main():
    """Executa migrações no banco indicado por GATEWAY_DATABASE_URL."""
    import os
    url = os.getenv("GATEWAY_DATABASE_URL")
    if not url:
        raise SystemExit("Defina GATEWAY_DATABASE_URL para banco dedicado")
    migrate(database_engine(url))


if __name__ == "__main__":
    main()
