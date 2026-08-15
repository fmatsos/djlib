import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context
from djlib.config import DjlibConfig
from djlib.db import models  # noqa: F401  registers ORM models on Base.metadata
from djlib.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    config_path = os.environ.get('DJLIB_CONFIG')
    djlib_config = DjlibConfig.load(Path(config_path)) if config_path else DjlibConfig.defaults()
    return djlib_config.database_url


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration['sqlalchemy.url'] = _database_url()
    connectable = engine_from_config(
        configuration, prefix='sqlalchemy.', poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
