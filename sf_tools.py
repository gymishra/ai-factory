"""
SAP SuccessFactors OData v2 tools for the SAP Smart MCP Server.

Env vars required:
  SF_COMPANY_ID     — SuccessFactors company ID
  SF_CLIENT_ID      — OAuth2 client ID
  SF_CLIENT_SECRET  — OAuth2 client secret
  SF_DC             — Data center number (default: 4 → api4.successfactors.com)
  SF_TOKEN_URL      — Override full token URL (optional)
"""
import os, json, httpx, time as _time

_sf_token_cache: dict = {"token": None, "expires_at": 0}


def _get_sf_token() -> str:
    now = _time.time()
    if _sf_token_cache["token"] and now < _sf_token_cache["expires_at"]:
        return _sf_token_cache["token"]
    dc         = os.environ.get("SF_DC", "4")
    company_id = os.environ.get("SF_COMPANY_ID", "")
    client_id  = os.environ.get("SF_CLIENT_ID", "")
    client_sec = os.environ.get("SF_CLIENT_SECRET", "")
    token_url  = os.environ.get("SF_TOKEN_URL",
                     f"https://api{dc}.successfactors.com/oauth/token")
    if not all([company_id, client_id, client_sec]):
        raise ValueError("SF_COMPANY_ID, SF_CLIENT_ID, SF_CLIENT_SECRET env vars required")
    with httpx.Client(timeout=15.0) as c:
        r = c.post(token_url,
                   data={"grant_type": "client_credentials", "company_id": company_id},
                   auth=(client_id, client_sec),
                   headers={"Accept": "application/json"})
        r.raise_for_status()
        d = r.json()
    _sf_token_cache["token"] = d["access_token"]
    _sf_token_cache["expires_at"] = now + d.get("expires_in", 3600) - 300
    return _sf_token_cache["token"]


def _sf_get(path: str, params: dict = None) -> dict:
    dc   = os.environ.get("SF_DC", "4")
    base = f"https://api{dc}.successfactors.com"
    with httpx.Client(timeout=30.0) as c:
        r = c.get(f"{base}{path}",
                  params={**(params or {}), "$format": "json"},
                  headers={"Authorization": f"Bearer {_get_sf_token()}",
                           "Accept": "application/json"})
        r.raise_for_status()
        return r.json()


def _sf_results(path: str, top: int, skip: int, filter_: str, select: str) -> str:
    params: dict = {"$top": top, "$skip": skip}
    if filter_: params["$filter"] = filter_
    if select:  params["$select"] = select
    try:
        data    = _sf_get(path, params)
        results = data.get("d", {}).get("results", data.get("value", []))
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Employee Central ──────────────────────────────────────────────────────────

def sf_list_employees(top: int = 50, skip: int = 0,
                      filter_: str = "", select: str = "") -> str:
    """List SAP SuccessFactors employees (PerPerson).
    Useful select fields: personIdExternal, firstName, lastName, dateOfBirth.
    """
    return _sf_results("/odata/v2/PerPerson", top, skip, filter_, select)


def sf_get_employee_employment(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """Get SAP SuccessFactors employee employment records (EmpEmployment).
    Filter e.g. by userId or startDate.
    """
    return _sf_results("/odata/v2/EmpEmployment", top, skip, filter_, "")


def sf_list_positions(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors positions. Filter e.g. by department or location."""
    return _sf_results("/odata/v2/Position", top, skip, filter_, "")


def sf_list_departments(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors departments (FODepartment)."""
    return _sf_results("/odata/v2/FODepartment", top, skip, filter_, "")


def sf_list_locations(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors locations (FOLocation)."""
    return _sf_results("/odata/v2/FOLocation", top, skip, filter_, "")


def sf_list_users(top: int = 50, skip: int = 0,
                  filter_: str = "", select: str = "") -> str:
    """List SAP SuccessFactors users.
    Useful select: userId,firstName,lastName,email,department,title,location.
    """
    return _sf_results("/odata/v2/User", top, skip, filter_, select)


# ── Recruiting ────────────────────────────────────────────────────────────────

def sf_list_job_requisitions(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors job requisitions (open roles for recruiting)."""
    return _sf_results("/odata/v2/JobRequisition", top, skip, filter_, "")


def sf_list_candidates(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors candidates in the recruiting pipeline."""
    return _sf_results("/odata/v2/Candidate", top, skip, filter_, "")


# ── Learning ──────────────────────────────────────────────────────────────────

def sf_list_learning_activities(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors learning activities and completions."""
    return _sf_results("/odata/v2/LearningActivity", top, skip, filter_, "")


# ── Performance & Goals ───────────────────────────────────────────────────────

def sf_list_performance_reviews(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors performance review forms."""
    return _sf_results("/odata/v2/PerformanceReview", top, skip, filter_, "")


# ── Compensation ──────────────────────────────────────────────────────────────

def sf_list_compensation(top: int = 50, skip: int = 0, filter_: str = "") -> str:
    """List SAP SuccessFactors compensation employee records."""
    return _sf_results("/odata/v2/CompensationEmployee", top, skip, filter_, "")
