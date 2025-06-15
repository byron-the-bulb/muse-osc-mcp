# Muse OSC MCP Server

This project implements an **MCP (Model-Context Protocol) server** that records EEG and auxiliary signals from a **Muse headband**.

Signals are emitted by the **Mind Monitor** mobile app via **OSC (Open Sound Control)** and ingested here, stored in a PostgreSQL (AWS Aurora) database, and made available through MCP endpoints for downstream applications and analysis.

## Key links
- Mind-Monitor OSC specification: <https://mind-monitor.com/FAQ.php#oscspec>
- MCP Python SDK: <https://github.com/modelcontextprotocol/python-sdk>

## Tech stack
- Python 3.10+
- [`python-osc`](https://pypi.org/project/python-osc/) – OSC receiver
- [`SQLAlchemy`](https://pypi.org/project/SQLAlchemy/) & [`asyncpg`](https://pypi.org/project/asyncpg/) – async DB layer
- [`mcp-sdk`](https://pypi.org/project/mcp-sdk/) – MCP server utilities
- [`uv`](https://github.com/astral-sh/uv) – fast dependency manager / virtual-env tool (recommended for development)

## Quick start
```bash
# Create a virtual-env & install deps (requires uv >=0.1.38)
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt  # or `uv pip install -e .` if using pyproject



# Run the server (placeholder)
python -m muse_osc_mcp.server
```

## Project layout
```
├── muse_osc_mcp/        # Python package (server, models, mcp integration)
├── tests/               # Unit / integration tests
├── README.md            # This file
├── pyproject.toml       # Build & dependency spec
└── .gitignore
```

