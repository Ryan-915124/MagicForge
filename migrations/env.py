from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from persistence.models import Base
import persistence.storage_models as _storage_models  # noqa: F401


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _database_url() -> str:
    configured = config.attributes.get("database_url")
    if configured is None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        configured = os.getenv("DATABASE_URL")
    if not configured:
        raise RuntimeError(
            "DATABASE_URL is required for Alembic migrations; "
            "Production migrations do not fall back to SQLite."
        )

    url = make_url(str(configured))
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("Alembic migrations require a PostgreSQL DATABASE_URL.")
    return url.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Escaping percent signs prevents ConfigParser interpolation from treating
    # percent-encoded URL components as configuration substitutions.
    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
