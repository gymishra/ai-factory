"""
SAP Cloud ALM OData/REST tools — appended into sap_smart_mcp_server.py
Env vars:
  CALM_TENANT        — your tenant name  (e.g. my-company)
  CALM_REGION        — SAP region        (e.g. eu10, us10, ap10 …)
  CALM_CLIENT_ID     — OAuth2 client id
  CALM_CLIENT_SECRET — OAuth2 client secret
  CALM_TOKEN_URL     — full token URL (overrides tenant/region derivation)
  CALM_SANDBOX       — set to "true" to use SAP sandbox
  CALM_API_KEY       — sandbox API key (required when CALM_SANDBOX=true)
"""
import os, json, httpx, time as _time

# ── Auth ──────────────────────────────────────────────────────────────────────
_calm_odata_token_cache: dict = {"token": None, "expires_at": 0}

def _calm_base_url() -> str:
    if os.environ.get("CALM_SANDBOX", "").lower() == "true":
        return "https://sandbox.api.sap.com/SAPCALMv2"
    tenant = os.environ.get("CALM_TENANT", "")
    region = os.environ.get("CALM_REGION", "eu10")
    return f"https://{tenant}.{region}.alm.cloud.sap"

def _calm_odata_token() -> str:
    """OAuth2 client-credentials token for Cloud ALM OData/REST APIs."""
    if os.environ.get("CALM_SANDBOX", "").lower() == "true":
        return ""   # sandbox uses API key, not bearer
    now = _time.time()
    if _calm_odata_token_cache["token"] and now < _calm_odata_token_cache["expires_at"]:
        return _calm_odata_token_cache["token"]
    tenant = os.environ.get("CALM_TENANT", "")
    region = os.environ.get("CALM_REGION", "eu10")
    token_url = os.environ.get("CALM_TOKEN_URL",
        f"https://{tenant}.authentication.{region}.hana.ondemand.com/oauth/token")
    client_id     = os.environ.get("CALM_CLIENT_ID", "")
    client_secret = os.environ.get("CALM_CLIENT_SECRET", "")
    if not all([tenant or token_url, client_id, client_secret]):
        raise ValueError("CALM_TENANT/CALM_TOKEN_URL, CALM_CLIENT_ID, CALM_CLIENT_SECRET required")
    with httpx.Client(timeout=15.0) as c:
        r = c.post(token_url,
                   data={"grant_type": "client_credentials"},
                   auth=(client_id, client_secret),
                   headers={"Accept": "application/json"})
        r.raise_for_status()
        d = r.json()
    _calm_odata_token_cache["token"] = d["access_token"]
    _calm_odata_token_cache["expires_at"] = now + d.get("expires_in", 3600) - 300
    return _calm_odata_token_cache["token"]

def _calm_headers() -> dict:
    if os.environ.get("CALM_SANDBOX", "").lower() == "true":
        return {"APIKey": os.environ.get("CALM_API_KEY", ""), "Accept": "application/json"}
    return {"Authorization": f"Bearer {_calm_odata_token()}", "Accept": "application/json"}

def _calm_req(method: str, path: str, params: dict = None, body: dict = None) -> dict:
    url = _calm_base_url().rstrip("/") + path
    with httpx.Client(timeout=30.0) as c:
        r = c.request(method, url, params=params or {}, json=body,
                      headers={**_calm_headers(),
                                **({"Content-Type": "application/json"} if body else {})})
        r.raise_for_status()
        return r.json() if r.content else {}

def _odata_params(top=50, skip=0, filter_="", select="", orderby="") -> dict:
    p: dict = {"$top": top, "$skip": skip}
    if filter_:  p["$filter"]  = filter_
    if select:   p["$select"]  = select
    if orderby:  p["$orderby"] = orderby
    return p


