"""WikiPulse over MCP (stage 5): tools, a resource and prompts for any MCP client.

    uv run python -m wikipulse.mcp_server.server                     # stdio (Claude Desktop)
    uv run python -m wikipulse.mcp_server.server --transport http    # http://127.0.0.1:8765/mcp

`explore.py` does the work (reading the graph through `wikipulse.qa`, answering through
`QASession`); `server.py` only describes it to MCP clients.
"""
