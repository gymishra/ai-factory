"""Kiro stdio bridge for sap_ai_factory — Okta PKCE → AgentCore MCP runtime.
Uses MCP streamablehttp_client (same pattern as the working sap-smart-agent bridge).
"""
import os, sys, json, base64, time, asyncio, webbrowser, urllib.parse, secrets, hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Event
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ── Config ────────────────────────────────────────────────────────────────────
OKTA_DOMAIN      = os.environ.get("OKTA_DOMAIN",       "trial-1053860.okta.com")
OKTA_AUTH_SERVER = os.environ.get("OKTA_AUTH_SERVER",  "default")
OKTA_CLIENT_ID   = os.environ.get("OKTA_CLIENT_ID",    "0oa10vth79kZAuXGt698")
OKTA_CLIENT_SEC  = os.environ.get("OKTA_CLIENT_SECRET","")
OKTA_SCOPES      = os.environ.get("OKTA_SCOPES",       "agentcore")
OKTA_REDIRECT    = os.environ.get("OKTA_REDIRECT_URI", "http://localhost:8080/callback")
OKTA_PORT        = int(OKTA_REDIRECT.split(":")[-1].split("/")[0])

AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/authorize"
TOKEN_URL     = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/token"

RUNTIME_ARN   = os.environ.get("AGENTCORE_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:953841955037:runtime/sap_ai_factory-OFYrcN3gxF")
REGION        = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

TOKEN_CACHE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".okta_token_cache.json")

# Tools exposed to Kiro (the 5 router tools + 2 phase tools)
ROUTER_TOOLS = {
    "adt_agent_tool", "odata_agent_tool", "calm_agent_tool",
    "sf_agent_tool", "generator_agent_tool",
    "odata_analyze_tool", "odata_execute_tool",
}


# ── PKCE helpers ──────────────────────────────────────────────────────────────
def _pkce_pair():
    v = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


# ── OAuth callback ────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    auth_code = None
    event     = Event()

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Handler.auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Authenticated! Return to Kiro.</h2>")
        _Handler.event.set()

    def log_message(self, *a):
        pass


# ── Token management ──────────────────────────────────────────────────────────
def _load_cached_token() -> str:
    try:
        with open(TOKEN_CACHE) as f:
            token = json.load(f).get("access_token", "")
        if token:
            p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
            if json.loads(base64.b64decode(p)).get("exp", 0) > time.time() + 60:
                return token
    except Exception:
        pass
    return ""


def _save_token(token: str):
    with open(TOKEN_CACHE, "w") as f:
        json.dump({"access_token": token}, f)


def get_okta_token() -> str:
    cached = _load_cached_token()
    if cached:
        print("[bridge] Using cached Okta token", file=sys.stderr, flush=True)
        return cached

    verifier, challenge = _pkce_pair()
    _Handler.auth_code = None
    _Handler.event.clear()

    srv = HTTPServer(("localhost", OKTA_PORT), _Handler)
    Thread(target=lambda: srv.handle_request(), daemon=True).start()

    params = {
        "client_id": OKTA_CLIENT_ID, "response_type": "code",
        "scope": OKTA_SCOPES, "redirect_uri": OKTA_REDIRECT,
        "state": secrets.token_urlsafe(8),
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    print(f"[bridge] Opening browser for Okta login...\nIf it doesn't open:\n  {url}", file=sys.stderr, flush=True)
    webbrowser.open(url)

    _Handler.event.wait(timeout=120)
    srv.server_close()

    if not _Handler.auth_code:
        raise RuntimeError("Okta login timed out (120s)")

    print("[bridge] Auth code received, exchanging for token...", file=sys.stderr, flush=True)
    creds = base64.b64encode(f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SEC}".encode()).decode()
    with httpx.Client() as c:
        r = c.post(TOKEN_URL,
                   data={"grant_type": "authorization_code",
                         "code": _Handler.auth_code,
                         "redirect_uri": OKTA_REDIRECT,
                         "code_verifier": verifier},
                   headers={"Authorization": f"Basic {creds}",
                             "Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        token = r.json()["access_token"]

    _save_token(token)
    print("[bridge] Token acquired and cached", file=sys.stderr, flush=True)
    return token


# ── MCP URL ───────────────────────────────────────────────────────────────────
def get_mcp_url() -> str:
    enc = urllib.parse.quote(RUNTIME_ARN, safe="")
    return f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"


def _get_headers() -> dict:
    token = get_okta_token()
    return {
        "authorization": f"Bearer {token}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken": token,
        "Content-Type": "application/json",
    }


# ── Strip outputSchema (prevents Kiro validation errors) ─────────────────────
def _strip_output_schema(tools):
    for tool in tools:
        if hasattr(tool, "outputSchema"):
            object.__setattr__(tool, "outputSchema", None)
    return tools


# ── MCP Server (stdio) ────────────────────────────────────────────────────────
server = Server("sap-ai-factory-bridge")


@server.list_tools()
async def list_tools():
    headers = _get_headers()
    mcp_url = get_mcp_url()
    print(f"[bridge] Connecting to {mcp_url[:60]}...", file=sys.stderr, flush=True)
    async with streamablehttp_client(mcp_url, headers,
                                     timeout=timedelta(seconds=90),
                                     terminate_on_close=False) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.list_tools()
            tools = result.tools
            print(f"[bridge] Tools from runtime: {[t.name for t in tools]}", file=sys.stderr, flush=True)
            return _strip_output_schema(tools)


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    headers = _get_headers()
    mcp_url = get_mcp_url()
    async with streamablehttp_client(mcp_url, headers,
                                     timeout=timedelta(seconds=300),
                                     terminate_on_close=False) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments or {})
            return [types.TextContent(type="text", text=c.text)
                    for c in result.content if hasattr(c, "text")] \
                   or [types.TextContent(type="text", text="No result")]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
