"""Coterie commercial API (v1.6). Docs: https://docs.coterieinsurance.com

  Sandbox: https://api-sandbox.coterieinsurance.com/v1.6/commercial
  Live:    https://api.coterieinsurance.com/v1.6/commercial
  Auth:    header  Authorization: token <key>
           publishable key -> create quotes; secret key -> bind (server-side only)

Coterie classifies businesses by an "AKHash" (NAICS code + description hash). Your Coterie
Integration Manager provides the list; put the ones you need in COTERIE_AKHASH as JSON,
e.g. {"handyman":"ec16...","plumbing":"..."}.

Binding (bind_quote) commits real coverage. It only runs from the admin page, after the contractor
asked to bind and you've recorded the $50 fee. With "Agency Bill" Coterie bills your agency and you
collect the premium; no card token is sent. Confirm the bind path with Coterie in sandbox first:
their docs show it as "/{quoteId}/bind" under the commercial base URL; override with COTERIE_BIND_PATH.
"""
import json

import httpx

from ..config import get_settings

BASES = {"sandbox": "https://api-sandbox.coterieinsurance.com/v1.6/commercial",
         "live": "https://api.coterieinsurance.com/v1.6/commercial"}

# From Coterie's artisan & trade contractor guide (in appetite). Others: check with Coterie.
IN_APPETITE = {"drywall", "electrical", "paint_int", "paint_ext", "hvac", "masonry", "plumbing",
               "roofing", "remodel"}


def eligibility(a: dict) -> list[str]:
    """Reasons this risk may be out of Coterie's appetite. Empty list = looks eligible."""
    out = []
    if float(a.get("revenue") or 0) > 5_000_000:
        out.append("Revenue over $5M (Coterie's contractor limit).")
    if int(a.get("ft") or 0) + int(a.get("pt") or 0) > 15:
        out.append("More than 15 employees (Coterie's contractor limit).")
    if a.get("trade") == "paint_ext" and a.get("height") != "1-3":
        out.append("Exterior painting is limited to buildings under 3 stories.")
    if a.get("trade") not in IN_APPETITE:
        out.append("Trade isn't on Coterie's published contractor list. Confirm the class with Coterie.")
    return out


def _akhash(trade: str) -> str | None:
    try:
        return json.loads(get_settings().coterie_akhash or "{}").get(trade)
    except json.JSONDecodeError:
        return None


def build_quote_request(a: dict) -> dict:
    """Maps Bindswift answers to Coterie's GL quote fields (names exactly as documented)."""
    s = get_settings()
    occ, agg = (2_000_000, 4_000_000) if a.get("limits") == "2/4" else (1_000_000, 2_000_000)
    first, _, last = (a.get("contactName") or "").strip().partition(" ")
    akhash = _akhash(a.get("trade", ""))
    if not akhash:
        raise ValueError(f"No Coterie AKHash for trade '{a.get('trade')}'. Add it to COTERIE_AKHASH "
                         "(your Coterie Integration Manager provides these).")
    loc = {"street": a["street"], "city": a["city"], "state": "NY", "zip": a["zip"]}
    body = {
        "applicationTypes": ["GL"],
        "agencyExternalId": s.coterie_agency_external_id,
        "glLimit": occ,
        "glAggregateLimit": agg,
        "glAggregatePcoLimit": agg,
        "annualPayroll": round(float(a.get("payroll") or 0)),
        "grossAnnualSales": round(float(a.get("revenue") or 0)),
        "businessName": a["legalName"],
        "numEmployees": int(a.get("ft") or 0) + int(a.get("pt") or 0),
        "AKHash": akhash,
        "contactFirstName": first,
        "contactLastName": last or first,
        "contactEmail": a["email"],
        "contactPhone": a["phone"],
        "mailingAddressStreet": a["street"],
        "mailingAddressCity": a["city"],
        "mailingAddressState": "NY",
        "mailingAddressZip": a["zip"],
        "locations": [loc],
    }
    ais = [i for i in (a.get("interests") or []) if i.get("type") == "AdditionalInsured"] if a.get("gcReq") == "Y" else []
    if ais:
        body["endorsements"] = {"additionalInsureds": [
            # "City" is capitalized in Coterie's documented example
            {"name": i["name"], "street": i["street"], "City": i["city"], "state": i["state"], "zip": i["zip"],
             "description": "Additional insured per written contract"} for i in ais]}
    return body


def _find(d, *keys):
    """Pull a value out of Coterie's response without assuming exact nesting."""
    if isinstance(d, dict):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
        for v in d.values():
            r = _find(v, *keys)
            if r not in (None, ""):
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find(v, *keys)
            if r not in (None, ""):
                return r
    return None


def create_quote(a: dict, client: httpx.Client | None = None) -> dict:
    """Calls POST /quotes. Returns {ok, premium, quote_id, application_id, application_url, raw | error}."""
    s = get_settings()
    if not s.coterie_publishable_key:
        return {"ok": False, "error": "Coterie isn't connected yet: add COTERIE_PUBLISHABLE_KEY to your environment."}
    try:
        body = build_quote_request(a)
    except (ValueError, KeyError) as e:
        return {"ok": False, "error": str(e)}
    url = (s.coterie_base_url or BASES.get(s.coterie_env, BASES["sandbox"])) + "/quotes"
    own = client is None
    client = client or httpx.Client(timeout=25)
    try:
        r = client.post(url, json=body, headers={"Authorization": f"token {s.coterie_publishable_key}",
                                                 "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Couldn't reach Coterie: {type(e).__name__}"}
    finally:
        if own:
            client.close()
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:2000]}
    if r.status_code >= 400:
        msg = _find(data, "message", "error", "errors", "title") or f"HTTP {r.status_code}"
        return {"ok": False, "error": f"Coterie declined or rejected the request: {str(msg)[:300]}", "raw": data}
    premium = _find(data, "premium", "totalPremium", "annualPremium")
    return {"ok": True, "premium": premium,
            "quote_id": _find(data, "quoteId", "id"),
            "application_id": _find(data, "applicationId"),
            "application_url": _find(data, "applicationURL", "applicationUrl", "quoteProposalUrl"),
            "raw": data, "env": s.coterie_env}


def bind_quote(quote_id: str, payment_interval: str = "Annual", client: httpx.Client | None = None) -> dict:
    """POST .../{quoteId}/bind with the SECRET key, Agency Bill (no TokenizedPaymentId)."""
    s = get_settings()
    if not s.coterie_secret_key:
        return {"ok": False, "error": "Add COTERIE_SECRET_KEY to bind through the API, or bind in Coterie's dashboard and record it here."}
    if not quote_id:
        return {"ok": False, "error": "This quote has no Coterie quote ID."}
    path = (s.coterie_bind_path or "/{quote_id}/bind").format(quote_id=quote_id)
    url = (s.coterie_base_url or BASES.get(s.coterie_env, BASES["sandbox"])) + path
    body = {"paymentInterval": payment_interval, "agencyExternalId": s.coterie_agency_external_id}
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.post(url, json=body, headers={"Authorization": f"token {s.coterie_secret_key}",
                                                 "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Couldn't reach Coterie: {type(e).__name__}"}
    finally:
        if own:
            client.close()
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:2000]}
    if r.status_code >= 400:
        msg = _find(data, "message", "error", "errors", "title") or f"HTTP {r.status_code}"
        return {"ok": False, "error": f"Coterie didn't bind: {str(msg)[:300]}", "raw": data}
    return {"ok": True, "policy_number": _find(data, "policyNumber", "policyId", "policyNum"), "raw": data}
