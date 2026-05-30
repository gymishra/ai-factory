# SAP AI Factory — AgentCore Deployment & Kiro MCP Troubleshooting Guide

This document captures the full setup and the hard-won fixes for connecting the
`sap_ai_factory` MCP server (hosted on Amazon Bedrock AgentCore) to Kiro.

---

## Architecture Overview

```
Kiro (MCP client)
   │  stdio
   ▼
kiro_bridge.py  (local stdio → streamable-HTTP bridge, Okta PKCE auth)
   │  HTTPS + Bearer JWT
   ▼
Bedrock AgentCore Runtime  (sap_ai_factory-OFYrcN3gxF)
   │  validates JWT, forwards request
   ▼
Container (port 8000)  → parent_mcp_server.py
   ├── adt_agent.py        :8101
   ├── odata_agent.py      :8102
   ├── calm_agent.py       :8103
   ├── sf_agent.py         :8104
   └── generator_agent.py  :8105
```

- **Runtime ARN**: `arn:aws:bedrock-agentcore:us-east-1:953841955037:runtime/sap_ai_factory-OFYrcN3gxF`
- **Account / Region**: `953841955037` / `us-east-1`
- **ECR image**: `953841955037.dkr.ecr.us-east-1.amazonaws.com/bedrock-agentcore-sap_ai_factory:latest`
- **Auth**: Okta JWT (`trial-1053860.okta.com`), client `0oa10vth79kZAuXGt698`, scope `agentcore`

---

## THE ROOT CAUSE (read this first)

After a long debug session chasing what looked like an auth problem, the **actual
bug** was a Python dependency version:

> **`mcp==1.27.2` breaks the streamable-HTTP transport.** It returns `401 Unauthorized`
> on every request because newer MCP versions (>= 1.20) added a stricter session/auth
> handshake that is incompatible with how AgentCore proxies MCP requests.

The fix is to **pin the MCP library below 1.20**, which requires pinning
`strands-agents` below 1.10 (otherwise it transitively pulls `mcp>=1.20`).

### Working `requirements.txt`

```
mcp>=1.10.0,<1.20.0
httpx
boto3
uvicorn
starlette
strands-agents>=1.0.0,<1.10.0
bedrock-agentcore<=0.1.5
bedrock-agentcore-starter-toolkit==0.1.14
```

Confirmed working: **mcp 1.19.0**. The container `initialize` response shows
`serverInfo: AI Factory, version 1.19.0` and tools list correctly.

---

## Symptoms vs Real Cause (don't get misled)

| Symptom seen | What we thought | Actual cause |
|---|---|---|
| `401 Unauthorized` on `POST /mcp/` in container logs | OAuth/JWT misconfig | mcp 1.27 transport rejecting requests |
| `Authorization method mismatch (OAuth or SigV4)` | SigV4 vs Bearer conflict | boto3 adds SigV4; must use raw Bearer-only HTTPS |
| `accountID is required...` | wrong URL | URL needs full ARN URL-encoded in path |
| `424 Failed Dependency` | container down | `protocolConfiguration` (MCP) missing on runtime |
| `-32010 Received error (401) from runtime` | container auth check | mcp 1.27 transport (the real bug) |
| 0 tools in Kiro | bridge filter too strict | same mcp 1.27 issue — initialize failed |

---

## Critical Gotchas

### 1. `agentcore launch` RESETS runtime config
Every `agentcore launch` wipes both:
- `authorizerConfiguration` (JWT auth) → becomes `null`
- `protocolConfiguration` (MCP) → becomes `null`

**Always re-apply both after every launch** (script below). Without `protocolConfiguration: MCP`
you get `424 Failed Dependency`. Without the JWT authorizer you get auth-method-mismatch.

### 2. `agentcore launch` may SKIP rebuilds
The toolkit hashes the source and skips re-uploading to S3 if it thinks nothing
changed. Your Dockerfile / requirements edits then never reach CodeBuild — it
redeploys the OLD cached image. **Verify the build actually ran your changes.**

