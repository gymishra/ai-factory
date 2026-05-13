"""
AI Factory Parent MCP Server — Direct passthrough to sub-agents.

Architecture (FAST — no double LLM hop):
  Kiro (LLM picks tool) → parent_mcp_server (port 8100)
           ├── odata_agent_tool()  → direct MCP call_tool → odata_agent.py :8102
           ├── adt_agent_tool()    → direct MCP call_tool → adt_agent.py   :8101
           ├── calm_agent_tool()   → direct MCP call_tool → calm_agent.py  :8103
           ├── sf_agent_tool()     → direct MCP call_tool → sf_agent.py    :8104
           └── generator_agent_tool() → Strands Agent → generator_agent.py :8105

Sub-agents must be running before starting this server (use start_all.py).

PERFORMANCE FIX: The 5 wrapper tools now forward directly to sub-agent MCP tools
using a lightweight Strands Agent with pre-connected, cached MCP clients.
No more per-request connection setup + teardown.
"""
import os, sys, json, logging, time, signal
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands.agent.conversation_manager import SlidingWindowConversationManager
from mcp.client.streamable_http import streamablehttp_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ai_factory")

MODEL_ID      = "us.anthropic.claude-sonnet-4-6"
ADT_URL       = os.environ.get("ADT_AGENT_URL",       "http://localhost:8101/mcp")
ODATA_URL     = os.environ.get("ODATA_AGENT_URL",     "http://localhost:8102/mcp")
CALM_URL      = os.environ.get("CALM_AGENT_URL",      "http://localhost:8103/mcp")
SF_URL        = os.environ.get("SF_AGENT_URL",        "http://localhost:8104/mcp")
GENERATOR_URL = os.environ.get("GENERATOR_AGENT_URL", "http://localhost:8105/mcp")

_PORT = int(os.environ.get("MCP_PORT", "8100"))
mcp = FastMCP("AI Factory", host="0.0.0.0", port=_PORT, stateless_http=True)


# ── Pre-load token ────────────────────────────────────────────────────────────
def _preload_token():
    import base64 as _b64
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".okta_token_cache.json")
    try:
        with open(cache) as f:
            token = json.load(f).get("access_token", "")
        if token:
            p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
            if json.loads(_b64.b64decode(p)).get("exp", 0) > time.time() + 60:
                os.environ["SAP_BEARER_TOKEN"] = token
                logger.info("SAP_BEARER_TOKEN pre-loaded from Okta cache")
                return
    except Exception:
        pass
    logger.warning("Could not pre-load token from Okta cache")

_preload_token()


def _extract_token(ctx: Context) -> str:
    """Extract bearer token — tries request headers, then Okta cache, then env var."""
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                t = val.replace("Bearer ", "").replace("bearer ", "") \
                    if val.lower().startswith("bearer ") else val
                if t:
                    os.environ["SAP_BEARER_TOKEN"] = t
                    return t
    except Exception:
        pass

    import base64 as _b64
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".okta_token_cache.json")
    try:
        with open(cache) as f:
            token = json.load(f).get("access_token", "")
        if token:
            p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
            if json.loads(_b64.b64decode(p)).get("exp", 0) > time.time() + 60:
                os.environ["SAP_BEARER_TOKEN"] = token
                return token
    except Exception:
        pass

    return os.environ.get("SAP_BEARER_TOKEN", "")


# ── Cached Strands Agents (created once, reused across requests) ──────────────
_agent_cache: dict = {}  # url -> (Agent, MCPClient, timestamp)
_AGENT_CACHE_TTL = 300   # refresh connections every 5 min
_SUB_AGENT_TIMEOUT = int(os.environ.get("SUB_AGENT_TIMEOUT", "55"))  # seconds — must be < HANA SQL 50s
_GENERATOR_TIMEOUT = int(os.environ.get("GENERATOR_TIMEOUT", "300"))  # 5 min for code generation + deploy
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sub-agent")


_MAX_AGENT_TURNS = int(os.environ.get("MAX_AGENT_TURNS", "10"))


