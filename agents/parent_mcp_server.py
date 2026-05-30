"""
AI Factory Parent MCP Server — IN-PROCESS tool execution (no sub-agent HTTP hop).

Architecture (matches the proven sap-smart-agent pattern):
  Kiro → AgentCore → parent_mcp_server (port 8000)
     └── 5 router tools, each runs a Strands Agent with DIRECT Python function
         tools imported in-process from the agent modules. No localhost HTTP hop.

Why in-process: the previous design connected the parent to sub-agents over
localhost HTTP via MCPClient. The token did not propagate through that hop and
tool calls hung until the 55s timeout. Running tools in-process (like
sap-smart-agent) executes SAP calls directly and returns in a few seconds.

Token flow: the request's JWT is set into os.environ['SAP_BEARER_TOKEN'] so the
imported tools' _get_token(ctx) fallback picks it up in-process.
"""
import os, sys, json, logging, time, base64, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AgentInitializedEvent, HookProvider, HookRegistry, MessageAddedEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ai_factory")

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
REGION   = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

_PORT = int(os.environ.get("MCP_PORT", "8100"))
mcp = FastMCP("AI Factory", host="0.0.0.0", port=_PORT, stateless_http=True)

# ── AgentCore Memory (durable, cross-instance conversation memory) ────────────
# A single short-term memory resource backs all router tools. Conversation turns
# are keyed by actor_id = JWT user hash, session_id = domain. This persists
# across container instances and restarts (unlike the in-RAM cache), so it works
# for Kiro, QuickSuite, or any client — bound to the user's identity.
_MEMORY_NAME = os.environ.get("AIFACTORY_MEMORY_NAME", "AIFactoryMemory")
_MEMORY_EXPIRY_DAYS = int(os.environ.get("AIFACTORY_MEMORY_EXPIRY_DAYS", "7"))
_memory_client = None
_memory_id = None


def _init_memory():
    """Create or look up the shared AgentCore Memory resource (once)."""
    global _memory_client, _memory_id
    if _memory_id is not None:
        return _memory_id
    try:
        from bedrock_agentcore.memory import MemoryClient
        _memory_client = MemoryClient(region_name=REGION)
        try:
            mem = _memory_client.create_memory_and_wait(
                name=_MEMORY_NAME,
                strategies=[],                       # short-term only
                description="AI Factory per-user conversation memory",
                event_expiry_days=_MEMORY_EXPIRY_DAYS,
            )
            _memory_id = mem["id"]
            logger.info(f"Created AgentCore memory: {_memory_id}")
        except Exception as e:
            # Likely already exists — look it up and wait until ACTIVE
            if "already exists" in str(e) or "ValidationException" in str(e):
                for m in _memory_client.list_memories():
                    if m["id"].startswith(_MEMORY_NAME):
                        _memory_id = m["id"]
                        break
                logger.info(f"Using existing AgentCore memory: {_memory_id}")
                # Wait for ACTIVE — create_event/list fail while still CREATING
                if _memory_id:
                    for _ in range(20):
                        try:
                            st = _memory_client.get_memory(memory_id=_memory_id).get("status")
                        except Exception:
                            st = None
                        if st == "ACTIVE":
                            break
                        logger.info(f"Memory {_memory_id} status={st}, waiting...")
                        time.sleep(10)
            else:
                raise
    except Exception as e:
        logger.warning(f"AgentCore Memory unavailable ({e}); falling back to in-RAM only")
        _memory_client = None
        _memory_id = None
    return _memory_id


class _MemoryHook(HookProvider):
    """Loads the user's recent turns on agent init and saves each new turn.
    actor_id = JWT user hash, session_id = domain (set via agent.state)."""

    def __init__(self, client, memory_id: str):
        self._client = client
        self._memory_id = memory_id

    def on_agent_initialized(self, event: AgentInitializedEvent):
        try:
            actor_id = event.agent.state.get("actor_id")
            session_id = event.agent.state.get("session_id")
            if not (actor_id and session_id):
                return
            turns = self._client.get_last_k_turns(
                memory_id=self._memory_id, actor_id=actor_id,
                session_id=session_id, k=8)
            if turns:
                lines = []
                for turn in turns:
                    for msg in turn:
                        txt = msg.get("content", {}).get("text", "")
                        if txt:
                            lines.append(f"{msg.get('role','?')}: {txt}")
                if lines:
                    event.agent.system_prompt += (
                        "\n\nRecent conversation (from memory):\n" + "\n".join(lines))
                    logger.info(f"Loaded {len(turns)} turns for actor={actor_id} session={session_id}")
        except Exception as e:
            logger.error(f"Memory load error: {e}")

    def on_message_added(self, event: MessageAddedEvent):
        try:
            actor_id = event.agent.state.get("actor_id")
            session_id = event.agent.state.get("session_id")
            if not (actor_id and session_id):
                return
            msgs = event.agent.messages
            last = msgs[-1]
            text = ""
            content = last.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                text = content[0].get("text", "")
            if text:
                self._client.create_event(
                    memory_id=self._memory_id, actor_id=actor_id,
                    session_id=session_id, messages=[(text, last.get("role", "user"))])
        except Exception as e:
            logger.error(f"Memory save error: {e}")

    def register_hooks(self, registry: HookRegistry):
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
        registry.add_callback(MessageAddedEvent, self.on_message_added)


