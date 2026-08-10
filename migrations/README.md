# MagicForge database migrations

Alembic is the only Production schema-management mechanism. Application
startup must never call `metadata.create_all()`.

The migration environment reads `DATABASE_URL`. Tests may pass a separate URL
through Alembic's in-process configuration without changing the environment.