# ── Projects API (REST) ───────────────────────────────────────────────────────
def calm_list_projects(top: int = 50, skip: int = 0) -> str:
    """List all SAP Cloud ALM projects."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-projects/v1/projects",
                                    {"$top": top, "$skip": skip}), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_get_project(project_id: str) -> str:
    """Get details of a specific SAP Cloud ALM project by ID."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-projects/v1/projects/{project_id}"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_project_timeboxes(project_id: str) -> str:
    """List sprints/timeboxes for a SAP Cloud ALM project."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-projects/v1/projects/{project_id}/timeboxes"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_project_teams(project_id: str) -> str:
    """List team members for a SAP Cloud ALM project."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-projects/v1/projects/{project_id}/teams"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_programs(top: int = 50, skip: int = 0) -> str:
    """List all SAP Cloud ALM programs."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-projects/v1/programs",
                                    {"$top": top, "$skip": skip}), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_create_project(title: str, description: str = "", project_type: str = "") -> str:
    """Create a new SAP Cloud ALM project. Requires title."""
    body: dict = {"title": title}
    if description: body["description"] = description
    if project_type: body["type"] = project_type
    try:
        return json.dumps(_calm_req("POST", "/api/calm-projects/v1/projects", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Tasks API (REST) ──────────────────────────────────────────────────────────
def calm_list_tasks(project_id: str, top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List tasks for a SAP Cloud ALM project. Optionally filter by OData $filter expression."""
    params: dict = {"$top": top, "$skip": skip}
    if filter_: params["$filter"] = filter_
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-tasks/v1/projects/{project_id}/tasks", params), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_get_task(project_id: str, task_id: str) -> str:
    """Get details of a specific task in a SAP Cloud ALM project."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-tasks/v1/projects/{project_id}/tasks/{task_id}"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_create_task(project_id: str, title: str, description: str = "",
                     assignee: str = "", due_date: str = "", status: str = "") -> str:
    """Create a new task in a SAP Cloud ALM project."""
    body: dict = {"title": title}
    if description: body["description"] = description
    if assignee:    body["assignee"] = assignee
    if due_date:    body["dueDate"] = due_date
    if status:      body["status"] = status
    try:
        return json.dumps(_calm_req("POST", f"/api/calm-tasks/v1/projects/{project_id}/tasks", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_update_task(project_id: str, task_id: str, title: str = "",
                     description: str = "", status: str = "", assignee: str = "") -> str:
    """Update an existing task in a SAP Cloud ALM project."""
    body: dict = {}
    if title:       body["title"] = title
    if description: body["description"] = description
    if status:      body["status"] = status
    if assignee:    body["assignee"] = assignee
    try:
        return json.dumps(_calm_req("PATCH", f"/api/calm-tasks/v1/projects/{project_id}/tasks/{task_id}", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_delete_task(project_id: str, task_id: str) -> str:
    """Delete a task from a SAP Cloud ALM project."""
    try:
        _calm_req("DELETE", f"/api/calm-tasks/v1/projects/{project_id}/tasks/{task_id}")
        return json.dumps({"success": True, "task_id": task_id})
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_task_comments(project_id: str, task_id: str) -> str:
    """List comments on a SAP Cloud ALM task."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-tasks/v1/projects/{project_id}/tasks/{task_id}/comments"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_create_task_comment(project_id: str, task_id: str, comment: str) -> str:
    """Add a comment to a SAP Cloud ALM task."""
    try:
        return json.dumps(_calm_req("POST", f"/api/calm-tasks/v1/projects/{project_id}/tasks/{task_id}/comments",
                                    body={"text": comment}), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_workstreams(project_id: str) -> str:
    """List workstreams for a SAP Cloud ALM project."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-tasks/v1/projects/{project_id}/workstreams"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Features API (OData v4) ───────────────────────────────────────────────────
def calm_list_features(top: int = 50, skip: int = 0, filter_: str = "",
                       select: str = "", orderby: str = "") -> str:
    """List SAP Cloud ALM features with optional OData filtering, selection and ordering."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-features/v1/Features",
                                    _odata_params(top, skip, filter_, select, orderby)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_get_feature(feature_id: str) -> str:
    """Get a single SAP Cloud ALM feature by UUID."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-features/v1/Features({feature_id})"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_create_feature(title: str, description: str = "", status: str = "", priority: str = "") -> str:
    """Create a new SAP Cloud ALM feature."""
    body: dict = {"title": title}
    if description: body["description"] = description
    if status:      body["status"] = status
    if priority:    body["priority"] = priority
    try:
        return json.dumps(_calm_req("POST", "/api/calm-features/v1/Features", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_update_feature(feature_id: str, title: str = "", description: str = "",
                        status: str = "", priority: str = "") -> str:
    """Update an existing SAP Cloud ALM feature."""
    body: dict = {}
    if title:       body["title"] = title
    if description: body["description"] = description
    if status:      body["status"] = status
    if priority:    body["priority"] = priority
    try:
        return json.dumps(_calm_req("PATCH", f"/api/calm-features/v1/Features({feature_id})", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_delete_feature(feature_id: str) -> str:
    """Delete a SAP Cloud ALM feature by UUID."""
    try:
        _calm_req("DELETE", f"/api/calm-features/v1/Features({feature_id})")
        return json.dumps({"success": True, "feature_id": feature_id})
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_feature_statuses() -> str:
    """List available status codes for SAP Cloud ALM features."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-features/v1/FeatureStatuses"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_feature_priorities() -> str:
    """List available priority codes for SAP Cloud ALM features."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-features/v1/FeaturePriorities"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Documents API (OData v4) ──────────────────────────────────────────────────
def calm_list_documents(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP Cloud ALM documents with optional OData filtering."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-documents/v1/Documents",
                                    _odata_params(top, skip, filter_)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_get_document(document_id: str) -> str:
    """Get a single SAP Cloud ALM document by ID."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-documents/v1/Documents({document_id})"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_create_document(title: str, content: str = "", doc_type: str = "", status: str = "") -> str:
    """Create a new SAP Cloud ALM document."""
    body: dict = {"title": title}
    if content:  body["content"] = content
    if doc_type: body["type"] = doc_type
    if status:   body["status"] = status
    try:
        return json.dumps(_calm_req("POST", "/api/calm-documents/v1/Documents", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_update_document(document_id: str, title: str = "", content: str = "", status: str = "") -> str:
    """Update an existing SAP Cloud ALM document."""
    body: dict = {}
    if title:   body["title"] = title
    if content: body["content"] = content
    if status:  body["status"] = status
    try:
        return json.dumps(_calm_req("PATCH", f"/api/calm-documents/v1/Documents({document_id})", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_delete_document(document_id: str) -> str:
    """Delete a SAP Cloud ALM document."""
    try:
        _calm_req("DELETE", f"/api/calm-documents/v1/Documents({document_id})")
        return json.dumps({"success": True, "document_id": document_id})
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_document_types() -> str:
    """List available document types in SAP Cloud ALM."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-documents/v1/DocumentTypes"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Test Management API (OData v4) ────────────────────────────────────────────
def calm_list_testcases(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP Cloud ALM manual test cases with optional OData filtering."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-testmanagement/v1/TestCases",
                                    _odata_params(top, skip, filter_)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_get_testcase(testcase_id: str) -> str:
    """Get a single SAP Cloud ALM test case by ID."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-testmanagement/v1/TestCases({testcase_id})"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_create_testcase(title: str, description: str = "", status: str = "") -> str:
    """Create a new SAP Cloud ALM test case."""
    body: dict = {"title": title}
    if description: body["description"] = description
    if status:      body["status"] = status
    try:
        return json.dumps(_calm_req("POST", "/api/calm-testmanagement/v1/TestCases", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_test_activities(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP Cloud ALM test activities."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-testmanagement/v1/TestActivities",
                                    _odata_params(top, skip, filter_)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_test_actions(top: int = 50, skip: int = 0) -> str:
    """List SAP Cloud ALM test actions."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-testmanagement/v1/TestActions",
                                    _odata_params(top, skip)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Process Hierarchy API (OData v4) ──────────────────────────────────────────
def calm_list_hierarchy_nodes(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP Cloud ALM process hierarchy nodes."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-processhierarchy/v1/ProcessHierarchyNodes",
                                    _odata_params(top, skip, filter_)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_get_hierarchy_node(node_id: str) -> str:
    """Get a single SAP Cloud ALM process hierarchy node by ID."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-processhierarchy/v1/ProcessHierarchyNodes({node_id})"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_create_hierarchy_node(title: str, parent_id: str = "", node_type: str = "") -> str:
    """Create a new SAP Cloud ALM process hierarchy node."""
    body: dict = {"title": title}
    if parent_id:  body["parentId"] = parent_id
    if node_type:  body["type"] = node_type
    try:
        return json.dumps(_calm_req("POST", "/api/calm-processhierarchy/v1/ProcessHierarchyNodes", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_update_hierarchy_node(node_id: str, title: str = "", status: str = "") -> str:
    """Update a SAP Cloud ALM process hierarchy node."""
    body: dict = {}
    if title:  body["title"] = title
    if status: body["status"] = status
    try:
        return json.dumps(_calm_req("PATCH", f"/api/calm-processhierarchy/v1/ProcessHierarchyNodes({node_id})", body=body), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_delete_hierarchy_node(node_id: str) -> str:
    """Delete a SAP Cloud ALM process hierarchy node."""
    try:
        _calm_req("DELETE", f"/api/calm-processhierarchy/v1/ProcessHierarchyNodes({node_id})")
        return json.dumps({"success": True, "node_id": node_id})
    except Exception as e: return json.dumps({"error": str(e)})


# ── Process Monitoring API (OData v4) ─────────────────────────────────────────
def calm_list_monitoring_events(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP Cloud ALM process monitoring events with optional OData filtering."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-processmonitoring/v1/MonitoringEvents",
                                    _odata_params(top, skip, filter_)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_get_monitoring_event(event_id: str) -> str:
    """Get details of a specific SAP Cloud ALM monitoring event."""
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-processmonitoring/v1/MonitoringEvents({event_id})"), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def calm_list_monitored_services(top: int = 50, skip: int = 0) -> str:
    """List services monitored in SAP Cloud ALM."""
    try:
        return json.dumps(_calm_req("GET", "/api/calm-processmonitoring/v1/MonitoredServices",
                                    _odata_params(top, skip)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Analytics API (OData v4) ──────────────────────────────────────────────────
_ANALYTICS_PROVIDERS = {
    "requirements": "Requirements", "tasks": "Tasks", "alerts": "Alerts",
    "defects": "Defects", "features": "Features", "tests": "Tests",
    "quality_gates": "QualityGates", "projects": "Projects",
    "configuration_items": "ConfigurationItems", "exceptions": "Exceptions",
    "jobs": "Jobs", "messages": "Messages", "metrics": "Metrics",
    "monitoring_events": "MonitoringEvents", "requests": "Requests",
    "scenario_executions": "ScenarioExecutions", "service_levels": "ServiceLevels",
    "status_events": "StatusEvents",
}

def calm_list_analytics_providers() -> str:
    """List all available SAP Cloud ALM analytics data providers."""
    return json.dumps({"providers": list(_ANALYTICS_PROVIDERS.keys())}, indent=2)

def calm_query_analytics(provider: str, top: int = 100, skip: int = 0,
                         filter_: str = "", select: str = "") -> str:
    """Query a SAP Cloud ALM analytics dataset by provider name.
    Use calm_list_analytics_providers() to see available providers.
    """
    entity = _ANALYTICS_PROVIDERS.get(provider, provider)
    try:
        return json.dumps(_calm_req("GET", f"/api/calm-analytics/v1/{entity}",
                                    _odata_params(top, skip, filter_, select)), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})
