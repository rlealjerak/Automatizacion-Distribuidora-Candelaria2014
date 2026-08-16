from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# alembic/ sits next to src/ under backend/ - add src/ to the path the same
# way pyproject.toml's pytest config does, so this works without the
# package being pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adc_backend.config import get_database_url  # noqa: E402
from adc_backend.db import models  # noqa: E402, F401 - registers all tables on Base.metadata
from adc_backend.db.base import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# config.set_main_option() stores this through a configparser.ConfigParser,
# which by default treats a literal "%" as the start of interpolation
# syntax ("%(name)s") and raises ValueError on anything else - and the
# real RDS-generated master password, once percent-encoded by
# get_database_url()'s quote_plus(), is virtually guaranteed to contain
# "%XX" sequences. Never caught locally (DATABASE_URL's hardcoded dev
# password has no special characters to encode) - found live on the
# first real deploy against actual RDS credentials (2026-08-16), where
# it crashed migrations before the app ever started. "%%" is
# configparser's own escape for a literal "%".
config.set_main_option("sqlalchemy.url", get_database_url().replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
