"""
SAP OData Sub-Agent — S/4HANA OData queries, smart NL→SQL, entity discovery.
Port: 8102

On startup, loads the full SAP service catalog + entity metadata into memory
so searches are instant without hitting SAP on every request.
"""
import os, json, logging, time, re, hashlib, httpx, xml.etree.ElementTree as ET, boto3
from threading import Thread
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.storage import (save_catalog, load_catalog, save_entities, load_all_entities,
                            cache_age_seconds, save_research, load_research, list_research_sessions,
                            save_plan, load_plan, delete_plan,
                            save_system_info, load_system_info,
                            save_question_route, record_route_failure, lookup_question_route,
                            find_similar_route, list_cached_routes, clear_route, clear_all_routes,
                            save_api_validation, load_api_validation)
from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("odata_agent")

MODEL_ID     = "us.anthropic.claude-sonnet-4-6"
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
CATALOG_URL  = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"

mcp = FastMCP("AI Factory — OData Agent", host="0.0.0.0", port=8102)

# ── In-memory metadata cache (populated on startup) ───────────────────────────
# { "SVC_NAME": { Title, TechnicalServiceName, ServiceUrl, Description } }
_catalog_cache: dict = {}
# { "SVC_NAME": [ { entity_type, entity_set, keys, properties, nav_properties } ] }
_entity_cache: dict = {}
# Flat index: [ { service, entity_name, entity_set, property_name, property_type } ]
_field_index: list = []
_cache_loaded = False
_cache_error  = ""

# ── S/4HANA system version (detected once, cached to disk + memory) ───────────
_s4hana_version: str = ""    # e.g. "2025"
_s4hana_product: str = ""    # e.g. "SAP S/4HANA 2025"
_s4hana_platform: str = ""   # e.g. "ABAP PLATFORM 2025"


# ── SAP API Hub known API mappings (for offline/fallback search) ──────────────
_API_HUB_KNOWN_APIS = {
    "sales order": ["API_SALES_ORDER_SRV", "CE_SALESORDER_0001", "API_SALES_ORDER_SIMULATION_SRV"],
    "business partner": ["API_BUSINESS_PARTNER"],
    "purchase order": ["API_PURCHASEORDER_PROCESS_SRV"],
    "material": ["API_PRODUCT_SRV", "API_PRODUCT_SRV;v=0002"],
    "equipment": ["API_EQUIPMENT"],
    "invoice": ["API_SUPPLIERINVOICE_PROCESS_SRV", "API_BILLING_DOCUMENT_SRV"],
    "delivery": ["API_OUTBOUND_DELIVERY_SRV;v=0002", "API_INBOUND_DELIVERY_SRV;v=0002"],
    "quality notification": ["API_QUALITYNOTIFICATION_SRV"],
    "defect": ["API_DEFECT_SRV"],
    "credit memo": ["API_CREDIT_MEMO_REQUEST_SRV"],
    "debit memo": ["API_DEBIT_MEMO_REQUEST_SRV"],
    "returns": ["API_RETURNS_DELIVERY_SRV"],
    "cost center": ["API_COSTCENTER_SRV"],
    "profit center": ["API_PROFITCENTER_SRV"],
    "journal entry": ["API_JOURNALENTRYITEMBASIC_SRV"],
    "bank": ["API_BANKDETAIL_SRV"],
    "gl account": ["API_GLACCOUNTINCHARTOFACCOUNTS_SRV"],
    "employee": ["ECEmployeeProfile"],
    "plant": ["API_PLANT_SRV"],
    "warehouse": ["API_WAREHOUSE_SRV"],
    "batch": ["API_BATCH_SRV"],
    "production order": ["API_PRODUCTION_ORDER_2_SRV"],
    "maintenance order": ["API_MAINTENANCEORDER"],
    "service order": ["API_SERVICE_ORDER_SRV"],
}


# ── Plan data model, serialization, and in-memory store ───────────────────────
def serialize_plan(plan: dict) -> str:
    """Serialize an Execution Plan dict to a JSON string."""
    return json.dumps(plan, indent=2, default=str)


def parse_plan(json_str: str) -> dict:
    """Parse a JSON string back into an Execution Plan dict.
    Raises ValueError with descriptive message on invalid input."""
    try:
        plan = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")
    if not isinstance(plan, dict):
        raise ValueError(f"Expected JSON object, got {type(plan).__name__}")
    if "plan_id" not in plan:
        raise ValueError("Missing required field: plan_id")
    if "tasks" not in plan or not isinstance(plan["tasks"], list):
        raise ValueError("Missing or invalid 'tasks' field: expected JSON array")
    return plan


_plan_store: dict = {}  # kept as fast in-memory cache, backed by disk
_PLAN_TTL = 600  # 10 minutes in seconds
_inactive_services: set = set()


def _cleanup_expired_plans():
    """Remove expired plans from in-memory cache. Disk cleanup is handled by storage module."""
    now = time.time()
    expired = [pid for pid, (_, ts) in _plan_store.items() if now - ts > _PLAN_TTL]
    for pid in expired:
        del _plan_store[pid]


def _store_plan(plan_id: str, plan: dict):
    """Save plan to both in-memory cache and persistent disk storage."""
    ts = time.time()
    _plan_store[plan_id] = (plan, ts)
    save_plan(plan_id, plan)  # persist to EFS/disk


def _retrieve_plan(plan_id: str):
    """Retrieve plan from in-memory cache first, then fall back to disk.
    Returns (plan, created_ts) or (None, None) if not found/expired."""
    _cleanup_expired_plans()

    # Try in-memory first (fast path)
    if plan_id in _plan_store:
        plan, ts = _plan_store[plan_id]
        if time.time() - ts <= _PLAN_TTL:
            return plan, ts
        else:
            del _plan_store[plan_id]
            delete_plan(plan_id)
            return None, None

    # Fall back to disk (survives across stateless HTTP sessions)
    plan, ts = load_plan(plan_id)
    if plan is not None:
        _plan_store[plan_id] = (plan, ts)  # re-populate in-memory cache
        logger.info(f"Plan {plan_id} restored from disk storage")
        return plan, ts

    return None, None


def _get_token(ctx: Context) -> str:
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
    except Exception: pass
    return os.environ.get("SAP_BEARER_TOKEN", "")


def _sap_get(path: str, token: str, params: dict = None) -> dict:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()


def _sap_get_xml_raw(path: str, token: str) -> str:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.text


def _parse_metadata_xml(xml_text: str) -> list:
    """Parse OData $metadata XML into list of entity dicts with properties, keys, nav properties."""
    root = ET.fromstring(xml_text)
    entities = []
    for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                   "http://schemas.microsoft.com/ado/2009/11/edm"]:
        assoc_map = {}
        for assoc in root.iter(f"{{{ns_uri}}}Association"):
            aname = assoc.get("Name", "")
            ends  = assoc.findall(f"{{{ns_uri}}}End")
            if len(ends) == 2:
                assoc_map[aname] = {
                    ends[0].get("Role", ""): {"type": ends[0].get("Type", ""), "mult": ends[0].get("Multiplicity", "")},
                    ends[1].get("Role", ""): {"type": ends[1].get("Type", ""), "mult": ends[1].get("Multiplicity", "")},
                }
        entity_set_map = {}
        for ec in root.iter(f"{{{ns_uri}}}EntityContainer"):
            for es in ec.findall(f"{{{ns_uri}}}EntitySet"):
                es_type = es.get("EntityType", "").split(".")[-1]
                entity_set_map[es_type] = es.get("Name", "")
        for et in root.iter(f"{{{ns_uri}}}EntityType"):
            ename = et.get("Name", "")
            props = [{"name": p.get("Name", ""), "type": p.get("Type", ""), "nullable": p.get("Nullable", "true")}
                     for p in et.findall(f"{{{ns_uri}}}Property")]
            nav_props = []
            for nav in et.findall(f"{{{ns_uri}}}NavigationProperty"):
                to_role = nav.get("ToRole", "")
                rel     = nav.get("Relationship", "").split(".")[-1]
                target, mult = "", ""
                if rel in assoc_map and to_role in assoc_map[rel]:
                    target = assoc_map[rel][to_role]["type"].split(".")[-1]
                    mult   = assoc_map[rel][to_role]["mult"]
                nav_props.append({"name": nav.get("Name", ""), "target_entity": target, "multiplicity": mult})
            key_el = et.find(f"{{{ns_uri}}}Key")
            keys   = [kr.get("Name", "") for kr in key_el.findall(f"{{{ns_uri}}}PropertyRef")] if key_el is not None else []
            entities.append({"entity_type": ename, "entity_set": entity_set_map.get(ename, ""),
                             "keys": keys, "properties": props, "nav_properties": nav_props})
    return entities


def _detect_s4hana_version():
    """Detect S/4HANA version from PRDVERS table. Runs once, caches to memory + disk."""
    global _s4hana_version, _s4hana_product, _s4hana_platform

    # Try loading from disk cache first
    cached = load_system_info()
    if cached:
        _s4hana_version = cached.get("s4hana_version", "")
        _s4hana_product = cached.get("s4hana_product", "")
        _s4hana_platform = cached.get("abap_platform", "")
        logger.info(f"S/4HANA version from cache: {_s4hana_product}")
        return

    # No cache — detect from SAP via ADT SQL
    token = os.environ.get("SAP_BEARER_TOKEN", "")
    if not token:
        logger.warning("No SAP_BEARER_TOKEN — cannot detect S/4HANA version")
        return

    try:
        rows = _adt_sql(
            "SELECT name, version, descript FROM prdvers "
            "WHERE inststatus = '+' AND ( name LIKE '%S4HANA%' OR name LIKE '%ABAP PLATFORM%' )",
            token, max_rows=10
        )
        for row in rows:
            name = row.get("NAME", "").strip()
            version = row.get("VERSION", "").strip()
            descript = row.get("DESCRIPT", "").strip()
            if "S4HANA ON PREMISE" in name or "S4HANA CLOUD" in name:
                _s4hana_version = version
                _s4hana_product = descript
            elif "ABAP PLATFORM" in name:
                _s4hana_platform = descript

        if _s4hana_version:
            info = {
                "s4hana_version": _s4hana_version,
                "s4hana_product": _s4hana_product,
                "abap_platform": _s4hana_platform,
                "source": "PRDVERS table via ADT SQL",
            }
            save_system_info(info)
            logger.info(f"S/4HANA version detected and cached: {_s4hana_product}")
        else:
            logger.warning("Could not determine S/4HANA version from PRDVERS")
    except Exception as e:
        logger.warning(f"S/4HANA version detection failed: {e}")


