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
import os, sys, json, logging, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ai_factory")

MODEL_ID = "us.anthropic.claude-sonnet-4-6"

_PORT = int(os.environ.get("MCP_PORT", "8100"))
mcp = FastMCP("AI Factory", host="0.0.0.0", port=_PORT, stateless_http=True)


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


# ── In-process Strands runner ─────────────────────────────────────────────────
def _run(system_prompt: str, question: str, tool_functions: list, token: str) -> str:
    """Run a Strands Agent with DIRECT Python function tools (no MCP/HTTP hop).
    Token is injected into env so each tool's _get_token(ctx) fallback finds it."""
    if token:
        os.environ["SAP_BEARER_TOKEN"] = token
    try:
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000, temperature=0.1),
            tools=tool_functions,
            system_prompt=system_prompt,
        )
        result = agent(question)
        # Extract plain text (avoids MCP outputSchema validation on AgentResult)
        if hasattr(result, "message") and isinstance(result.message, dict):
            parts = [c.get("text", "") for c in result.message.get("content", [])
                     if isinstance(c, dict) and c.get("text")]
            return "\n".join(parts) if parts else str(result)
        return str(result)
    except Exception as e:
        logger.error(f"Strands runner error: {e}")
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
        question, _ADT_TOOLS, _extract_token(ctx))


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
        question, _ODATA_TOOLS, _extract_token(ctx))


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
        question, _CALM_TOOLS, _extract_token(ctx))


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
        question, _SF_TOOLS, _extract_token(ctx))


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
        question, _GEN_TOOLS, _extract_token(ctx))


if __name__ == "__main__":
    logger.info(f"=== AI Factory Parent MCP Server (in-process) starting on port {_PORT} ===")
    logger.info(f"Tools loaded: ADT={len(_ADT_TOOLS)} OData={len(_ODATA_TOOLS)} "
                f"CALM={len(_CALM_TOOLS)} SF={len(_SF_TOOLS)} Gen={len(_GEN_TOOLS)}")
    mcp.run(transport="streamable-http")
