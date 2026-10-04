"""Entry point: ``python -m nas_reach_mcp`` speaks stdio MCP.

mcpo is what turns this into Streamable HTTP for MCPHub.
"""

from .server import main

if __name__ == "__main__":
    main()
