"""Bindswift API + admin portal.

Run locally:  uvicorn app.main:app --reload
Production:   uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=<your proxy>
"""
import base64
import hashlib
import json
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
from .models import ProfileIn, SubmissionIn, VerifyCheck, VerifyStart
from . import appetite
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
_csp_cache: dict = {}


def frontend_csp() -> str:
    """Rebuilt whenever index.html changes, so editing the page never needs a server restart
    (a stale hash would block the page's scripts and leave it blank)."""
    path = settings.frontend_dir / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    if _csp_cache.get("mtime") != mtime:
        _csp_cache["mtime"] = mtime
        _csp_cache["csp"] = ("default-src 'self'; script-src 'self' " + _inline_script_hashes() + _TS + "; worker-src 'self' blob:; "
                             + ("frame-src 'self' https://challenges.cloudflare.com; " if _TS else "")
                             + "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                             "img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'")
    return _csp_cache["csp"]


# frame-ancestors 'self': the page's own phone preview shows the site in an iframe. No other site can frame it.
# Howard Analytics site, served at /howard-analytics/ (public/howard-analytics). It's a plain marketing
# page: Google Fonts, Unsplash photos, inline scripts and a Formspree form, so it gets its own policy.
SITE_PREFIX = "/howard-analytics"
SITE_DIR = settings.frontend_dir / "public" / "howard-analytics"
SITE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self' https://formspree.io; "
            "form-action 'self' https://formspree.io; base-uri 'none'; frame-ancestors 'none'")
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
    is_site = request.url.path == SITE_PREFIX or request.url.path.startswith(SITE_PREFIX + "/")
    h["Content-Security-Policy"] = ADMIN_CSP if is_admin else (SITE_CSP if is_site else frontend_csp())
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY" if (is_admin or is_site or request.url.path.startswith(("/q/", "/c/", "/api/"))) else "SAMEORIGIN"
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


@app.get(SITE_PREFIX, include_in_schema=False)
@app.get("/howardanalytics", include_in_schema=False)
@app.get("/howardanalytics/", include_in_schema=False)
@app.get("/howard", include_in_schema=False)
@app.get("/ha", include_in_schema=False)
@app.get("/HA", include_in_schema=False)
@app.get("/Ha", include_in_schema=False)
@app.get("/jay", include_in_schema=False)
@app.get("/Jay", include_in_schema=False)
def site_slash():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(SITE_PREFIX + "/", status_code=308)   # relative image paths need the slash


@app.get(SITE_PREFIX + "/{name:path}", include_in_schema=False)
def site_file(name: str = ""):
    """Howard Analytics site. On Vercel these files are served straight from public/ (no function);
    this route serves them locally. Only files that are in the folder, never a path outside it."""
    name = name or "index.html"
    files = {p.name: p for p in SITE_DIR.iterdir() if p.is_file()} if SITE_DIR.is_dir() else {}
    if name not in files:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(files[name], headers={"Cache-Control": "public, max-age=3600"})


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
    return {"live": True, "mode": "demo" if settings.site_mode == "demo" else "live",
            "emailVerification": settings.require_email_verification,
            "turnstileSiteKey": settings.turnstile_site_key or None, "businessLookup": True, "accounts": True,
            "classes": appetite.for_page(), "instant": appetite.hiscox_ready(),
            "eligibilityAI": bool(settings.anthropic_api_key)}


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
        if a.profileRef:   # the short profile this application continues is now replaced by it
            cur.execute("""UPDATE submissions SET status = 'superseded', review_note = %s
                            WHERE public_id::text = %s AND kind = 'profile' AND contact_email = %s
                              AND status IN ('pending_review', 'needs_application') RETURNING id""",
                        (f"Continued as full application #{sid}", a.profileRef, str(a.email)))
            prof = cur.fetchone()
            if prof:
                cur.execute("UPDATE submissions SET profile_id = %s WHERE id = %s", (prof["id"], sid))
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


# ---------------------------------------------------------------- short profile + instant quote

def _ticket(ref: str) -> str:
    """Lets the page that created a profile ask for its instant quote, without an account."""
    import hmac
    return hmac.new(verify._key("profile-ticket"), ref.encode(), hashlib.sha256).hexdigest()[:40]


