# Database development

MagicForge Production governance uses PostgreSQL through SQLAlchemy 2.x and
psycopg 3. Alembic is the only supported Production migration path. There is
no SQLite fallback and application startup must not create or reset tables.
Production readiness verifies both `SELECT 1` and the expected Alembic head;
a reachable but missing or outdated schema remains unavailable to the API.

## Local PostgreSQL

The repository includes `docker-compose.dev.yml` with an explicitly local-only
credential and a loopback-only port binding. After Docker Desktop WSL
integration is enabled:

```bash
docker compose -f docker-compose.dev.yml up -d postgres
docker compose -f docker-compose.dev.yml ps
```

The matching example `DATABASE_URL` is documented in `.env.example`. Replace
it with secret-managed credentials outside local development.

## Migrations

With `DATABASE_URL` exported or present in the repository `.env` file:

```bash
alembic upgrade head
alembic current
```

Downgrade exists to verify an empty disposable development database. Production
rollbacks use a database backup plus a forward-fix migration; never run an
automatic destructive reset or downgrade on Production data.

## Migration integration test

The migration test creates and drops a uniquely named PostgreSQL schema. Its
database user therefore needs `CREATE` privilege:

```bash
TEST_DATABASE_URL=postgresql+psycopg://... pytest -q tests/test_database_migrations.py
```

Without `TEST_DATABASE_URL`, the PostgreSQL integration test is reported as
skipped. It never falls back to SQLite because that would hide PostgreSQL JSONB,
foreign-key, trigger, and transactional-DDL behavior.
