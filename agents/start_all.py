"""
Start the AI Factory Parent MCP Server (in-process tools — no sub-agent processes).

The parent now imports all tool functions in-process and runs them via Strands
agents directly. No separate sub-agent HTTP servers are needed.

Container:  MCP_PORT=8000 python start_all.py  (set via Dockerfile ENV)
Local dev:  python start_all.py  → port 8100
"""
import os, sys, runpy

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))

# Default to 8100 locally; Dockerfile sets MCP_PORT=8000 for the container
os.environ.setdefault("MCP_PORT", "8100")

# Run the parent server as __main__ so its `if __name__ == "__main__"` block executes
runpy.run_path(os.path.join(BASE, "parent_mcp_server.py"), run_name="__main__")
