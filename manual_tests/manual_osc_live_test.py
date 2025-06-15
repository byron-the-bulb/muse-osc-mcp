import os
# Set the environment variable to point to the test .env file
# This MUST be done before importing 'settings' from config
os.environ["APP_ENV_FILE"] = ".env"

import asyncio
import logging
import signal

from muse_osc_mcp.config import settings
from muse_osc_mcp.db import (
    initialize_global_db,
    get_engine,
    warm_up_connection_pool,
    init_db,
    dispose_global_engine, # Corrected import
    get_async_session_factory,
)
from muse_osc_mcp.server import (
    create_recording_session,
    start_osc_server,
    batch_commit_data,
    RecordingSession, # Import RecordingSession for type hinting if needed
)

# Global event to signal shutdown
stop_event = asyncio.Event()


def handle_shutdown_signal(signame: str) -> None:
    print(f"\nCaught signal {signame}. Initiating shutdown...")
    stop_event.set()


async def run_live_test() -> None:
    logging.basicConfig(
        level=logging.DEBUG,  # Capture DEBUG logs for buffer sizes etc.
        format="%(asctime)s %(levelname)s %(name)s [%(funcName)s]: %(message)s",
    )
    logger = logging.getLogger(__name__) # Logger for this script

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, handle_shutdown_signal, s.name)

    osc_server_task: asyncio.Task | None = None
    batch_commit_task: asyncio.Task | None = None

    try:
        logger.info("Initializing global database resources...")
        initialize_global_db()

        logger.info("Getting database engine...")
        engine = get_engine()
        if not engine:
            logger.critical("Failed to get database engine. Exiting.")
            return

        logger.info(
            f"Warming up database connection pool (target: {settings.db_pool_warmup_connections} connections)..."
        )
        await warm_up_connection_pool(
            engine, num_connections=settings.db_pool_warmup_connections
        )
        logger.info("Database connection pool warming complete.")


        logger.info("Creating new recording session...")
        rec_session: RecordingSession = await create_recording_session()
        # TODO: If user/description are needed, modify create_recording_session or set them post-creation
        logger.info(
            f"Recording session created: ID {rec_session.id}, User: {rec_session.user}"
        )

        session_factory = get_async_session_factory()
        if not session_factory:
            logger.critical("Failed to get async session factory. Exiting.")
            return

        logger.info(
            f"Starting batch commit task (interval: {settings.batch_interval_seconds}s)..."
        )
        batch_commit_task = asyncio.create_task(
            batch_commit_data(
                session_factory=session_factory,
                interval_seconds=settings.batch_interval_seconds,
            )
        )

        logger.info(f"Starting OSC server on port {settings.osc_port}...")
        osc_server_task = asyncio.create_task(
            start_osc_server(
                port=settings.osc_port,
                rec_session=rec_session,
                loop=loop,
                session_factory=session_factory,
            )
        )

        print("\n" + "=" * 60)
        print("OSC Server is LIVE and listening for Muse data.")
        print(f"Recording to Session ID: {rec_session.id}")
        print(f"OSC Port: {settings.osc_port}")
        print("Data will be batched and committed to the database.")
        print("Logs (DEBUG level) will show buffer sizes and commit timings.")
        print("Press Ctrl+C to stop the server and test.")
        print("=" * 60 + "\n")

        # Wait for the stop_event to be set by the signal handler
        await stop_event.wait()

    except asyncio.CancelledError:
        logger.info("Live test main task cancelled.")
    except Exception as e:
        logger.error(f"An unexpected error occurred in live test: {e}", exc_info=True)
    finally:
        logger.info("Initiating shutdown sequence...")

        if osc_server_task and not osc_server_task.done():
            logger.info("Cancelling OSC server task...")
            osc_server_task.cancel()
            try:
                await osc_server_task
            except asyncio.CancelledError:
                logger.info("OSC server task successfully cancelled.")
            except Exception as e_osc:
                logger.error(
                    f"Error during OSC server task cancellation: {e_osc}",
                    exc_info=True,
                )

        if batch_commit_task and not batch_commit_task.done():
            logger.info("Cancelling batch commit task...")
            batch_commit_task.cancel()
            try:
                await batch_commit_task
            except asyncio.CancelledError:
                logger.info("Batch commit task successfully cancelled.")
            except Exception as e_batch:
                logger.error(
                    f"Error during batch commit task cancellation: {e_batch}",
                    exc_info=True,
                )

        logger.info("Disposing global database engine...")
        await dispose_global_engine() # Corrected function call
        logger.info("Shutdown complete. Goodbye!")


if __name__ == "__main__":
    asyncio.run(run_live_test())
