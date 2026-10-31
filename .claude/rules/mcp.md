---
paths:
  - "chronicle/mcp_server.py"
  - "Dockerfile.mcp"
  - "tests/test_mcp_server.py"
---
Split from CLAUDE.md, loaded when matching files are touched: MCP server: hard-won facts.

21. **An MCP SDK major breaks the server API — pin the major.** 2.0.0
   removed `mcp.server.fastmcp`; an unpinned rebuild pulled it and
   crash-looped the container. Ported to 2.x on 2026-09-26
   (`mcp.server.mcpserver.MCPServer`) and pinned `>=2.2,<3` in both
   `Dockerfile.mcp` and `pyproject.toml`; `tests/test_mcp_server.py` keeps the
   two pins in step. The next major is deliberate work too.
22. **host and port go to `run()` in 2.x — the opposite of 1.x.** 1.x took
   them on the constructor and `FastMCP.run()` rejected them; 2.x's
   constructor has none and `mcp.run("sse", host=, port=)` takes them. The
   default host is `127.0.0.1`, which inside a container means nothing outside
   it can ever connect — and in 2.x a loopback host also switches on the DNS-
   rebinding guard, which rejects every Host header but localhost.
23. **The SDK's SSE transport serves `/sse` and `/messages/` — there is no
   `/health`.** The healthcheck probed `/health`, got 404, and marked a
   working server unhealthy; with `autoheal=true` that is an infinite restart
   loop on a service that was fine. `/sse` is a stream so curl always exits
   28 — compare `%{http_code}` instead of trusting the exit code. Unchanged
   in 2.x, as are all seven tool schemas.
