import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

# Resolution order: the `-x dsn=...` CLI argument, then the DATABASE_URL
# environment variable, then the placeholder in alembic.ini. alembic.ini
# must never contain a real DSN with a password, so DATABASE_URL (or -x dsn)
# is required outside of local template inspection.
MIGRATION_DSN = (
    context.get_x_argument(as_dictionary=True).get("dsn")
    or os.environ.get("DATABASE_URL")
    or config.get_main_option("sqlalchemy.url")
)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(MIGRATION_DSN)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await engine.dispose()


asyncio.run(run_migrations_online())
