"""
SAP Smart Parent Agent — Strands router.
Receives any SAP-related question, routes to the right sub-agent:
  - ADT Agent    (port 8101) — ABAP source, syntax, ATC, transports, DDIC
  - OData Agent  (port 8102) — S/4HANA OData queries, smart NL→SQL
  - Cloud ALM    (port 8103) — projects, tasks, features, monitoring, analytics
  - SF Agent     (port 8104) — employees, recruiting, learning, performance

Model: us.anthropic.claude-sonnet-4-6 (1M context, all sub-agents)
"""
import os, sys, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("parent_agent")

MODEL_ID = "us.anthropic.claude-sonnet-4-6"

# Sub-agent endpoints — override via env vars for deployed AgentCore URLs
ADT_URL       = os.environ.get("ADT_AGENT_URL",       "http://localhost:8101/mcp")
ODATA_URL     = os.environ.get("ODATA_AGENT_URL",     "http://localhost:8102/mcp")
CALM_URL      = os.environ.get("CALM_AGENT_URL",      "http://localhost:8103/mcp")
SF_URL        = os.environ.get("SF_AGENT_URL",        "http://localhost:8104/mcp")
GENERATOR_URL = os.environ.get("GENERATOR_AGENT_URL", "http://localhost:8105/mcp")

SYSTEM_PROMPT = """You are the AI Factory MCP Server — a unified SAP AI research and automation platform.

Your purpose is to explore, understand, and extend SAP system capabilities across S/4HANA, Cloud ALM,
and SuccessFactors. You are continuously evolving — discovering what OData services, ABAP programs,
and APIs exist in the SAP landscape.

You have five specialized sub-agents. Route every request to the right one:

1. OData Agent — FIRST choice for any data retrieval from SAP S/4HANA.
   Use when: user asks for sales orders, purchase orders, invoices, RFQs, materials, vendors,
   customers, or any business data from the SAP system.
   If OData exists but is not active → try to activate it, or ask the user to activate it.
   If no OData exists → hand off to ADT Agent to generate and run a SQL query instead.
   Keywords: sales order, purchase order, invoice, RFQ, material, vendor, customer, stock, delivery

2. ADT Agent — for ABAP development, understanding ABAP objects, and CDS views.
   Use when: user wants to read/write ABAP source, understand a program or class, check syntax,
   run ATC quality checks, manage transports, explore DDIC tables, or create new OData via CDS.
   If an ADT API is not available as a tool, use SAP knowledge to call call_adt_api directly.
   If user wants to create OData: create CDS view → @OData.publish → activate. But ALWAYS ask first.
   If user just wants data and no OData exists: generate SQL and run via get_table_contents.
   Keywords: ABAP, program, class, function module, CDS, transport, syntax, ATC, DDIC, create OData

3. Cloud ALM Agent — for SAP Cloud ALM / Rise / Cloud ERP use cases.
   Use when: customer is on SAP Rise offering. Cloud ALM is the central operations and project
   management platform for Rise customers. Covers projects, tasks, features, test management,
   process hierarchy, monitoring events, and analytics.
   Keywords: Cloud ALM, ALM, Rise, Cloud ERP, project, task, feature, test case, monitoring

4. SuccessFactors Agent — for HR and HCM use cases.
   Use when: client needs employee data, HR processes, performance, recruiting, learning,
   compensation, or workforce analytics from SAP SuccessFactors.
   Keywords: employee, HR, HCM, SuccessFactors, performance, recruiting, learning, compensation

5. Generator Agent — creates and deploys new focused SAP MCP agents to AgentCore.
   Use when: user asks to create, build, or deploy a new agent for a specific SAP domain.
   Keywords: create agent, deploy agent, generate agent, new MCP server, build agent

Research mode: When exploring S/4HANA capabilities (e.g. "what OData exists for plant maintenance?"),
use OData Agent to search the catalog, then ADT Agent to inspect programs and tables.

Rules:
- OData Agent is always the first attempt for data questions
- ADT Agent is the fallback when no OData exists
- Always confirm with the user before creating or deploying anything
- For cross-domain questions, call multiple sub-agents and synthesize results
- This platform is continuously evolving — if a tool doesn't exist, try call_adt_api with SAP knowledge
"""


def create_parent_agent() -> Agent:
    """Create the parent Strands agent with all sub-agent tools loaded."""
    clients = [
        MCPClient(lambda: streamablehttp_client(ADT_URL)),
        MCPClient(lambda: streamablehttp_client(ODATA_URL)),
        MCPClient(lambda: streamablehttp_client(CALM_URL)),
        MCPClient(lambda: streamablehttp_client(SF_URL)),
        MCPClient(lambda: streamablehttp_client(GENERATOR_URL)),
    ]
    all_tools = []
    for client in clients:
        try:
            with client:
                all_tools.extend(client.tools)
        except Exception as e:
            logger.warning(f"Could not connect to sub-agent: {e}")

    return Agent(
        model=BedrockModel(
            model_id=MODEL_ID,
            max_tokens=64000,          # large context for multi-domain answers
            temperature=0.1,           # low temp for precise tool selection
        ),
        tools=all_tools,
        system_prompt=SYSTEM_PROMPT,
    )


def ask(question: str) -> str:
    """Ask the parent agent a question and return the response."""
    agent = create_parent_agent()
    result = agent(question)
    return str(result)


if __name__ == "__main__":
    import sys
    question = " ".join(sys.argv[1:]) or "What SAP systems are available?"
    print(ask(question))
