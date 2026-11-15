"""Anti-spam and business verification.

1) Email verification: a 6-digit code is emailed to the contractor; submitting requires a short-lived
   signed token proving they own the email on the application. Codes are stored only as HMACs, expire
   in 10 minutes, allow 5 tries, and are rate-limited per email and per IP. Throwaway-email domains
   are refused. Optional Cloudflare Turnstile (invisible bot check) if TURNSTILE_SECRET is set.

2) Business verification: looks the business up in New York Department of State records
   (data.ny.gov "Active Corporations", dataset n9v6-gdp6, free public API) and checks the
   legal name and entity type match what the contractor entered.
"""
import base64
import hashlib
import hmac
import re
import secrets
import time

import httpx

from . import db
from .config import get_settings

# ------------------------------------------------------------------ email verification
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com", "10minutemail.com",
    "10minutemail.net", "tempmail.com", "temp-mail.org", "temp-mail.io", "yopmail.com", "yopmail.net",
    "trashmail.com", "getnada.com", "nada.email", "dispostable.com", "maildrop.cc", "mintemail.com",
    "throwawaymail.com", "fakeinbox.com", "tempr.email", "discard.email", "mohmal.com", "emailondeck.com",
    "spamgourmet.com", "mailnesia.com", "mytemp.email", "tempinbox.com", "burnermail.io", "moakt.com",
    "mailpoof.com", "tmpmail.org", "tmail.ws", "inboxkitten.com", "getairmail.com", "33mail.com",
}
CODE_TTL_MIN = 10
TOKEN_TTL_SEC = 3 * 3600


def _key(purpose: str) -> bytes:
    return hmac.new(get_settings().data_key.encode(), purpose.encode(), hashlib.sha256).digest()


def _code_hash(email: str, code: str) -> bytes:
    return hmac.new(_key("email-code"), f"{email.lower()}|{code}".encode(), hashlib.sha256).digest()