# ── Per-user agent cache (warm-start optimization; memory is durable in AgentCore) ──
_agent_cache: dict = {}          # key -> (Agent, last_used_ts)
_AGENT_TTL = 1800                # 30 min idle → drop the warm agent
_MAX_TURNS = int(os.environ.get("MAX_AGENT_TURNS", "12"))


# ── Import tool functions in-process from each agent module ───────────────────
# The @mcp.tool() decorator on the sub-agent modules returns the original
# callable, so these names are plain functions we can hand to a Strands Agent.

from agents import adt_agent as _adt
from agents import odata_agent as _odata
from agents import generator_agent as _gen

# Cloud ALM and SuccessFactors expose their tools from helper modules
import calm_tools as _calm_tools
import sf_tools as _sf_tools


_ADT_TOOLS = [
    _adt.get_abap_program, _adt.get_abap_class, _adt.get_function_module,
    _adt.get_abap_interface, _adt.get_abap_include, _adt.search_objects,
    _adt.get_package, _adt.get_transaction, _adt.get_table_definition,
    _adt.get_type_info, _adt.get_table_contents, _adt.syntax_check,
    _adt.run_atc_check, _adt.create_transport, _adt.release_transport,
    _adt.list_user_transports, _adt.create_odata_service,
    _adt.verify_cds_exists, _adt.activate_odata_service,
    _adt.list_backend_services, _adt.adt_discovery,
]

_ODATA_TOOLS = [
    _odata.search_sap_services, _odata.get_service_metadata, _odata.query_sap_odata,
    _odata.get_sap_entity, _odata.create_sap_entity, _odata.update_sap_entity,
    _odata.delete_sap_entity, _odata.get_service_entities, _odata.get_entity_properties,
    _odata.find_entity_by_field, _odata.cache_stats, _odata.get_system_version,
    _odata.search_api_hub, _odata.validate_api_availability, _odata.lookup_cached_route,
    _odata.save_route, _odata.record_failed_route, _odata.show_cached_routes,
    _odata.clear_cache, _odata.list_past_research_sessions, _odata.load_past_research,
    _odata.run_sql_query, _odata.get_research_summary, _odata.create_cds_views_from_research,
    _odata.clear_research_history, _odata.list_sap_ec2_instances, _odata.run_hana_sql,
    _odata.smart_query,
]

_CALM_TOOLS = [
    _calm_tools.calm_list_projects, _calm_tools.calm_get_project,
    _calm_tools.calm_list_project_timeboxes, _calm_tools.calm_list_project_teams,
    _calm_tools.calm_list_programs, _calm_tools.calm_create_project,
    _calm_tools.calm_list_tasks, _calm_tools.calm_get_task, _calm_tools.calm_create_task,
    _calm_tools.calm_update_task, _calm_tools.calm_delete_task,
    _calm_tools.calm_list_task_comments, _calm_tools.calm_create_task_comment,
    _calm_tools.calm_list_workstreams, _calm_tools.calm_list_features,
    _calm_tools.calm_get_feature, _calm_tools.calm_create_feature,
    _calm_tools.calm_update_feature, _calm_tools.calm_delete_feature,
    _calm_tools.calm_list_feature_statuses, _calm_tools.calm_list_feature_priorities,
    _calm_tools.calm_list_documents, _calm_tools.calm_get_document,
    _calm_tools.calm_create_document, _calm_tools.calm_update_document,
    _calm_tools.calm_delete_document, _calm_tools.calm_list_document_types,
    _calm_tools.calm_list_testcases, _calm_tools.calm_get_testcase,
    _calm_tools.calm_create_testcase, _calm_tools.calm_list_test_activities,
    _calm_tools.calm_list_test_actions, _calm_tools.calm_list_hierarchy_nodes,
    _calm_tools.calm_get_hierarchy_node, _calm_tools.calm_create_hierarchy_node,
    _calm_tools.calm_update_hierarchy_node, _calm_tools.calm_delete_hierarchy_node,
    _calm_tools.calm_list_monitoring_events, _calm_tools.calm_get_monitoring_event,
    _calm_tools.calm_list_monitored_services, _calm_tools.calm_list_analytics_providers,
    _calm_tools.calm_query_analytics,
]

