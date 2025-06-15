"""SQLAlchemy ORM models for sessions and high-rate Muse signal tables.

Tables
------
Session
    One recording session; parent for all sample tables.
EegSample
    Raw EEG voltages (TP9, AF7, AF8, TP10, AUX) at 256 Hz.
AccelerometerSample
    3-axis accelerometer data (~52 Hz).
FrequencyAbsoluteSample
    Absolute power for delta/theta/alpha/beta/gamma bands per channel.
HorseshoeSample
    Contact-quality (horseshoe fit) values per sensor (0-3).
TouchingForeheadSample
    Boolean flag indicating forehead contact quality.
BlinkEvent
    Discrete blink detections.
JawClenchEvent
    Discrete jaw-clench detections.

Design notes
------------
High-rate sample tables are *append-only* and partitionable (future work).
We keep `session_id` FKs and an index on `(session_id, timestamp)` for efficient
range queries per session. Timestamps are stored in UTC.
"""
from __future__ import annotations

import datetime as _dt
from typing import List

import sqlalchemy as sa
from sqlalchemy import BigInteger, Float, ForeignKey, Index, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), default=lambda: _dt.datetime.now(_dt.timezone.utc))
    ended_at: Mapped[_dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # relationships
    eeg_samples: Mapped[List["EegSample"]] = relationship(back_populates="session", cascade="all, delete-orphan")  # noqa: F821
    accel_samples: Mapped[List["AccelerometerSample"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    freq_abs_samples: Mapped[List["FrequencyAbsoluteSample"]] = relationship(back_populates="session", cascade="all, delete-orphan")  # noqa: F821
    horseshoe_samples: Mapped[List["HorseshoeSample"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    touching_samples: Mapped[List["TouchingForeheadSample"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    blink_events: Mapped[List["BlinkEvent"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    jaw_events: Mapped[List["JawClenchEvent"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Session id={self.id} user={self.user}>"


class EegSample(Base):
    """EEG sample at 256 Hz (4 main channels + up to 4 auxiliary)."""

    __tablename__ = "eeg_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)

    # channel micro-volts
    tp9: Mapped[float] = mapped_column(Float)
    af7: Mapped[float] = mapped_column(Float)
    af8: Mapped[float] = mapped_column(Float)
    tp10: Mapped[float] = mapped_column(Float)
    aux_1: Mapped[float | None] = mapped_column(Float)
    aux_2: Mapped[float | None] = mapped_column(Float)
    aux_3: Mapped[float | None] = mapped_column(Float)
    aux_4: Mapped[float | None] = mapped_column(Float)

    session: Mapped[Session] = relationship(back_populates="eeg_samples")

    __table_args__ = (
        Index("ix_eeg_session_ts", "session_id", "timestamp"),
    )


class AccelerometerSample(Base):
    """Accelerometer sample (~52 Hz)."""

    __tablename__ = "accel_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)

    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    z: Mapped[float] = mapped_column(Float)

    session: Mapped[Session] = relationship(back_populates="accel_samples")

    __table_args__ = (
        Index("ix_accel_session_ts", "session_id", "timestamp"),
    )


class FrequencyAbsoluteSample(Base):
    """Absolute power for frequency bands (delta/theta/alpha/beta/gamma).

    Mind Monitor emits *separate* OSC messages per band (e.g. `/muse/elements/delta_absolute`).
    We normalise them into a single row with the `band` column naming the band.
    Each message provides either four floats (TP9, AF7, AF8, TP10) or a single average value.
    """

    __tablename__ = "freq_abs_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)

    band: Mapped[str] = mapped_column(String(8), index=True)  # e.g., "delta", "theta"
    avg_value: Mapped[float | None] = mapped_column(Float)
    tp9: Mapped[float | None] = mapped_column(Float)
    af7: Mapped[float | None] = mapped_column(Float)
    af8: Mapped[float | None] = mapped_column(Float)
    tp10: Mapped[float | None] = mapped_column(Float)

    session: Mapped[Session] = relationship(back_populates="freq_abs_samples")

    __table_args__ = (
        Index("ix_freq_band_session_ts", "band", "session_id", "timestamp"),
    )


class HorseshoeSample(Base):
    """Contact/horseshoe fit values per channel (0-3)."""

    __tablename__ = "horseshoe_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)

    tp9: Mapped[int] = mapped_column()
    af7: Mapped[int] = mapped_column()
    af8: Mapped[int] = mapped_column()
    tp10: Mapped[int] = mapped_column()

    session: Mapped[Session] = relationship(back_populates="horseshoe_samples")

    __table_args__ = (
        Index("ix_horse_session_ts", "session_id", "timestamp"),
    )


class TouchingForeheadSample(Base):
    """Binary flag (0/1) indicating sensor touching forehead quality metric."""

    __tablename__ = "touching_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)

    value: Mapped[bool] = mapped_column(Boolean)

    session: Mapped[Session] = relationship(back_populates="touching_samples")


class BlinkEvent(Base):
    """Blink detection (event)."""

    __tablename__ = "blink_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)

    strength: Mapped[float | None] = mapped_column(Float)  # Mind Monitor sends 1/0, keep float for future

    session: Mapped[Session] = relationship(back_populates="blink_events")


class JawClenchEvent(Base):
    """Jaw clench detection (event)."""

    __tablename__ = "jaw_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[_dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)

    strength: Mapped[float | None] = mapped_column(Float)

    session: Mapped[Session] = relationship(back_populates="jaw_events")