def is_disposable(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower().strip()
    return domain in DISPOSABLE or any(domain.endswith("." + d) for d in DISPOSABLE)


def start(email: str, iphash: str) -> tuple[bool, str | None]:
    """Creates and returns a code to send, or (False, reason). Caller emails it."""
    email = email.strip().lower()
    if is_disposable(email):
        return False, "Please use a permanent email address. Temporary inboxes can't receive your policy."
    row = db.one("""SELECT
                      count(*) FILTER (WHERE email = %(e)s AND created_at > now() - interval '15 minutes') AS by_email,
                      count(*) FILTER (WHERE ip_hash = %(ip)s AND created_at > now() - interval '1 hour')   AS by_ip
                    FROM email_verifications""", {"e": email, "ip": iphash})
    if row["by_email"] >= 3 or row["by_ip"] >= 10:
        return False, "Too many codes requested. Wait a few minutes and try again."
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.run("""INSERT INTO email_verifications (email, code_hash, ip_hash, expires_at)
              VALUES (%s, %s, %s, now() + make_interval(mins => %s))""",
           (email, _code_hash(email, code), iphash, CODE_TTL_MIN))
    return True, code


def check(email: str, code: str) -> str | None:
    """Returns a signed verification token if the code is right, else None."""
    email = email.strip().lower()
    code = re.sub(r"\D", "", code or "")
    with db.tx() as cur:
        cur.execute("""SELECT id, code_hash, attempts FROM email_verifications
                        WHERE email = %s AND used_at IS NULL AND expires_at > now()
                        ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (email,))
        row = cur.fetchone()
        if not row or row["attempts"] >= 5 or len(code) != 6:
            if row:
                cur.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE id = %s", (row["id"],))
            return None
        if not hmac.compare_digest(bytes(row["code_hash"]), _code_hash(email, code)):
            cur.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE id = %s", (row["id"],))
            return None
        cur.execute("UPDATE email_verifications SET used_at = now() WHERE id = %s", (row["id"],))
    exp = int(time.time()) + TOKEN_TTL_SEC
    payload = f"{email}|{exp}"
    sig = hmac.new(_key("email-token"), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def token_email(token: str) -> str | None:
    """The verified email inside a valid, unexpired token, else None."""
    try:
        email, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 2)
    except Exception:
        return None
    good = hmac.new(_key("email-token"), f"{email}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig) or int(exp) < time.time():
        return None
    return email


def turnstile_ok(token: str | None, ip: str) -> bool:
    secret = get_settings().turnstile_secret
    if not secret:
        return True
    if not token:
        return False
    try:
        r = httpx.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                       data={"secret": secret, "response": token, "remoteip": ip}, timeout=8)
        return bool(r.json().get("success"))
    except Exception:
        return False


# ------------------------------------------------------------------ business verification
NY_DATASET = "https://data.ny.gov/resource/n9v6-gdp6.json"
SUFFIXES = r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|LLC|L L C|PLLC|LLP|LP|THE)\b"
ENTITY_WORDS = {
    "LLC": ["LIMITED LIABILITY COMPANY"],
    "Corporation": ["BUSINESS CORPORATION"],
    "SCorp": ["BUSINESS CORPORATION"],          # S status is a federal tax election, not a state record
    "Partnership": ["LIMITED PARTNERSHIP", "LIMITED LIABILITY PARTNERSHIP"],
    "JointVenture": [],
    "Individual": [],
}


def norm(name: str) -> str:
    n = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper().replace("&", " AND "))
    n = re.sub(SUFFIXES, " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _row(r: dict) -> dict:
    return {"dosId": str(r.get("dos_id", "")), "name": r.get("current_entity_name", ""),
            "type": r.get("entity_type") or r.get("entitytype") or "", "filed": (r.get("initial_dos_filing_date") or "")[:10],
            "county": r.get("county", ""), "jurisdiction": r.get("jurisdiction", "")}


def _get(params: dict, client: httpx.Client | None = None) -> list[dict]:
    s = get_settings()
    headers = {"X-App-Token": s.socrata_app_token} if s.socrata_app_token else {}
    own = client is None
    client = client or httpx.Client(timeout=8)
    try:
        r = client.get(s.ny_entities_url or NY_DATASET, params=params, headers=headers)
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    finally:
        if own:
            client.close()


def lookup(name: str, client: httpx.Client | None = None) -> list[dict]:
    """Up to 6 best NY DOS matches for a business name."""
    q = norm(name)
    if len(q) < 3:
        return []
    rows = [_row(r) for r in _get({"$q": q, "$limit": 25}, client)]
    qn = norm(name)

    def score(r):
        rn = norm(r["name"])
        return (rn == qn, rn.startswith(qn) or qn.startswith(rn), -abs(len(rn) - len(qn)))
    return sorted(rows, key=score, reverse=True)[:6]


# Where a business can be registered (your five licensed states) and what each free public record shows.
REGISTRIES = {
    "NY": {"name": "New York Department of State", "url": "https://apps.dos.ny.gov/publicInquiry/",
           "id_label": "DOS ID", "live": True, "status_shown": True},
    "NJ": {"name": "NJ Division of Revenue and Enterprise Services", "url": "https://www.njportal.com/DOR/BusinessNameSearch/",
           "id_label": "Entity ID (10 digits)", "live": False, "status_shown": False},
    "NC": {"name": "NC Secretary of State", "url": "https://www.sosnc.gov/online_services/search/by_title/_Business_Registration",
           "id_label": "SOSID", "live": False, "status_shown": True},
    "DE": {"name": "Delaware Division of Corporations", "url": "https://icis.corp.delaware.gov/Ecorp/EntitySearch/NameSearch.aspx",
           "id_label": "File number", "live": False, "status_shown": False},
    "MD": {"name": "Maryland SDAT (Maryland Business Express)", "url": "https://egov.maryland.gov/businessexpress/entitysearch",
           "id_label": "Department ID", "live": False, "status_shown": True},
}


def check_business(entered_name: str, entity: str, dos_id: str | None,
                   client: httpx.Client | None = None, reg_state: str = "NY", reg_id: str = "") -> dict:
    """Server-side check at submission. Never blocks the submission: the broker sees the result."""
    if reg_state and reg_state != "NY" and entity not in ("Individual", "JointVenture"):
        reg = REGISTRIES[reg_state]
        out = {"status": "manual_check", "reg_state": reg_state, "reg_id": reg_id, "registry_url": reg["url"],
               "note": f"Registered in {reg_state}: confirm {reg['id_label']} {reg_id or '(not given)'} at {reg['name']}."}
        try:  # a business formed elsewhere that works in NY should also be on file in NY as a foreign entity
            rows = lookup(entered_name, client)
            hit = next((r for r in rows if norm(r["name"]) == norm(entered_name)), None)
        except Exception:
            hit = None
        if hit:
            out.update(dos_id=hit["dosId"], registry_name=hit["name"], registry_type=hit["type"], filed=hit["filed"],
                       jurisdiction=hit["jurisdiction"])
            out["note"] += f" Also on file in NY (DOS ID {hit['dosId']}, jurisdiction {hit['jurisdiction'] or 'not listed'})."
        else:
            out["note"] += " No NY foreign registration found under this name; ask if they work in NY."
        return out
    if entity in ("Individual", "JointVenture"):
        return {"status": "not_applicable", "note": "Sole proprietors and joint ventures aren't in the NY DOS entity database."}
    try:
        if dos_id:
            rows = [_row(r) for r in _get({"dos_id": re.sub(r"\D", "", dos_id)}, client)]
        else:
            rows = lookup(entered_name, client)
    except Exception as e:
        return {"status": "unavailable", "note": f"NY records lookup failed ({type(e).__name__}). Check manually."}
    if not rows:
        return {"status": "not_found", "note": "No active NY entity found under this name."}
    best = rows[0]
    out = {"dos_id": best["dosId"], "registry_name": best["name"], "registry_type": best["type"],
           "filed": best["filed"], "jurisdiction": best["jurisdiction"]}
    name_ok = best["name"].strip().upper() == entered_name.strip().upper()
    close = norm(best["name"]) == norm(entered_name)
    wanted = ENTITY_WORDS.get(entity, [])
    type_ok = not wanted or not best["type"] or any(w in best["type"].upper() for w in wanted)
    if not close:
        out.update(status="not_found", note="Closest NY record has a different name.")
    elif not type_ok:
        out.update(status="type_mismatch", note=f"Entered {entity}; NY records show {best['type'].title()}.")
    elif not name_ok:
        out.update(status="name_variation", note="Same business, but the name isn't written exactly as registered.")
    else:
        out.update(status="verified", note="Name and entity type match NY Department of State records.")
    if entity == "SCorp" and type_ok:
        out["note"] += " S-corp status is a federal tax election: confirm with the IRS acceptance letter (CP261)."
    return out
