"""
SAP MCP Generator Agent — generates and deploys focused SAP MCP servers to AgentCore.

Detects domain from prompt (S/4HANA OData, Cloud ALM, SuccessFactors) and:
1. Discovers relevant APIs / services
2. Generates Python FastMCP server code via Claude (claude-sonnet-4-6)
3. Uploads to S3 + triggers CodeBuild → deploys to AgentCore Runtime

Port: 8105
Model: us.anthropic.claude-sonnet-4-6 (code generation needs full reasoning)
"""
import os, sys, json, logging, uuid, httpx, xml.etree.ElementTree as ET
import boto3, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
from agents.storage import save_generated_agent, list_generated_agents
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generator_agent")

MODEL_ID     = "us.anthropic.claude-sonnet-4-6"
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
CATALOG_URL  = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"

mcp = FastMCP("AI Factory — Generator Agent", host="0.0.0.0", port=8105)
logger = logging.getLogger("ai_factory_generator")

# ── Domain detection ──────────────────────────────────────────────────────────
_CALM_KEYWORDS = {"cloud alm", "cloudalm", "calm", "alm monitoring", "alm project",
                  "alm task", "alm feature", "process monitoring", "alm analytics",
                  "alm document", "test management", "process hierarchy"}
_SF_KEYWORDS   = {"successfactors", "success factors", "sfsf", "hcm", "employee central",
                  "payroll", "recruiting", "learning", "performance", "compensation",
                  "workforce", "onboarding", "succession"}

def _detect_domain(prompt: str) -> str:
    p = prompt.lower()
    if any(k in p for k in _CALM_KEYWORDS): return "calm"
    if any(k in p for k in _SF_KEYWORDS):   return "sf"
    return "s4"


# ── Token helper ──────────────────────────────────────────────────────────────
def _get_token(ctx: Context) -> str:
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") \
                    if val.lower().startswith("bearer ") else val
    except Exception: pass
    return os.environ.get("SAP_BEARER_TOKEN", "")


# ── S/4 service discovery helpers ────────────────────────────────────────────
def _sap_get(path: str, token: str, params: dict = None) -> dict:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()

def _sap_get_xml(path: str, token: str) -> str:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
        r.raise_for_status()
        return r.text

def _discover_relevant_services(token: str, keywords: list) -> list:
    try:
        services = _sap_get(CATALOG_URL, token, {"$format": "json"}).get("d", {}).get("results", [])
        matched = []
        for svc in services:
            title = (svc.get("Title","") + svc.get("TechnicalServiceName","") +
                     svc.get("Description","")).lower()
            if any(kw.lower() in title for kw in keywords):
                matched.append({"Title": svc.get("Title",""),
                                 "TechnicalServiceName": svc.get("TechnicalServiceName","")})
        api = [s for s in matched if s["Title"].startswith("API_")]
        std = [s for s in matched if not s["TechnicalServiceName"].startswith("Z") and not s["Title"].startswith("API_")]
        z   = [s for s in matched if s["TechnicalServiceName"].startswith("Z") and not s["Title"].startswith("API_")]
        return (api + std + z)[:10]
    except Exception as e:
        logger.error(f"Service discovery failed: {e}"); return []

def _get_entities_for_service(service_path: str, token: str) -> list:
    try:
        root = ET.fromstring(_sap_get_xml(f"{service_path}/$metadata", token))
        entities = []
        for ns in ["http://schemas.microsoft.com/ado/2008/09/edm",
                   "http://schemas.microsoft.com/ado/2009/11/edm",
                   "http://docs.oasis-open.org/odata/ns/edm"]:
            for et in root.iter(f"{{{ns}}}EntityType"):
                name = et.get("Name","")
                if name and not name.startswith("I_") and not name.startswith("SAP__"):
                    entities.append(name)
        return entities[:5]
    except Exception as e:
        logger.error(f"Entity fetch failed for {service_path}: {e}"); return []


# ── Bedrock code generation ───────────────────────────────────────────────────
def _generate_s4_tools(prompt: str, services_with_entities: list, agent_name: str) -> str:
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
    system = (
        "You are an expert SAP developer. Generate Python MCP tool functions for a FastMCP server.\n"
        "CRITICAL: Entity type names end in 'Type' — strip it for the URL path.\n"
        "  e.g. A_SalesOrderType → use A_SalesOrder in the URL\n\n"
        "You have ONE data access method:\n"
        "_sap_get(path, token, params) — for OData API queries\n\n"
        "Pattern:\n"
        "@mcp.tool()\n"
        "def func_name(ctx: Context, ...) -> str:\n"
        "    token = _get_token(ctx)\n"
        "    data = _sap_get('/sap/opu/odata/sap/SVC/Entity', token, {'$format':'json','$top':'10'})\n"
        "    return json.dumps(data.get('d',{}).get('results',[]))\n\n"
        "For complex business logic (aggregation, bucketing, filtering):\n"
        "- Fetch raw data via OData\n"
        "- Process/aggregate in Python code within the tool function\n"
        "- Return the computed result as JSON\n\n"
        "CRITICAL PYTHON SYNTAX RULES:\n"
        "- Do NOT use f-strings with nested quotes — use string concatenation instead\n"
        "- Use 'Bearer ' + token, NOT f'Bearer {token}'\n"
        "- Each function must start with @mcp.tool() decorator on its own line\n"
        "- Each function must have proper 4-space indentation\n"
        "- Wrap all tool logic in try/except returning json.dumps\n"
        "- No markdown, no ```, no explanation — ONLY Python function definitions\n"
    )
    # Build user message with OData services
    user_content = f"Request: {prompt}\n\nServices:\n{json.dumps(services_with_entities, indent=2)}\n\n"
    user_content += "Generate 3-7 MCP tools for this domain."

    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                         "system": system,
                         "messages": [{"role": "user", "content": user_content}]}),
        contentType="application/json", accept="application/json")
    return json.loads(resp["body"].read())["content"][0]["text"]

def _generate_calm_tools(prompt: str, custom_apis: dict = None) -> str:
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
    calm_apis = custom_apis if custom_apis else {
        "projects":   "/api/calm-projects/v1/projects",
        "tasks":      "/api/calm-tasks/v1/projects/{project_id}/tasks",
        "features":   "/api/calm-features/v1/Features",
        "documents":  "/api/calm-documents/v1/Documents",
        "testcases":  "/api/calm-testmanagement/v1/TestCases",
        "monitoring": "/api/calm-processmonitoring/v1/MonitoringEvents",
        "analytics":  "/api/calm-analytics/v1/",
        "hierarchy":  "/api/calm-processhierarchy/v1/ProcessHierarchyNodes",
    }
    system = (
        "You are an expert SAP Cloud ALM developer. Generate Python MCP tool functions.\n"
        "Use _calm_get(path, params) for GET, _calm_post(path, body) for POST.\n"
        "Each function: def func_name(ctx: Context, ...) -> str\n"
        "Return ONLY function definitions, no imports, no main block."
    )
    resp = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name).invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                         "system": system,
                         "messages": [{"role": "user", "content":
                             f"Request: {prompt}\n\nAPIs:\n{json.dumps(calm_apis, indent=2)}\n\n"
                             f"Generate 4-8 focused tools. Use names like calm_list_monitoring_events."}]}),
        contentType="application/json", accept="application/json")
    return json.loads(resp["body"].read())["content"][0]["text"]

def _generate_sf_tools(prompt: str, custom_apis: dict = None) -> str:
    sf_apis = custom_apis if custom_apis else {
        "User": "/odata/v2/User", "PerPerson": "/odata/v2/PerPerson",
        "EmpEmployment": "/odata/v2/EmpEmployment", "Position": "/odata/v2/Position",
        "FODepartment": "/odata/v2/FODepartment", "FOLocation": "/odata/v2/FOLocation",
        "JobRequisition": "/odata/v2/JobRequisition", "Candidate": "/odata/v2/Candidate",
        "LearningActivity": "/odata/v2/LearningActivity",
        "PerformanceReview": "/odata/v2/PerformanceReview",
        "CompensationEmployee": "/odata/v2/CompensationEmployee",
    }
    system = (
        "You are an expert SAP SuccessFactors developer. Generate Python MCP tool functions.\n"
        "Use _sf_get(path, params). Parse results from response.get('d',{}).get('results',[])\n"
        "Each function: def func_name(ctx: Context, top: int = 10, skip: int = 0, filter_expr: str = '') -> str\n"
        "Return ONLY function definitions, no imports, no main block."
    )
    resp = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name).invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                         "system": system,
                         "messages": [{"role": "user", "content":
                             f"Request: {prompt}\n\nEntities:\n{json.dumps(sf_apis, indent=2)}\n\n"
                             f"Generate 4-8 focused tools. Use names like sf_list_employees."}]}),
        contentType="application/json", accept="application/json")
    return json.loads(resp["body"].read())["content"][0]["text"]


