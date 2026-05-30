"""
SAP SuccessFactors Sub-Agent — Employee Central, Recruiting, Learning,
Performance, Compensation.
Port: 8104
"""
import os, sys, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP
from strands import Agent
from strands.models import BedrockModel
from sf_tools import (
    sf_list_employees, sf_get_employee_employment, sf_list_positions,
    sf_list_departments, sf_list_locations, sf_list_users,
    sf_list_job_requisitions, sf_list_candidates,
    sf_list_learning_activities, sf_list_performance_reviews, sf_list_compensation,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sf_agent")

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
mcp = FastMCP("AI Factory — SuccessFactors Agent", host="0.0.0.0", port=8104)

for fn in [
    sf_list_employees, sf_get_employee_employment, sf_list_positions,
    sf_list_departments, sf_list_locations, sf_list_users,
    sf_list_job_requisitions, sf_list_candidates,
    sf_list_learning_activities, sf_list_performance_reviews, sf_list_compensation,
]:
    mcp.tool()(fn)


def create_sf_strands_agent() -> Agent:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8104/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000),
            tools=client.tools,
            system_prompt=(
                "You are the SuccessFactors Agent within the AI Factory MCP Server.\n\n"
                "Use this agent when the client needs HR or HCM capabilities from SAP SuccessFactors. "
                "SuccessFactors is SAP's cloud HCM suite covering the full employee lifecycle.\n\n"
                "Use this agent for:\n"
                "- Employee data: headcount, personal information, employment records (Employee Central)\n"
                "- Organizational structure: positions, departments, locations, org charts\n"
                "- Recruiting: job requisitions, candidate pipeline, hiring status\n"
                "- Learning: training completions, learning activities, certifications\n"
                "- Performance: performance review forms, ratings, goal achievement\n"
                "- Compensation: salary data, compensation records, pay grades\n"
                "- User management: SuccessFactors user accounts and access\n\n"
                "Always use filter_ to narrow results when a specific employee, department, "
                "or time period is mentioned — avoid pulling large unfiltered datasets.\n"
                "Use select to limit fields returned when only specific attributes are needed."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP SuccessFactors Sub-Agent starting on port 8104 ===")
    mcp.run(transport="streamable-http")
