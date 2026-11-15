"""Bindswift API + admin portal.

Run locally:  uvicorn app.main:app --reload
Production:   uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=<your proxy>
"""
import base64
import hashlib
import logging
import re
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from . import db
from .acord_map import TRADES, build_acord
from .admin import router as admin_router
from .config import get_settings
from .mailer import send_submission_email
from .models import SubmissionIn, VerifyCheck, VerifyStart
from . import verify
from .mailer import send_code
from .fee import FEE_VERSION, agreement_text
from .pdfs import fee_agreement_pdf, fill_acord, summary_pdf
from .security import client_ip, encrypt, ip_hash, mask_fein, submission_rate_ok

log = logging.getLogger("bindswift")
settings = get_settings()

app = FastAPI(title="Bindswift", docs_url=None, redoc_url=None, openapi_url=None)  # no public API docs

def _inline_script_hashes() -> str:
    """CSP hashes for the page's own inline <script> blocks, so injected scripts can't run
    (no 'unsafe-inline' for scripts)."""
    html = (settings.frontend_dir / "index.html").read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"<script>(.*?)</script>", html, re.S):
        out.append("'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode() + "'")
    return " ".join(out)


_TS = " https://challenges.cloudflare.com" if settings.turnstile_site_key else ""
FRONTEND_CSP = ("default-src 'self'; script-src 'self' " + _inline_script_hashes() + _TS + "; worker-src 'self' blob:; "
                + ("frame-src https://challenges.cloudflare.com; " if _TS else "")
                + 
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                "img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
ADMIN_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
             "base-uri 'none'; frame-ancestors 'none'")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    limit = settings.max_upload_bytes if request.url.path.endswith("/quotes/upload") else settings.max_body_bytes
    if cl and cl.isdigit() and int(cl) > limit:
        return JSONResponse({"error": "Request too large"}, status_code=413)
    response: Response = await call_next(request)
    is_admin = request.url.path.startswith(settings.admin_path)
    h = response.headers
    h["Content-Security-Policy"] = ADMIN_CSP if is_admin else FRONTEND_CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    # /q/ pages: "same-origin" keeps the private quote link from leaking to other sites while still
    # letting the browser send a real Origin on the bind form (no-referrer would send "null").
    h["Referrer-Policy"] = ("no-referrer" if is_admin else
                            "same-origin" if request.url.path.startswith(("/q/", "/c/")) else "strict-origin-when-cross-origin")
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    if is_admin or request.url.path.startswith(("/api/", "/q/", "/c/")):
        h["Cache-Control"] = "no-store"
    if not settings.is_dev:
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
if hosts and hosts != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

app.include_router(admin_router, prefix=settings.admin_path)
from .offers import router as offers_router  # noqa: E402
app.include_router(offers_router)
from .cert_portal import router as cert_router  # noqa: E402
app.include_router(cert_router)
from .client import router as client_router  # noqa: E402
app.include_router(client_router)


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse(settings.frontend_dir / "index.html", media_type="text/html")


@app.get("/vendor/{name}", include_in_schema=False)
def vendor_file(name: str):
    """Self-hosted pdf.js (no third-party script CDN)."""
    if name not in ("pdf.min.js", "pdf.worker.min.js"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(settings.frontend_dir / "vendor" / name, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/forms/{name}", include_in_schema=False)
def blank_form(name: str):
    """Blank ACORD templates for the in-browser preview (no customer data)."""
    files = {"ACORD_125.pdf": settings.acord125_pdf, "ACORD_126.pdf": settings.acord126_pdf}
    if name not in files:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(files[name], media_type="application/pdf")


@app.get("/healthz", include_in_schema=False)
def health():
    db.one("SELECT 1 AS ok")
    return {"ok": True}


def _limited(bucket: str, iph: str, per_hour: int) -> bool:
    row = db.one("""SELECT count(*) AS n FROM api_hits
                     WHERE bucket = %s AND ip_hash = %s AND created_at > now() - interval '1 hour'""", (bucket, iph))
    if row["n"] >= per_hour:
        return True
    db.run("INSERT INTO api_hits (bucket, ip_hash) VALUES (%s, %s)", (bucket, iph))
    return False


async def _json_body(request: Request, limit: int = 4000):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return None
    raw = await request.body()
    return raw if len(raw) <= limit else None


@app.get("/api/config")
def public_config():
    """Tells the page it's running against the live backend, and which checks are on."""
    return {"live": True, "emailVerification": settings.require_email_verification,
            "turnstileSiteKey": settings.turnstile_site_key or None, "businessLookup": True, "accounts": True}


@app.post("/api/verify/start")
async def verify_start(request: Request):
    raw = await _json_body(request)
    if raw is None:
        return JSONResponse({"error": "Expected JSON"}, status_code=415)
    try:
        body = VerifyStart.model_validate_json(raw)
    except ValidationError:
        return JSONResponse({"error": "Enter a valid email address."}, status_code=422)
    if not await run_in_threadpool(verify.turnstile_ok, body.turnstile, client_ip(request)):
        return JSONResponse({"error": "Please complete the security check."}, status_code=400)
    ok, out = await run_in_threadpool(verify.start, str(body.email), ip_hash(request))
    if not ok:
        return JSONResponse({"error": out}, status_code=429 if out.startswith("Too many") else 422)
    try:
        await run_in_threadpool(send_code, str(body.email), out)
    except Exception:
        log.exception("verification email failed")
        return JSONResponse({"error": "We couldn't send the code. Check the email address and try again."}, status_code=502)
    return {"ok": True}


@app.post("/api/verify/check")
async def verify_check(request: Request):
    raw = await _json_body(request)
    if raw is None:
        return JSONResponse({"error": "Expected JSON"}, status_code=415)
    try:
        body = VerifyCheck.model_validate_json(raw)
    except ValidationError:
        return JSONResponse({"error": "Enter the 6-digit code."}, status_code=422)
    token = await run_in_threadpool(verify.check, str(body.email), body.code)
    if not token:
        return JSONResponse({"error": "That code isn't right or has expired."}, status_code=400)
    return {"ok": True, "token": token}


@app.get("/api/business/lookup")
async def business_lookup(request: Request, name: str = ""):
    name = name.strip()[:120]
    if len(name) < 3:
        return {"results": []}
    if await run_in_threadpool(_limited, "bizlookup", ip_hash(request), 60):
        return JSONResponse({"error": "Too many lookups. Try again later."}, status_code=429)
    try:
        rows = await run_in_threadpool(verify.lookup, name)
    except Exception:
        return JSONResponse({"error": "NY records are unavailable right now.", "results": []}, status_code=503)
    return {"results": rows, "source": "New York Department of State (data.ny.gov)"}


@app.post("/api/submissions")
async def create_submission(request: Request, bg: BackgroundTasks):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"error": "Expected JSON"}, status_code=415)
    raw = await request.body()
    if len(raw) > settings.max_body_bytes:
        return JSONResponse({"error": "Request too large"}, status_code=413)

    iph = ip_hash(request)
    if not submission_rate_ok(iph):
        return JSONResponse({"error": "Too many submissions. Please call us instead."}, status_code=429)

    try:
        body = SubmissionIn.model_validate_json(raw)
    except ValidationError as e:
        fields = sorted({".".join(str(p) for p in err["loc"][1:2]) or "form" for err in e.errors()})[:12]
        return JSONResponse({"error": "Some answers need fixing.", "fields": fields}, status_code=422)

    if body.website_url:  # honeypot tripped: pretend success, store nothing
        return JSONResponse({"ok": True, "ref": "00000000-0000-0000-0000-000000000000"}, status_code=202)

    a = body.answers
    email_ok = verify.token_email(body.verification) == str(a.email).lower()
    if not email_ok:  # a signed-in contractor's session counts as a verified email
        from .client import client_email
        email_ok = await run_in_threadpool(client_email, request) == str(a.email).lower()
    if settings.require_email_verification and not email_ok:
        return JSONResponse({"error": "Please verify your email address first.", "fields": ["email"]}, status_code=422)
    biz = await run_in_threadpool(verify.check_business, a.legalName, a.entity, a.dosId or None, None,
                                  a.regState, a.regId)
    if a.bizUnlisted and biz.get("status") == "not_found":
        biz["note"] += " Contractor said the business isn't listed (new, out-of-state, or a DBA)."
    fein = a.fein.replace("-", "")
    fein_fmt = f"{fein[:2]}-{fein[2:]}" if fein else ""
    answers = a.model_dump(mode="json", exclude={"fein"})
    built = build_acord(a, fein_fmt)                   # full FEIN: only goes into the encrypted PDFs
    f125, f126 = built["acord125"], built["acord126"]
    f125_store = {k: v for k, v in f125.items() if k != "ACORD_Policy_Insured1_FEINSSN"}  # JSONB copy: no FEIN
    f125_store["_yn125"] = built["yn125"]
    t = TRADES[a.trade]

    with db.tx() as cur:
        cur.execute(
            """INSERT INTO submissions
                 (legal_name, contact_name, contact_email, contact_phone, trade, class_code, county,
                  annual_revenue, annual_payroll, sub_cost, employees_ft, employees_pt, losses_5yr,
                  limits, effective_date, answers, acord125, acord126, fein_enc, ip_hash,
                  email_verified, business_verification)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb)
               RETURNING id, public_id, created_at""",
            (a.legalName, a.contactName, str(a.email), a.phone, a.trade, t["code"], a.county,
             a.revenue, a.payroll, a.subCost, a.ft, a.pt, int(a.losses), a.limits, a.effDate,
             _json(answers), _json(f125_store), _json(f126), encrypt(fein.encode()) if fein else None, iph,
             email_ok, _json(biz)),
        )
        row = cur.fetchone()
        sid, pub, created = row["id"], str(row["public_id"]), row["created_at"]
        docs = {
            "summary": (f"Bindswift_{sid}_answers.pdf", summary_pdf(sid, pub, answers, fein_fmt, created)),
            "acord125": (f"Bindswift_{sid}_ACORD125.pdf", fill_acord(settings.acord125_pdf, f125, built, "125")),
            "acord126": (f"Bindswift_{sid}_ACORD126.pdf", fill_acord(settings.acord126_pdf, f126, built, "126")),
            # editable copies (fields stay live, so you can correct a box in Acrobat before submitting)
            "fee_agreement": (f"Bindswift_{sid}_ServiceFeeAgreement.pdf",
                              fee_agreement_pdf(a.legalName, agreement_text(a.legalName, created.date()),
                                                a.signature, created, pub)),
            "acord125_fillable": (f"Bindswift_{sid}_ACORD125_fillable.pdf",
                                  fill_acord(settings.acord125_pdf, f125, built, "125", flatten=False)),
            "acord126_fillable": (f"Bindswift_{sid}_ACORD126_fillable.pdf",
                                  fill_acord(settings.acord126_pdf, f126, built, "126", flatten=False)),
        }
        for kind, (fname, data) in docs.items():
            cur.execute("INSERT INTO submission_documents (submission_id, kind, filename, content_enc) VALUES (%s,%s,%s,%s)",
                        (sid, kind, fname, encrypt(data)))
        # e-signature evidence: who signed, when, from what device (IP is stored only as a keyed hash)
        cur.execute("""INSERT INTO audit_log (action, submission_id, ip_hash, detail)
                       VALUES ('submission_signed', %s, %s, %s::jsonb)""",
                    (sid, iph, _json({"signer": a.signature, "attested": True, "signed_at": created.isoformat(),
                                      "fee_agreement": FEE_VERSION,
                                      "user_agent": (request.headers.get("user-agent") or "")[:300]})))

    if settings.send_email_inline:
        # Serverless (Vercel) can freeze the function right after the response, which would drop
        # background work, so send before responding.
        await run_in_threadpool(_email_broker, sid, pub, answers, a, created)
    else:
        bg.add_task(_email_broker, sid, pub, answers, a, created)
    return JSONResponse({"ok": True, "ref": pub, "status": "pending_review"}, status_code=201)


def _email_broker(sid: int, pub: str, answers: dict, a, created: datetime) -> None:
    """Runs after the response is sent. Emailed copies carry a masked FEIN."""
    try:
        masked = mask_fein(a.fein)
        m = build_acord(a, masked)
        attachments = [
            (f"Bindswift_{sid}_answers.pdf", summary_pdf(sid, pub, answers, masked, created)),
            (f"Bindswift_{sid}_ACORD125.pdf", fill_acord(settings.acord125_pdf, m["acord125"], m, "125")),
            (f"Bindswift_{sid}_ACORD126.pdf", fill_acord(settings.acord126_pdf, m["acord126"], m, "126")),
        ]
        send_submission_email(sid, a.legalName, TRADES[a.trade]["l"], a.county, attachments)
        db.run("UPDATE submissions SET email_sent_at = %s, email_error = NULL WHERE id = %s",
               (datetime.now(timezone.utc), sid))
    except Exception as e:  # never lose the submission because email failed
        log.exception("email failed for submission %s", sid)
        db.run("UPDATE submissions SET email_error = %s WHERE id = %s", (type(e).__name__ + ": " + str(e)[:300], sid))


def _json(obj) -> str:
    import json
    return json.dumps(obj, default=str)