_SF_TOOLS = [
    _sf_tools.sf_list_employees, _sf_tools.sf_get_employee_employment,
    _sf_tools.sf_list_positions, _sf_tools.sf_list_departments,
    _sf_tools.sf_list_locations, _sf_tools.sf_list_users,
    _sf_tools.sf_list_job_requisitions, _sf_tools.sf_list_candidates,
    _sf_tools.sf_list_learning_activities, _sf_tools.sf_list_performance_reviews,
    _sf_tools.sf_list_compensation,
]

_GEN_TOOLS = [
    _gen.generate_and_deploy_mcp_server, _gen.check_generation_status,
    _gen.list_generated_mcp_servers,
]


# ── Token extraction ──────────────────────────────────────────────────────────
def _extract_token(ctx: Context) -> str:
    """Pull the JWT from the request headers; set it in env for in-process tools."""
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken",
                  "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken",
                  "authorization"]:
            val = req.headers.get(h, "")
            if val:
                t = val.replace("Bearer ", "").replace("bearer ", "") \
                    if val.lower().startswith("bearer ") else val
                if t:
                    os.environ["SAP_BEARER_TOKEN"] = t
                    return t
    except Exception:
        pass
    return os.environ.get("SAP_BEARER_TOKEN", "")


def _decode_jwt_claims(token: str) -> dict:
    """Decode JWT payload (no signature check — AgentCore already validated it)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _user_key(token: str) -> str:
    """Derive a STABLE per-user memory key from the JWT identity claims.

    This is client-independent — it works the same whether the caller is Kiro,
    QuickSuite, or a raw HTTP client, and survives reconnects/new transport
    sessions because it is bound to the user's identity, not the connection.

    Preference order (most human-meaningful first):
      email → preferred_username → upn → unique_name → username → sub → client_id
    The chosen value is hashed so we never use raw PII as a dict key / in logs.
    """
    claims = _decode_jwt_claims(token)
    raw = ""
    for c in ("email", "preferred_username", "upn", "unique_name",
              "username", "sub", "client_id", "cid"):
        v = claims.get(c)
        if v:
            raw = str(v)
            break
    if not raw:
        raw = "anonymous"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return digest


# ── In-process Strands runner (USER-scoped, with conversation memory) ─────────
def _run(system_prompt: str, question: str, tool_functions: list,
         token: str, domain: str) -> str:
    """Run a Strands Agent with DIRECT Python function tools (no MCP/HTTP hop).

    Memory is keyed by (user_identity, domain) extracted from the JWT — NOT by
    transport session. So a user's conversation context persists across calls,
    reconnects, and clients (Kiro, QuickSuite, etc.). History is bounded by
    SlidingWindowConversationManager. The SAP token is refreshed in env on every
    call (it rotates ~hourly) without discarding the conversation memory.
    """
    if token:
        os.environ["SAP_BEARER_TOKEN"] = token

    user = _user_key(token)
    key = f"{user}::{domain}"
    now = time.time()

    agent = None
    cached = _agent_cache.get(key)
    if cached:
        c_agent, created = cached
        if now - created < _AGENT_TTL:
            agent = c_agent          # reuse — keeps the user's conversation history
        else:
            _agent_cache.pop(key, None)   # idle TTL expired

    if agent is None:
        memory_id = _init_memory()
        hooks = []
        state = {}
        if memory_id and _memory_client is not None:
            hooks = [_MemoryHook(_memory_client, memory_id)]
            # actor_id = per-user identity; session_id = domain (separate thread per domain)
            state = {"actor_id": f"u_{user}", "session_id": domain}
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000, temperature=0.1),
            tools=tool_functions,
            system_prompt=system_prompt,
            hooks=hooks,
            state=state,
            conversation_manager=SlidingWindowConversationManager(
                window_size=_MAX_TURNS,
                should_truncate_results=True,
            ),
        )
        _agent_cache[key] = (agent, now)
        logger.info(f"Agent created for user={user} domain={domain} memory={'on' if memory_id else 'off'}")
    else:
        # refresh timestamp so an active user's agent stays warm
        _agent_cache[key] = (agent, now)
        logger.info(f"Agent reused for user={user} domain={domain}")

    try:
        result = agent(question)
        if hasattr(result, "message") and isinstance(result.message, dict):
            parts = [c.get("text", "") for c in result.message.get("content", [])
                     if isinstance(c, dict) and c.get("text")]
            return "\n".join(parts) if parts else str(result)
        return str(result)
    except Exception as e:
        logger.error(f"Strands runner error (user={user} domain={domain}): {e}")
        # Drop the possibly-corrupted cached agent so the next call starts fresh
        _agent_cache.pop(key, None)
        return json.dumps({"error": str(e)})


# ── 5 Router Tools ─────────────────────────────────────────────────────────────

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
    return _run(
        "You are the ADT Agent — SAP ABAP development specialist. "
        "Use the most specific tool available. For data queries with no OData, "
        "generate SQL and run via get_table_contents. "
        "For OData creation: create_odata_service → activate_odata_service. "
        "Always confirm before creating or deploying anything.",
        question, _ADT_TOOLS, _extract_token(ctx), "adt")


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
    return _run(
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
        "7. If ADT SQL fails, tell user and move to TIER 3.\n\n"
        "TIER 3 — HANA SQL via SSM (last resort):\n"
        "8. Tell user both OData and ADT SQL failed; ask for the EC2 instance ID "
        "(use list_sap_ec2_instances if they don't know it).\n"
        "9. Use run_hana_sql with SAPHANADB schema prefix (e.g., SAPHANADB.BSIK).\n\n"
        "RULES:\n"
        "- NEVER silently fall back without telling the user what happened at each tier.\n"
        "- ALWAYS end with a 'Data Source' section: which tier, service/table, and exact query used.\n"
        "- For complex questions use smart_query.",
        question, _ODATA_TOOLS, _extract_token(ctx), "odata")


@mcp.tool()
def calm_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — Cloud ALM Agent for SAP Rise / Cloud ERP customers.

    Covers: projects, tasks, features, documents, test management,
    process hierarchy, process monitoring, analytics, ITSM.

    Examples: 'List all open tasks in my Cloud ALM project',
    'Show me process monitoring exceptions from today'
    """
    return _run(
        "You are the Cloud ALM Agent — SAP Rise/Cloud ERP specialist. "
        "For analytics, call calm_list_analytics_providers first to discover datasets, "
        "then calm_query_analytics.",
        question, _CALM_TOOLS, _extract_token(ctx), "calm")


