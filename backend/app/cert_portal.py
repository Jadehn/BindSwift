"""Contractor certificate portal: /c/{token}. A private link per bound policy, no password.

Certificates issue instantly when the request fits the policy on file. A request for additional
insured / primary & non-contributory / waiver of subrogation that the policy doesn't carry as a
blanket endorsement waits for your approval instead of going out wrong.
"""
import hashlib
import hmac
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import db
from .certs import certificate_pdf, needs_approval
from .config import get_settings
from .mailer import notify_cert_request, send_certificate
from .security import decrypt, encrypt, ip_hash
from .verify import _key

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
DAILY_LIMIT = 25


def _load(token: str):
    if not token or len(token) > 100:
        return None
    return db.one("SELECT * FROM policies WHERE portal_token_hash = %s AND ready",
                  (hashlib.sha256(token.encode()).digest(),))


def _nonce(token: str) -> str:
    return hmac.new(_key("cert-form"), token.encode(), hashlib.sha256).hexdigest()[:32]


def _usable(p) -> str | None:
    if p["status"] != "active":
        return "This policy isn't active, so new certificates can't be issued. Contact your broker."
    if p["expiration_date"] and p["expiration_date"] < date.today():
        return "This policy has expired. Contact your broker about renewal."
    return None


def _page(request, p, token, **extra):
    certs = db.all_("""SELECT id, number, holder_name, project, want_ai, status, created_at FROM certificates
                        WHERE policy_id = %s ORDER BY created_at DESC LIMIT 50""", (p["id"],))
    return templates.TemplateResponse(request, "certs.html", {
        "p": p, "token": token, "certs": certs, "nonce": _nonce(token), "blocked": _usable(p), **extra})


@router.get("/c/{token}", response_class=HTMLResponse)
def portal(request: Request, token: str):
    p = _load(token)
    if not p:
        return HTMLResponse("<h1>Link not found</h1><p>Ask your broker for a new certificate link.</p>", status_code=404)
    return _page(request, p, token)


@router.post("/c/{token}", response_class=HTMLResponse)
def issue(request: Request, token: str, n: str = Form("", max_length=64),
          holder_name: str = Form("", max_length=120), holder_street: str = Form("", max_length=120),
          holder_city: str = Form("", max_length=60), holder_state: str = Form("NY", max_length=2),
          holder_zip: str = Form("", max_length=10), holder_email: str = Form("", max_length=254),
          project: str = Form("", max_length=200), ai: str = Form(""), pnc: str = Form(""), wos: str = Form("")):
    p = _load(token)
    if not p:
        return JSONResponse({"error": "Not found"}, status_code=404)
    s = get_settings()
    origin = request.headers.get("origin")
    if origin and s.public_base_url and origin.rstrip("/") != s.public_base_url.rstrip("/") and not s.is_dev:
        return JSONResponse({"error": "Bad origin"}, status_code=403)
    if not hmac.compare_digest(n, _nonce(token)):
        return JSONResponse({"error": "Bad form"}, status_code=403)
    if (msg := _usable(p)):
        return _page(request, p, token, error=msg)
    holder_name, holder_street, holder_city = holder_name.strip(), holder_street.strip(), holder_city.strip()
    if not (holder_name and holder_street and holder_city and holder_zip.strip()):
        return _page(request, p, token, error="Enter the certificate holder's name and full address.")
    if holder_email and ("@" not in holder_email or "." not in holder_email.split("@")[-1]):
        return _page(request, p, token, error="That email address doesn't look right.")
    used = db.one("SELECT count(*) AS n FROM certificates WHERE policy_id = %s AND created_at > now() - interval '1 day'", (p["id"],))
    if used["n"] >= DAILY_LIMIT:
        return _page(request, p, token, error="Daily certificate limit reached. Contact your broker if you need more today.")
    want = dict(want_ai=ai == "1", want_pnc=pnc == "1", want_wos=wos == "1")
    needs = needs_approval(p, want["want_ai"], want["want_pnc"], want["want_wos"])
    addr = f"{holder_street}, {holder_city}, {holder_state.upper()[:2]} {holder_zip.strip()}"
    row = db.one("""INSERT INTO certificates (policy_id, holder_name, holder_address, holder_email, project, want_ai, want_pnc,
                                              want_wos, status, ip_hash)
                    VALUES (%s,%s,%s,NULLIF(%s,''),NULLIF(%s,''),%s,%s,%s,%s,%s) RETURNING id""",
                 (p["id"], holder_name, addr, holder_email.strip(), project.strip(), want["want_ai"], want["want_pnc"],
                  want["want_wos"], "pending_approval" if needs else "issued", ip_hash(request)))
    number = f"BSW-{p['id']:04d}-{row['id']:05d}"
    db.run("UPDATE certificates SET number = %s WHERE id = %s", (number, row["id"]))
    db.run("INSERT INTO audit_log (action, submission_id, detail, ip_hash) VALUES (%s, %s, %s::jsonb, %s)",
           ("cert_requested" if needs else "cert_issued", p["submission_id"],
            f'{{"policy": {p["id"]}, "cert": {row["id"]}}}', ip_hash(request)))
    if needs:
        try:
            notify_cert_request(p["submission_id"], p["id"], p["insured_name"], holder_name, needs)
        except Exception:
            pass
        return _page(request, p, token, pending=", ".join(needs), holder=holder_name)
    c = {"holder_name": holder_name, "holder_address": addr, "project": project.strip(), **want}
    pdf = certificate_pdf(p, c, number)
    db.run("UPDATE certificates SET pdf_enc = %s, issued_at = now() WHERE id = %s", (encrypt(pdf), row["id"]))
    if holder_email.strip():
        try:
            send_certificate(holder_email.strip(), p["insured_name"], holder_name, pdf, number)
        except Exception:
            pass
    return _page(request, p, token, issued=row["id"], holder=holder_name, emailed=bool(holder_email.strip()))


@router.get("/c/{token}/{cid}.pdf")
def download(request: Request, token: str, cid: int):
    p = _load(token)
    if not p:
        return JSONResponse({"error": "Not found"}, status_code=404)
    c = db.one("SELECT number, pdf_enc FROM certificates WHERE id = %s AND policy_id = %s AND status = 'issued'", (cid, p["id"]))
    if not c or not c["pdf_enc"]:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(decrypt(c["pdf_enc"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="COI_{c["number"]}.pdf"', "Cache-Control": "no-store"})
