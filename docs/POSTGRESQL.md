# PostgreSQL & TimescaleDB Configuration

This guide describes how to configure and run the isolated PostgreSQL instance for `ln-gateway` development and automated integration testing.

---

## 1. Local Database Environment

The gateway requires standard PostgreSQL 15+ (with optional TimescaleDB extension for analytical metrics).

### Default Connection Parameters
- **Host**: `127.0.0.1`
- **Port**: `55432` (or standard `5432`)
- **Database**: `ln_gateway` (application) / `ln_gateway_test` (test suite)

---

## 2. Managing the Local Cluster

### Starting the Database Instance
```bash
/usr/lib/postgresql/16/bin/pg_ctl \
  -D .postgres-data \
  -o "-p 55432 -k $PWD/.postgres-run -c listen_addresses=127.0.0.1" \
  -l .postgres-data/logfile \
  start
```

### Stopping the Instance
```bash
/usr/lib/postgresql/16/bin/pg_ctl \
  -D .postgres-data \
  stop
```

---

## 3. Running Automated Tests

To run the complete test suite against the PostgreSQL engine:

```bash
export PGPASSFILE="$PWD/.postgres-run/pgpass"
export GATEWAY_TEST_DATABASE_URL='postgresql+psycopg://ln_gateway_test@127.0.0.1:55432/ln_gateway_test'
PYTHONPATH=src pytest -v
```
