"""The MCP tool surface is a contract with agent-runner, not an implementation detail.

Agents are prompted against these exact tool names and parameters; an SDK
major bump that renames or drops one breaks every caller without an error on
this side. Hard-won fact 21: the 1.x -> 2.x move crash-looped the container.
"""

import asyncio
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

TOOLS = {
    "recall": ["query", "date_from", "date_to", "source", "limit"],
    "first_mention": ["term"],
    "evolution": ["topic", "bin_width"],
    "tally": ["question"],
    "timeline": ["date_from", "date_to", "sources"],
    "open_commitments": ["older_than_days"],
    "ground": ["claim", "limit"],
}


def test_dockerfile_and_pyproject_pin_the_same_mcp_range():
    # Runs without mcp installed. Two pins that drift apart is how the image
    # ends up on a major the code was never ported to.
    docker = re.search(r"pip install.*'mcp([^']*)'", (ROOT / "Dockerfile.mcp").read_text()).group(1)
    pyproject = re.search(r'"mcp([^"]*)"', (ROOT / "pyproject.toml").read_text()).group(1)
    assert docker == pyproject
    assert "<" in docker, "an unbounded mcp pin pulls the next breaking major"


def test_tool_surface_is_unchanged():
    # `make test` installs only pytest/ruff/numpy, so this skips there; it runs
    # wherever the mcp 2.x SDK is installed.
    pytest.importorskip("httpx")
    pytest.importorskip("mcp.server.mcpserver")
    from chronicle.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert {t.name: list(t.input_schema["properties"]) for t in tools} == TOOLS
    assert all(t.description for t in tools)
