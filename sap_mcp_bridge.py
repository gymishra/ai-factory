"""
sap_mcp_bridge.py — stdio MCP bridge to the SAP AgentCore runtime.

Kiro (or any MCP client) launches this as a subprocess over stdio.
It proxies all MCP JSON-RPC calls to the remote AgentCore runtime,
authenticating via a Federate JWT token.

Auth (in priority order):
  1. FEDERATE_TOKEN env var  — paste a token you obtained externally
  2. PKCE browser flow       — set FEDERATE_REDIRECT_URI to the allowlisted URI
"""

import asyncio
import json
import os
import sys
import secrets
import hashlib
import base64
import threading
import webbrowser
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import botocore.session

# ── Configuration ─────────────────────────────────────────────────────────────
REGION       = "us-east-1"
RUNTIME_ARN  = "arn:aws:bedrock-agentcore:us-east-1:026090527506:runtime/AWS_For_SAP_MCP_Server_sapmcp4-63cgTa5OLl"
RUNTIME_ID   = RUNTIME_ARN.split("/")[-1]
RUNTIME_URL  = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{RUNTIME_ID}/invocations"

CLIENT_ID    = "sap-mcp-quicksuite-alpha"
TOKEN_URL    = "https://idp-integ.federate.amazon.com/api/oauth2/v2/token"
AUTH_URL     = "https://idp-integ.federate.amazon.com/api/oauth2/v1/authorize"
SAP_SCOPES   = "/IWFND/SG_MED_CATALOG_0002 ZAPI_SALES_ORDER_SRV_0001"
REDIRECT_URI = os.environ.get("FEDERATE_REDIRECT_URI", "http://localhost:9876/callback")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _pkce_pair():
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code  = None
    auth_error = None

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            msg = b"<h2>Login successful. You can close this tab.</h2>"
        else:
            _CallbackHandler.auth_error = params.get("error", ["unknown"])[0]
            msg = b"<h2>Login failed.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, *args):
        pass


def _get_token_pkce() -> str:
    verifier, challenge = _pkce_pair()
    parsed = urlparse(REDIRECT_URI)
    port   = parsed.port or 80

    server = HTTPServer(("localhost", port), _CallbackHandler)
    threading.Thread(target=lambda: server.handle_request(), daemon=True).start()

    params = {
        "response_type": "code", "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI, "scope": SAP_SCOPES,
        "state": secrets.token_urlsafe(16),
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    webbrowser.open(f"{AUTH_URL}?{urlencode(params)}")

    # Wait up to 120s for the callback
    for _ in range(120):
        import time; time.sleep(1)
        if _CallbackHandler.auth_code or _CallbackHandler.auth_error:
            break
    server.server_close()

    if _CallbackHandler.auth_error:
        raise RuntimeError(f"IDP error: {_CallbackHandler.auth_error}")
    if not _CallbackHandler.auth_code:
        raise RuntimeError("Login timed out")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI, "code": _CallbackHandler.auth_code,
        "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def acquire_token() -> str:
    t = os.environ.get("FEDERATE_TOKEN", "").strip()
    if t:
        return t
    return _get_token_pkce()


# ── Remote MCP call ───────────────────────────────────────────────────────────

_boto_session = botocore.session.get_session()

def _remote_call(token: str, rpc_request: dict) -> dict:
    """Forward a JSON-RPC request to the AgentCore runtime and return the response dict."""
    body = json.dumps(rpc_request).encode()

    creds = _boto_session.get_credentials().get_frozen_credentials()
    aws_req = AWSRequest(
        method="POST",
        url=RUNTIME_URL,
        data=body,
        headers={
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_req)

    resp = requests.request(
        method=aws_req.method,
        url=aws_req.url,
        headers=dict(aws_req.headers),
        data=body,
        timeout=60,
    )

    if not resp.ok:
        # Return a JSON-RPC error so the client handles it gracefully
        return {
            "jsonrpc": "2.0",
            "id": rpc_request.get("id"),
            "error": {"code": -32000, "message": f"HTTP {resp.status_code}: {resp.text[:300]}"},
        }

    raw = resp.text
    return json.loads(raw[raw.find("{"):])


# ── stdio MCP server loop ─────────────────────────────────────────────────────

async def run_stdio(token: str):
    """
    Read JSON-RPC requests from stdin (one per line), proxy to the runtime,
    write responses to stdout.
    """
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    # stdout writer
    transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.BaseProtocol(), sys.stdout.buffer
    )

    def write(obj: dict):
        line = json.dumps(obj) + "\n"
        sys.stdout.buffer.write(line.encode())
        sys.stdout.buffer.flush()

    while True:
        try:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                write({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32700, "message": f"Parse error: {e}"}})
                continue

            response = await loop.run_in_executor(None, _remote_call, token, request)
            write(response)

        except asyncio.CancelledError:
            break
        except Exception as e:
            write({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32603, "message": str(e)}})


def main():
    try:
        token = acquire_token()
    except Exception as e:
        # Write a fatal error as a JSON-RPC notification so the client sees it
        msg = json.dumps({
            "jsonrpc": "2.0", "method": "notifications/message",
            "params": {"level": "error", "data": f"Auth failed: {e}"}
        }) + "\n"
        sys.stdout.buffer.write(msg.encode())
        sys.stdout.buffer.flush()
        sys.exit(1)

    asyncio.run(run_stdio(token))


if __name__ == "__main__":
    main()