# ── Server templates ──────────────────────────────────────────────────────────
_S4_TEMPLATE = '''"""
__DESCRIPTION__ — Auto-generated AI Factory MCP Agent (S/4HANA).
Uses OData APIs exclusively for stable, production-safe data access.
"""
import os, json, logging, httpx
from mcp.server.fastmcp import FastMCP, Context
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("__AGENT_NAME__")
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
mcp = FastMCP("__AGENT_NAME__", host="0.0.0.0", stateless_http=True)
def _get_token(ctx):
    try:
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = ctx.request_context.request.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
    except: pass
    return os.environ.get("SAP_BEARER_TOKEN","")
def _sap_get(path, token, params=None):
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(f"{SAP_BASE_URL}{path}", headers={"Authorization":"Bearer " + token,"Accept":"application/json"}, params=params or {})
        r.raise_for_status(); return r.json()
__TOOLS__
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''

# ── SQL-backed S/4HANA agent (ABAP SQL via ADT Data Preview — no OData/CDS) ───
_S4_SQL_TEMPLATE = '''"""
__DESCRIPTION__ — Auto-generated AI Factory MCP Agent (S/4HANA, SQL-backed).
Runs ABAP SQL directly via the ADT Data Preview endpoint. No OData services or
CDS views required. The validated Okta JWT is forwarded to SAP as the bearer
token (SAP trusts Okta-issued JWTs), exactly like the AI Factory parent.
"""
import os, json, logging, re, httpx
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET
from mcp.server.fastmcp import FastMCP, Context
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("__AGENT_NAME__")
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
ADT_FREESTYLE = "/sap/bc/adt/datapreview/freestyle"
ADT_SQLCONSOLE = "/sap/bc/adt/datapreview/sqlConsole"
mcp = FastMCP("__AGENT_NAME__", host="0.0.0.0", stateless_http=True)
def _get_token(ctx):
    try:
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = ctx.request_context.request.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
    except Exception: pass
    return os.environ.get("SAP_BEARER_TOKEN", "")
def _parse_adt_table(text):
    """Parse ADT Data Preview XML (column-oriented) into a list of row dicts."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                return [{str(k).upper(): v for k, v in r.items()} for r in obj]
        except Exception:
            pass
    try:
        root = ET.fromstring(text)
    except Exception:
        return []
    def _local(t): return t.rsplit("}", 1)[-1]
    cols, vals = [], []
    for col in root.iter():
        if _local(col.tag) != "columns":
            continue
        name = None; cells = []
        for child in col:
            lt = _local(child.tag)
            if lt == "metadata":
                name = child.attrib.get("name")
            elif lt in ("dataSet", "data"):
                for cell in (child.iter() if lt == "dataSet" else [child]):
                    if _local(cell.tag) == "data":
                        cells.append(cell.text or "")
        if name is None:
            for k, v in col.attrib.items():
                if _local(k) == "name": name = v; break
        if name:
            cols.append(name.upper()); vals.append(cells)
    if not cols:
        return []
    n = max((len(c) for c in vals), default=0)
    rows = []
    for i in range(n):
        rows.append({cols[ci]: (vals[ci][i] if i < len(vals[ci]) else "") for ci in range(len(cols))})
    return rows
def _run_sql(token, sql, max_rows=100):
    """Execute ABAP SQL via ADT Data Preview freestyle POST.
    On a 4xx/5xx SQL error, return it immediately (do NOT hang on a dead fallback).
    Only the sqlConsole GET is tried as a last resort, with a short timeout."""
    sql = " ".join(sql.split())
    with httpx.Client(verify=False, timeout=60.0) as c:
        csrf = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                     headers={"Authorization": "Bearer " + token, "x-csrf-token": "Fetch", "Accept": "*/*"}).headers.get("x-csrf-token", "")
        last = None
        # Correct content type FIRST (table.v1+xml), then xml, then */*. A 406 means
        # try the next Accept; any other status (200/400/500) is final for freestyle.
        for accept in ["application/vnd.sap.adt.datapreview.table.v1+xml", "application/xml", "*/*"]:
            r = c.post(f"{SAP_BASE_URL}{ADT_FREESTYLE}", content=sql.encode("utf-8"),
                       headers={"Authorization": "Bearer " + token, "Content-Type": "text/plain",
                                "Accept": accept, "x-csrf-token": csrf},
                       params={"rowNumber": str(max_rows)})
            last = r
            if r.status_code == 200:
                return _parse_adt_table(r.text)
            if r.status_code != 406:
                break
        # Surface a real SQL error from freestyle immediately — sqlConsole does not
        # exist on all systems (404) and must never hang the request.
        if last is not None and last.status_code not in (404, 406):
            msg = last.text
            import re as _re
            m = _re.search(r"<message[^>]*>([^<]+)</message>", msg or "")
            raise RuntimeError("ADT SQL error (freestyle HTTP " + str(last.status_code) + "): "
                               + (m.group(1) if m else (msg or "")[:200]))
        try:
            r2 = c.get(f"{SAP_BASE_URL}{ADT_SQLCONSOLE}",
                       headers={"Authorization": "Bearer " + token, "Accept": "application/xml"},
                       params={"rowNumber": str(max_rows), "sqlCommand": sql},
                       timeout=15.0)
            if r2.status_code == 200:
                return _parse_adt_table(r2.text)
            raise RuntimeError("ADT SQL failed: freestyle=" + str(last.status_code if last else "n/a")
                               + " sqlConsole=" + str(r2.status_code))
        except httpx.TimeoutException:
            raise RuntimeError("ADT SQL failed: freestyle=" + str(last.status_code if last else "n/a")
                               + " sqlConsole=timeout")
def _num(v):
    try: return float(str(v).strip() or 0)
    except Exception: return 0.0
def _sap_date(d): return d.strftime("%Y%m%d")
def _window(days_back):
    t = date.today()
    return _sap_date(t - timedelta(days=days_back)), _sap_date(t)
def _safe(s):
    return re.sub(r"[^A-Za-z0-9]", "", str(s or ""))
__TOOLS__
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''


def _strip_code_fences(code: str) -> str:
    """Remove markdown code fences the model sometimes wraps generated code in.
    Strips a leading ```python / ``` line and a trailing ``` line, plus any stray
    fence lines, so the result is pure Python ready to embed in the template."""
    if not code:
        return code
    lines = code.splitlines()
    cleaned = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("```"):      # drop any fence line (```python, ```, etc.)
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip("\n")


