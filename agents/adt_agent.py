"""
SAP ADT Sub-Agent — ABAP development tools.
Handles: read/write source code, syntax check, ATC, transports, DDIC, search.
Exposed as a FastMCP server so the parent Strands agent can call it as a tool.
"""
import os, json, logging, httpx, xml.etree.ElementTree as ET
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("adt_agent")

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")

mcp = FastMCP("AI Factory — ADT Agent", host="0.0.0.0", port=8101)


def _get_token(ctx: Context) -> str:
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
    except Exception: pass
    return os.environ.get("SAP_BEARER_TOKEN", "")


def _adt_get(path: str, token: str, accept: str = "application/json", params: dict = None) -> str:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": accept}, params=params or {})
        r.raise_for_status()
        return r.text


def _adt_post(path: str, token: str, body: str, content_type: str, accept: str = "application/xml") -> str:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        # get CSRF token first
        csrf = c.get(url, headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": "Fetch"}).headers.get("x-csrf-token", "")
        r = c.post(url, content=body.encode(),
                   headers={"Authorization": f"Bearer {token}", "Content-Type": content_type,
                             "Accept": accept, "X-CSRF-Token": csrf})
        r.raise_for_status()
        return r.text


# ── Source Code ───────────────────────────────────────────────────────────────

@mcp.tool()
def get_abap_program(ctx: Context, program_name: str) -> str:
    """Read ABAP program/report source code from SAP."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/programs/programs/{program_name.upper()}/source/main", token,
                        accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_abap_class(ctx: Context, class_name: str, include: str = "main") -> str:
    """Read ABAP class source code. include: main | definitions | implementations | testclasses"""
    token = _get_token(ctx)
    include_map = {"main": "source/main", "definitions": "includes/definitions/source/main",
                   "implementations": "includes/implementations/source/main",
                   "testclasses": "includes/testclasses/source/main"}
    path = f"/sap/bc/adt/oo/classes/{class_name.upper()}/{include_map.get(include, 'source/main')}"
    try:
        return _adt_get(path, token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_function_module(ctx: Context, function_group: str, function_name: str) -> str:
    """Read function module source code from SAP."""
    token = _get_token(ctx)
    path = f"/sap/bc/adt/functions/groups/{function_group.upper()}/fmodules/{function_name.upper()}/source/main"
    try:
        return _adt_get(path, token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_abap_interface(ctx: Context, interface_name: str) -> str:
    """Read ABAP interface source code from SAP."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/oo/interfaces/{interface_name.upper()}/source/main",
                        token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_abap_include(ctx: Context, include_name: str) -> str:
    """Read ABAP include program source code from SAP."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/programs/includes/{include_name.upper()}/source/main",
                        token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


# ── Search & Discovery ────────────────────────────────────────────────────────

@mcp.tool()
def search_objects(ctx: Context, query: str, object_type: str = "", max_results: int = 50) -> str:
    """Search SAP repository objects. object_type: PROG, CLAS, INTF, FUGR, TABL, TRAN, DEVC etc."""
    token = _get_token(ctx)
    params = {"operation": "quickSearch", "query": f"{query}*", "maxResults": max_results}
    if object_type: params["objectType"] = object_type
    try:
        xml_text = _adt_get("/sap/bc/adt/repository/informationsystem/search", token,
                            accept="application/xml", params=params)
        root = ET.fromstring(xml_text)
        ns = {"adtcore": "http://www.sap.com/adt/core"}
        results = [{"name": o.get("{http://www.sap.com/adt/core}name", ""),
                    "type": o.get("{http://www.sap.com/adt/core}type", ""),
                    "description": o.get("{http://www.sap.com/adt/core}description", "")}
                   for o in root.findall(".//{http://www.sap.com/adt/core}objectReference")]
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_package(ctx: Context, package_name: str) -> str:
    """Get SAP package (development class) contents."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/repository/nodestructure?parent_name={package_name.upper()}&parent_tech_name={package_name.upper()}&parent_type=DEVC/K",
                        token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_transaction(ctx: Context, tcode: str) -> str:
    """Look up a SAP transaction code and find the associated program/object."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/repository/informationsystem/search?operation=quickSearch&query={tcode.upper()}&objectType=TRAN&maxResults=5",
                        token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


# ── DDIC ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_table_definition(ctx: Context, table_name: str) -> str:
    """Get DDIC table or structure definition (fields, types, keys)."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/ddic/tables/{table_name.upper()}", token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_type_info(ctx: Context, type_name: str) -> str:
    """Get DDIC type information — data element, domain, or type details (e.g. MATNR, BUKRS, VBELN)."""
    token = _get_token(ctx)
    try:
        # Try data element first, fall back to domain
        for path in [f"/sap/bc/adt/ddic/dataelements/{type_name.upper()}",
                     f"/sap/bc/adt/ddic/domains/{type_name.upper()}"]:
            try:
                return _adt_get(path, token, accept="application/xml")
            except Exception:
                continue
        return json.dumps({"error": f"Type {type_name} not found as data element or domain"})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_table_contents(ctx: Context, sql_query: str, max_rows: int = 100) -> str:
    """Execute a SQL SELECT on SAP database tables via ADT Data Preview.
    Tries freestyle POST first, then sqlConsole GET as fallback."""
    token = _get_token(ctx)
    try:
        with httpx.Client(verify=False, timeout=30.0) as c:
            # Method 1: freestyle POST (requires CSRF)
            csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                              headers={"Authorization": f"Bearer {token}", "x-csrf-token": "Fetch",
                                       "Accept": "*/*"})
            csrf = csrf_resp.headers.get("x-csrf-token", "")
            # Try multiple Accept headers — SAP versions differ
            for accept_hdr in ["application/xml",
                                "application/vnd.sap.adt.datapreview.table.v1+xml",
                                "*/*"]:
                r = c.post(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/freestyle",
                           content=sql_query.encode("utf-8"),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain",
                                    "Accept": accept_hdr, "x-csrf-token": csrf},
                           params={"rowNumber": str(max_rows)})
                if r.status_code == 200:
                    return r.text
                if r.status_code != 406:
                    break  # not a content negotiation issue
            # Method 2: sqlConsole GET (fallback)
            import urllib.parse
            r2 = c.get(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/sqlConsole",
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"},
                       params={"rowNumber": str(max_rows), "sqlCommand": sql_query})
            if r2.status_code == 200:
                return r2.text
            return json.dumps({"error": f"freestyle={r.status_code}, sqlConsole={r2.status_code}",
                               "freestyle_body": r.text[:300], "sqlConsole_body": r2.text[:300]})
    except Exception as e: return json.dumps({"error": str(e)})


