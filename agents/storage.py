"""
Persistent storage helper for AI Factory agents.
Uses EFS mount at AI_FACTORY_DATA_DIR (Fargate) or local .data/ dir (dev).

Layout on disk:
  {data_dir}/
    cache/
      catalog.json          — OData service catalog
      entities/{SVC}.json   — per-service entity metadata
    research/
      {session_id}.json     — research history per session
    generated/
      {agent_name}/
        server.py           — generated MCP server code
        meta.json           — generation metadata
"""
import os, json, logging, time, hashlib

logger = logging.getLogger("ai_factory.storage")

# EFS in Fargate, local .data/ in dev
DATA_DIR = os.environ.get("AI_FACTORY_DATA_DIR",
                          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data"))

CACHE_DIR     = os.path.join(DATA_DIR, "cache")
ENTITIES_DIR  = os.path.join(DATA_DIR, "cache", "entities")
RESEARCH_DIR  = os.path.join(DATA_DIR, "research")
GENERATED_DIR = os.path.join(DATA_DIR, "generated")

for d in [CACHE_DIR, ENTITIES_DIR, RESEARCH_DIR, GENERATED_DIR]:
    os.makedirs(d, exist_ok=True)


# ── System info cache (S/4HANA version, detected once, persists forever) ──────

SYSTEM_INFO_FILE = os.path.join(CACHE_DIR, "system_info.json")
SYSTEM_INFO_MAX_AGE = 3600 * 24 * 30  # refresh after 30 days (version rarely changes)


def save_system_info(info: dict):
    info["detected_at"] = time.time()
    with open(SYSTEM_INFO_FILE, "w") as f:
        json.dump(info, f, indent=2)
    logger.info(f"System info saved: {info.get('s4hana_product', 'unknown')}")


def load_system_info() -> dict:
    if os.path.exists(SYSTEM_INFO_FILE):
        with open(SYSTEM_INFO_FILE) as f:
            data = json.load(f)
        age = time.time() - data.get("detected_at", 0)
        if age < SYSTEM_INFO_MAX_AGE:
            logger.info(f"System info loaded from cache: {data.get('s4hana_product', 'unknown')}")
            return data
        logger.info("System info cache expired (>30 days), will re-detect")
    return {}


# ── Catalog cache ─────────────────────────────────────────────────────────────

def save_catalog(catalog: dict):
    with open(os.path.join(CACHE_DIR, "catalog.json"), "w") as f:
        json.dump(catalog, f)
    logger.info(f"Catalog saved: {len(catalog)} services")


def load_catalog() -> dict:
    path = os.path.join(CACHE_DIR, "catalog.json")
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        logger.info(f"Catalog loaded from disk: {len(data)} services")
        return data
    return {}


def save_entities(service_name: str, entities: list):
    path = os.path.join(ENTITIES_DIR, service_name + ".json")
    with open(path, "w") as f:
        json.dump(entities, f)


def load_all_entities() -> dict:
    result = {}
    if not os.path.isdir(ENTITIES_DIR):
        return result
    for fname in os.listdir(ENTITIES_DIR):
        if fname.endswith(".json"):
            svc = fname[:-5]
            with open(os.path.join(ENTITIES_DIR, fname)) as f:
                result[svc] = json.load(f)
    if result:
        logger.info(f"Entities loaded from disk: {len(result)} services")
    return result


def cache_age_seconds() -> float:
    """How old is the catalog cache in seconds. Returns inf if no cache."""
    path = os.path.join(CACHE_DIR, "catalog.json")
    if os.path.exists(path):
        return time.time() - os.path.getmtime(path)
    return float("inf")


# ── Plan store (file-backed, survives across sessions) ────────────────────────

PLANS_DIR = os.path.join(DATA_DIR, "plans")
os.makedirs(PLANS_DIR, exist_ok=True)
_PLAN_TTL = 600  # 10 minutes


def save_plan(plan_id: str, plan: dict):
    """Persist a plan to disk so it survives across stateless HTTP sessions."""
    path = os.path.join(PLANS_DIR, plan_id + ".json")
    with open(path, "w") as f:
        json.dump({"plan": plan, "created_ts": time.time()}, f)
    logger.info(f"Plan saved to disk: {plan_id}")


def load_plan(plan_id: str):
    """Load a plan from disk. Returns (plan_dict, created_ts) or (None, None) if missing/expired."""
    _cleanup_expired_plans()
    path = os.path.join(PLANS_DIR, plan_id + ".json")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path) as f:
            data = json.load(f)
        created_ts = data.get("created_ts", 0)
        if time.time() - created_ts > _PLAN_TTL:
            os.remove(path)
            logger.info(f"Plan expired and removed: {plan_id}")
            return None, None
        return data["plan"], created_ts
    except Exception as e:
        logger.warning(f"Failed to load plan {plan_id}: {e}")
        return None, None