def _generate_s4_sql_tools(prompt: str, agent_name: str, tables: list = None) -> str:
    """Generate ABAP-SQL-backed MCP tool functions via Claude. No OData — the
    generated tools call _run_sql(token, sql) and aggregate in Python."""
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
    system = (
        "You are an expert SAP ABAP/S4HANA developer. Generate Python MCP tool functions "
        "for a FastMCP server that queries SAP using DIRECT ABAP SQL (no OData, no CDS).\n\n"
        "You have ONE data access method already defined in the file:\n"
        "  _run_sql(token, sql, max_rows=100) -> list[dict]   # runs ABAP SQL via ADT Data Preview\n"
        "Helpers also available: _num(v)->float, _sap_date(date)->'YYYYMMDD', "
        "_window(days_back)->(date_from,date_to), _safe(s)->sanitized string.\n\n"
        "ABAP SQL rules (CRITICAL):\n"
        "- The ADT Data Preview freestyle parser does NOT accept table ALIASES in JOINs.\n"
        "  Use FULL TABLE NAMES with ~ everywhere — NEVER 'ekko AS h' or 'ekko h'.\n"
        "  CORRECT:  FROM ekko INNER JOIN ekbe ON ekbe~ebeln = ekko~ebeln\n"
        "            INNER JOIN lfa1 ON lfa1~lifnr = ekko~lifnr\n"
        "  Refs use the full table name: ekko~ebeln, ekbe~wrbtr, lfa1~name1.\n"
        "  WRONG (rejected): FROM ekko AS h ... e~ebeln  /  FROM ekko h ... h~ebeln\n"
        "- JOIN / INNER JOIN, GROUP BY, SUM(), COUNT(), MAX(), MIN(), CASE WHEN are supported.\n"
        "- Do NOT use 'UP TO n ROWS' on JOIN queries (engine auto-caps at 100).\n"
        "- NEVER use an empty string literal '' in a CASE/ELSE or anywhere — the ADT engine\n"
        "  rejects it ('empty character literal'). For a 'no value' fallback use a single\n"
        "  space ' ' for char fields, '00000000' for date (e.g. budat) fields, or 0 for numeric.\n"
        "  e.g. MAX( CASE WHEN ekbe~vgabe = '1' THEN ekbe~budat ELSE '00000000' END ).\n"
        "- Use LOWERCASE column aliases (AS gr_amt, not AS GR_AMT).\n"
        "- SAP dates are 'YYYYMMDD' strings. Amounts come back as strings — wrap with _num().\n"
        "- Result dict keys are UPPER-CASE column/alias names.\n\n"
        "Tool pattern (follow EXACTLY):\n"
        "@mcp.tool()\n"
        "def tool_name(ctx: Context, days_back: int = 30, company_code: str = '') -> str:\n"
        "    \"\"\"One-line summary of what this tool returns. Mention the SAP tables and the\n"
        "    key business concept (e.g. 'GR/IR open items: POs with goods received but not\n"
        "    yet invoiced'). Document each argument. This docstring is REQUIRED.\"\"\"\n"
        "    token = _get_token(ctx)\n"
        "    if not token:\n"
        "        return 'ERROR: no bearer token'\n"
        "    date_from, date_to = _window(days_back)\n"
        "    bukrs = (\" AND ekko~bukrs = '\" + _safe(company_code) + \"'\") if company_code else ''\n"
        "    sql = (\"SELECT ekko~ebeln, ekko~bukrs, lfa1~name1, \"\n"
        "           \"SUM( CASE WHEN ekbe~vgabe = '1' THEN ekbe~wrbtr ELSE 0 END ) AS gr_amt \"\n"
        "           \"FROM ekko INNER JOIN ekbe ON ekbe~ebeln = ekko~ebeln \"\n"
        "           \"INNER JOIN lfa1 ON lfa1~lifnr = ekko~lifnr \"\n"
        "           \"WHERE ekko~bstyp = 'F' AND ekko~bedat >= '\" + date_from + \"' AND ekko~bedat <= '\" + date_to + \"'\" + bukrs +\n"
        "           \" GROUP BY ekko~ebeln, ekko~bukrs, lfa1~name1\")\n"
        "    try:\n"
        "        rows = _run_sql(token, sql)\n"
        "    except Exception as e:\n"
        "        return json.dumps({'error': str(e), 'sql': sql})\n"
        "    # aggregate / compute in Python, then build the report\n"
        "    return json.dumps({'rows': rows, 'data_source': {'tables': [...], 'sql': sql}}, indent=2)\n\n"
        "CRITICAL PYTHON SYNTAX RULES:\n"
        "- Build SQL with string concatenation, NOT f-strings (avoid nested-quote bugs).\n"
        "- Use 'Bearer ' + token, never f'Bearer {token}'.\n"
        "- Every tool starts with @mcp.tool() on its own line, 4-space indentation.\n"
        "- Every tool MUST have a descriptive docstring as its first statement (used by\n"
        "  MCP clients to describe the tool — never leave it blank).\n"
        "- Every tool wraps logic in try/except and returns a JSON string.\n"
        "- Every tool's return MUST include a 'data_source' object with the exact tables and SQL used.\n"
        "- Output ONLY Python function definitions. No imports, no main block, no markdown, no ```.\n"
    )
    user = f"Request: {prompt}\n\n"
    if tables:
        user += f"Primary SAP tables to use: {', '.join(tables)}\n\n"
    user += ("Generate the focused MCP tools described in the request. "
             "Each tool must run real ABAP SQL via _run_sql and compute the result in Python.\n"
             "Keep each tool compact and self-contained. CRITICAL: emit COMPLETE, "
             "syntactically valid Python — every '(' , '[' and '{' must be closed. "
             "Do not truncate mid-function; if space is tight, generate fewer helper "
             "lines rather than leaving a structure unclosed.")

    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 8192,
                         "system": system,
                         "messages": [{"role": "user", "content": user}]}),
        contentType="application/json", accept="application/json")
    code = json.loads(resp["body"].read())["content"][0]["text"]
    return _strip_code_fences(code)


_CALM_TEMPLATE = '''"""
__DESCRIPTION__ — Auto-generated AI Factory MCP Agent (Cloud ALM).
"""
import os, json, logging, httpx, time as _t
from mcp.server.fastmcp import FastMCP, Context
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("__AGENT_NAME__")
mcp = FastMCP("__AGENT_NAME__", host="0.0.0.0", stateless_http=True)
_tc = {"token": None, "exp": 0}
def _get_calm_token():
    now = _t.time()
    if _tc["token"] and now < _tc["exp"]: return _tc["token"]
    tenant=os.environ.get("CALM_TENANT",""); region=os.environ.get("CALM_REGION","eu10")
    r = httpx.post(os.environ.get("CALM_TOKEN_URL",f"https://{tenant}.authentication.{region}.hana.ondemand.com/oauth/token"),
        data={"grant_type":"client_credentials"}, auth=(os.environ["CALM_CLIENT_ID"],os.environ["CALM_CLIENT_SECRET"]))
    r.raise_for_status(); d=r.json(); _tc["token"]=d["access_token"]; _tc["exp"]=now+d.get("expires_in",3600)-300; return _tc["token"]
def _calm_get(path, params=None):
    base=f"https://{os.environ.get('CALM_TENANT','')}.{os.environ.get('CALM_REGION','eu10')}.alm.cloud.sap"
    r=httpx.get(f"{base}{path}",params=params or {},headers={"Authorization":f"Bearer {_get_calm_token()}","Accept":"application/json"}); r.raise_for_status(); return r.json()
def _calm_post(path, body):
    base=f"https://{os.environ.get('CALM_TENANT','')}.{os.environ.get('CALM_REGION','eu10')}.alm.cloud.sap"
    r=httpx.post(f"{base}{path}",json=body,headers={"Authorization":f"Bearer {_get_calm_token()}","Content-Type":"application/json"}); r.raise_for_status(); return r.json() if r.content else {}
__TOOLS__
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''

_SF_TEMPLATE = '''"""
__DESCRIPTION__ — Auto-generated AI Factory MCP Agent (SuccessFactors).
"""
import os, json, logging, httpx, time as _t
from mcp.server.fastmcp import FastMCP, Context
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("__AGENT_NAME__")
mcp = FastMCP("__AGENT_NAME__", host="0.0.0.0", stateless_http=True)
_tc = {"token": None, "exp": 0}
def _get_sf_token():
    now=_t.time()
    if _tc["token"] and now < _tc["exp"]: return _tc["token"]
    dc=os.environ.get("SF_DC","4"); company_id=os.environ.get("SF_COMPANY_ID","")
    r=httpx.post(os.environ.get("SF_TOKEN_URL",f"https://api{dc}.successfactors.com/oauth/token"),
        data={"grant_type":"client_credentials","company_id":company_id},
        auth=(os.environ["SF_CLIENT_ID"],os.environ["SF_CLIENT_SECRET"]))
    r.raise_for_status(); d=r.json(); _tc["token"]=d["access_token"]; _tc["exp"]=now+d.get("expires_in",3600)-300; return _tc["token"]