If it skips, force a fresh build by uploading the source zip yourself (script below).

### 3. The container must NOT do its own auth
AgentCore validates the JWT at the gateway. The container should just run:
```python
mcp.run(transport="streamable-http")
```
Do **not** wrap it in a custom `OAuthMiddleware` — that double-checks auth and
returns 401 because AgentCore strips/relocates the Authorization header.

### 4. Bridge must use `streamablehttp_client`, not raw `requests`
The Kiro bridge connects with MCP's native `streamablehttp_client` over async,
sending the token in BOTH headers:
```python
headers = {
    "authorization": f"Bearer {token}",
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken": token,
}
```
Raw `requests.post` with SigV4 causes "Authorization method mismatch".

---

## Operational Runbook

### A. Full redeploy (code changed)

```bash
# 1. Launch (builds via CodeBuild in the cloud — no local Docker needed)
agentcore launch --agent sap_ai_factory --auto-update-on-conflict
```

```python
# 2. Re-apply MCP protocol + JWT auth (ALWAYS required after launch)
import boto3
c = boto3.client('bedrock-agentcore-control', region_name='us-east-1')
r = c.get_agent_runtime(agentRuntimeId='sap_ai_factory-OFYrcN3gxF')
c.update_agent_runtime(
    agentRuntimeId='sap_ai_factory-OFYrcN3gxF',
    agentRuntimeArtifact=r['agentRuntimeArtifact'],
    roleArn=r['roleArn'],
    networkConfiguration=r['networkConfiguration'],
    protocolConfiguration={'serverProtocol': 'MCP'},
    authorizerConfiguration={'customJWTAuthorizer': {
        'discoveryUrl': 'https://trial-1053860.okta.com/oauth2/default/.well-known/openid-configuration',
        'allowedClients': ['0oa10vth79kZAuXGt698'],
    }},
)
```

### B. Force a rebuild when `agentcore launch` skips it

```python
import boto3, zipfile, os
root = r'C:\Users\gyanmis\Documents\AI Factory\ai-factory'
exclude_dirs = {'.git', '__pycache__', '.venv', 'venv', '.data',
                'lambda_extracted', 'lambda_mcp_proxy'}
exclude_ext = {'.pyc', '.log', '.zip'}

out = os.path.join(root, 'source_fresh.zip')
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in exclude_dirs]
        for fn in fns:
            if any(fn.endswith(e) for e in exclude_ext) or fn == 'source_fresh.zip':
                continue
            full = os.path.join(dp, fn)
            z.write(full, os.path.relpath(full, root))

s3 = boto3.client('s3', region_name='us-east-1')
bucket = 'bedrock-agentcore-codebuild-sources-953841955037-us-east-1'
s3.upload_file(out, bucket, 'sap_ai_factory/source.zip')

cb = boto3.client('codebuild', region_name='us-east-1')
build = cb.start_build(projectName='bedrock-agentcore-sap_ai_factory-builder')
print('Build:', build['build']['id'])
```

Then update the runtime to pull `:latest` (use the update script in step A2, or
explicitly set `agentRuntimeArtifact` to the `:latest` containerUri).

### C. Verify the deployed image (sanity test)

```python
import urllib.parse, requests, json
ARN = 'arn:aws:bedrock-agentcore:us-east-1:953841955037:runtime/sap_ai_factory-OFYrcN3gxF'
with open(r'C:\Users\gyanmis\Documents\AI Factory\ai-factory\.okta_token_cache.json') as f:
    token = json.load(f)['access_token']
enc = urllib.parse.quote(ARN, safe='')
url = f'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT'
body = json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize',
    'params':{'protocolVersion':'2024-11-05','capabilities':{},
              'clientInfo':{'name':'test','version':'1.0'}}}).encode()
resp = requests.post(url, data=body, headers={
    'authorization': f'Bearer {token}',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream'}, timeout=60)
print(resp.status_code, resp.text[:300])
# GOOD: serverInfo with version 1.19.0
# BAD:  -32010 "Received error (401)" → mcp version regression, rebuild needed
```

