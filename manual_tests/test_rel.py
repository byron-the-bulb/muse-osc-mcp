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
from muse_osc_mcp.models import Session, FrequencyAbsoluteSample


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
    result = None
    async with session_factory() as session:
        result = await session.get(FrequencyAbsoluteSample, 3)

    if not result:
        logger.info("No samples found in the database.")
    else:
        logger.info("Sample 3 is session id: %d", result.session_id)




if __name__ == "__main__":
    asyncio.run(list_sessions())