def _sf_get(path, params=None):
    dc=os.environ.get("SF_DC","4")
    r=httpx.get(f"https://api{dc}.successfactors.com{path}",params={**(params or {}), "$format":"json"},
        headers={"Authorization":f"Bearer {_get_sf_token()}","Accept":"application/json"}); r.raise_for_status(); return r.json()
__TOOLS__
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''


# ── Infrastructure provisioning ───────────────────────────────────────────────
# ── CodeBuild buildspec (clean, canonical). Passed as buildspecOverride on every
# start_build so we never depend on a stale/broken buildspec stored in the shared
# project (an older project had a malformed YAML buildspec that broke DOWNLOAD_SOURCE).
_BUILDSPEC = """version: 0.2
env:
  parameter-store:
    OKTA_CLIENT_ID: "/sap_smart_agent/okta_client_id"
    OKTA_DOMAIN: "/sap_smart_agent/okta_domain"
phases:
  install:
    runtime-versions:
      python: 3.11
    commands:
      - pip install bedrock-agentcore-starter-toolkit boto3 --quiet
  build:
    commands:
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/server.py ./server.py
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/requirements.txt ./requirements.txt
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/meta.json ./meta.json
      - |
        python - <<'EOF'
        import os,json,time,boto3
        from bedrock_agentcore_starter_toolkit import Runtime
        from boto3.session import Session
        agent_name=json.load(open("meta.json"))["agent_name"]
        region=Session().region_name
        runtime=Runtime()
        auth={"customJWTAuthorizer":{"allowedClients":[os.environ["OKTA_CLIENT_ID"]],
            "discoveryUrl":f"https://{os.environ['OKTA_DOMAIN']}/oauth2/default/.well-known/openid-configuration"}}
        runtime.configure(entrypoint="server.py",auto_create_execution_role=True,auto_create_ecr=True,
            requirements_file="requirements.txt",region=region,authorizer_configuration=auth,
            protocol="MCP",agent_name=agent_name)
        result=runtime.launch(auto_update_on_conflict=True)
        while True:
            status=runtime.status().endpoint["status"]
            print(f"Status: {status}")
            if status in ["READY","CREATE_FAILED","UPDATE_FAILED"]: break
            time.sleep(15)
        if status=="READY":
            boto3.client("ssm",region_name=region).put_parameter(
                Name=f"/sap_generated/{agent_name}/agent_arn",Value=result.agent_arn,Type="String",Overwrite=True)
            print(f"DEPLOY_SUCCESS:{result.agent_arn}")
        else: exit(1)
        EOF
"""