### D. Check CloudWatch logs

```python
import boto3, time
logs = boto3.client('logs', region_name='us-east-1')
lg = '/aws/bedrock-agentcore/runtimes/sap_ai_factory-OFYrcN3gxF-DEFAULT'
streams = logs.describe_log_streams(logGroupName=lg, orderBy='LastEventTime',
                                    descending=True, limit=1)
s = streams['logStreams'][0]['logStreamName']
events = logs.get_log_events(logGroupName=lg, logStreamName=s,
                             startTime=int((time.time()-300)*1000), limit=50)
for e in events['events']:
    if e['message'].strip(): print(e['message'])
```

### E. Verify mcp version in a CodeBuild run

The Dockerfile contains a build-time assertion that prints and validates the mcp
version, failing the build if it is not 1.1x:

```dockerfile
RUN python -c "import importlib.metadata as m; v=m.version('mcp'); \
    print('MCP VERSION:', v); assert v.startswith('1.1'), f'WRONG MCP {v}'"
```

Look for `MCP VERSION: 1.19.0` in the build log. If you instead see
`+ mcp==1.27.2` with NO `MCP VERSION` line, the build used a **cached image** and
your source wasn't picked up — use runbook B.

---

## Kiro MCP Configuration

User-level config: `~/.kiro/settings/mcp.json` → entry `sap-ai-factory`

```json
"sap-ai-factory": {
  "command": "python",
  "args": ["C:\\Users\\gyanmis\\Documents\\AI Factory\\ai-factory\\kiro_bridge.py"],
  "env": {
    "AGENTCORE_ARN": "arn:aws:bedrock-agentcore:us-east-1:953841955037:runtime/sap_ai_factory-OFYrcN3gxF",
    "AWS_DEFAULT_REGION": "us-east-1",
    "OKTA_DOMAIN": "trial-1053860.okta.com",
    "OKTA_AUTH_SERVER": "default",
    "OKTA_CLIENT_ID": "0oa10vth79kZAuXGt698",
    "OKTA_CLIENT_SECRET": "<secret>",
    "OKTA_REDIRECT_URI": "http://localhost:8080/callback",
    "OKTA_SCOPES": "agentcore",
    "SAP_BEARER_TOKEN": ""
  },
  "disabled": false,
  "autoApprove": []
}
```