def _get_or_create_agent(url: str, system_prompt: str, token: str = "") -> Agent:
    """Return a cached Strands Agent backed by the sub-agent at url.
    Creates once, reuses across requests. Reconnects if stale or token changed.
    Uses SlidingWindowConversationManager to cap tool call iterations."""
    now = time.time()
    cache_key = url
    if cache_key in _agent_cache:
        agent, client, created, cached_token = _agent_cache[cache_key]
        # Reconnect if stale OR if token changed (so sub-agent gets fresh auth)
        if now - created < _AGENT_CACHE_TTL and cached_token == token:
            return agent
        # Stale or token changed — close and recreate
        try:
            client.stop()
        except Exception:
            pass

    # Pass token as Authorization header so sub-agents receive it in ctx.request_context
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client = MCPClient(lambda u=url, h=headers: streamablehttp_client(u, headers=h))
    try:
        client.start()
        tools = client.list_tools_sync()
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000, temperature=0.1),
            tools=tools,
            system_prompt=system_prompt,
            # Cap conversation history to prevent unbounded growth across cached reuse
            conversation_manager=SlidingWindowConversationManager(
                window_size=_MAX_AGENT_TURNS,
                should_truncate_results=True,
            ),
        )
        _agent_cache[cache_key] = (agent, client, now, token)
        logger.info(f"Agent cached for {url} ({len(tools)} tools, token={'yes' if token else 'no'})")
        return agent
    except Exception as e:
        logger.error(f"Failed to create agent for {url}: {e}")
        try:
            client.stop()
        except Exception:
            pass
        raise


def _run_sub_agent(url: str, system_prompt: str, question: str, token: str = "", timeout_override: int = 0) -> str:
    """Run a question through a cached Strands Agent backed by a sub-agent MCP server.
    Token is forwarded as Authorization header to sub-agents (cross-process safe).
    Enforces a hard timeout to prevent infinite Strands Agent loops.
    Use timeout_override for agents that need more time (e.g. generator)."""
    if token:
        os.environ["SAP_BEARER_TOKEN"] = token

    def _invoke():
        agent = _get_or_create_agent(url, system_prompt, token)
        return str(agent(question))

    timeout = timeout_override if timeout_override > 0 else _SUB_AGENT_TIMEOUT
    start = time.time()
    try:
        future = _executor.submit(_invoke)
        result = future.result(timeout=timeout)
        elapsed = time.time() - start
        logger.info(f"Sub-agent {url} completed in {elapsed:.1f}s")
        return result
    except FuturesTimeoutError:
        elapsed = time.time() - start
        logger.error(f"Sub-agent {url} TIMED OUT after {elapsed:.1f}s (limit={timeout}s)")
        # Evict the cached agent — it may be stuck
        _agent_cache.pop(url, None)
        return json.dumps({
            "error": f"Agent timed out after {timeout}s",
            "hint": "SAP typically responds in <5s. The agent was likely stuck in a Claude reasoning loop. "
                    "Try a simpler query or break it into smaller steps.",
            "elapsed_seconds": round(elapsed, 1),
        })
    except Exception as e:
        elapsed = time.time() - start
        # Connection might be stale — evict cache and retry once
        _agent_cache.pop(url, None)
        logger.warning(f"Sub-agent call failed after {elapsed:.1f}s, retrying: {e}")
        try:
            future = _executor.submit(_invoke)
            result = future.result(timeout=max(timeout - elapsed, 15))
            return result
        except FuturesTimeoutError:
            logger.error(f"Sub-agent {url} retry TIMED OUT")
            _agent_cache.pop(url, None)
            return json.dumps({"error": f"Agent retry timed out after {timeout}s"})
        except Exception as e2:
            logger.error(f"Sub-agent retry failed ({url}): {e2}")
            return json.dumps({"error": str(e2)})


# ── 5 Sub-Agent Tools ─────────────────────────────────────────────────────────

