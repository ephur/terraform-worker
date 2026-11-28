from alembic import context
from sqlalchemy import engine_from_config, pool

from tfworker_console.db import Base

config = context.config
target_metadata = Base.metadata

if context.is_offline_mode():  # pragma: no cover
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = config.attributes.get("connection")
    if connectable is None:  # pragma: no cover
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        ).connect()
    context.configure(connection=connectable, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