Notes:
- `OKTA_REDIRECT_URI` must be **allowlisted** in the Okta app (8080/8086/8087 are registered).
- `OKTA_SCOPES = agentcore` → token gets `aud=SAPMCP`, `cid=0oa10vth79kZAuXGt698`
  (the `cid` is what the runtime's `allowedClients` validates).
- Token is cached in `.okta_token_cache.json` so you don't re-login on every restart.

To activate: Command Palette → **MCP: Restart MCP Server** → `sap-ai-factory`.
First `tools/list` may take ~30s while the container cold-starts (5 sub-agents).

---

## Okta / Auth Reference

- **Token endpoint**: `https://trial-1053860.okta.com/oauth2/default/v1/token`
- **Authorize endpoint**: `https://trial-1053860.okta.com/oauth2/default/v1/authorize`
- **Flow**: `authorization_code` + PKCE (browser login). `client_credentials` also
  works for testing and yields `aud=SAPMCP, scp=[agentcore]`.
- **Runtime authorizer** only checks `allowedClients` (the `cid` claim). Audience
  and scope checks are NOT enabled on the runtime.

---

## Quick Checklist When "0 tools" / connection fails

1. Direct `initialize` test (runbook C) → is it `-32010 (401)` or a real response?
2. If 401 → check the deployed image's mcp version (must be < 1.20). Rebuild via runbook B.
3. Check `protocolConfiguration` is `MCP` and `authorizerConfiguration` is set (runbook A2).
4. Check CloudWatch (runbook D) — container should reach `Uvicorn running on 0.0.0.0:8000`.
5. Confirm `kiro_bridge.py` uses `streamablehttp_client` (not raw requests + SigV4).
6. Restart MCP server in Kiro; allow ~30s for cold start.

---

## UPDATE: Second root cause — sub-agent HTTP hop (tool execution timeouts)

After the mcp 1.19 fix, `initialize`/`tools/list` worked but **tool execution**
timed out at exactly 55s with `"Agent timed out after 55s"`.

### Root cause
The parent (`parent_mcp_server.py`) connected to 5 sub-agent processes
(`adt_agent.py` etc. on ports 8101–8105) over localhost HTTP via Strands
`MCPClient`. The JWT token did **not** propagate through that internal HTTP hop,
so the sub-agent's SAP call hung until the parent's hardcoded 55s
`SUB_AGENT_TIMEOUT` fired. The container also cold-started on every request.

The working `sap-smart-agent` avoids this entirely: it's a single process that
passes tool **functions directly** to the Strands Agent (`tools=tool_functions`),
so SAP calls run in-process and return in ~5–7s.

### Fix — run tools in-process (mirror smart-agent)
Rewrote `parent_mcp_server.py` to:
1. `import` the tool functions from `adt_agent`, `odata_agent`, `generator_agent`,
   and `calm_tools` / `sf_tools` at module load.
2. Set `os.environ['SAP_BEARER_TOKEN']` from the request JWT so each tool's
   `_get_token(ctx)` env fallback works in-process.
3. Pass the function lists straight to a `strands.Agent(tools=[...])` — NO MCPClient,
   NO localhost HTTP hop.

`start_all.py` was simplified to just run the parent (no sub-agent subprocesses).
The 5 sub-agent modules keep their `@mcp.tool()` decorators (harmless) but their
`mcp.run()` is no longer invoked.

Result: ADT query `SELECT MANDT FROM T000` returns in **6s** (was 55s timeout).
Confirmed working: inactive-BADI query returned real data in 17s.

### CRITICAL: AgentCore caches image by digest
`update_agent_runtime` with the same `:latest` URI does **NOT** force a re-pull —
AgentCore keeps the old image digest. To deploy new code you MUST update with the
explicit digest:

```python
import boto3
ecr = boto3.client('ecr', region_name='us-east-1')
img = ecr.describe_images(repositoryName='bedrock-agentcore-sap_ai_factory',
                          imageIds=[{'imageTag':'latest'}])['imageDetails'][0]
digest = img['imageDigest']  # sha256:...
uri = f'953841955037.dkr.ecr.us-east-1.amazonaws.com/bedrock-agentcore-sap_ai_factory@{digest}'

c = boto3.client('bedrock-agentcore-control', region_name='us-east-1')
r = c.get_agent_runtime(agentRuntimeId='sap_ai_factory-OFYrcN3gxF')
c.update_agent_runtime(
    agentRuntimeId='sap_ai_factory-OFYrcN3gxF',
    agentRuntimeArtifact={'containerConfiguration': {'containerUri': uri}},
    roleArn=r['roleArn'],
    networkConfiguration=r['networkConfiguration'],
    protocolConfiguration={'serverProtocol': 'MCP'},
    authorizerConfiguration={'customJWTAuthorizer': {
        'discoveryUrl': 'https://trial-1053860.okta.com/oauth2/default/.well-known/openid-configuration',
        'allowedClients': ['0oa10vth79kZAuXGt698']}},
)
```

Verify the right code is live by checking CloudWatch for the parent startup line
`AI Factory Parent MCP Server (in-process)` and confirming there are NO
`Sub-Agent starting on port 810x` lines.
