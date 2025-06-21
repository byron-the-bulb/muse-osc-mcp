"""EnrichMCP integration exposing the existing SQLAlchemy models.

This module creates an `EnrichMCP` application that automatically
exposes all SQLAlchemy models declared in `muse_osc_mcp.models` (via
`muse_osc_mcp.db.Base`).  The server can be run directly::

    python -m muse_osc_mcp.mcp

or you can let an ASGI runner import the callable ``get_app`` which
returns the fully-initialised EnrichMCP app.  Example::

    uvicorn muse_osc_mcp.mcp:get_app

This avoids creating the app at *import* time and therefore prevents
``asyncio.run() cannot be called from a running event loop`` errors
when tools (like Uvicorn) import the module within a running loop.  The OSC‐recording server (``muse_osc_mcp.server``) can
still be executed independently; there is no runtime coupling between
the two entry-points.
"""

from __future__ import annotations

import asyncio
# HacK!!!!
# Set the environment variable to point to the test .env file
# This MUST be done before importing 'settings' from config
import os
import logging
from datetime import datetime

# Ensure APP_ENV_FILE points to the correct environment file _before_ other imports.
os.environ["APP_ENV_FILE"] = ".env"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGS_DIR = "logs"
MAX_LOG_FILES = 20
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_LEVEL = logging.INFO

def setup_logging():
    """Initializes timestamped, rolling file logging."""
    removed_files_messages = []
    
    # Ensure logs directory exists
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
    except OSError as e:
        print(f"Error creating logs directory {LOGS_DIR}: {e}")
        # If log directory creation fails, it's a critical issue for file logging.
        # Consider falling back to basic console logging or re-raising.
        logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
        logging.error(f"CRITICAL: Failed to create log directory {LOGS_DIR}. Error: {e}")
        return

    # Manage old log files for rollover
    try:
        existing_log_files = [f for f in os.listdir(LOGS_DIR) if f.startswith("mcp_") and f.endswith(".log")]
        existing_log_files.sort() # Sorts alphabetically (timestamps YYYY-MM-DD_HH-MM-SS ensure chronological)

        num_to_delete = len(existing_log_files) - MAX_LOG_FILES + 1
        if num_to_delete > 0:
            for i in range(num_to_delete):
                file_path_to_remove = os.path.join(LOGS_DIR, existing_log_files[i])
                try:
                    os.remove(file_path_to_remove)
                    removed_files_messages.append(f"Removed old log file: {file_path_to_remove}")
                except OSError as e:
                    # Use print for errors during this phase as logger might not be fully set up
                    print(f"Error removing old log file {file_path_to_remove}: {e}")
    except Exception as e:
        print(f"Error during log file cleanup: {e}")

    # Setup logging for the current run
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_filename = os.path.join(LOGS_DIR, f"mcp_{timestamp}.log")

    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)

    # Remove any existing handlers from the root logger
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    # Add new file handler
    try:
        file_handler = logging.FileHandler(log_filename, mode="a", encoding='utf-8')
        formatter = logging.Formatter(LOG_FORMAT)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"Error setting up file handler for {log_filename}: {e}")
        # Fallback to basic console logging if file handler fails
        logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
        logging.error(f"Failed to initialize file logging to {log_filename}. Using console. Error: {e}")
        return

    logging.info(f"Logging initialized. Log file: {log_filename}")
    for msg in removed_files_messages:
        logging.info(msg) # Log messages about removed files after main setup

# Initialize logging
setup_logging()



from enrichmcp import EnrichMCP
from enrichmcp.sqlalchemy import (
    include_sqlalchemy_models,
    sqlalchemy_lifespan,
)

from .db import initialize_global_db, get_engine, Base, get_async_session_factory # type: ignore
from .models import Session as RecordingSession
from .server import end_recording_session, create_recording_session, start_osc_handler, stop_osc_handler
import numpy as np
import datetime as dt
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from .models import Session as RecordingSessionModel, FrequencyAbsoluteSample, BlinkEvent, JawClenchEvent, HorseshoeSample, TouchingForeheadSample # Renamed Session to RecordingSessionModel to avoid conflict with mcp.py's rec_session global
from .db import get_async_session_factory
import pandas as pd


initialize_global_db()
engine = get_engine()
app = EnrichMCP("Muse EEG Recorder", "An API to record EEG data from Muse Headset and analyse the results", lifespan=sqlalchemy_lifespan(Base, engine))
include_sqlalchemy_models(app, Base)