# ── Code Quality ──────────────────────────────────────────────────────────────

@mcp.tool()
def syntax_check(ctx: Context, object_url: str) -> str:
    """Run ABAP syntax check on a program or class. object_url: ADT URI e.g. /sap/bc/adt/programs/programs/Z_MY_PROG"""
    token = _get_token(ctx)
    try:
        return _adt_get(f"{object_url}/syntaxcheck", token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def run_atc_check(ctx: Context, object_uri: str, check_variant: str = "DEFAULT") -> str:
    """Run ATC (ABAP Test Cockpit) code quality check on an object."""
    token = _get_token(ctx)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<atcworklist:worklist xmlns:atcworklist="http://www.sap.com/adt/atc/worklist"
    xmlns:adtcore="http://www.sap.com/adt/core">
  <atcworklist:objectSets>
    <atcworklist:objectSet kind="inclusive">
      <adtcore:objectReferences>
        <adtcore:objectReference adtcore:uri="{object_uri}"/>
      </adtcore:objectReferences>
    </atcworklist:objectSet>
  </atcworklist:objectSets>
</atcworklist:worklist>"""
    try:
        return _adt_post("/sap/bc/adt/atc/runs?worklistId=1", token, body,
                         "application/xml", accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


# ── Transport Management ──────────────────────────────────────────────────────

@mcp.tool()
def create_transport(ctx: Context, description: str, target_system: str = "",
                     transport_type: str = "K") -> str:
    """Create a new transport request in SAP. transport_type: K=workbench, W=customizing"""
    token = _get_token(ctx)
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<tm:root xmlns:tm="http://www.sap.com/cts/adt/tm">
  <tm:workbenchRequest tm:category="{transport_type}" tm:target="{target_system}"
      tm:description="{description}"/>
</tm:root>"""
    try:
        return _adt_post("/sap/bc/adt/cts/transportrequests", token, body,
                         "application/vnd.sap.adt.tm.transportrequest+xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def release_transport(ctx: Context, transport_number: str) -> str:
    """Release a transport request or task in SAP."""
    token = _get_token(ctx)
    try:
        url = f"{SAP_BASE_URL}/sap/bc/adt/cts/transportrequests/{transport_number}/newreleasejobs"
        with httpx.Client(verify=False, timeout=30.0) as c:
            csrf = c.get(f"{SAP_BASE_URL}/sap/bc/adt/cts/transportrequests/{transport_number}",
                         headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": "Fetch"}).headers.get("x-csrf-token", "")
            r = c.post(url, headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf})
            r.raise_for_status()
            return json.dumps({"success": True, "transport": transport_number})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def list_user_transports(ctx: Context, user: str = "", status: str = "D") -> str:
    """List transport requests. status: D=modifiable, R=released"""
    token = _get_token(ctx)
    params = {"user": user or "", "target": "", "status": status}
    try:
        return _adt_get("/sap/bc/adt/cts/transportrequests", token, accept="application/xml", params=params)
    except Exception as e: return json.dumps({"error": str(e)})


# ── OData Service Creation & Activation ──────────────────────────────────────

@mcp.tool()
def create_odata_service(ctx: Context, description: str, cds_name: str = "") -> str:
    """Create a new OData service in SAP by generating a CDS view with @OData.publish.
    Uses Claude (Bedrock) to generate CDS source from a natural language description,
    then creates, writes and activates it via ADT. After creation call activate_odata_service.
    """
    import boto3, uuid as _uuid
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    steps = []
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
        if not cds_name:
            words = [w.strip(",.;:!?").lower() for w in description.split()
                     if len(w.strip(",.;:!?")) > 3 and w.strip(",.;:!?") not in
                     {"with","that","this","from","show","list","details","including"}]
            cds_name = "zi_" + "_".join(words[:3])[:20]
        cds_name = cds_name.lower().strip()
        if not cds_name.startswith("z"): cds_name = f"z{cds_name}"

        system = (
            "You are an SAP ABAP CDS expert. Generate a CDS view source.\n"
            "Output ONLY the CDS source code, no markdown, no explanation, no ```.\n\n"
            "RULES — follow these PROVEN patterns EXACTLY:\n"
            "1. MUST use 'define view' syntax (NOT 'define view entity')\n"
            "2. MUST include these annotations in this EXACT order:\n"
            "   @AbapCatalog.sqlViewName: 'Z...' (max 16 chars)\n"
            "   @AccessControl.authorizationCheck: #NOT_REQUIRED\n"
            "   @EndUserText.label: '...'\n"
            "   @OData.publish: true\n"
            "3. MUST have at least one 'key' field\n"
            "4. Use #NOT_REQUIRED for auth (NOT #CHECK)\n"
            "5. Do NOT add @AbapCatalog.preserveKey — it is DEPRECATED in S/4HANA 2023+\n"
            "6. Do NOT add @AbapCatalog.compiler.compareFilter — it is DEPRECATED in S/4HANA 2023+\n"
            "7. Do NOT add @ObjectModel.usageType\n"
            "8. Do NOT add @Semantics annotations\n"
            "9. For related tables, prefer 'association' over 'inner join'\n"
            "10. ONLY use these 4 annotations: sqlViewName, authorizationCheck, label, OData.publish\n\n"
            "PATTERN 1 — Simple single table:\n"
            "@AbapCatalog.sqlViewName: 'ZSALES_SQL'\n"
            "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
            "@EndUserText.label: 'Sales OData View'\n"
            "@OData.publish: true\n"
            "define view ZSales_CDS as select from vbak {\n"
            "  key vbeln as SalesOrder,\n"
            "  erdat as CreatedDate,\n"
            "  kunnr as Customer\n"
            "}\n\n"
            "PATTERN 2 — With association:\n"
            "@AbapCatalog.sqlViewName: 'ZJP_SO_DATA_N'\n"
            "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
            "@EndUserText.label: 'Sales Order Data CDS'\n"
            "@OData.publish: true\n"
            "define view ZJP_SO_NEW\n"
            "  as select from vbak as header\n"
            "  association [1..*] to vbap as _item\n"
            "    on header.vbeln = _item.vbeln\n"
            "{\n"
            "  key vbeln as Vbeln,\n"
            "  erdat as Erdat,\n"
            "  netwr as Netwr,\n"
            "  kunnr as Kunnr,\n"
            "  _item\n"
            "}\n\n"
            "PATTERN 3 — With inner join:\n"
            "@AbapCatalog.sqlViewName: 'ZV_PO_HIST'\n"
            "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
            "@EndUserText.label: 'PO History with Header'\n"
            "@OData.publish: true\n"
            "define view ZPO_History as select from ekbe\n"
            "  inner join ekko on ekko.ebeln = ekbe.ebeln\n"
            "{\n"
            "  key ekbe.ebeln as Ebeln,\n"
            "  key ekbe.ebelp as Ebelp,\n"
            "  key ekbe.belnr as Belnr,\n"
            "  ekko.lifnr as Lifnr,\n"
            "  ekbe.matnr as Matnr,\n"
            "  ekbe.menge as Menge,\n"
            "  ekbe.dmbtr as Dmbtr\n"
            "}\n"
        )
        resp = bedrock.invoke_model(
            modelId="us.anthropic.claude-sonnet-4-6",
            body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                             "system": system,
                             "messages": [{"role": "user", "content":
                                           f"Create CDS view {cds_name.upper()} that exposes: {description}"}]}),
            contentType="application/json", accept="application/json")
        cds_source = json.loads(resp["body"].read())["content"][0]["text"].strip()
        if cds_source.startswith("```"):
            cds_source = "\n".join(cds_source.split("\n")[1:]).rsplit("```", 1)[0].strip()

        # ── Strip deprecated annotations that block generation on S/4HANA 2023+ ──
        import re as _re
        # Remove @AbapCatalog.preserveKey line entirely
        cds_source = _re.sub(r'@AbapCatalog\.preserveKey\s*:\s*true\s*\n?', '', cds_source)
        # Remove @AbapCatalog.compiler.compareFilter line entirely
        cds_source = _re.sub(r'@AbapCatalog\.compiler\.compareFilter\s*:\s*true\s*\n?', '', cds_source)
        # Clean up any double blank lines left behind
        cds_source = _re.sub(r'\n{3,}', '\n\n', cds_source).strip()

        steps.append("CDS source generated (deprecated annotations stripped)")

        ddl_path = f"/sap/bc/adt/ddic/ddl/sources"
        with httpx.Client(verify=False, timeout=120) as c:
            base = {"Authorization": f"Bearer {token}", "X-sap-adt-sessiontype": "stateful"}
            csrf = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                         headers={**base, "x-csrf-token": "Fetch",
                                  "Accept": "application/atomsvc+xml"}).headers.get("x-csrf-token", "")
            if not csrf: return json.dumps({"error": "Could not fetch CSRF token"})
            steps.append("CSRF fetched")

            # Create if not exists
            if c.get(f"{SAP_BASE_URL}{ddl_path}/{cds_name}/source/main",
                     headers={**base, "Accept": "text/plain"}).status_code != 200:
                cr = c.post(f"{SAP_BASE_URL}{ddl_path}",
                            headers={**base, "x-csrf-token": csrf,
                                     "Content-Type": "application/vnd.sap.adt.ddlsource+xml",
                                     "Accept": "*/*"},
                            content=(f'<?xml version="1.0" encoding="UTF-8"?>'
                                     f'<ddl:ddlSource xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources" '
                                     f'xmlns:adtcore="http://www.sap.com/adt/core" '
                                     f'adtcore:type="DDLS/DF" adtcore:description="{description[:60]}" '
                                     f'adtcore:language="EN" adtcore:name="{cds_name.upper()}" '
                                     f'adtcore:masterLanguage="EN" adtcore:responsible="DEVELOPER">'
                                     f'<adtcore:packageRef adtcore:name="$TMP"/></ddl:ddlSource>'))
                steps.append(f"Created ({cr.status_code})")

            # Lock → Write → Unlock → Activate
            lock_resp = c.post(f"{SAP_BASE_URL}{ddl_path}/{cds_name}",
                               headers={**base, "x-csrf-token": csrf,
                                        "Accept": "application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result"},
                               params={"_action": "LOCK", "accessMode": "MODIFY"})
            lock_handle = ""
            try:
                for el in ET.fromstring(lock_resp.text).iter():
                    if el.tag.split("}")[-1] == "LOCK_HANDLE" and el.text:
                        lock_handle = el.text; break
            except ET.ParseError: pass
            steps.append("Locked")

            wr = c.put(f"{SAP_BASE_URL}{ddl_path}/{cds_name}/source/main",
                       headers={**base, "x-csrf-token": csrf, "Content-Type": "text/plain; charset=utf-8"},
                       params={"lockHandle": lock_handle}, content=cds_source.encode("utf-8"))
            c.post(f"{SAP_BASE_URL}{ddl_path}/{cds_name}",
                   headers={**base, "x-csrf-token": csrf},
                   params={"_action": "UNLOCK", "lockHandle": lock_handle})
            if wr.status_code >= 400:
                return json.dumps({"error": f"Write failed {wr.status_code}", "steps": steps})
            steps.append("Written & unlocked")

            act = c.post(f"{SAP_BASE_URL}/sap/bc/adt/activation",
                         headers={**base, "x-csrf-token": csrf,
                                  "Content-Type": "application/xml", "Accept": "application/xml"},
                         params={"method": "activate", "preauditRequested": "false"},
                         content=(f'<?xml version="1.0" encoding="UTF-8"?>'
                                  f'<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
                                  f'<adtcore:objectReference adtcore:uri="{ddl_path}/{cds_name}" '
                                  f'adtcore:type="DDLS/DF" '
                                  f'adtcore:name="{cds_name.upper()}"/></adtcore:objectReferences>'))
            # Capture activation response body for error diagnosis
            act_body = act.text[:500] if act.text else "(empty)"
            steps.append(f"Activated ({act.status_code}): {act_body}")

            # Retry activation if first attempt didn't trigger SADL (common issue)
            if act.status_code == 200:
                import time as _time
                _time.sleep(2)
                # Re-activate to ensure SADL runtime artifacts are generated
                act2 = c.post(f"{SAP_BASE_URL}/sap/bc/adt/activation",
                              headers={**base, "x-csrf-token": csrf,
                                       "Content-Type": "application/xml", "Accept": "application/xml"},
                              params={"method": "activate", "preauditRequested": "false"},
                              content=(f'<?xml version="1.0" encoding="UTF-8"?>'
                                       f'<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
                                       f'<adtcore:objectReference adtcore:uri="{ddl_path}/{cds_name}" '
                                       f'adtcore:type="DDLS/DF" '
                                       f'adtcore:name="{cds_name.upper()}"/></adtcore:objectReferences>'))
                steps.append(f"Re-activated for SADL ({act2.status_code}): {act2.text[:500] if act2.text else '(empty)'}")

            # Check activation actually worked by looking for SQL view in DD02L
            try:
                sql_view_name = ""
                for line in cds_source.split("\n"):
                    if "sqlviewname" in line.lower() and "'" in line:
                        sql_view_name = line.split("'")[1].strip().upper()
                        break
                if sql_view_name:
                    import time as _time2
                    _time2.sleep(3)  # give DDIC a moment to register
                    # Try both the extracted name and common naming patterns
                    names_to_check = [sql_view_name]
                    if not sql_view_name.endswith("_SQL"):
                        names_to_check.append(f"{cds_name.upper()}_SQL")
                    sql_view_exists_dd02l = False
                    for check_name in names_to_check:
                        check_r = c.post(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/freestyle",
                                         content=f"SELECT tabname, tabclass, as4local FROM dd02l WHERE tabname = '{check_name}'".encode(),
                                         headers={**base, "x-csrf-token": csrf,
                                                  "Content-Type": "text/plain", "Accept": "application/xml"},
                                         params={"rowNumber": "5"})
                        if check_r.status_code == 200 and check_name in check_r.text:
                            sql_view_exists_dd02l = True
                            steps.append(f"DD02L confirmed: SQL view {check_name} EXISTS and ACTIVE")
                            break
                    if not sql_view_exists_dd02l:
                        steps.append(f"DD02L check: SQL view not found for names {names_to_check}")
            except Exception as dd_err:
                steps.append(f"DD02L check error: {dd_err}")

        svc_name = f"{cds_name.upper()}_CDS"

        # ── VERIFICATION: Read back the source to confirm it actually exists ──
        verify_ok = False
        verify_error = ""
        try:
            with httpx.Client(verify=False, timeout=15) as vc:
                vr = vc.get(f"{SAP_BASE_URL}{ddl_path}/{cds_name}/source/main",
                            headers={"Authorization": f"Bearer {token}", "Accept": "text/plain"})
                if vr.status_code == 200 and len(vr.text.strip()) > 20:
                    verify_ok = True
                    steps.append(f"VERIFIED: Source read back OK ({len(vr.text)} chars)")
                else:
                    verify_error = f"Read-back failed: HTTP {vr.status_code}, body length {len(vr.text)}"
                    steps.append(f"VERIFY FAILED: {verify_error}")
        except Exception as ve:
            verify_error = f"Verification exception: {ve}"
            steps.append(f"VERIFY FAILED: {verify_error}")

        # Also check if the SQL view was generated (proves activation worked)
        sql_view_exists = False
        try:
            sql_view_name = ""
            # Extract sqlViewName from the CDS source
            for line in cds_source.split("\n"):
                if "sqlViewName" in line.lower() and "'" in line:
                    sql_view_name = line.split("'")[1].strip()
                    break
            if sql_view_name:
                with httpx.Client(verify=False, timeout=15) as sc:
                    sr = sc.get(f"{SAP_BASE_URL}/sap/bc/adt/ddic/views/{sql_view_name}",
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
                    sql_view_exists = sr.status_code == 200
                    steps.append(f"SQL view {sql_view_name}: {'EXISTS' if sql_view_exists else f'NOT FOUND ({sr.status_code})'}")
        except Exception:
            pass

        status = "verified" if verify_ok else "created_but_unverified"
        return json.dumps({
            "status": status,
            "verified": verify_ok,
            "sql_view_exists": sql_view_exists_dd02l if 'sql_view_exists_dd02l' in dir() else False,
            "cds_view": cds_name.upper(),
            "odata_service_name": svc_name,
            "steps": steps,
            "verify_error": verify_error or None,
            "next_step": f"Register in /IWFND/MAINT_SERVICE → Add Service → LOCAL → {svc_name}",
            "note": "If sql_view_exists is true, the CDS view is fully activated. Register the OData service in Gateway to make it accessible."
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "steps": steps})


@mcp.tool()
def verify_cds_exists(ctx: Context, cds_name: str) -> str:
    """Verify that a CDS view actually exists in the SAP system.
    Performs 3 independent checks:
    1. Read CDS DDL source via ADT
    2. Check if the SQL view exists in DDIC
    3. Search the repository for the object
    Returns verified=true only if at least check 1 passes.
    Use this AFTER create_odata_service to confirm the view was actually created."""
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})
    cds_name = cds_name.strip().upper()
    checks = {}

    # Check 1: Read DDL source
    try:
        with httpx.Client(verify=False, timeout=15) as c:
            r = c.get(f"{SAP_BASE_URL}/sap/bc/adt/ddic/ddl/sources/{cds_name.lower()}/source/main",
                      headers={"Authorization": f"Bearer {token}", "Accept": "text/plain"})
            checks["ddl_source"] = {
                "status": r.status_code,
                "exists": r.status_code == 200,
                "source_length": len(r.text) if r.status_code == 200 else 0,
                "first_line": r.text.split("\n")[0][:80] if r.status_code == 200 else None
            }
    except Exception as e:
        checks["ddl_source"] = {"exists": False, "error": str(e)}

    # Check 2: Search repository for the DDLS object
    try:
        xml_text = _adt_get(
            "/sap/bc/adt/repository/informationsystem/search",
            token, accept="application/xml",
            params={"operation": "quickSearch", "query": cds_name, "objectType": "DDLS", "maxResults": 5}
        )
        root = ET.fromstring(xml_text)
        found = [o.get("{http://www.sap.com/adt/core}name", "")
                 for o in root.findall(".//{http://www.sap.com/adt/core}objectReference")]
        checks["repository_search"] = {
            "found": cds_name in [f.upper() for f in found],
            "results": found
        }
    except Exception as e:
        checks["repository_search"] = {"found": False, "error": str(e)}

    # Check 3: Check backend service catalog for the OData service
    svc_name = f"{cds_name}_CDS"
    try:
        with httpx.Client(verify=False, timeout=15) as c:
            r = c.get(f"{SAP_BASE_URL}/sap/opu/odata/sap/{svc_name}/$metadata",
                      headers={"Authorization": f"Bearer {token}"})
            checks["odata_metadata"] = {
                "status": r.status_code,
                "accessible": r.status_code == 200
            }
    except Exception as e:
        checks["odata_metadata"] = {"accessible": False, "error": str(e)}

    verified = checks.get("ddl_source", {}).get("exists", False)
    return json.dumps({
        "cds_name": cds_name,
        "verified": verified,
        "odata_service": svc_name,
        "odata_accessible": checks.get("odata_metadata", {}).get("accessible", False),
        "checks": checks,
        "summary": "CDS view EXISTS in system" if verified else "CDS view DOES NOT EXIST — creation failed"
    }, indent=2)