@mcp.tool()
def adt_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — ADT Agent for ABAP development and SAP object exploration.

    Use this tool when the task involves:
    - Reading or writing ABAP source code (programs, classes, function modules, includes, interfaces)
    - Understanding what an ABAP object does or how it is structured
    - Exploring CDS views and their data models
    - Running syntax checks or ATC code quality checks
    - Managing transport requests (create, release, list)
    - Querying DDIC table definitions and contents via SQL
    - Creating a new OData service via CDS view (always confirm with user first)
    - Activating an OData service in SAP Gateway

    Examples: 'Show me the source code of Z_INVOICE_3WAY_MATCH',
    'What fields does table EKKO have?', 'Run ATC check on ZCL_MY_CLASS'
    """
    return _run_sub_agent(ADT_URL,
        "You are the ADT Agent — SAP ABAP development specialist. "
        "Use the most specific tool available. For data queries with no OData, "
        "generate SQL and run via get_table_contents. "
        "For OData creation: create_odata_service → activate_odata_service. "
        "Always confirm before creating or deploying anything.",
        question, _extract_token(ctx))


@mcp.tool()
def odata_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — Hybrid Data Research Agent for SAP S/4HANA.

    FIRST tool to use for ANY S/4HANA data query. Combines OData APIs + SQL for complete results.

    This agent:
    - Tries OData first for structured business data
    - Detects inactive services and falls back to SQL via ADT
    - Combines OData + SQL results for hybrid queries
    - Tracks research history for later agent generation
    - Creates CDS views and OData services from SQL patterns (mandatory before agent generation)
    - Handles the FULL pipeline: research → CDS creation → OData activation → agent generation handoff

    IMPORTANT: When the user asks to "create agent" or "generate agent" after a research session
    that used SQL, route through THIS tool — it will create CDS views, activate OData services,
    verify them, and then hand off to the generator with only verified OData service names.

    Use cases: sales orders, purchase orders, invoices, materials, vendors, customers,
    stock, deliveries, accounting documents, AP/AR items — any S/4HANA business data.
    """
    return _run_sub_agent(ODATA_URL,
        "You are the Hybrid Data Research Agent — SAP S/4HANA data specialist. "
        "IMPORTANT — follow this exact 3-tier fallback sequence and ALWAYS communicate status to the user:\n\n"
        "TIER 1 — OData:\n"
        "1. Try OData first (search_sap_services → get_service_entities → query_sap_odata).\n"
        "2. If service returns 403 (not activated), STOP and tell the user: "
        "'OData service <name> is not activated on SAP Gateway.'\n"
        "3. Then try to activate it. If activation succeeds, tell user and retry OData.\n"
        "4. If activation fails, tell user and move to TIER 2.\n\n"
        "TIER 2 — ADT SQL:\n"
        "5. Tell user: 'Falling back to ADT SQL query.' Then use run_sql_query.\n"
        "6. NOTE: ADT SQL supports JOINs, GROUP BY, COUNT, SUM, AVG. Use ABAP SQL syntax with ~ for field refs (h~vbeln). "
        "IMPORTANT: Do NOT use 'UP TO n ROWS' on JOIN queries — the engine auto-caps at 100.\n"
        "7. If ADT SQL fails (404, timeout, or HANA-specific syntax needed), tell user and move to TIER 3.\n\n"
        "TIER 3 — HANA SQL via SSM (last resort):\n"
        "8. Tell user: 'Both OData and ADT SQL failed. I can query HANA directly via SSM but need the EC2 instance ID.'\n"
        "9. Ask user for the EC2 instance ID. If they don't know, call list_sap_ec2_instances to show them available instances.\n"
        "10. Once you have the instance ID, use run_hana_sql with SAPHANADB schema prefix (e.g., SAPHANADB.BSIK).\n"
        "11. HANA SQL supports FULL SQL: GROUP BY, COUNT, SUM, AVG, JOIN, ORDER BY, subqueries.\n\n"
        "RULES:\n"
        "- NEVER silently fall back without telling the user what happened at each tier.\n"
        "- Always explain WHY a tier failed before moving to the next.\n"
        "- ALWAYS include a 'Data Source' section at the end of every response with:\n"
        "  * Which tier was used (OData / ADT SQL / HANA SQL via SSM)\n"
        "  * The OData service name and entity set, OR the SQL table name\n"
        "  * If OData failed, state which service was tried and why it failed (e.g., 'API_SUPPLIERINVOICE_PROCESS_SRV — not activated (403)')\n"
        "  * The exact query or filter used to get the data\n"
        "- Track everything in research history.\n"
        "- For complex questions use smart_query.",
        question, _extract_token(ctx))


@mcp.tool()
def calm_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — Cloud ALM Agent for SAP Rise / Cloud ERP customers.

    Covers: projects, tasks, features, documents, test management,
    process hierarchy, process monitoring, analytics, ITSM.

    Examples: 'List all open tasks in my Cloud ALM project',
    'Show me process monitoring exceptions from today'
    """
    return _run_sub_agent(CALM_URL,
        "You are the Cloud ALM Agent — SAP Rise/Cloud ERP specialist. "
        "For analytics, call calm_list_analytics_providers first to discover datasets, "
        "then calm_query_analytics.",
        question, _extract_token(ctx))


@mcp.tool()
def sf_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — SuccessFactors Agent for SAP HCM and HR use cases.

    Covers: Employee Central, org structure, recruiting, learning,
    performance, compensation, user management.

    Examples: 'How many employees are in Finance?',
    'Show me open job requisitions for engineers'
    """
    return _run_sub_agent(SF_URL,
        "You are the SuccessFactors Agent — SAP HCM specialist. "
        "Always use filters to narrow results. Avoid pulling large unfiltered datasets.",
        question, _extract_token(ctx))


