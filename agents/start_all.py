"""
Start all AI Factory sub-agents + parent MCP server.

Local dev:  python start_all.py
            Parent MCP Server on http://localhost:8100/mcp

Container:  MCP_PORT=8000 python start_all.py  (set via Dockerfile ENV)
            Parent MCP Server on http://localhost:8000/mcp
"""
import subprocess, sys, time, os, signal

BASE   = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

# Parent port: 8000 in container (MCP_PORT env), 8100 locally
PARENT_PORT = int(os.environ.get("MCP_PORT", "8100"))

SUB_AGENTS = [
    ("ADT Agent",            "adt_agent.py",       8101),
    ("OData Agent",          "odata_agent.py",     8102),
    ("Cloud ALM Agent",      "calm_agent.py",      8103),
    ("SuccessFactors Agent", "sf_agent.py",        8104),
    ("Generator Agent",      "generator_agent.py", 8105),
]

procs = []  # list of (name, process, port, script)


def shutdown(signum=None, frame=None):
    print("\nShutting down all agents...")
    for name, p, port, script in procs:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

# Start sub-agents first
for name, script, port in SUB_AGENTS:
    env = {**os.environ}
    p = subprocess.Popen([PYTHON, os.path.join(BASE, script)], env=env)
    procs.append((name, p, port, script))
    print(f"Started {name} (pid {p.pid}) on port {port}")
    time.sleep(1)

# Wait for sub-agents to initialise, then start parent
print("\nWaiting for sub-agents to be ready...")
time.sleep(5)

# Start parent MCP server — the external MCP entry point
parent_env = {**os.environ, "MCP_PORT": str(PARENT_PORT)}
parent_proc = subprocess.Popen([PYTHON, os.path.join(BASE, "parent_mcp_server.py")], env=parent_env)
procs.append(("Parent MCP Server", parent_proc, PARENT_PORT, "parent_mcp_server.py"))
print(f"Started Parent MCP Server (pid {parent_proc.pid}) on port {PARENT_PORT}")

print(f"\n=== AI Factory ready ===")
print(f"MCP endpoint: http://localhost:{PARENT_PORT}/mcp")
print(f"Sub-agents:   ports 8101-8105 (internal)\n")

# Keep running — monitor for crashes
try:
    while True:
        for i, (name, p, port, script) in enumerate(procs):
            if p.poll() is not None:
                print(f"WARNING: {name} (port {port}) exited with code {p.returncode} — restarting...")
                env = {**os.environ}
                if script == "parent_mcp_server.py":
                    env["MCP_PORT"] = str(PARENT_PORT)
                new_p = subprocess.Popen([PYTHON, os.path.join(BASE, script)], env=env)
                procs[i] = (name, new_p, port, script)
                print(f"  Restarted {name} (pid {new_p.pid})")
        time.sleep(5)
except KeyboardInterrupt:
    shutdown()