def _search_api_hub_online(keyword: str) -> list:
    """Search SAP Business Accelerator Hub (api.sap.com) for APIs by keyword.
    Scrapes the Next.js server-rendered data from the search page."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        url = f"https://api.sap.com/search?searchTerm={keyword}"
        with httpx.Client(timeout=15, follow_redirects=True) as c:
            r = c.get(url, headers=headers)
            if r.status_code != 200:
                return []
            html = r.text
            import re as _re
            match = _re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
            if match:
                data = json.loads(match.group(1))
                props = data.get("props", {}).get("pageProps", {})
                search_data = props.get("searchResults", props.get("results", {}))
                items = search_data.get("results", search_data.get("items", []))
                return [{
                    "name": item.get("name", item.get("technicalName", "")),
                    "displayName": item.get("displayName", item.get("title", "")),
                    "description": (item.get("description", item.get("shortText", "")))[:300],
                    "type": item.get("type", item.get("artifactType", "")),
                    "url": f"https://api.sap.com/api/{item.get('name', '')}",
                } for item in items[:20]]
    except Exception as e:
        logger.debug(f"API Hub online search failed: {e}")
    return []


def _search_api_hub_offline(keyword: str) -> list:
    """Fallback: search known API mappings when api.sap.com is unreachable."""
    keyword_lower = keyword.lower()
    results = []
    for topic, apis in _API_HUB_KNOWN_APIS.items():
        if keyword_lower in topic or topic in keyword_lower:
            for api_name in apis:
                results.append({
                    "name": api_name,
                    "displayName": f"{topic.title()} API",
                    "description": f"SAP S/4HANA API for {topic}",
                    "type": "API",
                    "url": f"https://api.sap.com/api/{api_name}/overview",
                    "source": "offline_known_apis",
                })
    if not results:
        # Predict API name from SAP naming convention
        normalized = keyword.upper().replace(" ", "_").replace("-", "_")
        results.append({
            "name": f"API_{normalized}_SRV",
            "displayName": f"{keyword} (predicted name)",
            "description": f"Predicted API name based on SAP naming conventions. Verify at api.sap.com.",
            "type": "API",
            "url": f"https://api.sap.com/search?searchTerm={keyword}",
            "source": "predicted",
            "confidence": "low",
        })
    return results


def _check_catalogue_service(api_name: str, token: str) -> dict:
    """Check if an API is activated on the local SAP Gateway via CATALOGSERVICE."""
    try:
        clean_name = api_name.replace("_SRV", "").replace(";v=0002", "")
        with httpx.Client(verify=False, timeout=30) as c:
            r = c.get(
                f"{SAP_BASE_URL}/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"$filter": f"substringof('{clean_name}',TechnicalServiceName)", "$format": "json", "$top": "10"},
            )
            if r.status_code == 200:
                results = r.json().get("d", {}).get("results", [])
                if results:
                    svc = results[0]
                    return {
                        "activated": True,
                        "service_name": svc.get("TechnicalServiceName", ""),
                        "service_url": svc.get("ServiceUrl", ""),
                        "title": svc.get("Title", ""),
                    }
        return {"activated": False, "service_name": api_name}
    except Exception as e:
        logger.debug(f"Catalogue check failed for {api_name}: {e}")
        return {"activated": False, "service_name": api_name, "error": str(e)}


def _load_all_metadata():
    """Background thread: fetch full SAP catalog + metadata for every service on startup.
    Populates _catalog_cache, _entity_cache, _field_index for instant lookups."""
    global _catalog_cache, _entity_cache, _field_index, _cache_loaded, _cache_error

    # Try loading from persistent storage first (EFS or local .data/)
    CACHE_MAX_AGE = 3600 * 12  # refresh from SAP if cache older than 12 hours
    if cache_age_seconds() < CACHE_MAX_AGE:
        _catalog_cache.update(load_catalog())
        _entity_cache.update(load_all_entities())
        for svc, entities in _entity_cache.items():
            for ent in entities:
                for prop in ent.get("properties", []):
                    _field_index.append({
                        "service": svc, "entity_name": ent["entity_type"],
                        "entity_set": ent.get("entity_set", ""),
                        "property_name": prop["name"], "property_type": prop["type"],
                    })
        if _catalog_cache:
            _cache_loaded = True
            logger.info(f"Cache restored from disk: {len(_catalog_cache)} services, {len(_field_index)} fields")
            return

    logger.info("=== OData Agent: Starting full SAP metadata load ===")
    token = os.environ.get("SAP_BEARER_TOKEN", "")
    if not token:
        _cache_error = "No SAP_BEARER_TOKEN — cache will be empty until first authenticated request"
        logger.warning(_cache_error)
        return

    # 1. Load full service catalog
    try:
        with httpx.Client(verify=False, timeout=60) as c:
            r = c.get(f"{SAP_BASE_URL}{CATALOG_URL}",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                      params={"$format": "json", "$top": "5000"})
            r.raise_for_status()
            for svc in r.json().get("d", {}).get("results", []):
                title = svc.get("Title", "")
                _catalog_cache[title] = {
                    "Title": title,
                    "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                    "ServiceUrl": svc.get("ServiceUrl", ""),
                    "Description": svc.get("Description", ""),
                }
        logger.info(f"Catalog loaded: {len(_catalog_cache)} services")
    except Exception as e:
        _cache_error = f"Catalog fetch failed: {e}"
        logger.error(_cache_error)
        return

    # 2. Load metadata for every service
    total = len(_catalog_cache)
    success = skipped = 0
    for i, title in enumerate(_catalog_cache):
        try:
            xml_text = _sap_get_xml_raw(f"/sap/opu/odata/sap/{title}/$metadata", token)
            entities = _parse_metadata_xml(xml_text)
            _entity_cache[title] = entities
            for ent in entities:
                for prop in ent["properties"]:
                    _field_index.append({
                        "service": title,
                        "entity_name": ent["entity_type"],
                        "entity_set": ent.get("entity_set", ""),
                        "property_name": prop["name"],
                        "property_type": prop["type"],
                    })
            success += 1
        except Exception:
            skipped += 1
        if (i + 1) % 50 == 0:
            logger.info(f"  Metadata progress: {i+1}/{total} (ok={success}, skip={skipped})")

    _cache_loaded = True
    logger.info(f"Metadata load complete: {success}/{total} services, {len(_field_index)} fields indexed")

    # Persist to disk so next startup is instant
    save_catalog(_catalog_cache)
    for svc_name, entities in _entity_cache.items():
        save_entities(svc_name, entities)
    logger.info("Cache persisted to disk")


@mcp.tool()
def search_sap_services(ctx: Context, keyword: str, limit: int = 20) -> str:
    """Search SAP OData services by keyword.
    Uses in-memory cache if loaded (instant), otherwise hits SAP live."""
    # Use cache if available
    if _catalog_cache:
        kw = keyword.lower()
        results = [
            {"Title": v["Title"], "TechnicalServiceName": v["TechnicalServiceName"],
             "Description": v.get("Description", "")}
            for v in _catalog_cache.values()
            if kw in v["Title"].lower() or kw in v.get("TechnicalServiceName", "").lower()
               or kw in v.get("Description", "").lower()
        ][:limit]
        return json.dumps(results, indent=2)
    # Fallback: live SAP call
    token = _get_token(ctx)
    try:
        data = _sap_get(CATALOG_URL, token,
                        {"$format": "json", "$top": 200,
                         "$filter": f"substringof('{keyword}',Title) or substringof('{keyword}',TechnicalServiceName)"})
        results = data.get("d", {}).get("results", [])[:limit]
        return json.dumps([{"Title": r["Title"], "TechnicalServiceName": r.get("TechnicalServiceName", "")}
                           for r in results], indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_service_metadata(ctx: Context, service_path: str) -> str:
    """Get entity types and properties for a SAP OData service.
    Also detects if service exists in catalog but is NOT activated (403)."""
    token = _get_token(ctx)
    try:
        url = f"{SAP_BASE_URL}{service_path}/$metadata"
        with httpx.Client(verify=False, timeout=30.0) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
            if r.status_code == 403 and "/IWFND/MED/170" in r.text:
                svc_name = service_path.split("/")[-1]
                return json.dumps({
                    "error": "service_not_activated",
                    "service": svc_name,
                    "message": f"Service '{svc_name}' exists in the catalog but is NOT activated on the SAP Gateway. "
                               f"Activate it via transaction /IWFND/MAINT_SERVICE → Add Service → search for '{svc_name}'.",
                    "fallback": "Use run_sql_query to get the data directly from SAP tables instead."
                }, indent=2)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            entities = []
            for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                           "http://schemas.microsoft.com/ado/2009/11/edm"]:
                for et in root.iter(f"{{{ns_uri}}}EntityType"):
                    props = [p.get("Name") for p in et.iter(f"{{{ns_uri}}}Property")]
                    entities.append({"name": et.get("Name"), "properties": props[:15]})
            return json.dumps({"service": service_path, "entities": entities}, indent=2)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            svc_name = service_path.split("/")[-1]
            return json.dumps({
                "error": "service_not_activated",
                "service": svc_name,
                "message": f"Service '{svc_name}' is not activated. Activate via /IWFND/MAINT_SERVICE.",
                "fallback": "Use run_sql_query to get the data directly from SAP tables."
            }, indent=2)
        return json.dumps({"error": str(e)})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def query_sap_odata(ctx: Context, service_path: str, entity_set: str,
                    top: int = 10, skip: int = 0,
                    filter_expr: str = "", select_fields: str = "") -> str:
    """Query any SAP OData entity set with optional filter and field selection.
    Detects inactive services (403) and suggests SQL fallback."""
    token = _get_token(ctx)
    params: dict = {"$format": "json", "$top": top, "$skip": skip}
    if filter_expr:   params["$filter"]  = filter_expr
    if select_fields: params["$select"]  = select_fields
    try:
        data    = _sap_get(f"{service_path}/{entity_set}", token, params)
        results = data.get("d", {}).get("results", [])
        # Track successful OData query in research history
        _add_research_step("odata", service_path, entity_set, filter_expr, select_fields, len(results))
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403 and "/IWFND/MED/170" in e.response.text:
            svc_name = service_path.split("/")[-1]
            _inactive_services.add(svc_name)
            _add_research_step("odata_failed", service_path, entity_set, filter_expr, select_fields, 0,
                               error=f"Service {svc_name} not activated")
            return json.dumps({
                "error": "service_not_activated",
                "service": svc_name,
                "message": f"Service '{svc_name}' exists but is NOT activated on SAP Gateway. "
                           f"Activate via /IWFND/MAINT_SERVICE.",
                "suggestion": "Use run_sql_query to get this data directly from SAP tables. "
                              f"The underlying table for {entity_set} can be queried via SQL."
            }, indent=2)
        return json.dumps({"error": str(e)})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_sap_entity(ctx: Context, service_path: str, entity_set: str, key: str) -> str:
    """Get a single SAP OData entity by key. key e.g. \"SalesOrder='1000'\" """
    token = _get_token(ctx)
    try:
        data = _sap_get(f"{service_path}/{entity_set}({key})", token, {"$format": "json"})
        _add_research_step("odata_single", service_path, entity_set, key, "", 1)
        return json.dumps(data.get("d", data), indent=2)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return json.dumps({"error": "service_not_activated",
                               "message": f"Service not activated. Use run_sql_query instead."})
        return json.dumps({"error": str(e)})
    except Exception as e: return json.dumps({"error": str(e)})


# ── OData Write Operations (POST / PATCH / DELETE) ────────────────────────────

def _fetch_csrf(token: str, service_path: str = "/sap/opu/odata/sap/API_SALES_ORDER_SRV") -> tuple:
    """Fetch a CSRF token and session cookies from SAP. Returns (csrf_token, cookies_dict)."""
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(f"{SAP_BASE_URL}{service_path}/",
                  headers={"Authorization": f"Bearer {token}",
                           "x-csrf-token": "Fetch",
                           "Accept": "application/json"})
        r.raise_for_status()
        csrf = r.headers.get("x-csrf-token", "")
        cookies = dict(r.cookies)
        if not csrf:
            raise RuntimeError(f"No CSRF token returned. Response headers: {dict(r.headers)}")
        return csrf, cookies


def _sap_post(path: str, token: str, payload: dict, timeout: float = 120.0) -> dict:
    """POST a new entity to SAP OData. Handles CSRF token fetch automatically.
    Returns the parsed JSON response body."""
    # Extract service path for CSRF fetch (everything before the entity set)
    parts = path.rsplit("/", 1)
    svc_path = parts[0] if len(parts) > 1 else path
    csrf, cookies = _fetch_csrf(token, svc_path)
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=timeout) as c:
        r = c.post(url,
                   headers={"Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "x-csrf-token": csrf},
                   json=payload, cookies=cookies)
        r.raise_for_status()
        return r.json()


def _sap_patch(path: str, token: str, payload: dict, etag: str = "*", timeout: float = 60.0) -> int:
    """PATCH (update) an existing SAP OData entity. Returns HTTP status code.
    SAP PATCH typically returns 204 No Content on success."""
    # Extract service path for CSRF fetch (path up to entity set with key)
    svc_path = re.sub(r'/[A-Za-z_]+\(.*$', '', path)
    csrf, cookies = _fetch_csrf(token, svc_path)
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=timeout) as c:
        r = c.patch(url,
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/json",
                             "Content-Type": "application/json",
                             "x-csrf-token": csrf,
                             "If-Match": etag},
                    json=payload, cookies=cookies)
        r.raise_for_status()
        return r.status_code


