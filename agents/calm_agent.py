"""
SAP Cloud ALM Sub-Agent — Projects, Tasks, Features, Documents,
Test Management, Process Hierarchy, Monitoring, Analytics, ITSM.
Port: 8103
"""
import os, sys, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP
from strands import Agent
from strands.models import BedrockModel
from calm_tools import (
    calm_list_projects, calm_get_project, calm_list_project_timeboxes,
    calm_list_project_teams, calm_list_programs, calm_create_project,
    calm_list_tasks, calm_get_task, calm_create_task, calm_update_task,
    calm_delete_task, calm_list_task_comments, calm_create_task_comment,
    calm_list_workstreams,
    calm_list_features, calm_get_feature, calm_create_feature,
    calm_update_feature, calm_delete_feature,
    calm_list_feature_statuses, calm_list_feature_priorities,
    calm_list_documents, calm_get_document, calm_create_document,
    calm_update_document, calm_delete_document, calm_list_document_types,
    calm_list_testcases, calm_get_testcase, calm_create_testcase,
    calm_list_test_activities, calm_list_test_actions,
    calm_list_hierarchy_nodes, calm_get_hierarchy_node,
    calm_create_hierarchy_node, calm_update_hierarchy_node,
    calm_delete_hierarchy_node,
    calm_list_monitoring_events, calm_get_monitoring_event,
    calm_list_monitored_services,
    calm_list_analytics_providers, calm_query_analytics,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("calm_agent")

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
mcp = FastMCP("AI Factory — Cloud ALM Agent", host="0.0.0.0", port=8103, stateless_http=True)

# Register all Cloud ALM tools
for fn in [
    calm_list_projects, calm_get_project, calm_list_project_timeboxes,
    calm_list_project_teams, calm_list_programs, calm_create_project,
    calm_list_tasks, calm_get_task, calm_create_task, calm_update_task,
    calm_delete_task, calm_list_task_comments, calm_create_task_comment,
    calm_list_workstreams,
    calm_list_features, calm_get_feature, calm_create_feature,
    calm_update_feature, calm_delete_feature,
    calm_list_feature_statuses, calm_list_feature_priorities,
    calm_list_documents, calm_get_document, calm_create_document,
    calm_update_document, calm_delete_document, calm_list_document_types,
    calm_list_testcases, calm_get_testcase, calm_create_testcase,
    calm_list_test_activities, calm_list_test_actions,
    calm_list_hierarchy_nodes, calm_get_hierarchy_node,
    calm_create_hierarchy_node, calm_update_hierarchy_node,
    calm_delete_hierarchy_node,
    calm_list_monitoring_events, calm_get_monitoring_event,
    calm_list_monitored_services,
    calm_list_analytics_providers, calm_query_analytics,
]:
    mcp.tool()(fn)


def create_calm_strands_agent() -> Agent:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8103/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000),
            tools=client.tools,
            system_prompt=(
                "You are the Cloud ALM Agent within the AI Factory MCP Server.\n\n"
                "Use this agent when the customer is on SAP Rise or SAP Cloud ERP offering. "
                "SAP Cloud ALM is the central operations, monitoring, and project management platform "
                "for Rise customers — it is also known as Cloud ERP lifecycle management.\n\n"
                "Cloud ALM provides the following capabilities you can work with:\n"
                "- Implementation Projects: manage projects, tasks, workstreams, timeboxes, and team members\n"
                "- Features: track and manage functional requirements and features\n"
                "- Documents: manage project documentation and specifications\n"
                "- Test Management: create and track test cases, test activities, and test actions\n"
                "- Process Hierarchy: manage the SAP process hierarchy for the implementation scope\n"
                "- Process Monitoring: monitor business process exceptions and events in real-time\n"
                "- Analytics: query analytics datasets for requirements, tasks, alerts, defects, quality gates, "
                "  metrics, monitoring events, and more\n"
                "- ITSM: manage support cases, landscape installations, and contacts\n\n"
                "For analytics, always call calm_list_analytics_providers first to discover available datasets, "
                "then use calm_query_analytics with the appropriate provider name."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP Cloud ALM Sub-Agent starting on port 8103 ===")
    mcp.run(transport="streamable-http")
