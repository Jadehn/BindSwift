"""Contractor accounts and the signed-in dashboard.

Login is passwordless: the contractor enters their email, gets a 6-digit code (the same code system
the quote form uses), and gets a session cookie. A contractor sees only records whose contact email
is their verified email, so there's nothing to "claim" and no password to leak.
"""
import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from . import db, verify
from .config import get_settings
from .models import VerifyCheck
from .security import decrypt, ip_hash

router = APIRouter()
SESSION_DAYS = 14
COOKIE = "__Host-bs_client"
DEV_COOKIE = "bs_client_dev"

STATUS_TEXT = {
    "pending_review": "Received. Your broker is reviewing it",
    "approved": "Approved. Your broker is shopping carriers",
    "sent_to_market": "With insurance companies for quotes",
    "quoted": "Quote ready",
    "bind_requested": "Binding in progress",
    "bound": "Covered",
    "rejected": "Closed",
}
CLIENT_DOCS = {  # what a contractor may download from their own submission (never the editable copies)
    "summary": "Your application answers",
    "acord125": "ACORD 125 (commercial application)",
    "acord126": "ACORD 126 (general liability section)",
    "fee_agreement": "Bindswift service fee agreement",
}


def _cookie() -> str:
    return DEV_COOKIE if get_settings().is_dev else COOKIE


def _hash(t: str) -> bytes:
    return hashlib.sha256(t.encode()).digest()


def _same_origin(request: Request) -> bool:
    """Blocks cross-site form posts. Browsers always send Origin on POST from scripts."""
    s = get_settings()
    origin = request.headers.get("origin")
    if not origin:
        return False
    if s.public_base_url and origin.rstrip("/") == s.public_base_url.rstrip("/"):
        return True
    host = request.headers.get("host", "")
    return origin.split("://", 1)[-1].rstrip("/") == host


def client_email(request: Request) -> str | None:
    t = request.cookies.get(_cookie())
    if not t or len(t) > 100:
        return None
    row = db.one("""UPDATE client_sessions SET last_seen_at = now()
                     WHERE token_hash = %s AND expires_at > now() RETURNING email""", (_hash(t),))
    return str(row["email"]).lower() if row else None


def _unauth():
    return JSONResponse({"error": "Please log in."}, status_code=401)