def _sap_delete(path: str, token: str, etag: str = "*", timeout: float = 60.0) -> int:
    """DELETE an SAP OData entity. Returns HTTP status code (204 on success)."""
    svc_path = re.sub(r'/[A-Za-z_]+\(.*$', '', path)
    csrf, cookies = _fetch_csrf(token, svc_path)
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=timeout) as c:
        r = c.delete(url,
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json",
                              "x-csrf-token": csrf,
                              "If-Match": etag},
                     cookies=cookies)
        r.raise_for_status()
        return r.status_code


@mcp.tool()
def create_sap_entity(ctx: Context, service_path: str, entity_set: str, payload: str) -> str:
    """Create a new entity in SAP via OData POST.
    Handles CSRF token fetch and session cookies automatically.

    Args:
        service_path: OData service path, e.g. '/sap/opu/odata/sap/API_SALES_ORDER_SRV'
        entity_set: Entity set name, e.g. 'A_SalesOrder'
        payload: JSON string with entity data including deep-insert navigation properties.

    Example — Create a Sales Order:
        service_path: /sap/opu/odata/sap/API_SALES_ORDER_SRV
        entity_set: A_SalesOrder
        payload: {
            "SalesOrderType": "TA",
            "SalesOrganization": "1710",
            "DistributionChannel": "10",
            "OrganizationDivision": "00",
            "SoldToParty": "17100001",
            "to_Item": [{
                "Material": "TG11",
                "RequestedQuantity": "1",
                "RequestedQuantityUnit": "PC"
            }]
        }

    Returns: Created entity JSON on success (HTTP 201), or error details.
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token available"})
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON payload: {e}"})
    try:
        result = _sap_post(f"{service_path}/{entity_set}", token, data)
        entity = result.get("d", result)
        _add_research_step("odata_create", service_path, entity_set, "", json.dumps(data)[:200], 1)
        return json.dumps({"status": "created", "entity": entity}, indent=2)
    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text[:500]
        _add_research_step("odata_create_failed", service_path, entity_set, error=str(e))
        return json.dumps({"error": f"HTTP {e.response.status_code}", "details": error_body}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def update_sap_entity(ctx: Context, service_path: str, entity_set: str,
                      key: str, payload: str, etag: str = "*") -> str:
    """Update an existing SAP entity via OData PATCH.

    Args:
        service_path: OData service path, e.g. '/sap/opu/odata/sap/API_SALES_ORDER_SRV'
        entity_set: Entity set name, e.g. 'A_SalesOrder'
        key: Entity key, e.g. "SalesOrder='6324'"
        payload: JSON string with fields to update (only changed fields needed).
        etag: ETag for optimistic locking. Default '*' matches any version.

    Example — Update a Sales Order:
        service_path: /sap/opu/odata/sap/API_SALES_ORDER_SRV
        entity_set: A_SalesOrder
        key: SalesOrder='6324'
        payload: {"PurchaseOrderByCustomer": "PO-12345"}

    Returns: Success confirmation or error details.
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token available"})
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON payload: {e}"})
    try:
        status = _sap_patch(f"{service_path}/{entity_set}({key})", token, data, etag)
        _add_research_step("odata_update", service_path, f"{entity_set}({key})", "", json.dumps(data)[:200], 1)
        return json.dumps({"status": "updated", "http_status": status, "key": key})
    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text[:500]
        _add_research_step("odata_update_failed", service_path, f"{entity_set}({key})", error=str(e))
        return json.dumps({"error": f"HTTP {e.response.status_code}", "details": error_body}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def delete_sap_entity(ctx: Context, service_path: str, entity_set: str,
                      key: str, etag: str = "*") -> str:
    """Delete an SAP entity via OData DELETE.

    Args:
        service_path: OData service path, e.g. '/sap/opu/odata/sap/API_SALES_ORDER_SRV'
        entity_set: Entity set name, e.g. 'A_SalesOrder'
        key: Entity key, e.g. "SalesOrder='6324'"
        etag: ETag for optimistic locking. Default '*' matches any version.

    Returns: Success confirmation or error details.
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token available"})
    try:
        status = _sap_delete(f"{service_path}/{entity_set}({key})", token, etag)
        _add_research_step("odata_delete", service_path, f"{entity_set}({key})", "", "", 1)
        return json.dumps({"status": "deleted", "http_status": status, "key": key})
    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text[:500]
        _add_research_step("odata_delete_failed", service_path, f"{entity_set}({key})", error=str(e))
        return json.dumps({"error": f"HTTP {e.response.status_code}", "details": error_body}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Cache tools (served from startup-loaded metadata) ─────────────────────────

@mcp.tool()
def get_service_entities(ctx: Context, service: str) -> str:
    """Get entity types for a SAP OData service from cache.
    Returns entity names, entity set names (for OData queries), key fields, and nav properties.
    Much faster than get_service_metadata — no SAP call needed."""
    entities = _entity_cache.get(service, [])
    if not entities:
        # Try case-insensitive match
        for k in _entity_cache:
            if k.lower() == service.lower():
                entities = _entity_cache[k]
                break
    if not entities:
        return json.dumps({"error": f"Service '{service}' not in cache. Use search_sap_services to find the correct name.",
                           "cache_loaded": _cache_loaded, "cache_error": _cache_error})
    summary = [{"entity_type": e["entity_type"], "entity_set": e.get("entity_set", ""),
                "keys": e.get("keys", []), "property_count": len(e["properties"]),
                "nav_properties": [{"name": n["name"], "target": n["target_entity"],
                                    "multiplicity": n["multiplicity"]}
                                   for n in e.get("nav_properties", [])]}
               for e in entities]
    return json.dumps(summary, indent=2)


@mcp.tool()
def get_entity_properties(ctx: Context, service: str, entity: str) -> str:
    """Get all properties/fields for a specific entity type from cache.
    Returns field names, types, nullability, keys, and nav properties."""
    entities = _entity_cache.get(service, [])
    for e in entities:
        if e["entity_type"] == entity or e["entity_type"].lower() == entity.lower():
            return json.dumps({
                "entity_type": e["entity_type"],
                "entity_set": e.get("entity_set", ""),
                "keys": e.get("keys", []),
                "properties": e["properties"],
                "nav_properties": e.get("nav_properties", [])
            }, indent=2)
    return json.dumps({"error": f"Entity '{entity}' not found in service '{service}'"})


@mcp.tool()
def find_entity_by_field(ctx: Context, field_name: str, limit: int = 15) -> str:
    """Find which SAP services and entities contain a specific field name.
    Useful for reverse lookup — e.g. 'which service has SalesOrder field?'"""
    kw = field_name.lower()
    results = [entry for entry in _field_index if kw in entry["property_name"].lower()][:limit]
    if not results:
        return json.dumps({"message": f"No fields matching '{field_name}' found in cache.",
                           "cache_loaded": _cache_loaded})
    return json.dumps(results, indent=2)


@mcp.tool()
def cache_stats(ctx: Context) -> str:
    """Show metadata cache statistics — services loaded, entities indexed, fields indexed."""
    total_entities = sum(len(v) for v in _entity_cache.values())
    return json.dumps({
        "status": "loaded" if _cache_loaded else "loading" if not _cache_error else "failed",
        "services_in_catalog": len(_catalog_cache),
        "services_with_metadata": len(_entity_cache),
        "total_entities": total_entities,
        "total_fields_indexed": len(_field_index),
        "s4hana_version": _s4hana_version or "not detected",
        "s4hana_product": _s4hana_product or "not detected",
        "error": _cache_error or None
    }, indent=2)


@mcp.tool()
def get_system_version(ctx: Context) -> str:
    """Get the detected S/4HANA system version. Cached in memory — no SQL call needed."""
    if not _s4hana_version:
        # Try detecting now if not done at startup
        _detect_s4hana_version()
    return json.dumps({
        "s4hana_version": _s4hana_version or "unknown",
        "s4hana_product": _s4hana_product or "unknown",
        "abap_platform": _s4hana_platform or "unknown",
        "source": "cached" if _s4hana_version else "not_available",
    }, indent=2)


@mcp.tool()
def search_api_hub(ctx: Context, keyword: str, top: int = 10) -> str:
    """Search SAP Business Accelerator Hub (api.sap.com) for Published APIs.
    Automatically filters by the detected S/4HANA version.
    Tries online search first, falls back to known API mappings.

    Use this BEFORE calling any SAP API to verify it is a Published API
    (compliance with SAP API Policy Section 1.1).

    Args:
        keyword: Search term (e.g. 'equipment', 'sales order', 'warranty')
        top: Max results to return (default 10)
    """
    version = _s4hana_version or "2025"

    # Try online search first
    results = _search_api_hub_online(keyword)

    # Fallback to offline known APIs
    if not results:
        results = _search_api_hub_offline(keyword)

    # Add version context to results
    return json.dumps({
        "s4hana_version": version,
        "s4hana_product": _s4hana_product or f"SAP S/4HANA {version}",
        "search_term": keyword,
        "results_count": len(results[:top]),
        "results": results[:top],
        "api_hub_url": f"https://api.sap.com/search?searchTerm={keyword}",
        "note": f"Results filtered for S/4HANA {version}. Verify activation on your system with validate_api_availability.",
    }, indent=2)


@mcp.tool()
def validate_api_availability(ctx: Context, api_name: str) -> str:
    """Validate if an API is both Published on SAP API Hub AND activated on this S/4HANA system.
    Three-layer check: API Hub → Gateway Catalogue → $metadata.

    Use this before making any OData call to ensure compliance with SAP API Policy.

    Args:
        api_name: API service name (e.g. 'API_EQUIPMENT', 'API_SALES_ORDER_SRV')
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token available"})

    result = {
        "api_name": api_name,
        "s4hana_version": _s4hana_version or "unknown",
        "checks": {},
        "safe_to_call": False,
    }

    # Check 1: Is it on the API Hub (Published API)?
    hub_results = _search_api_hub_offline(api_name.lower().replace("api_", "").replace("_srv", "").replace("_", " "))
    hub_found = any(api_name in r.get("name", "") for r in hub_results)
    result["checks"]["api_hub"] = {
        "status": "found" if hub_found else "not_found",
        "note": "Published API on SAP Business Accelerator Hub" if hub_found else "Not found in known API list — verify at api.sap.com",
    }

    # Check 2: Is it activated on the local Gateway?
    catalogue = _check_catalogue_service(api_name, token)
    result["checks"]["gateway_catalogue"] = {
        "status": "activated" if catalogue.get("activated") else "not_activated",
        "service_name": catalogue.get("service_name", ""),
        "service_url": catalogue.get("service_url", ""),
    }

    # Check 3: Can we reach $metadata?
    metadata_ok = False
    if catalogue.get("activated"):
        try:
            xml_text = _sap_get_xml_raw(f"/sap/opu/odata/sap/{api_name}/$metadata", token)
            metadata_ok = bool(xml_text and "EntityType" in xml_text)
        except Exception:
            pass
    result["checks"]["metadata"] = {
        "status": "accessible" if metadata_ok else "not_accessible",
    }

    # Final verdict
    result["safe_to_call"] = hub_found and catalogue.get("activated", False) and metadata_ok
    if result["safe_to_call"]:
        result["verdict"] = f"✅ {api_name} is a Published API, activated on this system, and accessible. Safe to call."
    elif not hub_found:
        result["verdict"] = f"⚠️ {api_name} not confirmed as Published API. Verify at https://api.sap.com/api/{api_name}"
    elif not catalogue.get("activated"):
        result["verdict"] = f"❌ {api_name} exists but is NOT activated. Activate via /IWFND/MAINT_SERVICE."
    else:
        result["verdict"] = f"❌ {api_name} activated but $metadata not accessible. Check service configuration."

    return json.dumps(result, indent=2)


@mcp.tool()
def lookup_cached_route(ctx: Context, question: str) -> str:
    """Look up a cached API route for a question. Returns the known best route
    if one exists with sufficient confidence, or empty if the question is new.

    The agent should check this BEFORE doing full API discovery to save time.
    If a route is found, use it directly. If not, do normal discovery and the
    route will be cached automatically via save_route.

    Args:
        question: Natural language question (e.g. 'blocked invoices', 'sales order details')
    """
    # Try exact match first
    route = lookup_question_route(question.lower().strip())
    if route:
        return json.dumps({
            "cache_hit": True,
            "match_type": "exact",
            "question": question,
            "route": route,
            "note": "Use this route directly. If it fails, call record_failed_route to auto-invalidate.",
        }, indent=2)

    # Try fuzzy match
    matched_key, route = find_similar_route(question)
    if route:
        return json.dumps({
            "cache_hit": True,
            "match_type": "fuzzy",
            "matched_question": matched_key,
            "original_question": question,
            "route": route,
            "note": "Fuzzy match found. Use this route but verify it answers the right question.",
        }, indent=2)

    return json.dumps({
        "cache_hit": False,
        "question": question,
        "note": "No cached route. Do full discovery, then call save_route to cache the result.",
    }, indent=2)


@mcp.tool()
def save_route(ctx: Context, question: str, method: str, service: str = "",
               entity_set: str = "", query: str = "", filter_template: str = "",
               expand: str = "", select: str = "") -> str:
    """Save a successful question → API route mapping to cache.
    Call this AFTER a successful query so future identical questions are instant.

    Args:
        question: The natural language question (e.g. 'blocked invoices')
        method: 'odata' or 'adt_sql'
        service: OData service name (e.g. 'API_SALES_ORDER_SRV')
        entity_set: OData entity set (e.g. 'A_SalesOrder')
        query: SQL query if method is adt_sql
        filter_template: OData $filter template with {placeholders}
        expand: OData $expand value
        select: OData $select value
    """
    route = {
        "method": method,
        "service": service,
        "entity_set": entity_set,
        "query": query,
        "filter_template": filter_template,
        "expand": expand,
        "select": select,
    }
    save_question_route(question.lower().strip(), route)
    return json.dumps({
        "saved": True,
        "question": question,
        "method": method,
        "service": service or query[:60],
        "note": "Route cached. Next time this question is asked, it will skip discovery.",
    }, indent=2)


@mcp.tool()
def record_failed_route(ctx: Context, question: str) -> str:
    """Record that a cached route failed. Reduces confidence and auto-invalidates
    if confidence drops below threshold.

    Call this when a cached route returns an error, then retry with full discovery.

    Args:
        question: The question whose cached route failed
    """
    record_route_failure(question.lower().strip())
    return json.dumps({
        "recorded": True,
        "question": question,
        "note": "Failure recorded. Route will be auto-invalidated if confidence drops below 70%. Retry with full discovery.",
    }, indent=2)


@mcp.tool()
def show_cached_routes(ctx: Context) -> str:
    """Show all cached question → API route mappings with usage stats and confidence scores.
    Useful for debugging and understanding what the agent has learned."""
    routes = list_cached_routes()
    return json.dumps({
        "total_cached_routes": len(routes),
        "routes": routes,
        "note": "Routes with confidence < 0.7 are auto-invalidated. Use clear_cache to remove specific routes.",
    }, indent=2)


@mcp.tool()
def clear_cache(ctx: Context, question: str = "", clear_all: bool = False) -> str:
    """Clear cached routes. Either a specific question or all routes.

    Args:
        question: Specific question to clear (e.g. 'blocked invoices'). Leave empty with clear_all=True to clear everything.
        clear_all: If True, clears ALL cached routes.
    """
    if clear_all:
        clear_all_routes()
        return json.dumps({"cleared": "all", "note": "All cached routes cleared. Agent will rediscover on next query."})

    if question:
        removed = clear_route(question.lower().strip())
        return json.dumps({
            "cleared": question,
            "found": removed,
            "note": f"Route {'removed' if removed else 'not found'}. Agent will rediscover on next query for this question.",
        })

    return json.dumps({"error": "Provide a question to clear, or set clear_all=True"})


# ── Research History ──────────────────────────────────────────────────────────
_research_session_id = "session_" + str(int(time.time()))
_research_history: list = []

# ── CDS Conversion Tracking ──────────────────────────────────────────────────
# { sql_query_hash: { "original_sql": str, "service_name": str, "status": str, ... } }
_cds_conversions: dict = {}

def _add_research_step(method: str, service_or_table: str, entity_or_query: str,
                       filter_expr: str = "", select_fields: str = "",
                       result_count: int = 0, error: str = ""):
    _research_history.append({
        "method": method,
        "source": service_or_table,
        "entity": entity_or_query,
        "filter": filter_expr,
        "select": select_fields,
        "result_count": result_count,
        "error": error,
    })
    save_research(_research_session_id, _research_history)



@mcp.tool()
def list_past_research_sessions(ctx: Context) -> str:
    """List all past research sessions persisted on disk.
    Each session contains the OData queries, SQL statements, and results
    from a previous exploration. Use to resume or review past work."""
    sessions = list_research_sessions()
    if not sessions:
        return json.dumps({"message": "No past research sessions found."})
    return json.dumps(sessions, indent=2)


@mcp.tool()
def load_past_research(ctx: Context, session_id: str) -> str:
    """Load a specific past research session by ID.
    Returns the full history of OData/SQL queries and results."""
    history = load_research(session_id)
    if not history:
        return json.dumps({"error": f"Session '{session_id}' not found."})
    return json.dumps({"session_id": session_id, "steps": history}, indent=2)

# ── SQL Query via ADT (hybrid fallback) ──────────────────────────────────────

@mcp.tool()
def run_sql_query(ctx: Context, sql_query: str, max_rows: int = 100) -> str:
    """Execute ABAP SQL on SAP database tables via ADT Data Preview.
    Supports modern ABAP SQL: JOINs, GROUP BY, COUNT, SUM, AVG, MIN, MAX, CASE.
    
    SYNTAX RULES:
    - Use table aliases with ~ for field references: h~vbeln, k~augru
    - Use 'UP TO n ROWS' ONLY on single-table queries (no JOINs)
    - For JOIN queries, OMIT 'UP TO n ROWS' — engine auto-caps at 100
    - Use ABAP SQL syntax, NOT native HANA SQL
    
    Examples:
      - SELECT ebeln, ebelp, matnr, menge, netpr FROM ekpo WHERE ebeln = '4500000001'
      - SELECT h~vbeln, h~auart, k~zterm FROM vbak AS h INNER JOIN vbkd AS k ON h~vbeln = k~vbeln WHERE h~auart = 'G2'
      - SELECT vkorg, COUNT(*) AS cnt, SUM( netwr ) AS total FROM vbak WHERE auart = 'CR' GROUP BY vkorg
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    try:
        result = _adt_sql(sql_query, token, max_rows)
        _add_research_step("sql", _extract_table_from_sql(sql_query), sql_query, "", "", len(result))
        return json.dumps({"count": len(result), "sql": sql_query, "results": result}, indent=2)
    except Exception as e:
        _add_research_step("sql_failed", _extract_table_from_sql(sql_query), sql_query, error=str(e))
        return json.dumps({"error": str(e), "sql": sql_query})


def _adt_sql(sql_query: str, token: str, max_rows: int = 100) -> list:
    """Execute SQL via ADT Data Preview — tries freestyle POST first, then sqlConsole GET.
    Returns parsed rows or raises RuntimeError with clear, terminal error message."""
    freestyle_status = None
    freestyle_body = ""
    sqlconsole_status = None
    sqlconsole_body = ""
    with httpx.Client(verify=False, timeout=30.0) as c:
        # Method 1: freestyle POST (requires CSRF)
        try:
            csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                              headers={"Authorization": f"Bearer {token}", "x-csrf-token": "Fetch",
                                       "Accept": "*/*"})
            csrf = csrf_resp.headers.get("x-csrf-token", "")
            for accept_hdr in ["application/xml",
                                "application/vnd.sap.adt.datapreview.table.v1+xml",
                                "*/*"]:
                r = c.post(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/freestyle",
                           content=sql_query.encode("utf-8"),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain",
                                    "Accept": accept_hdr, "x-csrf-token": csrf},
                           params={"rowNumber": str(max_rows)})
                if r.status_code == 200:
                    return _parse_adt_data_preview(r.text)
                freestyle_status = r.status_code
                freestyle_body = r.text[:300]
                if r.status_code != 406:
                    break
        except httpx.TimeoutException:
            freestyle_status = "TIMEOUT"
            freestyle_body = "Request timed out after 30s"
        except Exception as e:
            freestyle_status = "EXCEPTION"
            freestyle_body = str(e)[:200]
        # Method 2: sqlConsole GET (fallback)
        try:
            r = c.get(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/sqlConsole",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"},
                      params={"rowNumber": str(max_rows), "sqlCommand": sql_query})
            if r.status_code == 200:
                return _parse_adt_data_preview(r.text)
            sqlconsole_status = r.status_code
            sqlconsole_body = r.text[:300]
        except httpx.TimeoutException:
            sqlconsole_status = "TIMEOUT"
            sqlconsole_body = "Request timed out after 30s"
        except Exception as e:
            sqlconsole_status = "EXCEPTION"
            sqlconsole_body = str(e)[:200]
        # Both failed — raise with clear details so Strands Agent does NOT retry
        raise RuntimeError(
            f"ADT SQL FAILED (TERMINAL — do not retry). "
            f"freestyle={freestyle_status} ({freestyle_body}), "
            f"sqlConsole={sqlconsole_status} ({sqlconsole_body}). "
            f"ADT SQL supports JOINs, GROUP BY, COUNT, SUM but NOT 'UP TO n ROWS' with JOINs (omit it — engine auto-caps at 100). "
            f"Use ABAP SQL syntax: table aliases with ~, e.g. h~vbeln. "
            f"Use run_hana_sql for native HANA SQL features like DAYS_BETWEEN, window functions, or recursive CTEs."
        )


def _parse_adt_data_preview(xml_text: str) -> list:
    """Parse ADT data preview columnar XML into list of dicts."""
    root = ET.fromstring(xml_text)
    dp_ns = "http://www.sap.com/adt/dataPreview"
    columns = root.findall(f".//{{{dp_ns}}}columns") or root.findall(f".//{{{dp_ns}}}column")
    if not columns:
        return []
    col_names = []
    col_data = []
    for col in columns:
        meta = col.find(f"{{{dp_ns}}}metadata")
        if meta is not None:
            name = meta.get(f"{{{dp_ns}}}name", "") or meta.get("name", "")
            col_names.append(name)
        dataset = col.find(f"{{{dp_ns}}}dataSet")
        if dataset is not None:
            values = [d.text or "" for d in dataset.findall(f"{{{dp_ns}}}data")]
            if not values:
                values = [d.text or "" for d in dataset]
            col_data.append(values)
        else:
            col_data.append([])
    # Transpose columns to rows
    num_rows = max(len(cd) for cd in col_data) if col_data else 0
    rows = []
    for i in range(num_rows):
        row = {}
        for j, name in enumerate(col_names):
            row[name] = col_data[j][i] if j < len(col_data) and i < len(col_data[j]) else ""
        rows.append(row)
    return rows


def _extract_table_from_sql(sql: str) -> str:
    """Extract table name from a SQL SELECT statement."""
    parts = sql.upper().split("FROM")
    if len(parts) > 1:
        table = parts[1].strip().split()[0].strip()
        return table
    return "UNKNOWN"


def _call_adt_agent(tool_name: str, arguments: dict, token: str) -> dict:
    """Call ADT Agent tool via HTTP POST JSON-RPC 2.0 to localhost:8101.
    Returns the parsed MCP result content or raises RuntimeError on failure."""
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
        "id": 1,
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(verify=False, timeout=60.0) as c:
            r = c.post("http://localhost:8101/mcp", json=payload, headers=headers)
            r.raise_for_status()
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise RuntimeError("ADT Agent is not running on port 8101") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"ADT Agent returned HTTP {e.response.status_code}: {e.response.text[:300]}") from e
    try:
        resp = r.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"ADT Agent returned invalid JSON: {r.text[:300]}") from e
    if "error" in resp:
        err = resp["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"ADT Agent MCP error: {msg}")
    return resp.get("result", resp)


def _verify_odata_endpoint(service_name: str, token: str) -> bool:
    """Test OData endpoint by requesting $metadata. Returns True if HTTP 200.
    Retries once after 3 seconds for SADL registration delay."""
    url = f"{SAP_BASE_URL}/sap/opu/odata/sap/{service_name}/$metadata"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/xml",
    }
    for attempt in range(2):
        try:
            with httpx.Client(verify=False, timeout=30.0) as c:
                r = c.get(url, headers=headers)
                if r.status_code == 200:
                    return True
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            return False
        except Exception:
            return False
        if attempt == 0:
            time.sleep(3)
    return False


def _analyze_sql_complexity(sql_query: str) -> str:
    """Analyze SQL complexity to determine the conversion path.
    Returns "simple" or "complex".

    Simple: basic SELECT, JOINs, WHERE with comparison operators, no computed columns
    Complex: CASE WHEN, date arithmetic, aggregation with computed columns, window functions
    """
    # Fast path: regex pattern matching for known complex constructs
    complex_patterns = [
        r'\bCASE\s+WHEN\b',
        r'\bDATS_DAYS_BETWEEN\b',
        r'\bADD_DAYS\b',
        r'\bROW_NUMBER\b',
        r'\bLAG\s*\(',
        r'\bLEAD\s*\(',
        r'\bRANK\s*\(',
        r'\bOVER\s*\(',
        r'\bSUM\s*\(\s*CASE\b',
        r'\bCOUNT\s*\(\s*\*\s*\)',
        r'\bHAVING\b',
        r'\bPARTITION\s+BY\b',
    ]
    for pattern in complex_patterns:
        if re.search(pattern, sql_query, re.IGNORECASE):
            return "complex"
    return "simple"


def _create_via_simple_cds(sql_query: str, token: str) -> dict:
    """Simple path: SQL → CDS View → OData.
    Calls ADT Agent's create_odata_service directly.
    For basic SELECT/JOIN/WHERE patterns that CDS supports natively.

    Returns: {"service_name": str, "cds_name": str, "status": "verified"|"activated"|"failed", "error": str|None}
    """
    service_name = None
    cds_name = None
    status = "failed"
    error_msg = None
    try:
        # 1. Extract table name from SQL
        table_name = _extract_table_from_sql(sql_query)

        # 2. Build a description from the SQL
        description = f"Open items from {table_name} — {sql_query[:120]}"

        # 3. Generate a CDS view name (keep short for SAP's 16-char SQL view name limit)
        cds_name = "zi_" + table_name[:15].lower()

        # 4. Create the CDS view via ADT Agent
        create_result = _call_adt_agent(
            "create_odata_service",
            {"description": description, "cds_name": cds_name},
            token,
        )

        # 5. Parse the result to get the service name
        if isinstance(create_result, dict):
            service_name = create_result.get("odata_service_name")
        if isinstance(create_result, str):
            try:
                parsed = json.loads(create_result)
                service_name = parsed.get("odata_service_name")
            except (json.JSONDecodeError, ValueError):
                pass
        if not service_name:
            service_name = cds_name.upper() + "_CDS"

        # 6. Activate the OData service
        _call_adt_agent(
            "activate_odata_service",
            {"service_name": service_name, "service_version": "0001", "system_alias": "LOCAL"},
            token,
        )

        # 7. Verify the OData endpoint
        if _verify_odata_endpoint(service_name, token):
            status = "verified"
        else:
            status = "activated"

    except RuntimeError as e:
        status = "failed"
        error_msg = str(e)

    # 8. Update _cds_conversions tracking dict
    sql_hash = hashlib.md5(sql_query.encode()).hexdigest()[:8]
    _cds_conversions[sql_hash] = {
        "original_sql": sql_query,
        "complexity": "simple",
        "path": "direct_cds",
        "cds_name": cds_name,
        "service_name": service_name,
        "status": status,
        "error": error_msg,
    }

    # 9. Return the result dict
    return {
        "service_name": service_name,
        "cds_name": cds_name,
        "status": status,
        "error": error_msg,
    }


def _create_via_amdp(sql_query: str, token: str) -> dict:
    """Complex path: SQL → AMDP Class → CDS Table Function → OData.
    Uses ADT Agent to create AMDP with full SQLScript logic, then wraps in CDS table function.

    Returns: {"service_name": str, "amdp_class": str, "cds_name": str, "status": str, "error": str|None}
    """
    service_name = None
    amdp_class = None
    cds_name = None
    status = "failed"
    error_msg = None
    try:
        # 1. Extract table name from SQL
        table_name = _extract_table_from_sql(sql_query)

        # 2. Generate names
        amdp_class = "ZCL_AMDP_" + table_name[:10].upper()
        cds_name = "zi_" + table_name[:12].lower() + "_tf"

        # 3. Build a detailed prompt for the ADT agent
        prompt = (
            f"Create an AMDP class {amdp_class} implementing IF_AMDP_MARKER_HDB "
            f"with a method that executes this SQLScript logic:\n\n{sql_query}\n\n"
            f"Then create a CDS table function view {cds_name} that consumes this AMDP "
            f"with @OData.publish: true. Activate both objects."
        )

        # 4. Call ADT agent to create AMDP + CDS table function
        _call_adt_agent("adt_agent_tool", {"question": prompt}, token)

        # 5. Parse the service name (CDS table function → OData service follows {cds_name}_CDS pattern)
        service_name = cds_name.upper() + "_CDS"

        # 6. Activate the OData service
        _call_adt_agent(
            "activate_odata_service",
            {"service_name": service_name, "service_version": "0001", "system_alias": "LOCAL"},
            token,
        )

        # 7. Verify the OData endpoint
        if _verify_odata_endpoint(service_name, token):
            status = "verified"
        else:
            status = "activated"

    except RuntimeError as e:
        status = "failed"
        error_msg = str(e)

    # 8. Update _cds_conversions tracking dict
    sql_hash = hashlib.md5(sql_query.encode()).hexdigest()[:8]
    _cds_conversions[sql_hash] = {
        "original_sql": sql_query,
        "complexity": "complex",
        "path": "amdp_cds",
        "cds_name": cds_name,
        "amdp_class": amdp_class,
        "service_name": service_name,
        "status": status,
        "error": error_msg,
    }

    # 9. Return the result dict
    return {
        "service_name": service_name,
        "amdp_class": amdp_class,
        "cds_name": cds_name,
        "status": status,
        "error": error_msg,
    }


def _decompose_complex_sql(sql_query: str) -> list:
    """Decompose complex SQL into simple CDS-compatible statements.
    Uses Claude (Bedrock) to analyze SQL and split into base table/join queries.
    This is the fallback when AMDP creation fails.

    Returns list of dicts: [{"sql": str, "tables": list, "description": str, "business_logic": str}]
    """
    from botocore.config import Config as BotoConfig

    system = (
        "You are an SAP CDS expert. Decompose this complex SQL into simple base queries "
        "that CDS views can handle. Each base query should be a simple SELECT from one table "
        "or a simple JOIN. Remove CASE WHEN, window functions, aggregation with computed columns. "
        "Return JSON array where each element has: "
        '"sql" (the simple query), '
        '"tables" (list of table names used), '
        '"description" (what this view provides), '
        '"business_logic" (what logic was removed and needs to be in tool code). '
        "Return ONLY valid JSON array, no explanation."
    )

    try:
        bedrock = boto3.client(
            "bedrock-runtime",
            region_name=boto3.session.Session().region_name,
            config=BotoConfig(read_timeout=30, connect_timeout=10),
        )
        resp = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "system": system,
                "messages": [{"role": "user", "content": sql_query}],
            }),
            contentType="application/json",
            accept="application/json",
        )
        raw = json.loads(resp["body"].read())["content"][0]["text"]
        result = json.loads(raw)
        if isinstance(result, dict):
            result = [result]
        return result
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        # Parsing failed — return original SQL with a note
        return [{
            "sql": sql_query,
            "tables": [],
            "description": "Original SQL (decomposition failed)",
            "business_logic": "All logic remains — automatic decomposition could not parse this query",
        }]
    except Exception:
        # Bedrock call or other unexpected error — return original SQL
        return [{
            "sql": sql_query,
            "tables": [],
            "description": "Original SQL (decomposition failed)",
            "business_logic": "All logic remains — automatic decomposition could not parse this query",
        }]


# ── Research Summary (for generator agent) ────────────────────────────────────

@mcp.tool()
def get_research_summary(ctx: Context) -> str:
    """Get the research history from this session — all OData queries, SQL statements,
    services tried, tables accessed, and what worked vs failed.
    Use this to feed into the generator agent when creating a dedicated MCP server."""
    successful = [s for s in _research_history if not s.get("error")]
    failed = [s for s in _research_history if s.get("error")]
    odata_sources = list(set(s["source"] for s in successful if s["method"].startswith("odata")))
    sql_sources = list(set(s["source"] for s in successful if s["method"] == "sql"))
    sql_queries = [s["entity"] for s in successful if s["method"] == "sql"]
    inactive_services = list(set(s["source"] for s in failed if "not_activated" in s.get("error", "")))

    # CDS conversion tracking fields
    verified_odata_services = [
        v["service_name"] for v in _cds_conversions.values()
        if v.get("status") == "verified"
    ]

    # SQL patterns from research that don't yet have a non-failed CDS conversion
    converted_hashes = {
        k for k, v in _cds_conversions.items() if v.get("status") != "failed"
    }
    pending_sql_patterns = []
    for s in _research_history:
        if s.get("method") == "sql" and s.get("count", 0) > 0:
            sql_hash = hashlib.md5(s["entity"].encode()).hexdigest()[:8]
            if sql_hash not in converted_hashes:
                pending_sql_patterns.append(s["entity"])

    return json.dumps({
        "total_steps": len(_research_history),
        "successful": len(successful),
        "failed": len(failed),
        "odata_services_used": odata_sources,
        "sql_tables_used": sql_sources,
        "sql_queries_used": sql_queries,
        "inactive_services": inactive_services,
        "full_history": _research_history,
        "recommendation": "hybrid" if odata_sources and sql_sources else
                          "odata_only" if odata_sources else
                          "sql_only" if sql_sources else "no_data",
        "verified_odata_services": verified_odata_services,
        "pending_sql_patterns": pending_sql_patterns,
        "cds_conversions": _cds_conversions,
        "ready_for_generation": len(pending_sql_patterns) == 0,
    }, indent=2)


@mcp.tool()
def create_cds_views_from_research(ctx: Context) -> str:
    """Create CDS views and OData services for all SQL patterns in the current research session.
    Analyzes SQL complexity and uses the appropriate path:
    - Simple SQL → direct CDS view → OData
    - Complex SQL → AMDP class → CDS table function → OData
    - Fallback: decompose into multiple simple CDS views if AMDP fails

    Returns JSON summary with per-pattern status."""
    token = _get_token(ctx)

    # 1. Collect successful SQL entries from research history
    sql_entries = [
        entry for entry in _research_history
        if entry.get("method") == "sql" and entry.get("count", 0) > 0
    ]

    # 2. Deduplicate by SQL query text (stored in the "entity" field)
    seen_queries: set = set()
    unique_patterns: list = []
    for entry in sql_entries:
        sql_text = entry.get("entity", "").strip()
        if sql_text and sql_text not in seen_queries:
            seen_queries.add(sql_text)
            unique_patterns.append(entry)

    if not unique_patterns:
        return json.dumps({
            "total_patterns": 0,
            "results": [],
            "all_verified": True,
            "ready_for_generation": True,
            "message": "No SQL patterns found in research history. Nothing to convert.",
        }, indent=2)

    # 3. Process each unique SQL pattern
    results: list = []
    for entry in unique_patterns:
        sql_query = entry.get("entity", "")
        sql_hash = hashlib.md5(sql_query.encode()).hexdigest()[:8]

        # 3a. Skip if already converted (exists in _cds_conversions with non-failed status)
        existing = _cds_conversions.get(sql_hash)
        if existing and existing.get("status") != "failed":
            results.append({
                "sql": sql_query[:120],
                "status": existing["status"],
                "service_name": existing.get("service_name"),
                "skipped": True,
                "reason": "Already converted",
            })
            continue

        # 3b. Analyze SQL complexity to determine conversion path
        complexity = _analyze_sql_complexity(sql_query)

        if complexity == "simple":
            # 3c. Simple path: direct CDS view
            result = _create_via_simple_cds(sql_query, token)
            results.append({
                "sql": sql_query[:120],
                "complexity": "simple",
                "path": "direct_cds",
                "status": result.get("status", "failed"),
                "service_name": result.get("service_name"),
                "cds_name": result.get("cds_name"),
                "error": result.get("error"),
            })
        else:
            # 3d. Complex path: try AMDP first
            result = _create_via_amdp(sql_query, token)

            if result.get("status") == "failed":
                # 3e. AMDP failed — fallback: decompose into multiple simple CDS views
                decomposed = _decompose_complex_sql(sql_query)
                sub_results = []
                for part in decomposed:
                    part_sql = part.get("sql", sql_query)
                    sub_result = _create_via_simple_cds(part_sql, token)
                    sub_results.append({
                        "sql": part_sql[:120],
                        "status": sub_result.get("status", "failed"),
                        "service_name": sub_result.get("service_name"),
                        "cds_name": sub_result.get("cds_name"),
                        "description": part.get("description", ""),
                        "business_logic": part.get("business_logic", ""),
                        "error": sub_result.get("error"),
                    })
                results.append({
                    "sql": sql_query[:120],
                    "complexity": "complex",
                    "path": "decomposed_fallback",
                    "amdp_error": result.get("error"),
                    "decomposed_views": sub_results,
                    "status": "verified" if all(
                        sr.get("status") == "verified" for sr in sub_results
                    ) else "partial",
                })
            else:
                results.append({
                    "sql": sql_query[:120],
                    "complexity": "complex",
                    "path": "amdp_cds",
                    "status": result.get("status", "failed"),
                    "service_name": result.get("service_name"),
                    "amdp_class": result.get("amdp_class"),
                    "cds_name": result.get("cds_name"),
                    "error": result.get("error"),
                })

    # 4. Build summary
    all_verified = all(r.get("status") == "verified" for r in results)

    summary = {
        "total_patterns": len(unique_patterns),
        "results": results,
        "all_verified": all_verified,
        "ready_for_generation": all_verified,
    }

    return json.dumps(summary, indent=2)


@mcp.tool()
def clear_research_history(ctx: Context) -> str:
    """Clear the research history for a fresh session."""
    _research_history.clear()
    return json.dumps({"status": "cleared"})


# ── HANA SQL via SSM (3rd-tier fallback) ──────────────────────────────────────

_hana_instance_id: str = ""  # cached after user provides or we discover it


@mcp.tool()
def list_sap_ec2_instances(ctx: Context) -> str:
    """List EC2 instances that are running SAP/HANA.
    Use this when the user needs to provide an EC2 instance ID for HANA SQL queries.
    Searches for instances tagged with SAP-related names or running SAP processes."""
    try:
        ec2 = boto3.client("ec2", region_name=boto3.session.Session().region_name or "us-east-1")
        # Look for running instances — filter by common SAP tags
        resp = ec2.describe_instances(Filters=[
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        instances = []
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                name = tags.get("Name", "")
                # Include if name suggests SAP/HANA or if no filter matches, show all running
                instances.append({
                    "instance_id": inst["InstanceId"],
                    "name": name,
                    "type": inst.get("InstanceType", ""),
                    "state": inst["State"]["Name"],
                    "private_ip": inst.get("PrivateIpAddress", ""),
                    "tags": tags,
                })
        if not instances:
            return json.dumps({"message": "No running EC2 instances found. Check your AWS region."})
        return json.dumps({"count": len(instances), "instances": instances}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def run_hana_sql(ctx: Context, instance_id: str, sql: str, sap_sid: str = "S4H") -> str:
    """Execute a SELECT query directly on SAP HANA database via AWS SSM.
    This is the LAST RESORT fallback — use only when both OData and ADT SQL have failed.

    Unlike ADT SQL, this supports FULL SQL: GROUP BY, COUNT, SUM, AVG, JOIN, ORDER BY, subqueries.

    The query runs via hdbsql on the EC2 instance using the DEFAULT hdbuserstore key.
    Use SAPHANADB schema prefix for SAP tables (e.g., SAPHANADB.EKKO).

    Common queries:
    - SELECT TOP 10 EBELN, BUKRS, LIFNR, NETWR FROM SAPHANADB.EKKO ORDER BY AEDAT DESC
    - SELECT BUKRS, COUNT(*) as CNT, SUM(WRBTR) as TOTAL FROM SAPHANADB.BSIK GROUP BY BUKRS
    - SELECT BELNR, BUKRS, LIFNR, WRBTR, ZLSPR FROM SAPHANADB.BSIK WHERE ZLSPR <> ''
    """
    token = _get_token(ctx)
    if not sql.strip().upper().startswith("SELECT"):
        return json.dumps({"error": "Only SELECT queries allowed for safety."})

    try:
        ssm = boto3.client("ssm", region_name=boto3.session.Session().region_name or "us-east-1")
        sid_lower = sap_sid.lower()
        cmd = f"sudo -i -u {sid_lower}adm hdbsql -U DEFAULT -j '{sql}'"

        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [cmd]},
            TimeoutSeconds=45,
        )
        command_id = resp["Command"]["CommandId"]

        # Wait for result — 25 polls × 2s = 50s max (must stay under parent's 55s timeout)
        import time as _t
        for _ in range(25):
            _t.sleep(2)
            result = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
            if result["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
                break

        if result["Status"] == "Success":
            output = result.get("StandardOutputContent", "")
            _add_research_step("hana_sql", instance_id, sql, "", "", 0)
            return json.dumps({"status": "SUCCESS", "instance": instance_id, "sql": sql, "data": output}, indent=2)
        else:
            error = result.get("StandardErrorContent", result.get("StatusDetails", ""))
            _add_research_step("hana_sql_failed", instance_id, sql, error=str(error))
            return json.dumps({"status": "ERROR", "error": error, "output": result.get("StandardOutputContent", "")})
    except Exception as e:
        _add_research_step("hana_sql_failed", instance_id, sql, error=str(e))
        return json.dumps({"error": str(e)})


@mcp.tool()
def smart_query(ctx: Context, question: str, max_rows: int = 100) -> str:
    """Answer a natural language question about SAP data using hybrid approach.
    Tries OData first, falls back to SQL if service is inactive or data is incomplete.
    Tracks all steps in research history for later agent generation.
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    import boto3
    from botocore.config import Config as BotoConfig
    bedrock = boto3.client("bedrock-runtime",
                           region_name=boto3.session.Session().region_name,
                           config=BotoConfig(read_timeout=30, connect_timeout=10))
    system = (
        "You are a SAP data expert. Given a natural language question, generate a data retrieval plan.\n"
        "Return a JSON array of steps. Each step is one of:\n"
        '  {"method":"odata","service_path":"...","entity_set":"...","filter":"...","select":"..."}\n'
        '  {"method":"sql","query":"SELECT ... FROM ... WHERE ..."}\n'
        "Use OData when a standard API exists. Use SQL for raw table access or when OData is limited.\n"
        "You can combine both — e.g., OData for PO headers + SQL for invoice line items.\n"
        "Return ONLY valid JSON array, no explanation."
    )
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                         "system": system, "messages": [{"role": "user", "content": question}]}),
        contentType="application/json", accept="application/json")
    raw = json.loads(resp["body"].read())["content"][0]["text"]
    # Parse — handle both single object and array
    try:
        plan = json.loads(raw)
        if isinstance(plan, dict): plan = [plan]
    except json.JSONDecodeError:
        return json.dumps({"error": "Could not parse plan", "raw": raw})

    all_results = []
    for step in plan:
        try:
            if step.get("method") == "sql":
                rows = _adt_sql(step["query"], token, max_rows)
                _add_research_step("sql", _extract_table_from_sql(step["query"]),
                                   step["query"], "", "", len(rows))
                all_results.append({"method": "sql", "query": step["query"],
                                    "count": len(rows), "data": rows})
            else:
                params: dict = {"$format": "json", "$top": max_rows}
                if step.get("filter"): params["$filter"] = step["filter"]
                if step.get("select"): params["$select"] = step["select"]
                try:
                    data = _sap_get(f"{step['service_path']}/{step['entity_set']}", token, params)
                    results = data.get("d", {}).get("results", [])
                    _add_research_step("odata", step["service_path"], step["entity_set"],
                                       step.get("filter", ""), step.get("select", ""), len(results))
                    all_results.append({"method": "odata", "service": step["service_path"],
                                        "entity": step["entity_set"], "count": len(results), "data": results})
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403:
                        svc = step["service_path"].split("/")[-1]
                        _add_research_step("odata_failed", step["service_path"], step["entity_set"],
                                           error=f"Service {svc} not activated")
                        all_results.append({"method": "odata_failed", "service": svc,
                                            "message": f"Service {svc} not activated. Activate via /IWFND/MAINT_SERVICE.",
                                            "fallback": "Will try SQL next."})
                    else:
                        raise
        except Exception as e:
            all_results.append({"method": step.get("method", "unknown"), "error": str(e)})

    return json.dumps({"steps": len(all_results), "results": all_results}, indent=2)