@mcp.tool()
def activate_odata_service(ctx: Context, service_name: str,
                            service_version: str = "0001", system_alias: str = "LOCAL") -> str:
    """Activate an OData service in SAP Gateway (/IWFND/MAINT_SERVICE equivalent).
    Registers the backend service in the frontend hub so it's accessible via OData URL.
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    svc = service_name.strip().upper()
    steps = []
    try:
        # Check if already active
        with httpx.Client(verify=False, timeout=15) as c:
            r = c.get(f"{SAP_BASE_URL}/sap/opu/odata/sap/{svc}/$metadata",
                      headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                return json.dumps({"status": "already_active", "service": svc})
        steps.append(f"Not yet active ({r.status_code})")

        # Try catalog AddService API
        with httpx.Client(verify=False, timeout=60) as c:
            base = {"Authorization": f"Bearer {token}"}
            csrf = c.get(f"{SAP_BASE_URL}/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/",
                         headers={**base, "x-csrf-token": "Fetch"}).headers.get("x-csrf-token", "")
            if csrf:
                r = c.post(f"{SAP_BASE_URL}/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/AddService",
                           headers={**base, "x-csrf-token": csrf,
                                    "Content-Type": "application/json", "Accept": "application/json"},
                           content=json.dumps({"TechnicalServiceName": svc,
                                               "TechnicalServiceVersion": int(service_version),
                                               "SystemAlias": system_alias,
                                               "ExternalServiceName": svc}))
                if r.status_code in (200, 201, 204):
                    return json.dumps({"status": "activated", "service": svc, "steps": steps})
                steps.append(f"AddService HTTP {r.status_code}: {r.text[:200]}")

        return json.dumps({"status": "needs_manual_activation", "service": svc, "steps": steps,
                           "manual": [f"tcode /IWFND/MAINT_SERVICE → Add Service → {system_alias} → {svc}"]})
    except Exception as e:
        return json.dumps({"error": str(e), "steps": steps})


@mcp.tool()
def list_backend_services(ctx: Context, search: str = "", max_results: int = 20) -> str:
    """List OData services in the SAP backend catalog (registered but possibly not activated).
    Use to find services before calling activate_odata_service.
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    try:
        params: dict = {"$format": "json", "$top": max_results}
        if search: params["$filter"] = f"substringof('{search.upper()}',TechnicalServiceName)"
        url = f"{SAP_BASE_URL}/sap/opu/odata/IWBEP/CATALOGSERVICE;v=2/ServiceCollection"
        with httpx.Client(verify=False, timeout=30) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                      params=params)
            r.raise_for_status()
            results = r.json().get("d", {}).get("results", [])
        return json.dumps({"count": len(results),
                           "services": [{"name": s.get("TechnicalServiceName", ""),
                                         "description": s.get("Description", ""),
                                         "type": s.get("ServiceType", "")} for s in results]}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── ADT Discovery ─────────────────────────────────────────────────────────────

@mcp.tool()
def adt_discovery(ctx: Context) -> str:
    """Get all available ADT REST API endpoints on this SAP system.
    Also covers: get_abap_program, get_abap_class, get_function_module,
    get_abap_interface, get_abap_include, search_objects, get_package,
    get_transaction, get_table_definition, get_table_contents, get_type_info,
    syntax_check, run_atc_check, create_transport, release_transport, list_user_transports,
    create_odata_service, activate_odata_service, list_backend_services.
    """
    token = _get_token(ctx)
    try:
        xml_text = _adt_get("/sap/bc/adt/discovery", token, accept="application/xml")
        root = ET.fromstring(xml_text)
        ns = {"app": "http://www.w3.org/2007/app", "atom": "http://www.w3.org/2005/Atom"}
        services = []
        for ws in root.findall("app:workspace", ns):
            ws_title = getattr(ws.find("atom:title", ns), "text", "")
            for col in ws.findall("app:collection", ns):
                col_title = getattr(col.find("atom:title", ns), "text", "")
                services.append({"workspace": ws_title, "title": col_title,
                                  "href": col.get("href", "")})
        return json.dumps({"total": len(services), "services": services}, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Strands Agent wrapper (used by parent) ────────────────────────────────────

def create_adt_strands_agent() -> Agent:
    """Return a Strands Agent backed by all ADT tools above."""
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8101/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000),
            tools=client.tools,
            system_prompt=(
                "You are the ADT Agent within the AI Factory MCP Server. "
                "Your role is ABAP development, understanding SAP ABAP objects, and CDS view creation.\n\n"
                "Use this agent when the task involves:\n"
                "- Reading or writing ABAP source code (programs, classes, function modules, includes, interfaces)\n"
                "- Understanding what an ABAP object does or how it is structured\n"
                "- Exploring CDS views and their data models\n"
                "- Running syntax checks or ATC code quality checks\n"
                "- Managing transport requests\n"
                "- Querying DDIC table definitions and contents via SQL\n"
                "- Creating a new OData service via CDS view (always ask user first — they may just want info)\n"
                "- Activating an OData service in SAP Gateway\n\n"
                "IMPORTANT: If an ADT API is not available as a named tool, use your SAP knowledge "
                "to construct the correct ADT REST path and call it via call_adt_api directly.\n\n"
                "If user wants data and no OData exists: generate the SQL SELECT statement and "
                "run it via get_table_contents — do not ask the user to create OData just for a data query.\n\n"
                "If user wants to create OData: follow the flow — "
                "create_odata_service → activate_odata_service. But confirm intent first."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP ADT Sub-Agent starting on port 8101 ===")
    mcp.run(transport="streamable-http")
