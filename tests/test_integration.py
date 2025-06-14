"""Integration test: run OSC server, send dummy data, verify DB inserts."""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
import socket
import time
from typing import AsyncGenerator

import pytest
from pythonosc.udp_client import SimpleUDPClient
from sqlalchemy import select, func, create_engine as sqlalchemy_create_engine
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.ext.asyncio import create_async_engine as sqlalchemy_create_async_engine, async_sessionmaker, AsyncSession

import datetime as dt
# os.environ["DATABASE_NAME"] = "musetest"
# os.environ["DATABASE_HOST"] = "10.0.33.131"
# os.environ["DATABASE_PORT"] = "5432"
# os.environ["DATABASE_USERNAME"] = "muse_test"
# os.environ["DATABASE_PASSWORD"] = "musetest2025"

import datetime as dt


@pytest.fixture(scope="session", autouse=True)
async def global_engine_manager():
    """Manages the lifecycle of the global engine for the test session."""
    print("\nInitializing global DB for test session...")
    await db.initialize_global_db() # Initialize global resources
    print("Global DB initialized.")
    yield # Run all tests
    # Teardown: Dispose of the global engine after all tests in the session are done
    print("\nDisposing global DB engine at end of test session...")
    await db.dispose_global_engine()
    print("Global DB engine disposed.")

from muse_osc_mcp import config, db, models, server  # noqa: E402

@pytest.fixture(scope="session", autouse=True)
async def warm_up_db_on_session_start():
    """Warms up the global DB connection pool at the beginning of the test session."""
    print("\nPerforming global DB connection pool warm-up for test session...")
    try:
        global_engine = await db.get_engine() # Get the global engine
        if global_engine:
            await db.warm_up_connection_pool(global_engine, num_connections=15) # Warm it up to pool_size
            print("Global DB connection pool warm-up completed for 15 connections.")
        else:
            print("Global engine not available for warm-up.")
    except Exception as e:
        print(f"Error during global DB warm-up: {e}")


@pytest.fixture(scope="session", autouse=True)
async def db_prepared(global_engine_manager) -> None:  # Add dependency
    """Initialise tables once for test session by calling db.init_db()."""
    # Ensure the global engine is initialized (global_engine_manager should do this)
    # Then run the async init_db function.
    try:
        await db.init_db() # Uses the global async engine
        print("Test session: Database tables prepared via db.init_db().")
    except Exception as e:
        print(f"Test session: Error during db.init_db() in db_prepared: {e}")
        raise


def _get_free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
async def db_session_for_test(monkeypatch) -> AsyncGenerator[tuple[AsyncSession, async_sessionmaker[AsyncSession]], None]:
    """Creates a new async engine and session factory for each test,
    and patches db.engine and db.async_session."""
    test_engine = sqlalchemy_create_async_engine(config.settings.async_db_url, echo=False, pool_pre_ping=True, pool_size=15, max_overflow=5) # Match global engine settings
    
    # Warm up the test-specific engine's connection pool
    # print(f"\nWarming up connection pool for test-specific engine ({id(test_engine)})...") # Optional: for debugging
    await db.warm_up_connection_pool(test_engine, num_connections=15) # Warm it up to pool_size
    # print(f"Connection pool for test-specific engine ({id(test_engine)}) warmed up for 15 connections.") # Optional: for debugging

    test_async_session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    # Patch the getter functions in db.py to return test-specific engine/factory
    async def mock_get_engine():
        return test_engine
    async def mock_get_async_session_factory():
        return test_async_session_factory

    monkeypatch.setattr(db, "get_engine", mock_get_engine)
    monkeypatch.setattr(db, "get_async_session_factory", mock_get_async_session_factory)

    async with test_async_session_factory() as session:
        yield session, test_async_session_factory # Yield both session and factory

    await asyncio.sleep(0) # Allow event loop to cycle before disposing
    await test_engine.dispose()