def _ensure_infrastructure(region: str, ssm, cb, s3) -> tuple:
    """Idempotent: provision S3 + CodeBuild on first use, return (bucket, project)."""
    CODEBUILD_PROJECT = "sap-mcp-generator"
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    STAGING_BUCKET = f"sap-mcp-generator-{account_id}-{region}"
    try:
        bucket = ssm.get_parameter(Name="/sap_smart_agent/staging_bucket")["Parameter"]["Value"]
        project = ssm.get_parameter(Name="/sap_smart_agent/codebuild_project")["Parameter"]["Value"]
        return bucket, project
    except ssm.exceptions.ParameterNotFound:
        pass

    iam = boto3.client("iam", region_name=region)
    ROLE = "sap-mcp-generator-codebuild-role"
    trust = {"Version":"2012-10-17","Statement":[{"Effect":"Allow",
        "Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}
    try:
        role_arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]

    iam.put_role_policy(RoleName=ROLE, PolicyName="policy", PolicyDocument=json.dumps({
        "Version":"2012-10-17","Statement":[
            {"Effect":"Allow","Action":["logs:*"],"Resource":f"arn:aws:logs:{region}:{account_id}:log-group:/aws/codebuild/{CODEBUILD_PROJECT}*"},
            {"Effect":"Allow","Action":["s3:*"],"Resource":[f"arn:aws:s3:::{STAGING_BUCKET}",f"arn:aws:s3:::{STAGING_BUCKET}/*"]},
            {"Effect":"Allow","Action":["ssm:GetParameter","ssm:PutParameter"],"Resource":f"arn:aws:ssm:{region}:{account_id}:parameter/sap_*"},
            {"Effect":"Allow","Action":["ecr:*","bedrock-agentcore:*","iam:CreateRole","iam:AttachRolePolicy","iam:PassRole","iam:GetRole","iam:PutRolePolicy"],"Resource":"*"},
        ]}))

    try:
        if region == "us-east-1": s3.create_bucket(Bucket=STAGING_BUCKET)
        else: s3.create_bucket(Bucket=STAGING_BUCKET, CreateBucketConfiguration={"LocationConstraint": region})
    except Exception: pass

    import time as _time; _time.sleep(10)

    buildspec = _BUILDSPEC
    try:
        cb.create_project(name=CODEBUILD_PROJECT, description="SAP MCP generator",
            source={"type":"NO_SOURCE","buildspec":buildspec},
            artifacts={"type":"NO_ARTIFACTS"},
            environment={"type":"LINUX_CONTAINER","image":"aws/codebuild/standard:7.0",
                         "computeType":"BUILD_GENERAL1_SMALL","privilegedMode":True},
            serviceRole=role_arn, timeoutInMinutes=30)
    except cb.exceptions.ResourceAlreadyExistsException: pass

    okta_domain = os.environ.get("OKTA_DOMAIN","trial-1053860.okta.com")
    okta_client_id = os.environ.get("OKTA_CLIENT_ID","0oa10vth79kZAuXGt698")
    for k,v in {"/sap_smart_agent/staging_bucket":STAGING_BUCKET,
                "/sap_smart_agent/codebuild_project":CODEBUILD_PROJECT,
                "/sap_smart_agent/okta_domain":okta_domain,
                "/sap_smart_agent/okta_client_id":okta_client_id}.items():
        ssm.put_parameter(Name=k, Value=v, Type="String", Overwrite=True)
    return STAGING_BUCKET, CODEBUILD_PROJECT


# ── Post-deploy: write bridge + update mcp.json ───────────────────────────────
def _write_agent_bridge_and_mcp_config(agent_name: str, region: str):
    """Write the correct kiro_bridge.py to the agent folder and add entry to mcp.json."""
    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    agent_dir = os.path.join(workspace_root, f"sap-{agent_name.replace('_', '-')}-agent")
    os.makedirs(agent_dir, exist_ok=True)

    # Use the working bridge template (MCP streamable HTTP, no ROUTER_TOOLS filter)
    working_bridge = os.path.join(workspace_root, "sap-smart-agent", "kiro_bridge.py")
    with open(working_bridge) as f:
        bridge_src = f.read()

    # Strip the ROUTER_TOOLS filter — generated agents expose all their tools
    bridge_src = bridge_src.replace(
        '"""Kiro stdio bridge for AI Factory MCP Server — Okta 3LO → AgentCore or local server.\nStrips outputSchema from all tools to prevent Kiro validation errors.\n"""',
        f'"""Kiro stdio bridge for {agent_name} — Okta 3LO → AgentCore.\nAuto-generated by generator_agent. Uses MCP streamable HTTP (same pattern as ai-factory).\n"""'
    )
    # Remove ROUTER_TOOLS filter block
    bridge_src = bridge_src.replace(
        '\nROUTER_TOOLS = {\n    "adt_agent_tool", "odata_agent_tool", "calm_agent_tool",\n    "sf_agent_tool", "generator_agent_tool"\n}\n',
        '\n'
    )
    bridge_src = bridge_src.replace(
        'server = Server("ai-factory-bridge")',
        f'server = Server("{agent_name}-bridge")'
    )
    # Remove the ROUTER_TOOLS filtering logic in list_tools
    bridge_src = bridge_src.replace(
        '            tools = result.tools\n            # Filter to router tools if this is the ai-factory server\n            filtered = [t for t in tools if t.name in ROUTER_TOOLS]\n            if filtered:\n                tools = filtered\n            return _strip_output_schema(tools)',
        '            return _strip_output_schema(result.tools)'
    )

    bridge_path = os.path.join(agent_dir, "kiro_bridge.py")
    with open(bridge_path, "w") as f:
        f.write(bridge_src)
    logger.info(f"Written bridge: {bridge_path}")

    # Update mcp.json — find next available callback port
    mcp_json_path = os.path.join(workspace_root, ".kiro", "settings", "mcp.json")
    try:
        with open(mcp_json_path) as f:
            mcp_config = json.load(f)
    except Exception:
        mcp_config = {"mcpServers": {}}

    servers = mcp_config.get("mcpServers", {})

    # Find next free port (start at 8090, skip used ones)
    used_ports = set()
    for s in servers.values():
        uri = s.get("env", {}).get("OKTA_REDIRECT_URI", "")
        if uri:
            try: used_ports.add(int(uri.split(":")[-1].split("/")[0]))
            except: pass
    port = 8090
    while port in used_ports:
        port += 1

    server_key = agent_name.replace("_", "-")
    if server_key not in servers:
        # Read Okta creds from existing ai-factory entry
        ai_factory_env = servers.get("ai-factory", {}).get("env", {})
        servers[server_key] = {
            "command": "python",
            "args": ["sap-smart-agent/kiro_bridge.py"],
            "env": {
                "OKTA_DOMAIN": ai_factory_env.get("OKTA_DOMAIN", os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")),
                "OKTA_AUTH_SERVER": "default",
                "OKTA_CLIENT_ID": ai_factory_env.get("OKTA_CLIENT_ID", os.environ.get("OKTA_CLIENT_ID", "")),
                "OKTA_CLIENT_SECRET": ai_factory_env.get("OKTA_CLIENT_SECRET", os.environ.get("OKTA_CLIENT_SECRET", "")),
                "OKTA_SCOPES": "openid email",
                "OKTA_REDIRECT_URI": f"http://localhost:{port}/callback",
                "AGENTCORE_ARN_SSM_PARAM": f"/sap_generated/{agent_name}/agent_arn"
            },
            "disabled": False,
            "autoApprove": []
        }
        mcp_config["mcpServers"] = servers
        with open(mcp_json_path, "w") as f:
            json.dump(mcp_config, f, indent=2)
        logger.info(f"Added {server_key} to mcp.json on port {port}")
    else:
        logger.info(f"{server_key} already in mcp.json — skipping")


# ── Container deployment (Fargate + ALB) ──────────────────────────────────────
def _deploy_to_container(agent_name: str, server_code: str, requirements: str,
                         meta: dict, region: str) -> str:
    """Build Docker image, push to ECR, deploy to existing Fargate cluster with ALB."""
    import tempfile, subprocess, base64, shutil

    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    ecr_repo = f"sap-generated-{agent_name.replace('_', '-')}"
    ecr_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ecr_repo}"
    ecr_client = boto3.client("ecr", region_name=region)

    # 1. Ensure ECR repo
    try:
        ecr_client.create_repository(repositoryName=ecr_repo,
                                      imageScanningConfiguration={"scanOnPush": True})
        logger.info(f"Created ECR repo: {ecr_repo}")
    except ecr_client.exceptions.RepositoryAlreadyExistsException:
        pass

    # 2. Build Docker image in temp dir
    with tempfile.TemporaryDirectory() as staging:
        # Write server code
        with open(os.path.join(staging, "server.py"), "w") as f:
            f.write(server_code)

        # Write requirements
        with open(os.path.join(staging, "requirements.txt"), "w") as f:
            f.write(requirements)

        # Write meta
        with open(os.path.join(staging, "meta.json"), "w") as f:
            json.dump(meta, f)

        # Write Dockerfile
        dockerfile = f"""FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY meta.json .
RUN mkdir -p /data
ENV AI_FACTORY_DATA_DIR=/data
EXPOSE 8000
CMD ["python", "server.py"]
"""
        with open(os.path.join(staging, "Dockerfile"), "w") as f:
            f.write(dockerfile)

        # Docker login to ECR
        token = ecr_client.get_authorization_token()["authorizationData"][0]
        user, pwd = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)
        registry = token["proxyEndpoint"]
        subprocess.run(["docker", "login", "--username", user, "--password-stdin", registry],
                       input=pwd.encode(), check=True, capture_output=True)

        image_tag = f"{ecr_uri}:latest"
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", image_tag, staging],
                       check=True, capture_output=True)
        subprocess.run(["docker", "push", image_tag], check=True, capture_output=True)
        logger.info(f"Image pushed: {image_tag}")

    # 3. Deploy to Fargate (reuse existing cluster/ALB or create new service)
    ecs = boto3.client("ecs", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)
    iam_client = boto3.client("iam", region_name=region)

    cluster_name = "sap-ai-factory"
    service_name = f"sap-{agent_name.replace('_', '-')}"
    task_family = service_name
    container_port = 8000

    # Get VPC/subnets
    vpcs = ec2_client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]
    subnets = ec2_client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    subnet_ids = [s["SubnetId"] for s in subnets["Subnets"][:2]]

    # Get or create security group
    sg_name = "sap-mcp-fargate-sg"
    existing_sg = ec2_client.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [sg_name]}, {"Name": "vpc-id", "Values": [vpc_id]}])
    if existing_sg["SecurityGroups"]:
        sg_id = existing_sg["SecurityGroups"][0]["GroupId"]
    else:
        sg = ec2_client.create_security_group(GroupName=sg_name, Description="SAP MCP Fargate", VpcId=vpc_id)
        sg_id = sg["GroupId"]
        ec2_client.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            {"IpProtocol": "tcp", "FromPort": container_port, "ToPort": container_port,
             "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        ])

    # Reuse existing task/execution roles
    try:
        task_role_arn = iam_client.get_role(RoleName="sap-mcp-fargate-task-role")["Role"]["Arn"]
    except Exception:
        task_role_arn = None
    try:
        exec_role_arn = iam_client.get_role(RoleName="sap-mcp-fargate-execution-role")["Role"]["Arn"]
    except Exception:
        exec_role_arn = None

    # Ensure log group
    logs_client = boto3.client("logs", region_name=region)
    try:
        logs_client.create_log_group(logGroupName=f"/ecs/{task_family}")
    except Exception:
        pass

    # Register task definition
    container_def = {
        "name": service_name,
        "image": image_tag,
        "portMappings": [{"containerPort": container_port, "protocol": "tcp"}],
        "environment": [
            {"name": "SAP_BASE_URL", "value": SAP_BASE_URL},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{task_family}",
                "awslogs-region": region,
                "awslogs-stream-prefix": "ecs",
            },
        },
        "essential": True,
    }
    td_kwargs = {
        "family": task_family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "containerDefinitions": [container_def],
    }
    if task_role_arn:
        td_kwargs["taskRoleArn"] = task_role_arn
    if exec_role_arn:
        td_kwargs["executionRoleArn"] = exec_role_arn

    task_def_arn = ecs.register_task_definition(**td_kwargs)["taskDefinition"]["taskDefinitionArn"]

    # Ensure cluster
    try:
        ecs.create_cluster(clusterName=cluster_name, capacityProviders=["FARGATE"])
    except Exception:
        pass

    # Create target group for this agent
    tg_name = f"sap-{agent_name.replace('_', '-')}-tg"[:32]
    try:
        tgs = elbv2.describe_target_groups(Names=[tg_name])
        tg_arn = tgs["TargetGroups"][0]["TargetGroupArn"]
    except Exception:
        tg = elbv2.create_target_group(
            Name=tg_name, Protocol="HTTP", Port=container_port,
            VpcId=vpc_id, TargetType="ip",
            HealthCheckPath="/mcp",
            HealthCheckIntervalSeconds=30,
            HealthyThresholdCount=2, UnhealthyThresholdCount=3)
        tg_arn = tg["TargetGroups"][0]["TargetGroupArn"]

    # Find existing ALB or create one
    alb_name = "sap-mcp-alb"
    alb_dns = ""
    try:
        existing_alb = elbv2.describe_load_balancers(Names=[alb_name])
        alb_arn = existing_alb["LoadBalancers"][0]["LoadBalancerArn"]
        alb_dns = existing_alb["LoadBalancers"][0]["DNSName"]
    except Exception:
        alb = elbv2.create_load_balancer(
            Name=alb_name, Subnets=subnet_ids, SecurityGroups=[sg_id],
            Scheme="internet-facing", Type="application")
        alb_arn = alb["LoadBalancers"][0]["LoadBalancerArn"]
        alb_dns = alb["LoadBalancers"][0]["DNSName"]

    # Add path-based rule to existing listener (or create listener)
    path_pattern = f"/{agent_name.replace('_', '-')}/*"
    try:
        listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
        if listeners:
            listener_arn = listeners[0]["ListenerArn"]
            # Add rule for this agent's path
            existing_rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
            max_priority = max((int(r["Priority"]) for r in existing_rules if r["Priority"] != "default"), default=0)
            elbv2.create_rule(
                ListenerArn=listener_arn,
                Conditions=[{"Field": "path-pattern", "Values": [path_pattern]}],
                Priority=max_priority + 1,
                Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}])
        else:
            elbv2.create_listener(
                LoadBalancerArn=alb_arn, Protocol="HTTP", Port=80,
                DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}])
    except Exception as e:
        logger.warning(f"ALB rule setup: {e}")

    # Deploy ECS service
    try:
        ecs.update_service(cluster=cluster_name, service=service_name,
                           taskDefinition=task_def_arn, desiredCount=1)
    except ecs.exceptions.ServiceNotFoundException:
        ecs.create_service(
            cluster=cluster_name, serviceName=service_name,
            taskDefinition=task_def_arn, desiredCount=1, launchType="FARGATE",
            networkConfiguration={"awsvpcConfiguration": {
                "subnets": subnet_ids, "securityGroups": [sg_id], "assignPublicIp": "ENABLED"}},
            loadBalancers=[{"targetGroupArn": tg_arn,
                            "containerName": service_name, "containerPort": container_port}])

    endpoint = f"http://{alb_dns}"
    logger.info(f"Container deployed: {endpoint}")
    return endpoint