# ── Two-Phase Workflow Tools ──────────────────────────────────────────────────

@mcp.tool()
def analyze_query(ctx: Context, question: str) -> str:
    """Analyze a natural language SAP data query and return an execution plan.
    Phase 1 of the two-phase workflow. Decomposes the question into data tasks,
    matches against the in-memory metadata cache, determines service activation
    status, and returns a structured plan for approval. No SAP calls are made.
    """
    import uuid
    from datetime import datetime, timezone

    _cleanup_expired_plans()

    # If cache is empty but we have a token from this request, do a quick catalog-only load
    global _cache_loaded
    if not _cache_loaded:
        token = _get_token(ctx)
        if token and not _catalog_cache:
            logger.info("analyze_query: Cache empty, loading catalog only (fast path)")
            os.environ["SAP_BEARER_TOKEN"] = token
            try:
                with httpx.Client(verify=False, timeout=30) as c:
                    r = c.get(f"{SAP_BASE_URL}{CATALOG_URL}",
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                              params={"$format": "json", "$top": "5000"})
                    r.raise_for_status()
                    for svc in r.json().get("d", {}).get("results", []):
                        title = svc.get("Title", "")
                        _catalog_cache[title] = {
                            "Title": title,
                            "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                            "ServiceUrl": svc.get("ServiceUrl", ""),
                            "Description": svc.get("Description", ""),
                        }
                if _catalog_cache:
                    _cache_loaded = True
                    logger.info(f"Quick catalog load: {len(_catalog_cache)} services (no entity metadata yet)")
                    # Kick off full metadata load in background for future calls
                    Thread(target=_load_all_metadata, daemon=True).start()
            except Exception as e:
                logger.error(f"Quick catalog load failed: {e}")

    # Check cache status after potential reload
    if not _cache_loaded:
        return json.dumps({
            "cache_status": "loading",
            "services_loaded": len(_catalog_cache),
            "message": f"Metadata cache is still loading ({len(_catalog_cache)} services loaded so far). Please retry in a few seconds."
        }, indent=2)

    # Build service summary for Bedrock prompt (top-level names + descriptions)
    service_summary_parts = []
    for title, info in list(_catalog_cache.items())[:200]:
        desc = info.get("Description", "")[:80]
        tech = info.get("TechnicalServiceName", "")
        service_summary_parts.append(f"{title} ({tech}): {desc}")
    service_summary = "\n".join(service_summary_parts)

    # Call Bedrock to decompose the prompt into data tasks
    try:
        from botocore.config import Config as BotoConfig
        bedrock = boto3.client("bedrock-runtime",
                               region_name=boto3.session.Session().region_name,
                               config=BotoConfig(read_timeout=30, connect_timeout=10))
        system = (
            "You are a SAP data expert. Given a natural language question, decompose it into "
            "discrete data retrieval tasks.\n"
            "Return a JSON array of tasks. Each task has:\n"
            '  {"description":"human-readable task description",'
            '   "domain":"data domain like credit_memos, purchase_orders",'
            '   "required_fields":["field1","field2"],'
            '   "filter_hint":"natural language filter, e.g. status=open"}\n'
            "Return ONLY valid JSON array, no explanation.\n\n"
            f"Available SAP services (top matches):\n{service_summary}"
        )
        resp = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                             "system": system, "messages": [{"role": "user", "content": question}]}),
            contentType="application/json", accept="application/json")
        raw = json.loads(resp["body"].read())["content"][0]["text"]
    except Exception as e:
        return json.dumps({"error": f"Failed to analyze query: {e}", "suggestion": "Please try again."}, indent=2)

    # Parse Bedrock response into task list
    try:
        bedrock_tasks = json.loads(raw)
        if isinstance(bedrock_tasks, dict):
            bedrock_tasks = [bedrock_tasks]
        if not isinstance(bedrock_tasks, list):
            bedrock_tasks = []
    except json.JSONDecodeError:
        # Try to extract JSON from response text
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            try:
                bedrock_tasks = json.loads(match.group())
            except json.JSONDecodeError:
                return json.dumps({"error": "Could not parse the analysis.", "raw": raw[:500],
                                   "suggestion": "Please rephrase your query."}, indent=2)
        else:
            return json.dumps({"error": "Could not parse the analysis.", "raw": raw[:500],
                               "suggestion": "Please rephrase your query."}, indent=2)

    if not bedrock_tasks:
        plan_id = str(uuid.uuid4())
        plan = {
            "plan_id": plan_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "original_prompt": question,
            "summary": "No data tasks identified",
            "cache_status": "loaded",
            "tasks": [],
            "status": "no_tasks",
            "warnings": ["Could not identify any data retrieval tasks. Try being more specific."]
        }
        _store_plan(plan_id, plan)
        return serialize_plan(plan)

    # Match each task against the metadata cache
    plan_tasks = []
    warnings = []
    for i, bt in enumerate(bedrock_tasks, 1):
        description = bt.get("description", f"Task {i}")
        domain = bt.get("domain", "unknown")
        required_fields = bt.get("required_fields", [])
        filter_hint = bt.get("filter_hint", "")

        # Search _catalog_cache by keyword matching (case-insensitive)
        keywords = [domain] + required_fields
        matched_service = None
        matched_entity = None
        matched_fields = []

        for kw in keywords:
            if not kw:
                continue
            kw_lower = kw.lower()
            for title, info in _catalog_cache.items():
                if (kw_lower in info.get("Title", "").lower() or
                    kw_lower in info.get("TechnicalServiceName", "").lower() or
                    kw_lower in info.get("Description", "").lower()):
                    matched_service = info
                    break
            if matched_service:
                break

        # Search _entity_cache for matching entity sets
        if matched_service:
            svc_title = matched_service["Title"]
            entities = _entity_cache.get(svc_title, [])
            for ent in entities:
                # Check if entity name or set matches domain keywords
                ent_name_lower = ent.get("entity_type", "").lower()
                ent_set_lower = ent.get("entity_set", "").lower()
                for kw in keywords:
                    if kw and (kw.lower() in ent_name_lower or kw.lower() in ent_set_lower):
                        matched_entity = ent
                        break
                if matched_entity:
                    break
            # If no entity matched by keyword, use the first entity
            if not matched_entity and entities:
                matched_entity = entities[0]

        # Search _field_index for matching fields
        if matched_service:
            svc_title = matched_service["Title"]
            for rf in required_fields:
                rf_lower = rf.lower()
                for entry in _field_index:
                    if entry["service"] == svc_title and rf_lower in entry["property_name"].lower():
                        matched_fields.append(entry["property_name"])
                        break

        # Determine service status and execution method
        if matched_service:
            svc_name = matched_service.get("TechnicalServiceName", matched_service["Title"])
            svc_title = matched_service["Title"]
            service_path = f"/sap/opu/odata/sap/{svc_title}"

            if svc_name in _inactive_services or svc_title in _inactive_services:
                # Service is known to be inactive
                task_dict = {
                    "task_number": i,
                    "description": description,
                    "domain": domain,
                    "execution_method": "sql_fallback",
                    "service_name": svc_title,
                    "technical_service_name": svc_name,
                    "service_status": "inactive",
                    "activation_path": f"/IWFND/MAINT_SERVICE → Add Service → search '{svc_name}'",
                    "fallback_table": domain.upper().replace(" ", "_")[:10],
                    "fallback_sql": f"SELECT * FROM {domain.upper().replace(' ', '_')[:10]}",
                    "estimated_complexity": "simple"
                }
                warnings.append(f"Service {svc_name} is not activated. Task {i} will use SQL fallback.")
            else:
                # Service is assumed active
                entity_set = matched_entity.get("entity_set", "") if matched_entity else ""
                select_fields = ", ".join(matched_fields) if matched_fields else ""
                if not select_fields and matched_entity:
                    # Use first few properties as default select
                    props = matched_entity.get("properties", [])[:10]
                    select_fields = ", ".join(p["name"] for p in props)

                task_dict = {
                    "task_number": i,
                    "description": description,
                    "domain": domain,
                    "execution_method": "odata",
                    "service_name": svc_title,
                    "technical_service_name": svc_name,
                    "service_status": "active",
                    "service_path": service_path,
                    "entity_set": entity_set,
                    "filter_expression": filter_hint,
                    "select_fields": select_fields,
                    "estimated_complexity": "simple",
                    "matched_fields": matched_fields,
                    "fallback_sql": f"SELECT * FROM {domain.upper().replace(' ', '_')[:10]}"
                }
        else:
            # No matching service found — SQL fallback
            task_dict = {
                "task_number": i,
                "description": description,
                "domain": domain,
                "execution_method": "sql_fallback",
                "service_name": None,
                "service_status": "not_found",
                "fallback_table": domain.upper().replace(" ", "_")[:10],
                "fallback_sql": f"SELECT * FROM {domain.upper().replace(' ', '_')[:10]}",
                "estimated_complexity": "simple"
            }

        plan_tasks.append(task_dict)

    # Generate the execution plan
    plan_id = str(uuid.uuid4())
    plan = {
        "plan_id": plan_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_prompt": question,
        "summary": f"Retrieve data for: {question[:100]}",
        "cache_status": "loaded",
        "tasks": plan_tasks,
        "status": "ready_for_approval",
        "warnings": warnings
    }

    # Store plan with TTL (in-memory + disk for cross-session persistence)
    _store_plan(plan_id, plan)

    # Record in research history
    _add_research_step("phase1_analyze", "metadata_cache", question,
                       filter_expr=json.dumps([t.get("domain") for t in plan_tasks]),
                       select_fields=str(len(plan_tasks)),
                       result_count=len(plan_tasks))

    return serialize_plan(plan)