@pytest.fixture()
async def running_server(db_session_for_test: tuple[AsyncSession, async_sessionmaker[AsyncSession]]) -> AsyncGenerator[int, None]:
    """Run OSC server and batch commit task in background, yield port."""
    session, test_specific_session_factory = db_session_for_test
    port = _get_free_udp_port()

    # Create recording session
    rec = models.Session(user=config.settings.session_user)
    session.add(rec)
    await session.commit()
    await session.refresh(rec)

    current_loop = asyncio.get_running_loop()
    batch_task_handle_for_test: asyncio.Task | None = None
    osc_server_task_for_test: asyncio.Task | None = None

    try:
        # Start batch commit task
        batch_task_handle_for_test = current_loop.create_task(
            server.batch_commit_data(test_specific_session_factory, config.settings.batch_interval_seconds)
        )
        print("\nTest fixture: Batch commit task started.")

        # Start OSC server task
        osc_server_task_for_test = current_loop.create_task(
            server.start_osc_server(port, rec, current_loop, test_specific_session_factory)
        )
        print(f"Test fixture: OSC server task starting on port {port}.")

        await asyncio.sleep(0.8)  # Increased sleep to allow both tasks to initialize
        print("Test fixture: Server and batch task should be running.")
        yield port

    finally:
        print("\nTest fixture: Tearing down server tasks...")
        if osc_server_task_for_test and not osc_server_task_for_test.done():
            print("Test fixture: Cancelling OSC server task...")
            osc_server_task_for_test.cancel()
            try:
                await osc_server_task_for_test
            except asyncio.CancelledError:
                print("Test fixture: OSC server task successfully cancelled.")
            except Exception as e:
                print(f"Test fixture: Exception during OSC server task cancellation: {e}")
        
        if batch_task_handle_for_test and not batch_task_handle_for_test.done():
            print("Test fixture: Cancelling batch commit task...")
            batch_task_handle_for_test.cancel()
            try:
                await batch_task_handle_for_test
            except asyncio.CancelledError:
                print("Test fixture: Batch commit task successfully cancelled.")
            except Exception as e:
                print(f"Test fixture: Exception during batch commit task cancellation: {e}")
        
        # Final data flush should be outside the batch_task cancellation try-except
        print("Test fixture: Performing final data flush...")
        try:
            # Ensure any remaining buffered data is flushed before test ends
            # Call batch_commit_data with interval 0 for an immediate flush
            await server.batch_commit_data(session_factory=test_specific_session_factory, interval_seconds=0)
            print("Test fixture: Final data flush complete.")
        except Exception as e:
            print(f"Test fixture: Error during final data flush: {e}")

        await asyncio.sleep(0.2) # Give a bit more time for cleanup
        print("Test fixture: Teardown complete.")


@pytest.fixture(autouse=True)
async def _clean_tables(db_session_for_test: tuple[AsyncSession, async_sessionmaker[AsyncSession]]):
    """Truncate all tables after each test for idempotency."""
    yield # Test runs here
    session, _ = db_session_for_test # We only need the session here
    for tbl in reversed(models.Base.metadata.sorted_tables):
        await session.execute(tbl.delete())
    await session.commit()


@pytest.mark.asyncio
async def test_eeg_accel_flow(running_server):  # type: ignore[missing-type-doc]
    print()  # Add a newline for better log readability
    port = running_server
    client = SimpleUDPClient("127.0.0.1", port)

    start_send_time = time.perf_counter()

    # Send random EEG packets
    eeg_packets = 10
    for _ in range(eeg_packets):
        floats = [random.uniform(-100.0, 100.0) for _ in range(5)]
        client.send_message("/muse/eeg", floats)

    # Send random accel packets
    acc_packets = 5
    for _ in range(acc_packets):
        floats = [random.uniform(-1.0, 1.0) for _ in range(3)]
        client.send_message("/muse/acc", floats)

    # Send one malformed packet (ignored)
    client.send_message("/muse/eeg", [1, 2])
    send_duration = time.perf_counter() - start_send_time
    print(f"OSC send duration: {send_duration:.4f} seconds")

    start_sleep_time = time.perf_counter()
    batch_wait_time = config.settings.batch_interval_seconds + 2.0  # Wait for at least one batch interval + buffer
    print(f"Waiting {batch_wait_time:.2f}s for batch commit based on interval: {config.settings.batch_interval_seconds:.2f}s")
    await asyncio.sleep(batch_wait_time)
    sleep_duration = time.perf_counter() - start_sleep_time
    print(f"Asyncio sleep duration: {sleep_duration:.4f} seconds")

    # Verify counts
    start_query_time = time.perf_counter()
    test_specific_session_factory = await db.get_async_session_factory() # Get the (monkeypatched) factory
    async with test_specific_session_factory() as s:
        eeg_count = (await s.execute(select(func.count(models.EegSample.id)))).scalar_one()
        acc_count = (await s.execute(select(func.count(models.AccelerometerSample.id)))).scalar_one()
    query_duration = time.perf_counter() - start_query_time
    print(f"DB query duration: {query_duration:.4f} seconds")

    total_duration = time.perf_counter() - start_send_time
    print(f"Total test critical section duration: {total_duration:.4f} seconds")

    assert eeg_count == eeg_packets  # All valid packets should be present; malformed ones skipped
    assert acc_count == acc_packets