# ── Job store for fire-and-forget ─────────────────────────────────────────────
_jobs = {}  # job_id -> {status, agent_name, domain, error, codebuild_build_id, ...}


def _run_generation_job(job_id: str, token: str, prompt: str, agent_name: str,
                        odata_services: list = None, backend: str = "odata",
                        tables: list = None):
    """Background worker — does the heavy lifting (discovery, code gen, S3, CodeBuild).
    
    When odata_services is provided (discovery-first mode),
    skips internal discovery and uses pre-discovered OData services directly.

    backend='sql' (s4 only): generate ABAP-SQL-backed tools (no OData/CDS discovery).
    """
    try:
        _jobs[job_id]["status"] = "discovering_services"
        region = boto3.session.Session().region_name
        ssm = boto3.client("ssm", region_name=region)
        s3  = boto3.client("s3",  region_name=region)
        cb  = boto3.client("codebuild", region_name=region)

        staging_bucket, codebuild_project = _ensure_infrastructure(region, ssm, cb, s3)
        domain = _detect_domain(prompt)
        _jobs[job_id]["domain"] = domain
        logger.info(f"[{job_id}] Domain: {domain} | Backend: {backend} | Agent: {agent_name} | Prompt: {prompt}"
                     + (f" | Pre-discovered OData: {len(odata_services or [])} services" if odata_services else ""))

        if domain == "calm":
            _jobs[job_id]["status"] = "generating_code"
            if odata_services:
                # Discovery-first: treat entries as CALM API endpoint paths
                custom_apis = {f"api_{i}": path for i, path in enumerate(odata_services)}
                tools_code = _generate_calm_tools(prompt, custom_apis=custom_apis)
                services_used = odata_services
            else:
                tools_code = _generate_calm_tools(prompt)
                services_used = ["calm-projects", "calm-tasks", "calm-monitoring", "calm-analytics"]
            server_code = _CALM_TEMPLATE.replace("__DESCRIPTION__", f"SAP Cloud ALM agent: {prompt}").replace("__AGENT_NAME__", agent_name).replace("__TOOLS__", tools_code)

        elif domain == "sf":
            _jobs[job_id]["status"] = "generating_code"
            if odata_services:
                # Discovery-first: treat entries as SF OData v2 entity paths
                custom_apis = {path.split("/")[-1]: path for path in odata_services}
                tools_code = _generate_sf_tools(prompt, custom_apis=custom_apis)
                services_used = odata_services
            else:
                tools_code = _generate_sf_tools(prompt)
                services_used = ["sf-odata-v2"]
            server_code = _SF_TEMPLATE.replace("__DESCRIPTION__", f"SAP SuccessFactors agent: {prompt}").replace("__AGENT_NAME__", agent_name).replace("__TOOLS__", tools_code)

        else:  # s4
            if backend == "sql":
                # SQL-backed: generate ABAP-SQL tools directly, no OData discovery.
                _jobs[job_id]["status"] = "generating_code"
                # Generate SQL tools, retrying if Claude emits invalid Python
                # (occasional unbalanced bracket / truncation). Validate each attempt.
                server_code = None
                last_err = None
                for attempt in range(1, 4):
                    tools_code = _generate_s4_sql_tools(prompt, agent_name, tables=tables)
                    candidate = (_S4_SQL_TEMPLATE
                                 .replace("__DESCRIPTION__", f"SAP S/4HANA SQL agent: {prompt}")
                                 .replace("__AGENT_NAME__", agent_name)
                                 .replace("__TOOLS__", tools_code))
                    try:
                        compile(candidate, f"{agent_name}_server.py", "exec")
                        server_code = candidate
                        logger.info(f"[{job_id}] SQL codegen valid on attempt {attempt}")
                        break
                    except SyntaxError as se:
                        last_err = f"line {se.lineno}: {se.msg}"
                        logger.warning(f"[{job_id}] SQL codegen attempt {attempt} invalid ({last_err}); retrying")
                if server_code is None:
                    _jobs[job_id]["status"] = "failed"
                    _jobs[job_id]["error"] = f"SQL codegen produced invalid Python after 3 attempts (last: {last_err})"
                    return
                services_used = [f"ABAP SQL (ADT Data Preview): {', '.join(tables)}" if tables
                                 else "ABAP SQL (ADT Data Preview)"]
                # falls through to the shared validate → save → deploy block below
            else:
                # ── OData-backed path (unchanged behavior) ──────────────────
                svcs_with_entities = []

                if odata_services:
                    # Discovery-first: use provided OData services, skip keyword discovery
                    logger.info(f"[{job_id}] Using {len(odata_services)} pre-discovered OData services")

                    # ── VERIFICATION GATE: Check all services are accessible before proceeding ──
                    inactive_services = []
                    for svc_name in odata_services:
                        try:
                            url = f"{SAP_BASE_URL}/sap/opu/odata/sap/{svc_name}/$metadata"
                            with httpx.Client(verify=False, timeout=15) as vc:
                                r = vc.get(url, headers={"Authorization": f"Bearer {token}"})
                                if r.status_code != 200:
                                    inactive_services.append({"service": svc_name, "status": r.status_code})
                                    logger.warning(f"[{job_id}] Service {svc_name} NOT accessible: HTTP {r.status_code}")
                        except Exception as ve:
                            inactive_services.append({"service": svc_name, "status": f"error: {ve}"})

                    if inactive_services:
                        _jobs[job_id]["status"] = "failed"
                        _jobs[job_id]["error"] = (
                            f"Cannot generate agent — {len(inactive_services)} OData service(s) are NOT accessible. "
                            f"Register them in /IWFND/MAINT_SERVICE → Add Service → LOCAL first.\n"
                            f"Inactive: {json.dumps(inactive_services)}"
                        )
                        return
                    logger.info(f"[{job_id}] All {len(odata_services)} OData services verified accessible")
                    # ── END VERIFICATION GATE ──

                    for svc_name in odata_services:
                        path = f"/sap/opu/odata/sap/{svc_name}"
                        entities = _get_entities_for_service(path, token)
                        if entities:
                            svcs_with_entities.append({"service_path": path,
                                                       "title": svc_name, "entities": entities})
                        else:
                            logger.warning(f"[{job_id}] No entities found for pre-discovered service: {svc_name}")
                else:
                    # Original behavior: keyword extraction + discovery
                    stop = {"with","that","this","from","have","will","should","tools","tool",
                            "details","including","information","related","help","about","also",
                            "their","them","these","those","into","like","such","some","other",
                            "more","most","very","just","only","both","each","every"}
                    keywords = list(dict.fromkeys(
                        w.strip(",.;:!?") for w in prompt.lower().split()
                        if len(w.strip(",.;:!?")) > 3 and w.strip(",.;:!?") not in stop))
                    matched = _discover_relevant_services(token, keywords)
                    if not matched:
                        _jobs[job_id]["status"] = "failed"
                        _jobs[job_id]["error"] = f"No S/4 services found for: {prompt}"
                        return
                    for svc in (matched or [])[:5]:
                        path = f"/sap/opu/odata/sap/{svc['Title']}"
                        entities = _get_entities_for_service(path, token)
                        if entities:
                            svcs_with_entities.append({"service_path": path,
                                                       "title": svc["Title"], "entities": entities})

                if not svcs_with_entities:
                    _jobs[job_id]["status"] = "failed"
                    _jobs[job_id]["error"] = "No OData services available. Use the OData Agent to explore data and create CDS views first, or use backend='sql' to query tables directly."
                    return

                _jobs[job_id]["status"] = "generating_code"
                tools_code = _generate_s4_tools(prompt, svcs_with_entities, agent_name)
                server_code = _S4_TEMPLATE.replace("__DESCRIPTION__", f"SAP S/4HANA agent: {prompt}").replace("__AGENT_NAME__", agent_name).replace("__TOOLS__", tools_code)
                services_used = [s["service_path"] for s in svcs_with_entities]

        _jobs[job_id]["services_used"] = services_used

        # Validate generated code syntax
        try:
            compile(server_code, f"{agent_name}_server.py", "exec")
        except SyntaxError as se:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = f"Generated code has syntax error at line {se.lineno}: {se.msg}"
            return

        # Build meta BEFORE saving (was a bug — meta used before defined)
        # PIN mcp<1.20: mcp>=1.20 (e.g. 1.27.2) breaks the AgentCore streamable-HTTP
        # transport — strict client handshakes (Kiro, QuickSuite) fail to connect.
        requirements = "mcp>=1.10.0,<1.20.0\nhttpx\nboto3\n"
        meta = {"agent_name": agent_name, "prompt": prompt, "domain": domain, "services": services_used}

        # Persist generated code to EFS/disk
        save_generated_agent(agent_name, server_code, meta)

        deploy_target = _jobs[job_id].get("deploy_target", "container")

        if deploy_target == "container":
            # ── Container deployment (Fargate + ALB) ──────────────────────
            _jobs[job_id]["status"] = "deploying_container"
            try:
                endpoint = _deploy_to_container(agent_name, server_code, requirements, meta, region)
                _jobs[job_id]["status"] = "completed"
                _jobs[job_id]["endpoint"] = endpoint
                _jobs[job_id]["deploy_target"] = "container"
                _jobs[job_id]["message"] = (
                    f"Container deployed for '{agent_name}' ({domain} domain). "
                    f"Endpoint: {endpoint}/mcp  "
                    f"ALB with Okta auth ready. ~2-3 min to become healthy."
                )
            except Exception as ce:
                _jobs[job_id]["status"] = "failed"
                _jobs[job_id]["error"] = f"Container deployment failed: {ce}"
        else:
            # ── AgentCore deployment (CodeBuild → AgentCore Runtime) ──────
            _jobs[job_id]["status"] = "uploading_to_s3"
            build_id = str(uuid.uuid4())[:8]
            prefix   = f"generated/{build_id}"

            for key, content in [(f"{prefix}/server.py", server_code),
                                  (f"{prefix}/requirements.txt", requirements),
                                  (f"{prefix}/meta.json", json.dumps(meta))]:
                s3.put_object(Bucket=staging_bucket, Key=key, Body=content.encode())

            _jobs[job_id]["status"] = "starting_codebuild"
            build_resp = cb.start_build(
                projectName=codebuild_project,
                buildspecOverride=_BUILDSPEC,   # always use the clean canonical buildspec
                environmentVariablesOverride=[
                    {"name": "BUILD_ID",        "value": build_id,        "type": "PLAINTEXT"},
                    {"name": "STAGING_BUCKET",  "value": staging_bucket,  "type": "PLAINTEXT"},
                ])
            cb_build_id = build_resp["build"]["id"]
            _jobs[job_id]["codebuild_build_id"] = cb_build_id

            # Write bridge + mcp.json — LOCAL-DEV convenience only. Inside the
            # AgentCore container the filesystem is read-only, so this will raise;
            # it must NOT fail the deploy (CodeBuild has already started).
            try:
                _write_agent_bridge_and_mcp_config(agent_name, region)
            except Exception as we:
                logger.warning(f"[{job_id}] Skipped bridge/mcp.json write (non-fatal): {we}")

            _jobs[job_id]["status"] = "deploying"
            _jobs[job_id]["ssm_arn_key"] = f"/sap_generated/{agent_name}/agent_arn"
            _jobs[job_id]["message"] = (
                f"CodeBuild started for '{agent_name}' ({domain} domain). "
                f"~10-15 min to deploy. "
                f"Track: aws codebuild batch-get-builds --ids '{cb_build_id}'"
            )
        logger.info(f"[{job_id}] CodeBuild triggered: {cb_build_id}")

    except Exception as e:
        import traceback
        logger.error(f"[{job_id}] Generation failed: {e}\n{traceback.format_exc()}")
        _jobs[job_id]["status"] = "failed"
        _jobs[job_id]["error"] = str(e)