def delete_plan(plan_id: str):
    """Remove a plan from disk."""
    path = os.path.join(PLANS_DIR, plan_id + ".json")
    if os.path.exists(path):
        os.remove(path)


def _cleanup_expired_plans():
    """Remove expired plan files from disk."""
    now = time.time()
    if not os.path.isdir(PLANS_DIR):
        return
    for fname in os.listdir(PLANS_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(PLANS_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
            if now - data.get("created_ts", 0) > _PLAN_TTL:
                os.remove(path)
        except Exception:
            pass


# ── Research history ──────────────────────────────────────────────────────────

def save_research(session_id: str, history: list):
    path = os.path.join(RESEARCH_DIR, session_id + ".json")
    with open(path, "w") as f:
        json.dump({"session_id": session_id, "updated": time.time(), "steps": history}, f, indent=2)


def load_research(session_id: str) -> list:
    path = os.path.join(RESEARCH_DIR, session_id + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f).get("steps", [])
    return []


def list_research_sessions() -> list:
    sessions = []
    if not os.path.isdir(RESEARCH_DIR):
        return sessions
    for fname in sorted(os.listdir(RESEARCH_DIR), reverse=True):
        if fname.endswith(".json"):
            path = os.path.join(RESEARCH_DIR, fname)
            with open(path) as f:
                data = json.load(f)
            sessions.append({
                "session_id": data.get("session_id", fname[:-5]),
                "updated": data.get("updated", 0),
                "step_count": len(data.get("steps", [])),
            })
    return sessions


# ── Generated agents ──────────────────────────────────────────────────────────

def save_generated_agent(agent_name: str, server_code: str, meta: dict):
    agent_dir = os.path.join(GENERATED_DIR, agent_name)
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "server.py"), "w") as f:
        f.write(server_code)
    with open(os.path.join(agent_dir, "meta.json"), "w") as f:
        json.dump({**meta, "saved_at": time.time()}, f, indent=2)
    logger.info(f"Generated agent saved: {agent_name}")


def load_generated_agent(agent_name: str) -> dict:
    agent_dir = os.path.join(GENERATED_DIR, agent_name)
    meta_path = os.path.join(agent_dir, "meta.json")
    code_path = os.path.join(agent_dir, "server.py")
    if os.path.exists(meta_path) and os.path.exists(code_path):
        with open(meta_path) as f:
            meta = json.load(f)
        with open(code_path) as f:
            code = f.read()
        return {"meta": meta, "server_code": code}
    return {}


