"""Contractor-facing quote page: you send a quote, they review it and press Bind.

The link carries a random 256-bit token (only its SHA-256 is stored), expires after OFFER_DAYS,
and is the contractor's only way in, so no account or password is needed. Pressing Bind records
their confirmation and notifies you. Nothing is bound and no money moves here: you collect the
$50 fee yourself, record it in the admin page, then bind.
"""
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import db
from .config import get_settings
from .security import ip_hash
from .verify import _key

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def new_token() -> tuple[str, bytes]:
    t = secrets.token_urlsafe(32)
    return t, hashlib.sha256(t.encode()).digest()


def _nonce(token: str) -> str:
    return hmac.new(_key("offer-form"), token.encode(), hashlib.sha256).hexdigest()[:32]


def _load(token: str):
    if not token or len(token) > 100:
        return None
    return db.one(
        """SELECT q.*, s.legal_name, s.contact_name, s.contact_email, s.answers, s.id AS sid, s.status AS sub_status
             FROM carrier_quotes q JOIN submissions s ON s.id = q.submission_id
            WHERE q.offer_token_hash = %s""", (hashlib.sha256(token.encode()).digest(),))


def _expired(q) -> bool:
    import time
    return q["offer_expires_at"] is not None and q["offer_expires_at"].timestamp() < time.time()


def _page(request, q, token, **extra):
    s = get_settings()
    expired = _expired(q)
    return templates.TemplateResponse(request, "quote.html", {
        "q": q, "token": token, "nonce": _nonce(token), "fee": s.fee_amount, "expired": expired,
        "carrier": {"coterie": "Coterie"}.get(q["carrier"], q["carrier"].title()), **extra})


@router.get("/q/{token}", response_class=HTMLResponse)
def view_offer(request: Request, token: str):
    q = _load(token)
    if not q:
        return HTMLResponse("<h1>Link not found</h1><p>This quote link is invalid. Contact your broker.</p>", status_code=404)
    db.run("INSERT INTO audit_log (action, submission_id, ip_hash) VALUES ('quote_viewed', %s, %s)", (q["sid"], ip_hash(request)))
    return _page(request, q, token)


@router.post("/q/{token}/bind")
def request_bind(request: Request, token: str, n: str = Form("", max_length=64), signer: str = Form("", max_length=80),
                 confirm: str = Form("", max_length=5)):
    q = _load(token)
    if not q:
        return JSONResponse({"error": "Not found"}, status_code=404)
    host = request.headers.get("host", "")
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not hmac.compare_digest(n, _nonce(token)) or (origin and host not in origin):
        return JSONResponse({"error": "Invalid form"}, status_code=403)
    if q["bind_requested_at"]:
        return _page(request, q, token, done=True)
    if confirm != "yes" or len(signer.strip().split()) < 2:
        return _page(request, q, token, error="Check the box and type your full name to request binding.")
    if _expired(q):
        return _page(request, q, token, error="This quote has expired. Contact your broker for a new one.")
    detail = {"signer": signer.strip(), "confirmed_no_changes": True,
              "user_agent": (request.headers.get("user-agent") or "")[:300]}
    with db.tx() as cur:
        cur.execute("UPDATE carrier_quotes SET bind_requested_at = now(), bind_request = %s::jsonb WHERE id = %s",
                    (json.dumps(detail), q["id"]))
        cur.execute("UPDATE submissions SET status = 'bind_requested' WHERE id = %s", (q["sid"],))
        cur.execute("""INSERT INTO audit_log (action, submission_id, ip_hash, detail)
                       VALUES ('bind_requested', %s, %s, %s::jsonb)""", (q["sid"], ip_hash(request), json.dumps(detail)))
    try:
        from .mailer import notify_bind_request
        notify_bind_request(q["sid"], q["legal_name"], q["carrier"], float(q["premium"] or 0))
    except Exception:
        pass  # the request is saved; the admin queue shows it either way
    return _page(request, _load(token), token, done=True)



def _pdf_response(data: bytes, filename: str):
    from fastapi.responses import Response
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/q/{token}/summary.pdf")
def offer_summary(request: Request, token: str):
    from .pdfs import quote_summary_pdf
    q = _load(token)
    if not q:
        return JSONResponse({"error": "Not found"}, status_code=404)
    data = quote_summary_pdf(q, q["legal_name"], get_settings().fee_amount)
    db.run("INSERT INTO audit_log (action, submission_id, ip_hash) VALUES ('quote_summary_downloaded', %s, %s)",
           (q["sid"], ip_hash(request)))
    return _pdf_response(data, "Bindswift_Quote_Summary.pdf")


@router.get("/q/{token}/quote.pdf")
def offer_original(request: Request, token: str):
    from .security import decrypt
    q = _load(token)
    if not q or not q["share_original"] or not q["source_pdf_enc"]:
        return JSONResponse({"error": "Not found"}, status_code=404)
    db.run("INSERT INTO audit_log (action, submission_id, ip_hash) VALUES ('quote_pdf_downloaded', %s, %s)",
           (q["sid"], ip_hash(request)))
    return _pdf_response(decrypt(q["source_pdf_enc"]), "Insurer_Quote.pdf")
