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
os.environ["APP_ENV_FILE"] = ".env"


from enrichmcp import EnrichMCP
from enrichmcp.sqlalchemy import (
    include_sqlalchemy_models,
    sqlalchemy_lifespan,
)

# Support both "python -m muse_osc_mcp.mcp" and direct "python muse_osc_mcp/mcp.py" execution
try:
    from .db import initialize_global_db, get_engine, Base  # type: ignore
except ImportError:  # pragma: no cover
    # When executed as a standalone script without the package context
    from muse_osc_mcp.db import initialize_global_db, get_engine, Base  # type: ignore

initialize_global_db()
engine = get_engine()
app = EnrichMCP("Muse EEG data API", "Records and analyses Muse EEG data", lifespan=sqlalchemy_lifespan(Base, engine))
include_sqlalchemy_models(app, Base)

app.run()
# CLI entry-point -----------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    # Build the app (initialises DB and discovers models) then start the
    # built-in ASGI server provided by EnrichMCP.
    app.run()