def list_generated_agents() -> list:
    agents = []
    if not os.path.isdir(GENERATED_DIR):
        return agents
    for name in os.listdir(GENERATED_DIR):
        meta_path = os.path.join(GENERATED_DIR, name, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            agents.append({"agent_name": name, **meta})
    return agents


# ── Question Route Cache (self-healing, learns from every query) ──────────────

ROUTES_FILE = os.path.join(CACHE_DIR, "question_routes.json")
API_VALIDATION_FILE = os.path.join(CACHE_DIR, "api_validation.json")

MIN_CONFIDENCE = 0.7  # below this, rediscover instead of using cache


def _load_routes() -> dict:
    if os.path.exists(ROUTES_FILE):
        try:
            with open(ROUTES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_routes(routes: dict):
    with open(ROUTES_FILE, "w") as f:
        json.dump(routes, f, indent=2)


def save_question_route(question_key: str, route: dict):
    """Save or update a question → API route mapping.
    route should contain: method, service/table, query/entity_set, etc."""
    routes = _load_routes()
    existing = routes.get(question_key, {})

    routes[question_key] = {
        "method": route.get("method", ""),                    # "odata" or "adt_sql"
        "service": route.get("service", ""),                  # e.g. "API_SALES_ORDER_SRV"
        "entity_set": route.get("entity_set", ""),            # e.g. "A_SalesOrder"
        "query": route.get("query", ""),                      # SQL query if adt_sql
        "filter_template": route.get("filter_template", ""),  # OData $filter template
        "expand": route.get("expand", ""),                    # OData $expand
        "select": route.get("select", ""),                    # OData $select
        "use_count": existing.get("use_count", 0) + 1,
        "success_count": existing.get("success_count", 0) + 1,
        "fail_count": existing.get("fail_count", 0),
        "confidence": _calc_confidence(
            existing.get("success_count", 0) + 1,
            existing.get("fail_count", 0)
        ),
        "created_at": existing.get("created_at", time.time()),
        "last_used": time.time(),
        "last_success": time.time(),
    }
    _save_routes(routes)
    logger.info(f"Route cached: '{question_key}' → {route.get('method')}:{route.get('service', route.get('query', '')[:50])}")


def record_route_failure(question_key: str):
    """Record a failure for a cached route. Drops confidence, auto-invalidates if too low."""
    routes = _load_routes()
    if question_key not in routes:
        return
    entry = routes[question_key]
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    entry["confidence"] = _calc_confidence(entry.get("success_count", 0), entry["fail_count"])
    entry["last_failure"] = time.time()

    if entry["confidence"] < MIN_CONFIDENCE:
        logger.warning(f"Route invalidated (confidence {entry['confidence']:.2f}): '{question_key}'")
        del routes[question_key]
    else:
        routes[question_key] = entry
        logger.info(f"Route failure recorded (confidence {entry['confidence']:.2f}): '{question_key}'")

    _save_routes(routes)


def lookup_question_route(question_key: str) -> dict:
    """Look up a cached route for a question. Returns {} if not found or confidence too low."""
    routes = _load_routes()
    entry = routes.get(question_key)
    if not entry:
        return {}
    if entry.get("confidence", 1.0) < MIN_CONFIDENCE:
        return {}
    return entry


def find_similar_route(question: str) -> tuple:
    """Fuzzy match a question against cached routes. Returns (key, route) or (None, {})."""
    routes = _load_routes()
    if not routes:
        return None, {}

    question_lower = question.lower().strip()
    question_words = set(question_lower.split())

    best_key = None
    best_score = 0

    for key, entry in routes.items():
        if entry.get("confidence", 1.0) < MIN_CONFIDENCE:
            continue

        key_lower = key.lower()
        key_words = set(key_lower.split())

        # Exact match
        if question_lower == key_lower:
            return key, entry

        # Word overlap score
        if key_words and question_words:
            overlap = len(key_words & question_words)
            score = overlap / max(len(key_words), len(question_words))
            if score > best_score and score >= 0.6:  # 60% word overlap threshold
                best_score = score
                best_key = key

    if best_key:
        return best_key, routes[best_key]
    return None, {}


def list_cached_routes() -> list:
    """List all cached question routes with stats."""
    routes = _load_routes()
    return [
        {
            "question": key,
            "method": entry.get("method"),
            "service": entry.get("service") or entry.get("query", "")[:60],
            "use_count": entry.get("use_count", 0),
            "confidence": entry.get("confidence", 1.0),
            "last_used": entry.get("last_used"),
        }
        for key, entry in sorted(routes.items(), key=lambda x: x[1].get("use_count", 0), reverse=True)
    ]


def clear_route(question_key: str) -> bool:
    """Remove a specific route from cache."""
    routes = _load_routes()
    if question_key in routes:
        del routes[question_key]
        _save_routes(routes)
        return True
    return False


def clear_all_routes():
    """Clear all cached routes."""
    _save_routes({})
    logger.info("All question routes cleared")


def _calc_confidence(success: int, fail: int) -> float:
    total = success + fail
    if total == 0:
        return 1.0
    return round(success / total, 3)


# ── API Validation Cache ──────────────────────────────────────────────────────

API_VALIDATION_MAX_AGE = 3600 * 24  # 24 hours


def save_api_validation(api_name: str, validation: dict):
    """Cache API validation result (hub + catalogue + metadata check)."""
    data = _load_api_validations()
    data[api_name] = {**validation, "validated_at": time.time()}
    with open(API_VALIDATION_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_api_validation(api_name: str) -> dict:
    """Load cached API validation. Returns {} if not found or expired."""
    data = _load_api_validations()
    entry = data.get(api_name)
    if not entry:
        return {}
    if time.time() - entry.get("validated_at", 0) > API_VALIDATION_MAX_AGE:
        return {}
    return entry


def _load_api_validations() -> dict:
    if os.path.exists(API_VALIDATION_FILE):
        try:
            with open(API_VALIDATION_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}