@mcp.tool()
def sf_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — SuccessFactors Agent for SAP HCM and HR use cases.

    Covers: Employee Central, org structure, recruiting, learning,
    performance, compensation, user management.

    Examples: 'How many employees are in Finance?',
    'Show me open job requisitions for engineers'
    """
    return _run(
        "You are the SuccessFactors Agent — SAP HCM specialist. "
        "Always use filters to narrow results. Avoid pulling large unfiltered datasets.",
        question, _SF_TOOLS, _extract_token(ctx), "sf")


@mcp.tool()
def generator_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — Generator Agent for creating and deploying new SAP MCP agents.

    Use when asked to create/build/deploy a new focused SAP agent.
    Auto-detects domain: Cloud ALM, SuccessFactors, or S/4HANA OData.
    Generates code via Claude, deploys to AgentCore via CodeBuild (~10-15 min).
    Always confirms agent_name and domain before deploying.

    If the user says "create agent" after a research session that used SQL,
    route through odata_agent_tool instead — it handles the SQL → CDS → OData
    activation → verification → generator handoff pipeline.
    """
    return _run(
        "You are the Generator Agent — SAP MCP agent factory. "
        "Use generate_and_deploy_mcp_server with a clear prompt and snake_case agent_name. "
        "Always confirm agent_name and domain before deploying.",
        question, _GEN_TOOLS, _extract_token(ctx), "gen")


if __name__ == "__main__":
    logger.info(f"=== AI Factory Parent MCP Server (in-process) starting on port {_PORT} ===")
    logger.info(f"Tools loaded: ADT={len(_ADT_TOOLS)} OData={len(_ODATA_TOOLS)} "
                f"CALM={len(_CALM_TOOLS)} SF={len(_SF_TOOLS)} Gen={len(_GEN_TOOLS)}")
    # Initialize the shared AgentCore Memory resource up front (best-effort)
    try:
        mid = _init_memory()
        logger.info(f"AgentCore Memory: {'ready (' + mid + ')' if mid else 'disabled — in-RAM only'}")
    except Exception as e:
        logger.warning(f"Memory init skipped: {e}")
    mcp.run(transport="streamable-http")
