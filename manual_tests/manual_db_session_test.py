"""Manual test script to verify DB connectivity and list all sessions.

This script initialises the global async SQLAlchemy engine using the same
pattern as ``mcp.py`` then fetches and prints every row from the
``sessions`` table. It is intended for quick interactive checks and does
NOT run inside the pytest suite.

Usage
-----
    python manual_tests/manual_db_session_test.py

It relies on environment variables from ``.env`` (or ``.env.test`` if you
modify the ``APP_ENV_FILE`` environment variable before running).
"""
from __future__ import annotations

import asyncio
import os
import logging

from sqlalchemy import select

# Ensure the correct .env file is loaded *before* importing settings
# (mirrors the hack in manual_osc_live_test.py and mcp.py)
os.environ.setdefault("APP_ENV_FILE", ".env")

from muse_osc_mcp.db import (
    initialize_global_db,
    get_async_session_factory,
)
from muse_osc_mcp.models import Session


async def list_sessions() -> None:
    """Initialise DB engine, query ``sessions`` table and print rows."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    # Initialise global engine / sessionmaker (no-op if already done)
    initialize_global_db()

    session_factory = get_async_session_factory()

    # Acquire a session and run the query
    async with session_factory() as session:
        result = await session.execute(select(Session))
        sessions = result.scalars().all()

    if not sessions:
        logger.info("No sessions found in the database.")
    else:
        logger.info("Fetched %d sessions from the database:", len(sessions))
        for s in sessions:
            # Rely on Session.__repr__ implementation for concise output
            logger.info("  %s", s)


if __name__ == "__main__":
    asyncio.run(list_sessions())
