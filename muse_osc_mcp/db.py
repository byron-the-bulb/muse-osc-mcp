"""Database utilities: engine and session factory."""
from __future__ import annotations

import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from enrichmcp.sqlalchemy import EnrichSQLAlchemyMixin
from sqlalchemy import MetaData
from logging import getLogger

from .config import settings

logger = getLogger(__name__)

# Use naming convention for alembic friendliness
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)


class Base(DeclarativeBase, EnrichSQLAlchemyMixin):
    metadata = metadata  # type: ignore[assignment]


_engine: create_async_engine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None

def initialize_global_db() -> None:
    """Initializes the global engine and session factory if they haven't been already."""
    global _engine, _async_session_factory
    if _engine is None:
        _engine = create_async_engine(
            settings.async_db_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=15,
            max_overflow=5,
        )
        _async_session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

def get_engine() -> create_async_engine:
    """Returns the global async engine. Assumes initialize_global_db() has been called."""
    if _engine is None:
        raise RuntimeError(
            "Global engine is not initialized. Call initialize_global_db() first."
        )
    return _engine

def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Returns the global async session factory. Assumes initialize_global_db() has been called."""
    if _async_session_factory is None:
        raise RuntimeError(
            "Global async session factory is not initialized. Call initialize_global_db() first."
        )
    return _async_session_factory

async def dispose_global_engine() -> None:
    """Dispose of the global async engine."""
    global _engine, _async_session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None


async def warm_up_connection_pool(engine: create_async_engine, num_connections: int = 5) -> None:
    """
    Warms up the connection pool by acquiring and using a specified number of connections.
    """
    if not engine:
        # Or raise an error, or log a warning
        logger.warning("Engine not available for pool warming.")
        return

    logger.info(f"Warming up {num_connections} connections in the pool...")
    warmup_tasks = []
    for i in range(num_connections):
        async def _warm_conn(idx: int):
            try:
                async with engine.connect() as conn:
                    await conn.execute(select(1))
            except Exception as e:
                logger.warning(f"Error warming up connection {idx+1}: {e}")
        warmup_tasks.append(_warm_conn(i))
    
    await asyncio.gather(*warmup_tasks)
    logger.info(f"Connection pool warming completed for {num_connections} connections.")


async def init_db() -> None:
    """Drop and re-create tables (dev/test only). In production use Alembic migrations."""
    # Safeguard to prevent running in production
    if getattr(settings, "app_env", "dev").lower() == "production":
        raise RuntimeError(
            "Refusing to drop and re-create tables in a production environment. "
            'Use Alembic migrations for production schema changes.'
        )

    current_engine = await get_engine()
    async with current_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
