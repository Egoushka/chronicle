"""MCP server — the surface agent-runner actually calls.

agent-runner is already a LangGraph ReAct+critic loop with MCP tools and a
6-step cap. It does not need rebuilding; it needs a better tool. Registering
this server in metamcp's `homelab` namespace makes Chronicle available to the
Telegram assistant with zero changes to agent-runner.

Tool design follows the retrieval pipeline, not the database. Five query
classes, five tools, because "when did I first mention X" and "how did my
view evolve" are different systems and collapsing them into one
`search_memory` tool is how you get 15% accuracy on temporal questions.
"""

from __future__ import annotations

import logging
import os

import httpx
from mcp.server.mcpserver import MCPServer

log = logging.getLogger(__name__)
API = os.environ.get("CHRONICLE_API_URL", "http://chronicle-api:8030")

# mcp 2.x renamed FastMCP to MCPServer and moved host/port the OTHER way:
# 1.x took them on the constructor and run() rejected them; 2.x's constructor
# has no host/port at all and run("sse", host=..., port=...) takes them
# (mcp/server/mcpserver/server.py, the run() overloads). See __main__ below.
mcp = MCPServer("chronicle")
_client = httpx.AsyncClient(base_url=API, timeout=120.0)


@mcp.tool()
async def recall(query: str, date_from: str | None = None,
                 date_to: str | None = None, source: str | None = None,
                 limit: int = 20) -> str:
    """Search the owner's life archive by meaning AND keyword (hybrid + rerank).

    Use for open-ended questions about what was said or happened: "what did we
    decide about the apartment", "what do I know about X". Returns segments
    (multi-message conversations), not individual messages, each with its date,
    thread and source event ids for citation.

    Do NOT use for: counting (use `tally`), earliest occurrence (use
    `first_mention`), or how something changed over years (use `evolution`).
    Those are different operations and this tool answers them badly.
    """
    r = await _client.post("/recall", json={
        "query": query, "date_from": date_from, "date_to": date_to,
        "source": source, "limit": limit})
    r.raise_for_status()
    return r.text


@mcp.tool()
async def first_mention(term: str) -> str:
    """Find the EARLIEST time the owner mentioned something.

    This is an argmin over timestamp, not a similarity search — top-k
    retrieval structurally cannot answer it, because the most similar message
    is almost never the earliest one. Handles Russian/Ukrainian morphology and
    cross-script spellings automatically.

    Use for: "when did I first hear about X", "when did I start talking about Y".
    """
    r = await _client.post("/first-mention", json={"term": term})
    r.raise_for_status()
    return r.text


@mcp.tool()
async def evolution(topic: str, bin_width: str = "3 months") -> str:
    """Trace how the owner's view on something changed across the whole archive.

    Retrieves independently within each time bin so early periods are not
    crowded out by a burst of later activity, summarizes each period, and
    presents them chronologically. Slower than `recall` (~30 LLM calls).

    Use for: "how did my opinion on X change", "what happened with Y over the
    years", "when did I stop caring about Z".
    """
    r = await _client.post("/evolution", json={"topic": topic, "bin_width": bin_width})
    r.raise_for_status()
    return r.text


@mcp.tool()
async def tally(question: str) -> str:
    """Count, rank or aggregate over the archive using SQL.

    Returns the generated SQL alongside the result — always show both to
    the user. Text-to-SQL is ~80% accurate even on simple schemas, so a query
    they cannot see is a number they cannot trust.

    Use for: "how many times did I message X in 2022", "which chat is busiest",
    "what months did I code the most".
    """
    r = await _client.post("/tally", json={"question": question})
    r.raise_for_status()
    return r.text


@mcp.tool()
async def timeline(date_from: str, date_to: str, sources: list[str] | None = None) -> str:
    """Reconstruct what was happening in a period, across ALL sources.

    This is the tool that uses more than Telegram: conversations, where the
    owner was (dawarich), what they were coding (wakapi), photos, transactions. The
    behavioural signals are more honest than the conversational ones because
    they are not curated.

    Use for: "what was going on in summer 2024", "what was happening before X".
    """
    r = await _client.post("/timeline", json={
        "date_from": date_from, "date_to": date_to, "sources": sources})
    r.raise_for_status()
    return r.text


@mcp.tool()
async def open_commitments(older_than_days: int = 90) -> str:
    """Promises the owner made that have no evidence of being kept.

    Extracted from conversation and tracked with a status lifecycle. This is
    what turns an archive into something that acts on its owner rather
    than something they have to remember to query.
    """
    r = await _client.post("/commitments", json={"older_than_days": older_than_days})
    r.raise_for_status()
    return r.text


@mcp.tool()
async def ground(claim: str, limit: int = 10) -> str:
    """Find evidence in the archive for (or against) a claim from Hindsight.

    Hindsight holds ~5,000 curated facts with no evidence trail. Chronicle
    holds 681,000 events that are nothing but evidence. This tool is the
    bridge: given a remembered claim, return the actual conversations behind
    it — INCLUDING any that contradict it.

    Use whenever a Hindsight fact is load-bearing for an answer, especially if
    it is old. A fact is stale when the evidence says it changed, not when a
    timer expires.
    """
    r = await _client.post("/ground", json={"claim": claim, "limit": limit})
    r.raise_for_status()
    return r.text


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # The default host is 127.0.0.1, which inside a container means nothing
    # outside it can connect — so 0.0.0.0 here is what makes the published
    # 127.0.0.1:8031 mapping reach anything. Exposure is bounded by that port
    # binding and by `edge`, not by this. Binding 127.0.0.1 would also switch
    # on the SDK's DNS-rebinding guard, which rejects any Host but localhost.
    # SSE still serves /sse + /messages/ in 2.x, so the compose healthcheck
    # and every client config stay as they were.
    mcp.run("sse", host="0.0.0.0", port=8031)