osc_server_task: asyncio.Task | None = None
rec_session: RecordingSession | None = None

@app.mcp.tool()
async def start_session(user: str, description: str | None = None) -> int:
    """Start a new EEG recording session. Returns the session ID."""
    global osc_server_task
    global rec_session
    rec_session = await create_recording_session(user=user, description=description)
    osc_server_task = await start_osc_handler(rec_session)
    return rec_session.id

@app.mcp.tool()
async def end_session() -> int:
    """End an EEG recording session. Returns the session ID."""
    global osc_server_task
    global rec_session

    if osc_server_task:
        try:
            await stop_osc_handler(osc_server_task)
        except Exception as e:
            logging.error(f"Error stopping OSC handler: {e}")
        finally:
            osc_server_task = None

    if rec_session:
        try:
            await end_recording_session(rec_session)
        except Exception as e:
            logging.error(f"Error ending recording session: {e}")
        finally:
            rec_session = None
    return rec_session.id if rec_session else -1


# This constant is based on the FrequencyAbsoluteSample docstring (10Hz sampling rate)
ASSUMED_WINDOW_LENGTH_SECONDS = 0.1

@app.mcp.tool()
async def get_distilled_eeg_summary(
    session_id: int,
    start_time_seconds: float,
    end_time_seconds: float,
    segment_length_seconds: float = 0.5
) -> str:
    """
    Provides a distilled summary of EEG frequency band data for a specific time window,
    using data from the FrequencyAbsoluteSample table.
    Use this function to get a quick overview of the EEG data in a specific time window to save on 
    processing multiple pages of data.
    Zoom in and out of the data using the segment_length_seconds parameter to get a better overview of the data.

    Args:
        session_id (int): The ID of the session to retrieve data for.
        start_time_seconds (float): The beginning of the time window to summarize (in seconds, relative to session start).
        end_time_seconds (float): The end of the time window to summarize (in seconds, relative to session start).
        segment_length_seconds (float): The length of each summary segment in seconds. Defaults to 0.5s.

    Returns:
        str: A distilled text summary of EEG data for the LLM, or an error message.
    """
    logging.info(f"[EEG Summary] Request for session {session_id}: {start_time_seconds:.2f}s - {end_time_seconds:.2f}s, segment: {segment_length_seconds:.2f}s")

    if start_time_seconds >= end_time_seconds:
        return f"Error: Start time ({start_time_seconds:.2f}s) must be before end time ({end_time_seconds:.2f}s)."
    if segment_length_seconds <= 0:
        return f"Error: Segment length ({segment_length_seconds:.2f}s) must be positive."

    async_session_factory = get_async_session_factory()
    async with async_session_factory() as db_session:
        # 1. Fetch Session to get its start time
        session_result = await db_session.execute(
            select(RecordingSessionModel).where(RecordingSessionModel.id == session_id)
        )
        session_record = session_result.scalar_one_or_none()

        if not session_record:
            logging.warning(f"[EEG Summary] Session {session_id} not found.")
            return f"Error: Session {session_id} not found."
        if not session_record.started_at:
            logging.warning(f"[EEG Summary] Session {session_id} has no start time.")
            return f"Error: Session {session_id} does not have a recorded start time."
        
        session_start_datetime = session_record.started_at

        # 2. Calculate absolute time window for fetching data
        abs_start_datetime = session_start_datetime + dt.timedelta(seconds=start_time_seconds)
        abs_end_datetime = session_start_datetime + dt.timedelta(seconds=end_time_seconds)

        logging.info(f"[EEG Summary] Session {session_id} started at {session_start_datetime}. Querying data from {abs_start_datetime} to {abs_end_datetime}.")

        # 3. Fetch FrequencyAbsoluteSample data for the specified window and session
        stmt = (
            select(FrequencyAbsoluteSample)
            .where(
                FrequencyAbsoluteSample.session_id == session_id,
                FrequencyAbsoluteSample.timestamp >= abs_start_datetime,
                FrequencyAbsoluteSample.timestamp < abs_end_datetime # Use < for end to match typical range behavior
            )
            .order_by(FrequencyAbsoluteSample.timestamp)
        )
        result = await db_session.execute(stmt)
        raw_samples = result.scalars().all()

        # Fetch Blink Events
        blink_stmt = select(BlinkEvent).where(
            BlinkEvent.session_id == session_id,
            BlinkEvent.timestamp >= abs_start_datetime,
            BlinkEvent.timestamp < abs_end_datetime
        ).order_by(BlinkEvent.timestamp)
        blink_result = await db_session.execute(blink_stmt)
        blink_events = blink_result.scalars().all()

        # Fetch Jaw Clench Events
        jaw_stmt = select(JawClenchEvent).where(
            JawClenchEvent.session_id == session_id,
            JawClenchEvent.timestamp >= abs_start_datetime,
            JawClenchEvent.timestamp < abs_end_datetime
        ).order_by(JawClenchEvent.timestamp)
        jaw_result = await db_session.execute(jaw_stmt)
        jaw_events = jaw_result.scalars().all()

        # Fetch Horseshoe Samples for fit
        horseshoe_stmt = select(HorseshoeSample).where(
            HorseshoeSample.session_id == session_id,
            HorseshoeSample.timestamp >= abs_start_datetime,
            HorseshoeSample.timestamp < abs_end_datetime
        ).order_by(HorseshoeSample.timestamp)
        horseshoe_result = await db_session.execute(horseshoe_stmt)
        horseshoe_samples = horseshoe_result.scalars().all()

        # Fetch TouchingForehead Samples for fit
        forehead_stmt = select(TouchingForeheadSample).where(
            TouchingForeheadSample.session_id == session_id,
            TouchingForeheadSample.timestamp >= abs_start_datetime,
            TouchingForeheadSample.timestamp < abs_end_datetime
        ).order_by(TouchingForeheadSample.timestamp)
        forehead_result = await db_session.execute(forehead_stmt)
        forehead_samples = forehead_result.scalars().all()

        if not raw_samples:
            logging.warning(f"[EEG Summary] No FrequencyAbsoluteSample data found for session {session_id} in window {abs_start_datetime} to {abs_end_datetime}.")
            return f"Error: No EEG frequency data found for session {session_id} in the specified time window [{start_time_seconds:.2f}s - {end_time_seconds:.2f}s]."

        # 4. Restructure fetched data into band_powers_history format
        temp_band_powers = {band: [] for band in ["Delta", "Theta", "Alpha", "Beta", "Gamma"]}
        min_ts_in_data = raw_samples[0].timestamp
        max_ts_in_data = raw_samples[-1].timestamp

        for sample in raw_samples:
            band_name = sample.band.capitalize()
            if band_name in temp_band_powers and sample.avg_value is not None:
                temp_band_powers[band_name].append((sample.timestamp, sample.avg_value))
        
        actual_data_start_abs = min_ts_in_data
        actual_data_duration_seconds = (max_ts_in_data - min_ts_in_data).total_seconds()
        if actual_data_duration_seconds < 0: actual_data_duration_seconds = 0 # if only one sample
        
        num_expected_samples_in_slice = int(actual_data_duration_seconds / ASSUMED_WINDOW_LENGTH_SECONDS) + 1
        band_powers_history = {band: [np.nan] * num_expected_samples_in_slice for band in temp_band_powers.keys()}

        for band, ts_value_pairs in temp_band_powers.items():
            if not ts_value_pairs: continue # Skip if a band had no data
            for timestamp, value in ts_value_pairs:
                time_offset_seconds = (timestamp - actual_data_start_abs).total_seconds()
                idx = int(round(time_offset_seconds / ASSUMED_WINDOW_LENGTH_SECONDS)) # round to nearest index
                if 0 <= idx < num_expected_samples_in_slice:
                    # If multiple values map to the same index due to rounding or irregular sampling,
                    # average them. For now, we overwrite; robust handling might average.
                    if np.isnan(band_powers_history[band][idx]) or value > band_powers_history[band][idx]: # or simply overwrite
                         band_powers_history[band][idx] = value
        
        for band_key in band_powers_history.keys():
            series = pd.Series(band_powers_history[band_key])
            # Forward fill, then backward fill for any remaining NaNs at the start
            series.ffill(inplace=True)
            series.bfill(inplace=True)
            # If all were NaNs (e.g. a band had no data), fill with 0
            band_powers_history[band_key] = series.fillna(0).tolist()

    # 5. Apply Summarization Logic
    total_windows_in_slice = num_expected_samples_in_slice
    if total_windows_in_slice == 0:
        logging.warning(f"[EEG Summary] No data points after processing for session {session_id}.")
        return f"Error: No data points to process for session {session_id}."

    windows_per_second = 1.0 / ASSUMED_WINDOW_LENGTH_SECONDS
    segment_windows = int(segment_length_seconds * windows_per_second)

    if segment_windows <= 0:
        logging.error(f"[EEG Summary] Segment length {segment_length_seconds:.2f}s is too short for resolution {ASSUMED_WINDOW_LENGTH_SECONDS:.3f}s/sample.")
        return f"Error: Segment length ({segment_length_seconds:.2f}s) is too short for data resolution."

    thresholds = {}
    for band, powers in band_powers_history.items():
        if powers:
            valid_powers = [p for p in powers if not np.isnan(p)] # Should be no NaNs now
            if valid_powers:
                thresholds[band] = np.percentile(valid_powers, 75)

    summaries = []
    for seg_start_idx in range(0, total_windows_in_slice, segment_windows):
        seg_end_idx = min(seg_start_idx + segment_windows, total_windows_in_slice)
        if seg_start_idx >= seg_end_idx: continue

        # Determine actual timestamps for the current segment relative to session_start_datetime
        # These are used to filter blinks, jaw clenches, and fit data for this specific segment.
        # actual_data_start_abs is the timestamp of the *first data point* in the fetched FrequencyAbsoluteSample set.
        # We need segment timestamps relative to this, then convert back to absolute for querying other event tables.
        
        segment_start_offset_from_data_start_seconds = seg_start_idx * ASSUMED_WINDOW_LENGTH_SECONDS
        segment_end_offset_from_data_start_seconds = seg_end_idx * ASSUMED_WINDOW_LENGTH_SECONDS

        current_segment_abs_start_dt = actual_data_start_abs + dt.timedelta(seconds=segment_start_offset_from_data_start_seconds)
        current_segment_abs_end_dt = actual_data_start_abs + dt.timedelta(seconds=segment_end_offset_from_data_start_seconds)
        
        # Count blinks in current segment
        blinks_in_segment = sum(1 for b in blink_events if current_segment_abs_start_dt <= b.timestamp < current_segment_abs_end_dt)
        
        # Count jaw clenches in current segment
        jaw_clenches_in_segment = sum(1 for j in jaw_events if current_segment_abs_start_dt <= j.timestamp < current_segment_abs_end_dt)
        
        # Calculate headband fit for current segment
        num_good_horseshoe_contacts_in_segment = 0
        num_horseshoe_samples_in_segment = 0
        for hs in horseshoe_samples:
            if current_segment_abs_start_dt <= hs.timestamp < current_segment_abs_end_dt:
                num_horseshoe_samples_in_segment += 1
                if hs.tp9 == 1: num_good_horseshoe_contacts_in_segment += 1
                if hs.af7 == 1: num_good_horseshoe_contacts_in_segment += 1
                if hs.af8 == 1: num_good_horseshoe_contacts_in_segment += 1
                if hs.tp10 == 1: num_good_horseshoe_contacts_in_segment += 1
        
        avg_good_horseshoe_sensors = (num_good_horseshoe_contacts_in_segment / num_horseshoe_samples_in_segment / 4) if num_horseshoe_samples_in_segment > 0 else 0 # Avg proportion of 4 sensors being good
        
        num_good_forehead_contacts_in_segment = 0
        num_forehead_samples_in_segment = 0
        for fs in forehead_samples:
            if current_segment_abs_start_dt <= fs.timestamp < current_segment_abs_end_dt:
                num_forehead_samples_in_segment += 1
                if fs.value: num_good_forehead_contacts_in_segment += 1
        
        avg_good_forehead_sensor = (num_good_forehead_contacts_in_segment / num_forehead_samples_in_segment) if num_forehead_samples_in_segment > 0 else 0 # Avg proportion of 1 sensor being good

        # Total 5 sensors. Fit is average of good sensors (scaled 0-1 for horseshoe part, 0-1 for forehead part)
        # (avg_good_horseshoe_sensors * 4 + avg_good_forehead_sensor * 1) / 5 * 100
        # Simplified: average proportion of good sensors across the 5 points
        # Let's calculate total good points / total possible points for samples in segment
        total_sensor_points_good = 0
        total_sensor_points_possible = 0

        hs_points_good_in_seg = 0
        hs_points_total_in_seg = 0
        for hs_samp in filter(lambda hs: current_segment_abs_start_dt <= hs.timestamp < current_segment_abs_end_dt, horseshoe_samples):
            hs_points_good_in_seg += (hs_samp.tp9 == 1) + (hs_samp.af7 == 1) + (hs_samp.af8 == 1) + (hs_samp.tp10 == 1)
            hs_points_total_in_seg += 4
        
        fh_points_good_in_seg = 0
        fh_points_total_in_seg = 0
        for fh_samp in filter(lambda fs: current_segment_abs_start_dt <= fs.timestamp < current_segment_abs_end_dt, forehead_samples):
            fh_points_good_in_seg += (1 if fh_samp.value else 0)
            fh_points_total_in_seg += 1

        total_sensor_points_good = hs_points_good_in_seg + fh_points_good_in_seg
        total_sensor_points_possible = hs_points_total_in_seg + fh_points_total_in_seg

        headband_fit_percentage = (total_sensor_points_good / total_sensor_points_possible * 100) if total_sensor_points_possible > 0 else 0


        current_segment_data = {band: band_powers_history[band][seg_start_idx:seg_end_idx] for band in band_powers_history}
        
        segment_summary = {}
        valid_segment_data_exists = False
        for band, powers_list in current_segment_data.items():
            powers_np = np.array([p for p in powers_list if not np.isnan(p)])
            if powers_np.size == 0:
                segment_summary[band] = {"mean": 0, "std": 0, "peak": 0}
                continue
            valid_segment_data_exists = True
            segment_summary[band] = {
                "mean": np.mean(powers_np),
                "std": np.std(powers_np),
                "peak": np.max(powers_np)
            }
        
        if not valid_segment_data_exists: continue

        notes = []
        for band_key_for_notes in ["Delta", "Theta", "Alpha", "Beta", "Gamma"]:
            if band_key_for_notes in segment_summary and band_key_for_notes in thresholds and segment_summary[band_key_for_notes]["peak"] > thresholds[band_key_for_notes]:
                notes.append(f"{band_key_for_notes} peak: {segment_summary[band_key_for_notes]['peak']:.1f}")
        
        # Timestamps for the summary line are relative to the *original requested start_time_seconds*
        report_seg_start_time = start_time_seconds + (seg_start_idx * ASSUMED_WINDOW_LENGTH_SECONDS)
        report_seg_end_time = start_time_seconds + (seg_end_idx * ASSUMED_WINDOW_LENGTH_SECONDS)
        report_seg_end_time = min(report_seg_end_time, end_time_seconds)

        summary_line_parts = [f"Time {report_seg_start_time:.1f}s–{report_seg_end_time:.1f}s (Fit:{headband_fit_percentage:.0f}%,B:{blinks_in_segment},J:{jaw_clenches_in_segment}):"]
        for band_key_report in ["Delta", "Theta", "Alpha", "Beta", "Gamma"]:
            if band_key_report in segment_summary:
                 summary_line_parts.append(f"{band_key_report[0]} {segment_summary[band_key_report]['mean']:.1f}±{segment_summary[band_key_report]['std']:.1f}")
        summary_line = ", ".join(summary_line_parts)

        if notes:
            summary_line += f" (Notable: {', '.join(notes)})"
        summaries.append(summary_line)
    
    if not summaries:
        logging.warning(f"[EEG Summary] No summary segments generated for session {session_id} in window [{start_time_seconds:.1f}s - {end_time_seconds:.1f}s].")
        return f"No summary segments generated for session {session_id} in window [{start_time_seconds:.1f}s - {end_time_seconds:.1f}s]. Check segment length and data availability."
        
    final_summary_header = f"EEG Summary for session {session_id} from {start_time_seconds:.1f}s to {end_time_seconds:.1f}s (Segment: {segment_length_seconds:.1f}s, Sample Interval: {ASSUMED_WINDOW_LENGTH_SECONDS*1000:.0f}ms):\n"
    logging.info(f"[EEG Summary] Successfully generated EEG summary for session {session_id}.")
    return final_summary_header + "\n".join(summaries)

app.run()
# CLI entry-point -----------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    # Build the app (initialises DB and discovers models) then start the
    # built-in ASGI server provided by EnrichMCP.
    app.run()
