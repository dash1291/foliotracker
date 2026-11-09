import os
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool

from foliotracker.db import Base, Money

config = context.config
if config.config_file_name is not None and os.path.exists(config.config_file_name):
    fileConfig(config.config_file_name)

db_path = config.get_main_option("foliotracker_db") or os.environ.get("FOLIOTRACKER_DB", "data/foliotracker.db")
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, Money):
        return "sa.String(length=40)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_item=render_item,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_item=render_item, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