@mcp.tool()
def generator_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — Generator Agent for creating and deploying new SAP MCP agents.

    IMPORTANT: Do NOT call this tool directly after a data research session.
    Instead, use odata_agent_tool first — it will:
    1. Create CDS views from SQL patterns discovered during research
    2. Activate OData services
    3. Verify endpoints are accessible
    4. Then hand off to the generator with verified OData service names

    Only call this tool directly when:
    - The user explicitly provides OData service names to use
    - The use case is Cloud ALM or SuccessFactors (no SQL-to-OData conversion needed)
    - The user wants to generate from an existing, already-active OData service

    If the user says "create agent" after a research session that used SQL,
    route through odata_agent_tool instead — it handles the full pipeline:
    SQL → CDS/AMDP → OData activation → verification → generator handoff.

    Examples: 'Create a Cloud ALM monitoring agent',
    'Build an agent using API_SALES_ORDER_SRV (already active)'
    """
    return _run_sub_agent(GENERATOR_URL,
        "You are the Generator Agent — SAP MCP agent factory. "
        "Use generate_and_deploy_mcp_server with a clear prompt and snake_case agent_name. "
        "Always confirm before deploying.",
        question, _extract_token(ctx), timeout_override=_GENERATOR_TIMEOUT)


# ── Direct MCP Call Helper (bypasses Strands Agent loop) ──────────────────────

def _direct_mcp_call(url: str, tool_name: str, arguments: dict, token: str = "") -> str:
    """Call a sub-agent MCP tool directly — no Strands Agent, no Claude loop.
    Opens a streamablehttp_client connection, calls the tool, returns the result."""
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client as _shc
    from datetime import timedelta

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = _SUB_AGENT_TIMEOUT

    async def _call():
        async with _shc(url, headers,
                        timeout=timedelta(seconds=timeout),
                        terminate_on_close=False) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                texts = [c.text for c in result.content if hasattr(c, "text")]
                return texts[0] if texts else "No result"

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(lambda: asyncio.run(_call())).result(timeout=timeout)
        else:
            return asyncio.run(_call())
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Two-Phase Workflow Wrapper Tools ──────────────────────────────────────────

@mcp.tool()
def odata_analyze_tool(ctx: Context, question: str) -> str:
    """AI Factory — Analyze an SAP data query and return an execution plan.
    Phase 1 of the two-phase workflow. Decomposes the question, matches against
    the metadata cache, and returns a plan for approval. No SAP calls are made.
    """
    token = _extract_token(ctx)
    return _direct_mcp_call(ODATA_URL, "analyze_query",
                            {"question": question}, token)


@mcp.tool()
def odata_execute_tool(ctx: Context, plan_id: str, skip_tasks: str = "",
                       use_sql_for_all: bool = False, activated_services: str = "") -> str:
    """AI Factory — Execute an approved SAP data query plan.
    Phase 2 of the two-phase workflow. Executes the plan with tiered fallback
    (OData → ADT SQL → HANA SQL).
    """
    token = _extract_token(ctx)
    return _direct_mcp_call(ODATA_URL, "execute_plan",
                            {"plan_id": plan_id, "skip_tasks": skip_tasks,
                             "use_sql_for_all": use_sql_for_all,
                             "activated_services": activated_services}, token)


if __name__ == "__main__":
    logger.info(f"=== AI Factory Parent MCP Server starting on port {_PORT} ===")
    logger.info(f"ADT={ADT_URL} | OData={ODATA_URL} | CALM={CALM_URL} | SF={SF_URL} | Gen={GENERATOR_URL}")

    OKTA_DOMAIN     = os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")
    OKTA_AUTH_SERVER = os.environ.get("OKTA_AUTH_SERVER", "default")
    AUTHORIZE_URL   = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/authorize"
    TOKEN_URL       = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/token"

    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    class OAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method in ("GET", "OPTIONS", "HEAD"):
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "message": "Bearer token required"},
                    headers={"WWW-Authenticate": (
                        f'Bearer realm="SAP AI Factory MCP",'
                        f' authorization_uri="{AUTHORIZE_URL}",'
                        f' token_uri="{TOKEN_URL}",'
                        f' scope="openid email"'
                    )}
                )
            return await call_next(request)

    asgi_app = mcp.streamable_http_app()
    uvicorn.run(OAuthMiddleware(asgi_app), host="0.0.0.0", port=_PORT)