@app.post("/api/profile")
async def create_profile(request: Request, bg: BackgroundTasks):
    """Short business profile: saved like a submission (kind='profile'), matched to carriers that
    can quote it automatically. The page then calls /instant-quote, or continues to the full
    application when nothing matches."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"error": "Expected JSON"}, status_code=415)
    raw = await request.body()
    if len(raw) > settings.max_body_bytes:
        return JSONResponse({"error": "Request too large"}, status_code=413)
    iph = ip_hash(request)
    if not submission_rate_ok(iph):
        return JSONResponse({"error": "Too many submissions. Please call us instead."}, status_code=429)
    try:
        body = ProfileIn.model_validate_json(raw)
    except ValidationError as e:
        fields = sorted({".".join(str(p) for p in err["loc"][1:2]) or "form" for err in e.errors()})[:12]
        msgs = [err["msg"].removeprefix("Value error, ") for err in e.errors() if err["type"] == "value_error"][:3]
        return JSONResponse({"error": " ".join(msgs) or "Some answers need fixing.", "fields": fields}, status_code=422)
    if body.website_url:
        return JSONResponse({"ok": True, "ref": "00000000-0000-0000-0000-000000000000", "route": "application"}, status_code=202)
    a = body.answers
    email_ok = verify.token_email(body.verification) == str(a.email).lower()
    if not email_ok:
        from .client import client_email
        email_ok = await run_in_threadpool(client_email, request) == str(a.email).lower()
    if settings.require_email_verification and not email_ok:
        return JSONResponse({"error": "Please verify your email address first.", "fields": ["email"]}, status_code=422)
    biz = await run_in_threadpool(verify.check_business, a.legalName, a.entity, None, None, "NY", "")
    answers = a.model_dump(mode="json")
    code = appetite.hiscox_code(a.trade, a.kind)
    answers["hiscoxClass"] = code or ""
    answers["kindLabel"] = appetite.kind_label(a.trade, a.kind)
    m = appetite.match(answers)
    label = TRADES[a.trade]["l"]

    with db.tx() as cur:
        cur.execute(
            """INSERT INTO submissions
                 (kind, status, legal_name, contact_name, contact_email, contact_phone, trade, class_code, county,
                  annual_revenue, annual_payroll, sub_cost, employees_ft, employees_pt, losses_5yr,
                  limits, effective_date, answers, acord125, acord126, ip_hash, email_verified,
                  business_verification, match)
               VALUES ('profile',%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,0,0,%s,%s,%s,%s::jsonb,'{}'::jsonb,'{}'::jsonb,%s,%s,
                       %s::jsonb,%s::jsonb)
               RETURNING id, public_id, created_at""",
            ("pending_review" if m["route"] == "instant" else "needs_application", a.legalName, a.contactName, str(a.email), a.phone, a.trade, code or "", a.county, a.revenue,
             int(a.claims), a.limits, a.effDate, _json(answers), iph, email_ok, _json(biz), _json(m)))
        row = cur.fetchone()
        sid, pub, created = row["id"], str(row["public_id"]), row["created_at"]
        fee_pdf = fee_agreement_pdf(a.legalName, agreement_text(a.legalName, created.date()), a.signature, created, pub)
        cur.execute("INSERT INTO submission_documents (submission_id, kind, filename, content_enc) VALUES (%s,'fee_agreement',%s,%s)",
                    (sid, f"Bindswift_{sid}_ServiceFeeAgreement.pdf", encrypt(fee_pdf)))
        cur.execute("""INSERT INTO audit_log (action, submission_id, ip_hash, detail)
                       VALUES ('profile_signed', %s, %s, %s::jsonb)""",
                    (sid, iph, _json({"signer": a.signature, "attested": True, "signed_at": created.isoformat(),
                                      "fee_agreement": FEE_VERSION, "route": m["route"],
                                      "user_agent": (request.headers.get("user-agent") or "")[:300]})))

    def notify():
        try:
            send_submission_email(sid, a.legalName, f"{label} ({answers['kindLabel']}) · short profile, "
                                  + ("instant quote running" if m["route"] == "instant" else "needs full application"),
                                  a.county, [(f"Bindswift_{sid}_ServiceFeeAgreement.pdf", fee_pdf)])
            db.run("UPDATE submissions SET email_sent_at = now(), email_error = NULL WHERE id = %s", (sid,))
        except Exception as e:
            log.exception("profile email failed for %s", sid)
            db.run("UPDATE submissions SET email_error = %s WHERE id = %s", (type(e).__name__ + ": " + str(e)[:300], sid))
    if settings.send_email_inline:
        await run_in_threadpool(notify)
    else:
        bg.add_task(notify)
    return JSONResponse({"ok": True, "ref": pub, "ticket": _ticket(pub), "route": m["route"],
                         "carriers": [c["name"] for c in m["carriers"]], "reasons": m["reasons"]}, status_code=201)


@app.post("/api/profile/{ref}/instant-quote")
async def instant_quote(request: Request, ref: str):
    """Runs the live carrier quote for a matched profile (about 30-90 seconds). Never binds."""
    import hmac
    raw = await _json_body(request, 500)
    try:
        ticket = json.loads(raw or b"{}").get("ticket", "")
    except ValueError:
        ticket = ""
    if not re.fullmatch(r"[0-9a-f-]{36}", ref) or not hmac.compare_digest(str(ticket), _ticket(ref)):
        return JSONResponse({"error": "Not found"}, status_code=404)
    row = await run_in_threadpool(db.one, """UPDATE submissions SET status = 'quoting'
                                             WHERE public_id::text = %s AND kind = 'profile' AND status = 'pending_review'
                                               AND match->>'route' = 'instant'
                                         RETURNING id, answers, effective_date, limits""", (ref,))
    if not row:   # already ran (or running): answer with what's there, so a reload is safe
        return await run_in_threadpool(_instant_status, ref)
    return await run_in_threadpool(_run_instant, row)


def _quote_out(q, token: str | None = None) -> dict:
    d = q["details"] or {}
    return {"ok": True, "carrier": d.get("carrier") or q["carrier"].title(), "premium": float(q["premium"] or 0),
            "monthly": d.get("monthly"), "quoteNumber": q["quote_id"], "limits": q["limits_text"],
            "link": f"/q/{token}" if token else None}


def _instant_status(ref: str) -> JSONResponse | dict:
    from .security import decrypt
    sub = db.one("SELECT id, status, match FROM submissions WHERE public_id::text = %s AND kind = 'profile'", (ref,))
    if not sub:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if sub["status"] == "quoting":
        return JSONResponse({"pending": True}, status_code=202)
    q = db.one("""SELECT * FROM carrier_quotes WHERE submission_id = %s AND ok AND offer_token_enc IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""", (sub["id"],))
    if q:
        return _quote_out(q, decrypt(q["offer_token_enc"]).decode())
    return {"ok": False, "route": "application", "reasons": (sub["match"] or {}).get("reasons") or []}


def _run_instant(row) -> dict:
    from datetime import timedelta
    from .integrations import hiscox
    from .offers import new_token
    sid, a = row["id"], dict(row["answers"])
    try:
        with db.tx() as cur:
            # one portal session at a time across every server instance
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('bindswift-hiscox'))")
            res = hiscox.create_quote(a)
    except Exception as e:   # never leave a profile stuck in 'quoting'
        log.exception("instant quote crashed for %s", sid)
        res = {"ok": False, "error": f"Instant quote failed: {type(e).__name__}"}
    raw = {k: res.get(k) for k in ("monthly", "amounts", "messages", "trail", "decision", "quote_number") if res.get(k) is not None}
    limits = "$2M per occurrence / $4M aggregate" if row["limits"] == "2/4" else "$1M per occurrence / $2M aggregate"
    if res.get("ok"):
        token, th = new_token()
        details = {"carrier": "Hiscox", "admitted": "Admitted", "coverage": "General Liability",
                   "quote_number": res.get("quote_number"), "monthly": res.get("monthly"),
                   "client_note": "Quoted online by Hiscox from your business profile. Your broker confirms the "
                                  "details with you before anything is bound."}
        with db.tx() as cur:
            cur.execute("""INSERT INTO carrier_quotes (submission_id, carrier, environment, ok, premium, quote_id, raw,
                                                       details, offer_token_hash, offer_token_enc, offer_expires_at,
                                                       sent_at, limits_text, effective_date, reviewed_at)
                           VALUES (%s,'hiscox','live',TRUE,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,now(),%s,%s,now())
                           RETURNING *""",
                        (sid, res["premium"], res.get("quote_number"), _json(raw), _json(details), th,
                         encrypt(token.encode()), datetime.now(timezone.utc) + timedelta(days=settings.offer_days),
                         limits, row["effective_date"]))
            q = cur.fetchone()
            cur.execute("UPDATE submissions SET status = 'quoted' WHERE id = %s", (sid,))
            cur.execute("INSERT INTO audit_log (action, submission_id, detail) VALUES ('instant_quote', %s, %s::jsonb)",
                        (sid, _json({"carrier": "hiscox", "ok": True, "premium": res["premium"]})))
        return _quote_out(q, token)
    reason = ("Hiscox couldn't offer coverage online for this business." if res.get("decision")
              else "The online quote didn't finish, so a broker will take it from here.")
    with db.tx() as cur:
        cur.execute("""INSERT INTO carrier_quotes (submission_id, carrier, environment, ok, error, raw)
                       VALUES (%s,'hiscox','live',FALSE,%s,%s::jsonb)""", (sid, res.get("error"), _json(raw)))
        cur.execute("""UPDATE submissions SET status = 'needs_application',
                              match = jsonb_set(coalesce(match, '{}'::jsonb), '{reasons}', %s::jsonb) WHERE id = %s""",
                    (_json([reason]), sid))
        cur.execute("INSERT INTO audit_log (action, submission_id, detail) VALUES ('instant_quote', %s, %s::jsonb)",
                    (sid, _json({"carrier": "hiscox", "ok": False, "error": (res.get("error") or "")[:300]})))
    return {"ok": False, "route": "application", "reasons": [reason]}


# ---------------------------------------------------------------- free eligibility check

@app.post("/api/eligibility")
async def eligibility(request: Request):
    """Free: a visitor describes their business; Claude picks the closest class, and the same
    appetite rules that route real profiles say whether it can be quoted online today."""
    raw = await _json_body(request, 2000)
    try:
        desc = str(json.loads(raw or b"{}").get("description", "")).strip()[:600]
    except ValueError:
        desc = ""
    if len(desc) < 8:
        return JSONResponse({"error": "Tell us a little more about the business."}, status_code=422)
    if await run_in_threadpool(_limited, "eligibility", ip_hash(request), 20):
        return JSONResponse({"error": "Too many checks. Try again in an hour."}, status_code=429)
    from .eligibility import check
    return await run_in_threadpool(check, desc)


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
    return json.dumps(obj, default=str)
