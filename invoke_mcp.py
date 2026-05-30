"""
invoke_mcp.py — Call the SAP MCP quicksuite AgentCore runtime.

Auth options (in priority order):
  1. Set env var:  FEDERATE_TOKEN=<jwt>   (paste token from browser/curl)
  2. Pass on CLI:  python invoke_mcp.py <jwt>
  3. Set env var:  FEDERATE_REDIRECT_URI=<uri>  to override the redirect URI
     and run the browser-based PKCE flow automatically.

To get a token manually:
  - Open the AUTH_URL below in a browser (already logged into Amazon Federate)
  - Complete login, copy the `code=` value from the redirect URL
  - Exchange it with curl (see get_token_from_code() below)
"""

import boto3
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
from botocore.credentials import Credentials
import botocore.session

# ── Configuration ─────────────────────────────────────────────────────────────
REGION      = "us-east-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:026090527506:runtime/AWS_For_SAP_MCP_Server_sapmcp4-63cgTa5OLl"
RUNTIME_URL = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{RUNTIME_ARN.split('/')[-1]}/invocations"

CLIENT_ID   = "sap-mcp-quicksuite-alpha"
TOKEN_URL   = "https://idp-integ.federate.amazon.com/api/oauth2/v2/token"
AUTH_URL    = "https://idp-integ.federate.amazon.com/api/oauth2/v1/authorize"
SAP_SCOPES  = "/IWFND/SG_MED_CATALOG_0002 ZAPI_SALES_ORDER_SRV_0001"

# Override via env var if you know the registered redirect URI
REDIRECT_URI = os.environ.get("FEDERATE_REDIRECT_URI", "http://localhost:9876/callback")


# ── Option A: token supplied directly ────────────────────────────────────────

def get_token_from_env_or_arg() -> str | None:
    """Return token from env var or CLI arg, if provided."""
    if len(sys.argv) > 1:
        print("[auth] Using token from command-line argument")
        return sys.argv[1]
    t = os.environ.get("FEDERATE_TOKEN", "")
    if t:
        print("[auth] Using token from FEDERATE_TOKEN env var")
        return t
    return None


# ── Option B: PKCE browser flow ───────────────────────────────────────────────

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
            msg = b"<h2>Login failed. Check the terminal for details.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, *args):
        pass


def get_token_via_pkce() -> str:
    """Browser-based PKCE flow. Requires REDIRECT_URI to be allowlisted in Federate."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    parsed = urlparse(REDIRECT_URI)
    port   = parsed.port or 80

    server = HTTPServer(("localhost", port), _CallbackHandler)
    t = threading.Thread(target=lambda: server.handle_request(), daemon=True)
    t.start()

    auth_params = {
        "response_type":         "code",
        "client_id":             CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SAP_SCOPES,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    login_url = f"{AUTH_URL}?{urlencode(auth_params)}"
    print(f"\n[auth] Opening browser — if it doesn't open, visit:\n  {login_url}\n")
    webbrowser.open(login_url)

    t.join(timeout=120)
    server.server_close()

    if _CallbackHandler.auth_error:
        raise RuntimeError(f"IDP error: {_CallbackHandler.auth_error}")
    if not _CallbackHandler.auth_code:
        raise RuntimeError("Timed out waiting for login (120s)")

    print("[auth] Code received, exchanging for token...")
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "code":          _CallbackHandler.auth_code,
        "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)

    if not resp.ok:
        print(f"[auth] {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"No access_token in response: {resp.json()}")
    print(f"[auth] Token acquired (expires_in={resp.json().get('expires_in')}s)")
    return token


# ── Option C: manual instructions ────────────────────────────────────────────

def print_manual_instructions():
    verifier, challenge = _pkce_pair()
    params = {
        "response_type":         "code",
        "client_id":             CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SAP_SCOPES,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    print("\n" + "=" * 70)
    print("MANUAL TOKEN ACQUISITION")
    print("=" * 70)
    print("1. Open this URL in your browser (must be logged into Amazon Federate):")
    print(f"\n   {AUTH_URL}?{urlencode(params)}\n")
    print("2. After login, you'll be redirected to a URL like:")
    print(f"   {REDIRECT_URI}?code=XXXXXX&state=...")
    print("\n3. Copy the `code` value and run:")
    print(f"""
   curl -X POST {TOKEN_URL} \\
     -d "grant_type=authorization_code" \\
     -d "client_id={CLIENT_ID}" \\
     -d "redirect_uri={REDIRECT_URI}" \\
     -d "code=PASTE_CODE_HERE" \\
     -d "code_verifier={verifier}"
""")
    print("4. Copy the access_token from the response and run:")
    print("   set FEDERATE_TOKEN=<paste_token_here>")
    print("   python invoke_mcp.py")
    print("=" * 70)
    print(f"\ncode_verifier (save this): {verifier}")
    print(f"code_challenge:            {challenge}\n")


# ── MCP invocation (raw HTTPS + SigV4 + Bearer) ───────────────────────────────

def call_mcp(token: str, method: str, params: dict | None = None):
    """
    Call the MCP runtime via raw HTTPS.
    Sends both SigV4 (for cross-account AWS auth) and Bearer token (for JWT authorizer).
    """
    if params is None:
        params = {}

    body = json.dumps({
        "jsonrpc": "2.0",
        "id":      1,
        "method":  method,
        "params":  params,
    }).encode()

    # Build and sign the request with SigV4
    boto_session = botocore.session.get_session()
    creds = boto_session.get_credentials().get_frozen_credentials()

    aws_request = AWSRequest(
        method="POST",
        url=RUNTIME_URL,
        data=body,
        headers={
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",   # JWT for the runtime's authorizer
        },
    )
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_request)

    resp = requests.request(
        method=aws_request.method,
        url=aws_request.url,
        headers=dict(aws_request.headers),
        data=body,
        timeout=30,
    )

    if not resp.ok:
        print(f"[mcp] HTTP {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()

    raw = resp.text
    json_data = json.loads(raw[raw.find("{"):])
    return json_data.get("result", json_data)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Try to get token
    token = get_token_from_env_or_arg()

    if not token:
        try:
            token = get_token_via_pkce()
        except Exception as e:
            print(f"[auth] PKCE flow failed: {e}")
            print_manual_instructions()
            sys.exit(1)

    # List available tools
    print("\n=== Available Tools ===")
    tools_result = call_mcp(token, "tools/list")
    for tool in tools_result.get("tools", []):
        print(f"  {tool['name']}: {tool.get('description', '')}")
    print()

    # Example tool calls
    print("add_numbers(5, 3)")
    result = call_mcp(token, "tools/call", {"name": "add_numbers", "arguments": {"a": 5, "b": 3}})
    print(json.dumps(result.get("structuredContent"), indent=2))
    print()

    print("multiply_numbers(4, 7)")
    result = call_mcp(token, "tools/call", {"name": "multiply_numbers", "arguments": {"a": 4, "b": 7}})
    print(json.dumps(result.get("structuredContent"), indent=2))
    print()

    print("greet_user('TestUser')")
    result = call_mcp(token, "tools/call", {"name": "greet_user", "arguments": {"name": "TestUser"}})
    print(json.dumps(result.get("structuredContent"), indent=2))


if __name__ == "__main__":
    main()