@pytest.mark.asyncio
async def test_direct_db_commit_performance(db_session_for_test: tuple[AsyncSession, async_sessionmaker[AsyncSession]]): # type: ignore[missing-type-doc]
    # This test fixture now provides both a session and the factory that created it.
    # For this test, we want to control session creation explicitly to time it.
    # So, we'll use the factory from the fixture, not the pre-made session.
    _, test_specific_session_factory = db_session_for_test
    print() # Newline for readability
    # Ensure tables are created (handled by db_prepared fixture).
    # Warm-up is now handled by the session-scoped warm_up_db_on_session_start fixture.

    test_total_start_time = time.perf_counter()

    # 1. Time session creation from factory
    session_creation_start_time = time.perf_counter()
    async with test_specific_session_factory() as s:
        session_creation_duration = time.perf_counter() - session_creation_start_time
        print(f"Direct DB Test: AsyncSession creation took {session_creation_duration:.4f} seconds")

        # 2. Time first object add/commit (RecordingSession)
        db_add_duration_rec = 0.0
        db_commit_duration_rec = 0.0
        first_commit_block_start_time = time.perf_counter()

        recording = models.Session(user="direct_test_rec", started_at=dt.datetime.now(dt.timezone.utc))
        db_add_start_time_rec = time.perf_counter()
        s.add(recording)
        db_add_duration_rec = time.perf_counter() - db_add_start_time_rec

        db_commit_start_time_rec = time.perf_counter()
        await s.commit()
        db_commit_duration_rec = time.perf_counter() - db_commit_start_time_rec
        await s.refresh(recording) # Ensure we have the ID for the next step
        first_commit_block_duration = time.perf_counter() - first_commit_block_start_time
        print(
            f"Direct DB Test: RecordingSession add={db_add_duration_rec:.4f}s, commit={db_commit_duration_rec:.4f}s, block_duration={first_commit_block_duration:.4f}s"
        )

        # 3. Time second object add/commit (EegSample) on the SAME session
        db_add_duration_eeg = 0.0
        db_commit_duration_eeg = 0.0
        second_commit_block_start_time = time.perf_counter()

        eeg_sample = models.EegSample(
            session_id=recording.id,
            timestamp=dt.datetime.now(dt.timezone.utc),
            tp9=1.0, af7=1.0, af8=1.0, tp10=1.0, aux=1.0
        )
        db_add_start_time_eeg = time.perf_counter()
        s.add(eeg_sample)
        db_add_duration_eeg = time.perf_counter() - db_add_start_time_eeg

        db_commit_start_time_eeg = time.perf_counter()
        await s.commit()
        db_commit_duration_eeg = time.perf_counter() - db_commit_start_time_eeg
        second_commit_block_duration = time.perf_counter() - second_commit_block_start_time

    test_total_duration = time.perf_counter() - test_total_start_time
    print(
        f"Direct DB Test: EegSample add={db_add_duration_eeg:.4f}s, commit={db_commit_duration_eeg:.4f}s, block_duration={second_commit_block_duration:.4f}s"
    )
    print(f"Direct DB Test: Total test function time={test_total_duration:.4f}s")

    # Basic assertion to ensure the test does something verifiable
    # We expect at least one EegSample to have been committed.
    async with test_specific_session_factory() as assert_s:
        count = (await assert_s.execute(select(func.count(models.EegSample.id)))).scalar_one()
        assert count >= 1, "Expected at least one EegSample to be committed in the test"
    # The assertion above (inside the 'assert_s' context) is sufficient.
    # Using the stale 's' session here would be incorrect and cause SAWarnings.

