"""Async OSC receiver that ingests Muse data into PostgreSQL via SQLAlchemy.

Usage::

    uvicorn muse_osc_mcp.server:app  # if exposing via ASGI (future)

For now run directly::

    python -m muse_osc_mcp.server

This will:
1. Connect to the Postgres database using asyncpg
2. Create a new `RecordingSession` row (one per runtime invocation)
3. Start an OSC UDP server on `settings.osc_port`
4. Insert incoming messages into their corresponding tables.

"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import logging
from typing import Any, Callable, Coroutine, Final, List

from aiohttp import web
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer

from .config import settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select
from .db import get_async_session_factory, init_db, initialize_global_db, dispose_global_engine, get_engine, warm_up_connection_pool

from .models import (
    AccelerometerSample,
    BlinkEvent,
    EegSample,
    FrequencyAbsoluteSample,
    HorseshoeSample,
    JawClenchEvent,
    Session as RecordingSession, # Alias to avoid conflict with SQLAlchemy Session
    TouchingForeheadSample,
)

logger = logging.getLogger(__name__)

# Data buffers for batching
eeg_samples_buffer: List[EegSample] = []
acc_samples_buffer: List[AccelerometerSample] = []
freq_abs_samples_buffer: List[FrequencyAbsoluteSample] = []
horseshoe_samples_buffer: List[HorseshoeSample] = []
touching_forehead_samples_buffer: List[TouchingForeheadSample] = []
blink_events_buffer: List[BlinkEvent] = []
jaw_clench_events_buffer: List[JawClenchEvent] = []

# Batch commit task handle
batch_commit_task_handle: asyncio.Task | None = None


async def batch_commit_data(session_factory: async_sessionmaker[AsyncSession], interval_seconds: float):
    global eeg_samples_buffer, acc_samples_buffer, freq_abs_samples_buffer, horseshoe_samples_buffer, touching_forehead_samples_buffer, blink_events_buffer, jaw_clench_events_buffer
    logger.info(f"Batch commit task started. Commit interval: {interval_seconds}s")
    while True:
        flush_type = "Periodic" if interval_seconds > 0 else "Immediate/Final"
        logger.info(f"{flush_type} flush cycle starting...")

        if interval_seconds > 0:
            await asyncio.sleep(interval_seconds)
        # For immediate flush (interval_seconds == 0), proceed directly to buffer processing

        items_to_commit_eeg: List[EegSample] = []
        items_to_commit_acc: List[AccelerometerSample] = []
        items_to_commit_freq_abs: List[FrequencyAbsoluteSample] = []
        items_to_commit_horseshoe: List[HorseshoeSample] = []
        items_to_commit_touching_forehead: List[TouchingForeheadSample] = []
        items_to_commit_blink: List[BlinkEvent] = []
        items_to_commit_jaw_clench: List[JawClenchEvent] = []

        # Atomically get and clear buffers
        logger.info(f"{flush_type} flush: Checking buffers.")
        if eeg_samples_buffer:
            items_to_commit_eeg = list(eeg_samples_buffer)
            eeg_samples_buffer.clear()
            logger.info(f"{flush_type} flush: Copied {len(items_to_commit_eeg)} EEG samples from buffer.")
        if acc_samples_buffer:
            items_to_commit_acc = list(acc_samples_buffer)
            acc_samples_buffer.clear()
            logger.info(f"{flush_type} flush: Copied {len(items_to_commit_acc)} ACC samples from buffer.")
        if freq_abs_samples_buffer:
            items_to_commit_freq_abs = list(freq_abs_samples_buffer)
            freq_abs_samples_buffer.clear()
            logger.info(f"{flush_type} flush: Copied {len(items_to_commit_freq_abs)} FreqAbs samples from buffer.")
        if horseshoe_samples_buffer:
            items_to_commit_horseshoe = list(horseshoe_samples_buffer)
            horseshoe_samples_buffer.clear()
            logger.info(f"{flush_type} flush: Copied {len(items_to_commit_horseshoe)} Horseshoe samples from buffer.")
        if touching_forehead_samples_buffer:
            items_to_commit_touching_forehead = list(touching_forehead_samples_buffer)
            touching_forehead_samples_buffer.clear()
            logger.info(f"{flush_type} flush: Copied {len(items_to_commit_touching_forehead)} TouchingForehead samples from buffer.")
        if blink_events_buffer:
            items_to_commit_blink = list(blink_events_buffer)
            blink_events_buffer.clear()
            logger.info(f"{flush_type} flush: Copied {len(items_to_commit_blink)} Blink events from buffer.")
        if jaw_clench_events_buffer:
            items_to_commit_jaw_clench = list(jaw_clench_events_buffer)
            jaw_clench_events_buffer.clear()
            logger.info(f"{flush_type} flush: Copied {len(items_to_commit_jaw_clench)} JawClench events from buffer.")

        total_items_in_batch = (
            len(items_to_commit_eeg) + len(items_to_commit_acc) + len(items_to_commit_freq_abs) +
            len(items_to_commit_horseshoe) + len(items_to_commit_touching_forehead) +
            len(items_to_commit_blink) + len(items_to_commit_jaw_clench)
        )

        logger.info(f"{flush_type} flush: Total items copied from all buffers: {total_items_in_batch}")
        if not total_items_in_batch:
            if interval_seconds == 0: # If it's an immediate flush, break after checking buffers once
                logger.info("Immediate flush: No data in buffers to commit.")
                break
            logger.info(f"{flush_type} flush: No data in buffers to commit. Continuing to next cycle.")
            # logger.debug("No data in buffers to commit.") # Can be noisy for periodic checks
            continue

        async with session_factory() as db_session:
            try:
                commit_start_time = time.perf_counter()
                num_committed_types = 0

                if items_to_commit_eeg:
                    db_session.add_all(items_to_commit_eeg)
                    num_committed_types +=1
                if items_to_commit_acc:
                    db_session.add_all(items_to_commit_acc)
                    num_committed_types +=1
                if items_to_commit_freq_abs:
                    db_session.add_all(items_to_commit_freq_abs)
                    num_committed_types +=1
                if items_to_commit_horseshoe:
                    db_session.add_all(items_to_commit_horseshoe)
                    num_committed_types +=1
                if items_to_commit_touching_forehead:
                    db_session.add_all(items_to_commit_touching_forehead)
                    num_committed_types +=1
                if items_to_commit_blink:
                    db_session.add_all(items_to_commit_blink)
                    num_committed_types +=1
                if items_to_commit_jaw_clench:
                    db_session.add_all(items_to_commit_jaw_clench)
                    num_committed_types +=1
                
                if num_committed_types > 0:
                    logger.info(f"{flush_type} flush: Attempting to commit {total_items_in_batch} items to DB...")
                    await db_session.commit()
                    db_session.expire_all()
                    commit_duration = time.perf_counter() - commit_start_time
                    logger.info(
                        f"{flush_type} flush: Successfully committed {len(items_to_commit_eeg)} EEG, {len(items_to_commit_acc)} ACC, "
                        f"{len(items_to_commit_freq_abs)} FreqAbs, {len(items_to_commit_horseshoe)} Horseshoe, "
                        f"{len(items_to_commit_touching_forehead)} Touch, {len(items_to_commit_blink)} Blink, "
                        f"{len(items_to_commit_jaw_clench)} Jaw Clench items. Total: {total_items_in_batch} in {commit_duration:.4f}s."
                    )
                    logger.info(
                        f"Batch committed {len(items_to_commit_eeg)} EEG, {len(items_to_commit_acc)} ACC, "
                        f"{len(items_to_commit_freq_abs)} FreqAbs, {len(items_to_commit_horseshoe)} Horseshoe, "
                        f"{len(items_to_commit_touching_forehead)} Touch, {len(items_to_commit_blink)} Blink, "
                        f"{len(items_to_commit_jaw_clench)} Jaw Clench items. Total: {total_items_in_batch} in {commit_duration:.4f}s."
                    )
                else:
                    logger.info(f"{flush_type} flush: All buffers were empty after swapping, no commit needed.")

            except asyncio.CancelledError:
                logger.info(f"{flush_type} flush: Task cancelled during DB operations. Re-buffering {total_items_in_batch} items.")
                # Re-add data to buffers if cancelled mid-operation to prevent data loss
                eeg_samples_buffer = items_to_commit_eeg + eeg_samples_buffer
                acc_samples_buffer = items_to_commit_acc + acc_samples_buffer
                freq_abs_samples_buffer = items_to_commit_freq_abs + freq_abs_samples_buffer
                horseshoe_samples_buffer = items_to_commit_horseshoe + horseshoe_samples_buffer
                touching_forehead_samples_buffer = items_to_commit_touching_forehead + touching_forehead_samples_buffer
                blink_events_buffer = items_to_commit_blink + blink_events_buffer
                jaw_clench_events_buffer = items_to_commit_jaw_clench + jaw_clench_events_buffer
                raise # Re-raise CancelledError to ensure task stops
            except Exception as e:  # pylint: disable=broad-except
                logger.exception(f"{flush_type} flush: EXCEPTION during batch commit: {e}. Data for this batch may be lost.")
        
        if interval_seconds == 0: # If it's an immediate flush, break after one attempt
            logger.info(f"{flush_type} flush: Immediate flush attempt complete. Breaking loop.")
            break

BAND_MAP: Final[dict[str, str]] = {
    "/muse/elements/delta_absolute": "delta",
    "/muse/elements/theta_absolute": "theta",
    "/muse/elements/alpha_absolute": "alpha",
    "/muse/elements/beta_absolute": "beta",
    "/muse/elements/gamma_absolute": "gamma",
}


async def create_recording_session(user: str="unknown", description: str | None = None) -> RecordingSession: # Renamed to avoid confusion
    session_factory = get_async_session_factory()
    async with session_factory() as db_session:
        sess = RecordingSession(user=user, description=description, started_at=dt.datetime.now(dt.timezone.utc))
        db_session.add(sess)
        await db_session.commit()
        await db_session.refresh(sess)
        logger.info("Created recording session id=%s", sess.id)
        return sess

async def end_recording_session(session: RecordingSession) -> None:
    session_factory = get_async_session_factory()
    async with session_factory() as db_session:
        session.ended_at = dt.datetime.now(dt.timezone.utc)
        db_session.add(session) # Add session to the current db_session to track changes
        await db_session.commit() # Persist changes to the database
        await db_session.refresh(session) # Refresh to get the latest state from DB
        logger.info("Ended recording session id=%s", session.id)

async def handle_eeg(osc_address: str, osc_params: tuple, rec_id: int) -> None:
    global eeg_samples_buffer
    handler_start_time = time.perf_counter()

    num_params = len(osc_params)
    if not (4 <= num_params <= 8):
        logger.warning(
            "Received EEG message with incorrect number of parameters. Expected 4-8, got %s. Message: %s %s",
            num_params, osc_address, osc_params,
        )
        return

    # Unpack main channels and up to 4 aux channels
    tp9, af7, af8, tp10, *aux_channels = osc_params
    
    aux_values = [float(v) if v is not None else None for v in aux_channels]
    # Pad with Nones to ensure there are always 4 aux values
    aux_values.extend([None] * (4 - len(aux_values)))

    eeg_sample = EegSample(
        session_id=rec_id, timestamp=dt.datetime.now(dt.timezone.utc),
        tp9=float(tp9), af7=float(af7), af8=float(af8), tp10=float(tp10),
        aux_1=aux_values[0],
        aux_2=aux_values[1],
        aux_3=aux_values[2],
        aux_4=aux_values[3],
    )
    eeg_samples_buffer.append(eeg_sample)
    handler_duration = time.perf_counter() - handler_start_time
    logger.debug(f"EEG Handler (rec_id {rec_id}): processed and buffered in {handler_duration:.4f}s")


async def handle_acc(osc_address: str, osc_params: tuple, rec_id: int) -> None:
    global acc_samples_buffer
    handler_start_time = time.perf_counter()
    try:
        x, y, z = osc_params
    except ValueError:
        logger.warning(
            "Received ACC message with incorrect number of parameters. Expected 3, got %s. Message: %s %s",
            len(osc_params), osc_address, osc_params,
        )
        return

    acc_sample = AccelerometerSample(
        session_id=rec_id, timestamp=dt.datetime.now(dt.timezone.utc),
        x=float(x), y=float(y), z=float(z),
    )
    acc_samples_buffer.append(acc_sample)
    handler_duration = time.perf_counter() - handler_start_time
    logger.debug(f"ACC Handler (rec_id {rec_id}): processed and buffered in {handler_duration:.4f}s")


async def handle_band(osc_address: str, osc_params: tuple, rec_id: int, band: str) -> None:
    global freq_abs_samples_buffer
    handler_start_time = time.perf_counter()
    
    num_params = len(osc_params)
    avg_value, tp9, af7, af8, tp10 = None, None, None, None, None

    if num_params == 1:
        avg_value = float(osc_params[0])
    elif num_params == 4:
        tp9, af7, af8, tp10 = (float(p) for p in osc_params)
    else:
        logger.warning(
            f"Received BAND message ({band}) with incorrect number of parameters. "
            f"Expected 1 or 4, got {num_params}. Message: {osc_address} {osc_params}"
        )
        return

    freq_sample = FrequencyAbsoluteSample(
        session_id=rec_id,
        timestamp=dt.datetime.now(dt.timezone.utc),
        band=band,
        avg_value=avg_value,
        tp9=tp9,
        af7=af7,
        af8=af8,
        tp10=tp10,
    )
    freq_abs_samples_buffer.append(freq_sample)
    handler_duration = time.perf_counter() - handler_start_time
    logger.debug(f"BAND Handler (rec_id {rec_id}, band {band}): processed and buffered in {handler_duration:.4f}s")


async def handle_horseshoe(osc_address: str, osc_params: tuple, rec_id: int) -> None:
    global horseshoe_samples_buffer
    handler_start_time = time.perf_counter()
    try:
        tp9, af7, af8, tp10 = osc_params
    except ValueError:
        logger.warning(
            "Received HORSESHOE message with incorrect number of parameters. Expected 4, got %s. Message: %s %s",
            len(osc_params), osc_address, osc_params,
        )
        return

    horseshoe_sample = HorseshoeSample(
        session_id=rec_id, timestamp=dt.datetime.now(dt.timezone.utc),
        tp9=int(tp9), af7=int(af7), af8=int(af8), tp10=int(tp10),
    )
    horseshoe_samples_buffer.append(horseshoe_sample)
    handler_duration = time.perf_counter() - handler_start_time
    logger.debug(f"HORSESHOE Handler (rec_id {rec_id}): processed and buffered in {handler_duration:.4f}s")


async def handle_touching(osc_address: str, osc_params: tuple, rec_id: int) -> None:
    global touching_forehead_samples_buffer
    handler_start_time = time.perf_counter()
    try:
        val = osc_params[0]
    except (IndexError, ValueError):
        logger.warning(
            "Received TOUCHING_FOREHEAD message with incorrect parameters. Expected 1, got %s. Message: %s %s",
            len(osc_params), osc_address, osc_params,
        )
        return

    touching_sample = TouchingForeheadSample(
        session_id=rec_id, timestamp=dt.datetime.now(dt.timezone.utc), value=bool(val),
    )
    touching_forehead_samples_buffer.append(touching_sample)
    handler_duration = time.perf_counter() - handler_start_time
    logger.debug(f"TOUCHING_FOREHEAD Handler (rec_id {rec_id}): processed and buffered in {handler_duration:.4f}s")


async def handle_blink(osc_address: str, osc_params: tuple, rec_id: int) -> None:
    global blink_events_buffer
    handler_start_time = time.perf_counter()
    try:
        val = osc_params[0]
    except (IndexError, ValueError):
        logger.warning(
            "Received BLINK message with incorrect parameters. Expected 1, got %s. Message: %s %s",
            len(osc_params), osc_address, osc_params,
        )
        return

    blink_event = BlinkEvent(
        session_id=rec_id, timestamp=dt.datetime.now(dt.timezone.utc), strength=float(val),
    )
    blink_events_buffer.append(blink_event)
    handler_duration = time.perf_counter() - handler_start_time
    logger.debug(f"BLINK Handler (rec_id {rec_id}): processed and buffered in {handler_duration:.4f}s")


async def handle_jaw(osc_address: str, osc_params: tuple, rec_id: int) -> None:
    global jaw_clench_events_buffer
    handler_start_time = time.perf_counter()
    try:
        val = osc_params[0]
    except (IndexError, ValueError):
        logger.warning(
            "Received JAW_CLENCH message with incorrect parameters. Expected 1, got %s. Message: %s %s",
            len(osc_params), osc_address, osc_params,
        )
        return

    jaw_event = JawClenchEvent(
        session_id=rec_id, timestamp=dt.datetime.now(dt.timezone.utc), strength=float(val),
    )
    jaw_clench_events_buffer.append(jaw_event)
    handler_duration = time.perf_counter() - handler_start_time
    logger.debug(f"JAW_CLENCH Handler (rec_id {rec_id}): processed and buffered in {handler_duration:.4f}s")


def _schedule_async_handler(
    loop: asyncio.AbstractEventLoop,
    # session_factory: async_sessionmaker[AsyncSession], # No longer needed here
    handler_coro_func: Callable[..., Coroutine[Any, Any, None]],
    *actual_handler_args: Any
) -> None:
    """Helper to schedule an async OSC handler, creating a new DB session for it."""
    async def _wrapper():
        # actual_handler_args will be (osc_address, osc_params_tuple, rec_id, [potentially_band_arg])
        # handler_coro_func now expects (osc_address, osc_params_tuple, rec_id, ...)
        try:
            await handler_coro_func(*actual_handler_args)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Unhandled exception in OSC handler %s for args: %s",
                handler_coro_func.__name__, actual_handler_args
            )
    loop.create_task(_wrapper())


async def start_osc_server(
    port: int,
    rec_session: RecordingSession,
    loop: asyncio.AbstractEventLoop,
    session_factory: async_sessionmaker[AsyncSession]
) -> None:
    logger.info("Starting OSC server on UDP %s for recording session %s", port, rec_session.id)
    dispatcher: Dispatcher = Dispatcher()

    # Map addresses
    # Note: osc_params from python-osc dispatcher are packed into a single 'args' tuple.
    # We pass 'args' (the tuple of OSC parameters) directly to _schedule_async_handler,
    # and the handler_coro_func (e.g. handle_eeg) will unpack it.
    dispatcher.map("/muse/eeg", lambda addr, *osc_params_tuple: _schedule_async_handler(loop, handle_eeg, addr, osc_params_tuple, rec_session.id))
    dispatcher.map("/muse/acc", lambda addr, *osc_params_tuple: _schedule_async_handler(loop, handle_acc, addr, osc_params_tuple, rec_session.id))
    dispatcher.map("/muse/elements/horseshoe", lambda addr, *osc_params_tuple: _schedule_async_handler(loop, handle_horseshoe, addr, osc_params_tuple, rec_session.id))
    dispatcher.map("/muse/elements/touching_forehead", lambda addr, *osc_params_tuple: _schedule_async_handler(loop, handle_touching, addr, osc_params_tuple, rec_session.id))
    dispatcher.map("/muse/elements/blink", lambda addr, *osc_params_tuple: _schedule_async_handler(loop, handle_blink, addr, osc_params_tuple, rec_session.id))
    dispatcher.map("/muse/elements/jaw_clench", lambda addr, *osc_params_tuple: _schedule_async_handler(loop, handle_jaw, addr, osc_params_tuple, rec_session.id))

    for osc_addr, band_name in BAND_MAP.items():
        dispatcher.map(osc_addr, lambda addr, *osc_params_tuple, b=band_name: _schedule_async_handler(loop, handle_band, addr, osc_params_tuple, rec_session.id, b))

    osc_udp_server = AsyncIOOSCUDPServer(("0.0.0.0", port), dispatcher, loop)
    transport, protocol = await osc_udp_server.create_serve_endpoint()
    logger.info("OSC server ready and listening on port %s", port)
    
    try:
        # Keep the server running until it's cancelled
        await asyncio.Future() # This will wait indefinitely
    except asyncio.CancelledError:
        logger.info("OSC server task cancelled.")
    finally:
        logger.info("Closing OSC server transport.")
        if transport:
            transport.close()
    # The shared_session will be closed automatically when exiting the 'async with' block


async def create_aiohttp_app() -> web.Application: # Renamed for clarity
    app = web.Application()
    app.router.add_get("/health", lambda _: web.json_response({"status": "ok"}))
    return app

async def start_osc_handler(rec_session: RecordingSession) -> asyncio.Task:
    current_loop = asyncio.get_running_loop()
    default_session_factory = get_async_session_factory()

    # Start the batch commit task
    global batch_commit_task_handle
    # Ensure settings.batch_interval_seconds is available (add to config.py)
    batch_interval = getattr(settings, 'batch_interval_seconds', 1.0) # Default to 1s if not in settings
    batch_commit_task_handle = asyncio.create_task(
        batch_commit_data(default_session_factory, batch_interval)
    )

    osc_server_task = asyncio.create_task(
        start_osc_server(settings.osc_port, rec_session, current_loop, default_session_factory)
    )

    # Optional: run aiohttp health server
    #aio_app = await create_aiohttp_app()
    #runner = web.AppRunner(aio_app)
    #await runner.setup()
    #site = web.TCPSite(runner, "0.0.0.0", 8080) # Standard port for such health checks
    #await site.start()
    #logger.info("AIOHTTP health server running on http://0.0.0.0:8080/health")
    #logger.info("Muse OSC MCP server fully running (recording session id=%s)", rec_session.id)

    return osc_server_task

async def stop_osc_handler(osc_server_task: asyncio.Task) -> None:
    if batch_commit_task_handle and not batch_commit_task_handle.done():
        logger.info("Cancelling batch commit task...")
        batch_commit_task_handle.cancel()
        try:
            await batch_commit_task_handle
        except asyncio.CancelledError:
            logger.info("Batch commit task cancelled. Final flush will run.")
        except Exception:
            logger.exception("Exception during batch commit task cancellation/shutdown.")
        
    # Flush any remaining data in buffers before exiting
    logger.info("Flushing remaining data from buffers before shutdown...")
    try:
        # Use a short timeout for the final flush to prevent hanging indefinitely
        await asyncio.wait_for(batch_commit_data(get_async_session_factory(), interval_seconds=0), timeout=10.0)
        logger.info("Final data flush complete.")
    except asyncio.TimeoutError:
        logger.error("Timeout during final data flush. Some data may not have been saved.")
    except Exception:
        logger.exception("Error during final data flush.")

    if osc_server_task and not osc_server_task.done():
        osc_server_task.cancel()
        try:
            await osc_server_task
        except asyncio.CancelledError:
            logger.info("OSC server task successfully cancelled.")

async def main() -> None:
    global batch_commit_task_handle # Declare global at the start of the function
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s [%(funcName)s]: %(message)s" # Added funcName
    )
    await initialize_global_db() # Initialize global DB resources

    # Warm up the connection pool
    logger.info("Warming up database connection pool...")
    try:
        global_engine = await get_engine()
        if global_engine:
            await warm_up_connection_pool(global_engine, num_connections=settings.db_pool_warmup_connections) # Use a configurable number
            logger.info("Database connection pool warming complete.")
        else:
            logger.warning("Global engine not available for pool warming.")
    except Exception as e:
        logger.error(f"Error during connection pool warming: {e}", exc_info=True)
        # Decide if this is fatal or if the app can continue without warming

    try:
        logger.info("Ensuring database schema exists...")
        try:
            await init_db() # Ensure tables exist
            logger.info("Database schema check/creation complete.")
        except Exception as e:
            logger.error(f"Failed to create database schema: {e}", exc_info=True)
            logger.critical("Database schema creation failed. Cannot proceed.")
            return
        # Rest of the code remains the same
        rec_session = await create_recording_session()

        osc_server_task = await start_osc_handler(rec_session)

        await osc_server_task # Keep main alive until OSC server task finishes or is cancelled

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down.")
    except Exception as e:
        logger.exception("Unhandled exception in main: %s", e)
    finally:
        # Gracefully shutdown batch commit task and flush remaining data
        # global batch_commit_task_handle # Moved to the top of the function
        if batch_commit_task_handle and not batch_commit_task_handle.done():
            logger.info("Cancelling batch commit task...")
            batch_commit_task_handle.cancel()
            try:
                await batch_commit_task_handle
            except asyncio.CancelledError:
                logger.info("Batch commit task cancelled. Final flush will run.")
            except Exception:
                logger.exception("Exception during batch commit task cancellation/shutdown.")
        
        # Flush any remaining data in buffers before exiting
        logger.info("Flushing remaining data from buffers before shutdown...")
        try:
            # Use a short timeout for the final flush to prevent hanging indefinitely
            await asyncio.wait_for(batch_commit_data(await get_async_session_factory(), interval_seconds=0), timeout=10.0)
            logger.info("Final data flush complete.")
        except asyncio.TimeoutError:
            logger.error("Timeout during final data flush. Some data may not have been saved.")
        except Exception:
            logger.exception("Error during final data flush.")

        if 'osc_server_task' in locals() and osc_server_task and not osc_server_task.done():
            osc_server_task.cancel()
            try:
                await osc_server_task
            except asyncio.CancelledError:
                logger.info("OSC server task successfully cancelled.")
        if 'runner' in locals() and runner: # if using aiohttp
            logger.info("Cleaning up AIOHTTP server.")
            await runner.cleanup()
        logger.info("Disposing global DB engine...")
        await dispose_global_engine()
        logger.info("Global DB engine disposed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated by user (KeyboardInterrupt).")