@router.post("/api/auth/login")
async def login(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Bad origin"}, status_code=403)
    raw = await request.body()
    if len(raw) > 2000:
        return JSONResponse({"error": "Request too large"}, status_code=413)
    try:
        body = VerifyCheck.model_validate_json(raw)
    except ValidationError:
        return JSONResponse({"error": "Enter your email and the 6-digit code."}, status_code=422)
    email = str(body.email).lower()
    if not await run_in_threadpool(verify.check, email, body.code):
        return JSONResponse({"error": "That code isn't right or has expired."}, status_code=400)
    token = secrets.token_urlsafe(32)
    db.run("""INSERT INTO client_sessions (token_hash, email, expires_at, ip_hash, user_agent)
              VALUES (%s, %s, now() + make_interval(days => %s), %s, %s)""",
           (_hash(token), email, SESSION_DAYS, ip_hash(request), (request.headers.get("user-agent") or "")[:300]))
    db.run("DELETE FROM client_sessions WHERE expires_at < now()")
    resp = JSONResponse({"ok": True, "email": email})
    s = get_settings()
    resp.set_cookie(_cookie(), token, max_age=SESSION_DAYS * 86400, httponly=True, secure=not s.is_dev,
                    samesite="lax", path="/")
    return resp


@router.post("/api/auth/logout")
def logout(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Bad origin"}, status_code=403)
    t = request.cookies.get(_cookie())
    if t:
        db.run("DELETE FROM client_sessions WHERE token_hash = %s", (_hash(t),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_cookie(), path="/", secure=not get_settings().is_dev, samesite="lax")
    return resp


def _link(enc, prefix: str) -> str | None:
    if not enc:
        return None
    try:
        return f"{prefix}{decrypt(enc).decode()}"
    except Exception:
        return None


def _d(x):
    return x.isoformat() if isinstance(x, (date, datetime)) else x


@router.get("/api/me")
def me(request: Request):
    email = client_email(request)
    if not email:
        return _unauth()
    subs = db.all_("""SELECT id, public_id, legal_name, contact_name, trade, status, created_at, effective_date, limits,
                             answers->>'entity' AS entity, answers->>'regState' AS reg_state,
                             business_verification->>'status' AS reg_status, business_verification->>'registry_name' AS reg_name,
                             business_verification->>'dos_id' AS reg_id
                        FROM submissions WHERE contact_email = %s ORDER BY created_at DESC LIMIT 25""", (email,))
    ids = [r["id"] for r in subs] or [0]
    quotes = db.all_("""SELECT q.id, q.submission_id, q.carrier, q.premium, q.total_due, q.details->>'carrier' AS carrier_name,
                               q.limits_text, q.effective_date, q.offer_expires_at, q.sent_at, q.bind_requested_at,
                               q.bound_at, q.offer_token_enc
                          FROM carrier_quotes q WHERE q.submission_id = ANY(%s) AND q.sent_at IS NOT NULL
                         ORDER BY q.sent_at DESC""", (ids,))
    pols = db.all_("""SELECT p.id, p.submission_id, p.carrier, p.policy_number, p.effective_date, p.expiration_date,
                             p.limits, p.ready, p.status, p.portal_token_enc,
                             (SELECT count(*) FROM certificates c WHERE c.policy_id = p.id AND c.status = 'issued') AS n_certs
                        FROM policies p WHERE p.submission_id = ANY(%s) ORDER BY p.id DESC""", (ids,))
    docs = db.all_("""SELECT submission_id, kind, filename, created_at FROM submission_documents
                       WHERE submission_id = ANY(%s) AND kind = ANY(%s) ORDER BY created_at DESC""",
                   (ids, list(CLIENT_DOCS)))
    certs = db.all_("""SELECT c.id, c.number, c.holder_name, c.issued_at FROM certificates c JOIN policies p ON p.id = c.policy_id
                        WHERE p.submission_id = ANY(%s) AND c.status = 'issued' ORDER BY c.issued_at DESC LIMIT 20""", (ids,))
    pub = {r["id"]: str(r["public_id"]) for r in subs}
    now = datetime.now(timezone.utc)

    def qstatus(q):
        if q["bound_at"]:
            return "Bound"
        if q["bind_requested_at"]:
            return "Binding in progress"
        if q["offer_expires_at"] and q["offer_expires_at"] < now:
            return "Expired"
        return "Ready to review"

    out = {
        "email": email,
        "name": (subs[0]["contact_name"] if subs else email.split("@")[0]),
        "business": ({"legalName": subs[0]["legal_name"], "entity": subs[0]["entity"], "regState": subs[0]["reg_state"] or "NY",
                      "regStatus": subs[0]["reg_status"], "regName": subs[0]["reg_name"], "regId": subs[0]["reg_id"]}
                     if subs else None),
        "applications": [{"ref": pub[r["id"]], "business": r["legal_name"], "trade": r["trade"], "status": r["status"],
                          "statusText": STATUS_TEXT.get(r["status"], r["status"]), "submitted": _d(r["created_at"]),
                          "effective": _d(r["effective_date"])} for r in subs],
        "quotes": [{"id": q["id"], "carrier": q["carrier_name"] or q["carrier"].title(),
                    "amount": float(q["total_due"] or q["premium"] or 0) or None, "isTotal": bool(q["total_due"]),
                    "limits": q["limits_text"], "starts": _d(q["effective_date"]), "expires": _d(q["offer_expires_at"]),
                    "status": qstatus(q), "link": _link(q["offer_token_enc"], "/q/") if qstatus(q) != "Expired" else None,
                    "summaryUrl": f"/api/me/files/quote/{q['id']}"} for q in quotes],
        "policies": [{"id": p["id"], "carrier": p["carrier"], "number": p["policy_number"], "starts": _d(p["effective_date"]),
                      "ends": _d(p["expiration_date"]), "occurrence": (p["limits"] or {}).get("each_occurrence"),
                      "aggregate": (p["limits"] or {}).get("general_aggregate"), "status": p["status"],
                      "certificatesOn": bool(p["ready"] and p["status"] == "active"), "certificates": p["n_certs"],
                      "certLink": _link(p["portal_token_enc"], "/c/") if p["ready"] and p["status"] == "active" else None}
                     for p in pols],
        "documents": [{"title": CLIENT_DOCS[d["kind"]], "group": "Application", "date": _d(d["created_at"]),
                       "url": f"/api/me/files/sub/{pub[d['submission_id']]}/{d['kind']}"} for d in docs]
                     + [{"title": f"Quote summary: {q['carrier_name'] or q['carrier'].title()}", "group": "Quotes",
                         "date": _d(q["sent_at"]), "url": f"/api/me/files/quote/{q['id']}"} for q in quotes]
                     + [{"title": f"Certificate {c['number']}: {c['holder_name']}", "group": "Certificates",
                         "date": _d(c["issued_at"]), "url": f"/api/me/files/cert/{c['id']}"} for c in certs],
    }
    return out


def _pdf(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/api/me/files/sub/{ref}/{kind}")
def file_sub(request: Request, ref: str, kind: str):
    email = client_email(request)
    if not email:
        return _unauth()
    if kind not in CLIENT_DOCS or len(ref) > 40:
        return JSONResponse({"error": "Not found"}, status_code=404)
    row = db.one("""SELECT d.filename, d.content_enc, s.id FROM submission_documents d JOIN submissions s ON s.id = d.submission_id
                     WHERE s.public_id::text = %s AND s.contact_email = %s AND d.kind = %s
                     ORDER BY d.created_at DESC LIMIT 1""", (ref, email, kind))
    if not row:
        return JSONResponse({"error": "Not found"}, status_code=404)
    db.run("INSERT INTO audit_log (action, submission_id, ip_hash, detail) VALUES ('client_doc_download', %s, %s, %s::jsonb)",
           (row["id"], ip_hash(request), f'{{"kind": "{kind}"}}'))
    return _pdf(decrypt(row["content_enc"]), row["filename"])


@router.get("/api/me/files/quote/{qid}")
def file_quote(request: Request, qid: int):
    from .pdfs import quote_summary_pdf
    email = client_email(request)
    if not email:
        return _unauth()
    q = db.one("""SELECT q.*, s.legal_name FROM carrier_quotes q JOIN submissions s ON s.id = q.submission_id
                   WHERE q.id = %s AND s.contact_email = %s AND q.sent_at IS NOT NULL""", (qid, email))
    if not q:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _pdf(quote_summary_pdf(q, q["legal_name"], get_settings().fee_amount), "Bindswift_Quote_Summary.pdf")


@router.get("/api/me/files/cert/{cid}")
def file_cert(request: Request, cid: int):
    email = client_email(request)
    if not email:
        return _unauth()
    c = db.one("""SELECT c.number, c.pdf_enc FROM certificates c JOIN policies p ON p.id = c.policy_id
                   JOIN submissions s ON s.id = p.submission_id
                  WHERE c.id = %s AND s.contact_email = %s AND c.status = 'issued'""", (cid, email))
    if not c or not c["pdf_enc"]:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _pdf(decrypt(c["pdf_enc"]), f"COI_{c['number']}.pdf")