# ── Main tool (fire-and-forget) ──────────────────────────────────────────────
@mcp.tool()
def generate_and_deploy_mcp_server(ctx: Context, prompt: str, agent_name: str,
                                    deploy_target: str = "container",
                                    odata_services: str = "",
                                    backend: str = "auto",
                                    tables: str = "") -> str:
    """Generate a new focused SAP MCP agent and deploy it.

    SYNCHRONOUS: discovery, code generation, S3 upload, and CodeBuild trigger all
    run inside this call, then it returns the real codebuild_build_id. (A previous
    fire-and-forget thread design did NOT survive AgentCore's container recycling —
    the thread was killed before reaching CodeBuild, so nothing ever deployed.)
    CodeBuild itself then runs in AWS (~10-15 min) independent of this container;
    poll check_generation_status / `aws codebuild batch-get-builds` for completion.

    Deployment targets:
    - 'container' (DEFAULT, recommended) — Builds Docker image, pushes to ECR,
      deploys to ECS Fargate with ALB. Easy to add Okta auth via ALB rules.
      Ready in ~2-3 minutes.
    - 'agentcore' — Uploads to S3, triggers CodeBuild → deploys to Bedrock AgentCore
      Runtime with Okta JWT authorizer. Takes ~10-15 minutes.

    Automatically detects the target domain from the prompt:
    - 'calm' — SAP Cloud ALM / Rise / Cloud ERP (projects, tasks, monitoring, analytics)
    - 'sf'   — SAP SuccessFactors HCM (employees, recruiting, learning, performance)
    - 's4'   — SAP S/4HANA (default)

    Backend (how the generated S/4HANA agent reaches SAP):
    - 'auto' (default) — PREFER OData. Resolves to 'odata' and runs discovery.
      OData is the production-safe, governed path and is always preferred.
    - 'odata' — PREFERRED. Generated tools call OData services (requires activated
      services / CDS views; pass them via odata_services). Discovery runs if none
      provided.
    - 'sql'   — FALLBACK ONLY (explicit opt-in). Generated tools run DIRECT ABAP SQL
      via the ADT Data Preview endpoint (no OData, no CDS, no BASIS activation). Use
      only when no activated OData service exists and a CDS view cannot be created.
      Use 'tables' to hint the primary SAP tables (e.g. "EKKO,EKPO,EKBE,LFA1").
      Only applies to the 's4' domain.

    Discovery-first mode: When odata_services is provided, the generator skips
    internal discovery and uses the pre-discovered, user-reviewed OData services
    directly.

    Args:
        prompt:         Natural language description of what the agent should do.
        agent_name:     Short lowercase underscore identifier for the agent.
        deploy_target:  'container' (Fargate+ALB, default) or 'agentcore' (Bedrock AgentCore).
        odata_services: Optional JSON-encoded list of OData service names (S4), API paths (CALM), or entity paths (SF).
        backend:        's4' data access: 'auto' | 'sql' | 'odata'.
        tables:         Comma-separated SAP tables to hint the SQL backend (e.g. "EKKO,EKPO,EKBE").

    Returns JSON with job_id to track progress via check_generation_status.
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})

    if deploy_target not in ("container", "agentcore"):
        return json.dumps({"error": f"Invalid deploy_target: {deploy_target}. Use 'container' or 'agentcore'."})

    if backend not in ("auto", "sql", "odata"):
        return json.dumps({"error": f"Invalid backend: {backend}. Use 'auto', 'sql', or 'odata'."})

    # Parse optional discovery-first parameters
    parsed_odata = None
    if odata_services and odata_services.strip() and odata_services.strip() != "[]":
        try:
            parsed_odata = json.loads(odata_services)
            if not isinstance(parsed_odata, list):
                return json.dumps({"error": "odata_services must be a JSON-encoded list of strings."})
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON in odata_services: {e}"})

    # Resolve 'auto' backend. OData is PREFERRED (production-safe, governed).
    # 'auto' only uses SQL as an explicit fallback when the caller already knows
    # there are no OData services AND passed none — otherwise it tries OData first.
    resolved_backend = backend
    if backend == "auto":
        resolved_backend = "odata"   # prefer OData; discovery will run if none supplied

    parsed_tables = [t.strip().upper() for t in tables.split(",") if t.strip()] if tables else []

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "status": "accepted",
        "agent_name": agent_name,
        "prompt": prompt,
        "domain": None,
        "deploy_target": deploy_target,
        "backend": resolved_backend,
        "tables": parsed_tables,
        "services_used": [],
        "codebuild_build_id": None,
        "endpoint": None,
        "error": None,
        "odata_services_provided": parsed_odata or [],
    }

    # SYNCHRONOUS deploy (matches the proven smart-agent design). Fire-and-forget
    # background threads DO NOT survive AgentCore's frequent container recycling —
    # the thread was being killed before reaching CodeBuild. Running inline means
    # the S3 upload + start_build complete within this tool call; CodeBuild then
    # runs independently in AWS (~10-15 min), unaffected by container lifecycle.
    logger.info(f"[{job_id}] Job accepted for '{agent_name}' (target={deploy_target}, backend={resolved_backend}) — running SYNCHRONOUSLY")
    _run_generation_job(job_id, token, prompt, agent_name,
                        odata_services=parsed_odata,
                        backend=resolved_backend,
                        tables=parsed_tables)

    job = _jobs.get(job_id, {})
    return json.dumps({
        "status": job.get("status"),
        "job_id": job_id,
        "agent_name": agent_name,
        "deploy_target": deploy_target,
        "backend": resolved_backend,
        "domain": job.get("domain"),
        "services_used": job.get("services_used"),
        "codebuild_build_id": job.get("codebuild_build_id"),
        "endpoint": job.get("endpoint"),
        "error": job.get("error"),
        "message": job.get("message",
                           f"Generation finished with status '{job.get('status')}'."),
    }, indent=2)


@mcp.tool()
def check_generation_status(ctx: Context, job_id: str) -> str:
    """Check the status of a fire-and-forget MCP server generation job.

    Args:
        job_id: The job ID returned by generate_and_deploy_mcp_server.

    Returns JSON with current status, progress details, and CodeBuild build ID once available.
    Status values: accepted, discovering_services, generating_code, uploading_to_s3,
                   starting_codebuild, deploying, failed.
    """
    if job_id not in _jobs:
        return json.dumps({"error": f"Unknown job_id: {job_id}"})

    job = _jobs[job_id].copy()

    # If deploying via AgentCore, try to get CodeBuild status
    if job.get("codebuild_build_id") and job["status"] == "deploying":
        try:
            cb = boto3.client("codebuild", region_name=boto3.session.Session().region_name)
            builds = cb.batch_get_builds(ids=[job["codebuild_build_id"]])
            if builds["builds"]:
                cb_status = builds["builds"][0]["buildStatus"]
                job["codebuild_status"] = cb_status
                if cb_status == "SUCCEEDED":
                    job["status"] = "completed"
                elif cb_status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
                    job["status"] = "codebuild_failed"
                    job["error"] = f"CodeBuild {cb_status}"
        except Exception as e:
            job["codebuild_check_error"] = str(e)

    # If container deployment completed, check ECS service health
    if job.get("deploy_target") == "container" and job["status"] == "completed" and job.get("endpoint"):
        try:
            ecs = boto3.client("ecs", region_name=boto3.session.Session().region_name)
            svc_name = f"sap-{job['agent_name'].replace('_', '-')}"
            services = ecs.describe_services(cluster="sap-ai-factory", services=[svc_name])
            if services["services"]:
                svc = services["services"][0]
                job["ecs_status"] = svc["status"]
                job["running_count"] = svc.get("runningCount", 0)
                job["desired_count"] = svc.get("desiredCount", 1)
        except Exception:
            pass

    return json.dumps(job, indent=2)




@mcp.tool()
def list_generated_mcp_servers(ctx: Context) -> str:
    """List all previously generated MCP agents (persisted on EFS).
    Shows agent name, domain, prompt, and when it was generated."""
    agents = list_generated_agents()
    if not agents:
        return json.dumps({"message": "No agents generated yet."})
    return json.dumps(agents, indent=2)

# ── Strands wrapper ───────────────────────────────────────────────────────────
def create_generator_strands_agent() -> Agent:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8105/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=32000),
            tools=client.tools,
            system_prompt=(
                "You are the Generator Agent within the AI Factory MCP Server.\n\n"
                "Your role is to create and deploy new focused SAP MCP agents. "
                "When a user asks to build, create, or deploy a new agent for a specific SAP domain, "
                "use generate_and_deploy_mcp_server. It returns a job_id immediately (fire-and-forget). "
                "Then use check_generation_status(job_id) to poll for progress.\n\n"
                "IMPORTANT — Before deploying, always ask the user:\n"
                "  'Where would you like to deploy this agent?'\n"
                "  1. **Container** (default, recommended) — Fargate + ALB. Fast (~2-3 min), "
                "easy to add Okta auth via ALB rules, works anywhere.\n"
                "  2. **AgentCore** — Bedrock AgentCore Runtime via CodeBuild. Takes ~10-15 min.\n\n"
                "If the user doesn't specify, default to 'container'.\n\n"
                "Backend selection for S/4HANA agents (how the agent reaches SAP):\n"
                "  - **OData is PREFERRED and the default** — production-safe and governed.\n"
                "    Use backend='odata' (or 'auto'). If specific activated services are known,\n"
                "    pass them via odata_services.\n"
                "  - **SQL is a FALLBACK only.** Use backend='sql' ONLY when the user confirms\n"
                "    there is no activated OData service and creating a CDS view is not an option.\n"
                "    SQL queries SAP tables directly via ADT (pass primary tables via 'tables').\n"
                "  Before generating an S/4HANA agent, if no OData service is available, ASK the\n"
                "  user: 'No activated OData service was found. Create a CDS view/OData service\n"
                "  (preferred), or fall back to direct SQL on tables?' Do NOT silently choose SQL.\n\n"
                "Domain detection is automatic:\n"
                "- 'calm' — SAP Cloud ALM (Rise/Cloud ERP: projects, monitoring, analytics)\n"
                "- 'sf'   — SAP SuccessFactors (HR: employees, recruiting, learning, performance)\n"
                "- 's4'   — SAP S/4HANA OData (default: discovers services from the catalog)\n\n"
                "Before deploying, always confirm:\n"
                "1. The agent_name (must be lowercase with underscores, e.g. calm_monitoring_agent)\n"
                "2. The domain detected from the prompt\n"
                "3. The deploy target (container or agentcore)\n"
                "4. What the agent will do\n\n"
                "Container deployment creates: ECR repo → Docker image → ECS Fargate service → ALB target group.\n"
                "AgentCore deployment creates: S3 upload → CodeBuild → AgentCore Runtime endpoint.\n"
                "The agent ARN (agentcore) or ALB endpoint (container) is stored once complete."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP Generator Agent starting on port 8105 ===")
    mcp.run(transport="streamable-http")