@mcp.tool()
def execute_plan(ctx: Context, plan_id: str, skip_tasks: str = "",
                 use_sql_for_all: bool = False, activated_services: str = "") -> str:
    """Execute an approved SAP data query plan.
    Phase 2 of the two-phase workflow. Executes the plan with tiered fallback
    (OData → ADT SQL → HANA SQL).

    Parameters:
    - plan_id: UUID of the plan to execute (from analyze_query)
    - skip_tasks: Comma-separated task numbers to skip, e.g. "2,4"
    - use_sql_for_all: If True, use SQL for all tasks regardless of plan method
    - activated_services: Comma-separated service names the user has activated since Phase 1
    """
    from datetime import datetime, timezone

    _cleanup_expired_plans()

    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No authentication token available. Please re-authenticate."})

    # Look up plan (in-memory first, then disk)
    plan, created_ts = _retrieve_plan(plan_id)
    if plan is None:
        return json.dumps({
            "error": f"Plan '{plan_id}' not found. It may have expired (plans are kept for 10 minutes). Please re-run the analysis.",
            "suggestion": "Call analyze_query again with your original question."
        }, indent=2)

    # Parse skip_tasks
    skip_set = set()
    if skip_tasks:
        for s in skip_tasks.split(","):
            s = s.strip()
            try:
                skip_set.add(int(s))
            except ValueError:
                pass  # Ignore invalid entries

    # Handle activated_services override
    activated_set = set()
    if activated_services:
        activated_set = {s.strip() for s in activated_services.split(",") if s.strip()}

    tasks = plan.get("tasks", [])
    results = []
    tasks_executed = 0
    tasks_skipped = 0

    for task in tasks:
        task_num = task.get("task_number", 0)

        # Skip if in skip set
        if task_num in skip_set:
            tasks_skipped += 1
            continue

        description = task.get("description", f"Task {task_num}")
        execution_method = task.get("execution_method", "odata")

        # Override: use_sql_for_all
        if use_sql_for_all:
            execution_method = "sql_fallback"

        # Override: activated_services
        svc_name = task.get("service_name", "") or ""
        tech_name = task.get("technical_service_name", "") or ""
        if activated_set and (svc_name in activated_set or tech_name in activated_set):
            execution_method = "odata"
            # Remove from inactive set
            _inactive_services.discard(svc_name)
            _inactive_services.discard(tech_name)

        task_result = {
            "task_number": task_num,
            "description": description,
        }

        if execution_method == "user_action_required":
            task_result["data_source"] = "N/A"
            task_result["method_used"] = "user_action_required"
            task_result["query"] = "N/A"
            task_result["row_count"] = 0
            task_result["message"] = f"Service {svc_name} needs activation. Activate via /IWFND/MAINT_SERVICE."
            task_result["data"] = []
            results.append(task_result)
            tasks_executed += 1
            _add_research_step("phase2_skip_activation", svc_name, description,
                               error="Service needs activation")
            continue

        # Try OData first
        if execution_method == "odata":
            service_path = task.get("service_path", "")
            entity_set = task.get("entity_set", "")
            filter_expr = task.get("filter_expression", "")
            select_fields = task.get("select_fields", "")

            if service_path and entity_set:
                try:
                    params = {"$format": "json", "$top": 100}
                    if filter_expr:
                        params["$filter"] = filter_expr
                    if select_fields:
                        params["$select"] = select_fields
                    data = _sap_get(f"{service_path}/{entity_set}", token, params)
                    rows = data.get("d", {}).get("results", [])
                    task_result["data_source"] = f"OData — {svc_name} / {entity_set}"
                    task_result["method_used"] = "odata"
                    task_result["query"] = f"{service_path}/{entity_set}?$filter={filter_expr}&$select={select_fields}"
                    task_result["row_count"] = len(rows)
                    task_result["data"] = rows
                    results.append(task_result)
                    tasks_executed += 1
                    _add_research_step("phase2_odata", service_path, entity_set,
                                       filter_expr, select_fields, len(rows))
                    continue
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403:
                        _inactive_services.add(service_path.split("/")[-1])
                        task_result["original_method"] = "odata"
                        task_result["fallback_reason"] = f"Service {svc_name} not activated (403)"
                        execution_method = "sql_fallback"
                    else:
                        task_result["original_method"] = "odata"
                        task_result["fallback_reason"] = f"OData error: {e}"
                        execution_method = "sql_fallback"
                except Exception as e:
                    task_result["original_method"] = "odata"
                    task_result["fallback_reason"] = f"OData error: {e}"
                    execution_method = "sql_fallback"

        # Try ADT SQL fallback
        if execution_method == "sql_fallback":
            fallback_sql = task.get("fallback_sql", "")
            if fallback_sql:
                try:
                    rows = _adt_sql(fallback_sql, token, 100)
                    table_name = _extract_table_from_sql(fallback_sql)
                    task_result["data_source"] = f"ADT SQL — {table_name}"
                    task_result["method_used"] = "sql_fallback"
                    task_result["query"] = fallback_sql
                    task_result["row_count"] = len(rows)
                    task_result["data"] = rows
                    results.append(task_result)
                    tasks_executed += 1
                    _add_research_step("phase2_sql", table_name, fallback_sql,
                                       result_count=len(rows))
                    continue
                except Exception as sql_err:
                    # Try HANA SQL as last resort
                    if _hana_instance_id:
                        try:
                            hana_sql = fallback_sql
                            # Prefix with SAPHANADB schema if not already
                            if "SAPHANADB." not in hana_sql.upper():
                                table_name = _extract_table_from_sql(hana_sql)
                                hana_sql = hana_sql.replace(table_name, f"SAPHANADB.{table_name}")
                            ssm = boto3.client("ssm", region_name=boto3.session.Session().region_name or "us-east-1")
                            sid_lower = "s4h"
                            cmd = f"sudo -i -u {sid_lower}adm hdbsql -U DEFAULT -j '{hana_sql}'"
                            resp = ssm.send_command(
                                InstanceIds=[_hana_instance_id],
                                DocumentName="AWS-RunShellScript",
                                Parameters={"commands": [cmd]},
                                TimeoutSeconds=45,
                            )
                            command_id = resp["Command"]["CommandId"]
                            import time as _t
                            for _ in range(25):
                                _t.sleep(2)
                                result = ssm.get_command_invocation(
                                    CommandId=command_id, InstanceId=_hana_instance_id)
                                if result["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
                                    break
                            if result["Status"] == "Success":
                                output = result.get("StandardOutputContent", "")
                                task_result["data_source"] = f"HANA SQL — {_hana_instance_id}"
                                task_result["method_used"] = "hana_sql"
                                task_result["query"] = hana_sql
                                task_result["row_count"] = 0
                                task_result["data"] = output
                                results.append(task_result)
                                tasks_executed += 1
                                _add_research_step("phase2_hana_sql", _hana_instance_id, hana_sql,
                                                   result_count=0)
                                continue
                            else:
                                error = result.get("StandardErrorContent", "")
                                task_result["data_source"] = "FAILED"
                                task_result["method_used"] = "all_failed"
                                task_result["query"] = fallback_sql
                                task_result["row_count"] = 0
                                task_result["error"] = f"All methods failed. ADT SQL: {sql_err}. HANA SQL: {error}"
                                task_result["data"] = []
                        except Exception as hana_err:
                            task_result["data_source"] = "FAILED"
                            task_result["method_used"] = "all_failed"
                            task_result["query"] = fallback_sql
                            task_result["row_count"] = 0
                            task_result["error"] = f"All methods failed. ADT SQL: {sql_err}. HANA SQL: {hana_err}"
                            task_result["data"] = []
                    else:
                        task_result["data_source"] = "FAILED"
                        task_result["method_used"] = "all_failed"
                        task_result["query"] = fallback_sql
                        task_result["row_count"] = 0
                        task_result["error"] = f"ADT SQL failed: {sql_err}. HANA SQL requires an EC2 instance ID — use list_sap_ec2_instances to find one."
                        task_result["data"] = []
            else:
                task_result["data_source"] = "FAILED"
                task_result["method_used"] = "no_query"
                task_result["query"] = "N/A"
                task_result["row_count"] = 0
                task_result["error"] = "No fallback SQL query available for this task."
                task_result["data"] = []

            results.append(task_result)
            tasks_executed += 1
            _add_research_step("phase2_execute", task.get("service_name", "unknown"), description,
                               result_count=task_result.get("row_count", 0),
                               error=task_result.get("error", ""))

    execution_result = {
        "plan_id": plan_id,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "tasks_executed": tasks_executed,
        "tasks_skipped": tasks_skipped,
        "results": results,
        "research_session": _research_session_id
    }

    return json.dumps(execution_result, indent=2, default=str)


def create_odata_strands_agent() -> Agent:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8102/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000),
            tools=client.tools,
            system_prompt=(
                "You are the Hybrid Data Research Agent within the AI Factory MCP Server.\n\n"
                "Your role is to find SAP data using the BEST available method — OData APIs, SQL queries, or both.\n\n"
                "WORKFLOW:\n"
                "1. Try OData first: search_sap_services → get_service_metadata → query_sap_odata\n"
                "2. If OData service is NOT ACTIVATED (403 error):\n"
                "   - Tell the user: 'Service X exists but is not activated. Activate via /IWFND/MAINT_SERVICE.'\n"
                "   - Fall back to SQL: use run_sql_query with the appropriate SAP table\n"
                "3. If OData returns partial data (missing fields/details):\n"
                "   - Use run_sql_query to get the missing data from SAP tables\n"
                "   - Merge both results in your response\n"
                "4. For complex questions: use smart_query which auto-plans OData + SQL steps\n\n"
                "COMMON SAP TABLE MAPPINGS:\n"
                "- Sales orders: VBAK (header), VBAP (items)\n"
                "- Purchase orders: EKKO (header), EKPO (items)\n"
                "- Vendor invoices: RBKP (header), RSEG (items)\n"
                "- AP open items: BSIK, Cleared: BSAK\n"
                "- AR open items: BSID, Cleared: BSAD\n"
                "- Accounting docs: BKPF (header), BSEG (items)\n"
                "- Materials: MARA (general), MARC (plant), MARD (storage)\n"
                "- Customers: KNA1, Vendors: LFA1\n"
                "- Deliveries: LIKP (header), LIPS (items)\n\n"
                "RESEARCH TRACKING:\n"
                "- All your queries (OData + SQL) are tracked in research history\n"
                "- After answering, ask: 'Would you like me to create a dedicated AI agent for this?'\n"
                "- If yes, you MUST follow the CDS CREATION ENFORCEMENT rules below BEFORE calling the generator\n\n"
                "═══════════════════════════════════════════════════════════════\n"
                "CDS CREATION ENFORCEMENT — MANDATORY, NEVER SKIP\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "RULE 1 — AFTER EVERY SQL QUERY, YOU MUST ASK THE USER:\n"
                "After presenting SQL results, you MUST say:\n"
                "'This data was retrieved using SQL (table: [TABLE_NAME]).\n"
                "For a production agent, I need to create a CDS view and OData service.\n"
                "Should I proceed with CDS view creation?'\n"
                "You are FORBIDDEN from skipping this prompt. Every SQL query triggers it.\n\n"
                "RULE 2 — BEFORE CALLING THE GENERATOR, YOU MUST PRESENT A PLAN:\n"
                "When the user asks to generate/create an agent, you MUST present this table FIRST:\n"
                "| # | Tool Name | Data Access Method | Tables | CDS View Needed? | Path (Simple/AMDP) |\n"
                "Show every tool the agent will have, whether it uses existing OData or needs a new CDS view.\n"
                "Count: 'X tools use existing OData, Y tools need new CDS views (Z simple + W AMDP)'\n"
                "Then ask: 'Shall I proceed with creating the CDS views?'\n"
                "You are FORBIDDEN from calling the generator without showing this plan first.\n\n"
                "RULE 3 — CDS CREATION FLOW (after user confirms):\n"
                "1. Call create_cds_views_from_research — it handles:\n"
                "   - Complexity analysis (simple SQL → direct CDS, complex SQL → AMDP + CDS table function)\n"
                "   - ADT Agent calls for CDS creation and activation\n"
                "   - $metadata verification\n"
                "2. After creation, call verify_cds_exists for EACH view to confirm it actually exists\n"
                "3. Report results: 'Created X/Y CDS views. Z verified, W failed.'\n"
                "4. If any failed, tell user which ones and suggest manual creation\n"
                "5. ONLY after ALL views are verified, offer to generate the agent\n\n"
                "RULE 4 — GENERATOR CALL RESTRICTIONS:\n"
                "- You MUST call get_research_summary first\n"
                "- If ready_for_generation is false, you are FORBIDDEN from calling the generator\n"
                "- Tell user: 'Cannot generate yet — these SQL patterns need CDS views: [list]'\n"
                "- Pass ONLY verified_odata_services to the generator — NEVER pass SQL statements\n"
                "- The generated agent will use ONLY OData endpoints, never raw SQL\n\n"
                "RULE 5 — NO SILENT SQL FALLBACK IN GENERATED AGENTS:\n"
                "- Generated agents must NEVER contain _adt_sql, datapreview/freestyle, or sqlConsole\n"
                "- If a tool needs complex logic (CASE WHEN, date math, aggregation), that logic\n"
                "  must be in an AMDP on the SAP side, exposed via CDS table function + OData\n"
                "- The MCP tool code only calls OData endpoints and does Python post-processing\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "═══════════════════════════════════════════════════════════════\n"
                "WRITE OPERATIONS — CREATE / UPDATE / DELETE\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "You can CREATE, UPDATE, and DELETE SAP entities via OData:\n\n"
                "CREATE: Use create_sap_entity with service_path, entity_set, and JSON payload.\n"
                "  - Supports deep insert (nested navigation properties like to_Item).\n"
                "  - CSRF token is handled automatically.\n"
                "  - Example: Create sales order via API_SALES_ORDER_SRV / A_SalesOrder\n\n"
                "UPDATE: Use update_sap_entity with service_path, entity_set, key, and JSON payload.\n"
                "  - Only send fields that need to change (PATCH semantics).\n"
                "  - Example: Update PO reference on sales order\n\n"
                "DELETE: Use delete_sap_entity with service_path, entity_set, and key.\n"
                "  - Use with caution — confirm with user before deleting.\n\n"
                "IMPORTANT RULES FOR WRITE OPERATIONS:\n"
                "1. Always confirm with the user before creating, updating, or deleting data.\n"
                "2. Show the payload you plan to send and ask for confirmation.\n"
                "3. Use the correct unit of measure codes (PC not ST, EA not Each).\n"
                "4. For sales orders, common services: API_SALES_ORDER_SRV, API_BUSINESS_PARTNER.\n"
                "5. For purchase orders: API_PURCHASEORDER_PROCESS_SRV.\n"
                "6. After successful creation, report the new document number to the user.\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "═══════════════════════════════════════════════════════════════\n"
                "API COMPLIANCE — SAP API POLICY VALIDATION\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "The S/4HANA system version is auto-detected at startup and cached.\n"
                "Use get_system_version to check the detected version.\n\n"
                "BEFORE calling any SAP API for the first time, you SHOULD:\n"
                "1. Call search_api_hub(keyword) to check if the API is Published on SAP API Hub\n"
                "2. Call validate_api_availability(api_name) for a 3-layer check:\n"
                "   - API Hub (Published?) → Gateway Catalogue (Activated?) → $metadata (Accessible?)\n"
                "3. Only proceed if safe_to_call is true\n\n"
                "This ensures compliance with SAP API Policy Section 1.1 (Published APIs only).\n"
                "For well-known APIs (API_SALES_ORDER_SRV, API_BUSINESS_PARTNER, etc.) you can skip\n"
                "validation as they are confirmed Published APIs.\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "═══════════════════════════════════════════════════════════════\n"
                "QUESTION ROUTE CACHE — SELF-HEALING PERFORMANCE OPTIMIZATION\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "The agent learns from every query and caches the API route for repeat questions.\n\n"
                "WORKFLOW FOR EVERY QUERY:\n"
                "1. FIRST: Call lookup_cached_route(question) to check if a route exists\n"
                "2. IF cache_hit=True: Use the cached route directly (skip discovery)\n"
                "3. IF cache_hit=False: Do full discovery (search catalogue, find API, etc.)\n"
                "4. AFTER SUCCESS: Call save_route(question, method, service, ...) to cache it\n"
                "5. IF CACHED ROUTE FAILS: Call record_failed_route(question), then rediscover\n\n"
                "This makes the agent progressively faster — first query is slow (discovery),\n"
                "every subsequent identical query is instant (cached route).\n"
                "Routes auto-invalidate if they fail too often (confidence < 70%).\n"
                "═══════════════════════════════════════════════════════════════\n\n"
                "IMPORTANT: Always present data clearly. If you used both OData and SQL, explain which "
                "data came from where so the user understands the data sources."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP OData Sub-Agent starting on port 8102 ===")
    # Detect S/4HANA version (fast — reads from cache or runs one SQL query)
    Thread(target=_detect_s4hana_version, daemon=True).start()
    # Start background metadata load — populates cache while server is already accepting requests
    Thread(target=_load_all_metadata, daemon=True).start()
    mcp.run(transport="streamable-http")
