"""Admin portal: login with password + authenticator code, review queue,
approve/reject, encrypted document download, and an export for your raters."""
import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db
from .acord_map import TRADES
from .integrations import coterie, next_ins
from .config import get_settings
from .security import encrypt
from .security import (check_password, check_totp, clear_session_cookie, client_ip, create_session,
                       current_admin, decrypt, destroy_session, ip_hash, mask_fein, record_attempt,
                       require_csrf, set_session_cookie, too_many_failures)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescape on by default
templates.env.globals["TRADES"] = TRADES
LOGIN_CSRF = "cc_login_csrf"
STATUSES = ["pending_review", "approved", "quoted", "bind_requested", "bound", "sent_to_market", "rejected"]


def ip_allowed(request: Request):
    allow = get_settings().allowlist
    if allow and client_ip(request) not in allow:
        raise HTTPException(status_code=404)  # don't reveal that an admin area exists


def require_admin(request: Request, _=Depends(ip_allowed)) -> dict:
    admin = current_admin(request)
    if not admin:
        raise HTTPException(status_code=303, headers={"Location": get_settings().admin_path + "/login"})
    return admin


def audit(admin_id, action, sid=None, detail=None, request: Request | None = None):
    db.run("INSERT INTO audit_log (admin_id, action, submission_id, detail, ip_hash) VALUES (%s,%s,%s,%s::jsonb,%s)",
           (admin_id, action, sid, json.dumps(detail or {}), ip_hash(request) if request else None))


def render(request, name, **ctx):
    ctx.setdefault("base", get_settings().admin_path)
    return templates.TemplateResponse(request, name, ctx)


# ---------------- login / logout ----------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, _=Depends(ip_allowed)):
    token = secrets.token_urlsafe(32)
    resp = render(request, "login.html", csrf=token, error=None)
    resp.set_cookie(LOGIN_CSRF, token, httponly=True, secure=not get_settings().is_dev, samesite="strict",
                    path=get_settings().admin_path, max_age=900)
    return resp


@router.post("/login")
def login(request: Request, email: str = Form(max_length=254), password: str = Form(max_length=256),
          code: str = Form(max_length=10), csrf: str = Form(max_length=100), _=Depends(ip_allowed)):
    s = get_settings()
    cookie = request.cookies.get(LOGIN_CSRF) or ""
    if not cookie or not secrets.compare_digest(cookie, csrf):
        raise HTTPException(status_code=403, detail="Login form expired. Reload the page.")
    email = email.strip().lower()
    iph = ip_hash(request)

    def fail(reason: str, msg="Email, password or code is incorrect."):
        record_attempt(email, iph, False, reason)
        token = secrets.token_urlsafe(32)
        resp = render(request, "login.html", csrf=token, error=msg)
        resp.status_code = 401 if reason != "locked" else 429
        resp.set_cookie(LOGIN_CSRF, token, httponly=True, secure=not s.is_dev, samesite="strict",
                        path=s.admin_path, max_age=900)
        return resp

    if too_many_failures(email, iph):
        return fail("locked", f"Too many attempts. Try again in {s.login_window_minutes} minutes.")
    admin = db.one("SELECT * FROM admins WHERE email = %s AND is_active", (email,))
    if not check_password(admin["password_hash"] if admin else None, password):
        return fail("bad_password")
    if not check_totp(admin, code):
        return fail("bad_totp")

    record_attempt(email, iph, True, "ok")
    token, _csrf = create_session(admin["id"], request)
    audit(admin["id"], "login", request=request)
    resp = RedirectResponse(s.admin_path + "/submissions", status_code=303)
    set_session_cookie(resp, token)
    resp.delete_cookie(LOGIN_CSRF, path=s.admin_path)
    return resp


@router.post("/logout")
def logout(request: Request, csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    require_csrf(admin, csrf)
    destroy_session(request)
    audit(admin["id"], "logout", request=request)
    resp = RedirectResponse(get_settings().admin_path + "/login", status_code=303)
    clear_session_cookie(resp)
    return resp


# ---------------- queue ----------------
@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def home(_=Depends(require_admin)):
    return RedirectResponse(get_settings().admin_path + "/submissions", status_code=303)


@router.get("/submissions", response_class=HTMLResponse)
def list_submissions(request: Request, status: str = "pending_review", admin=Depends(require_admin)):
    if status not in STATUSES + ["all"]:
        status = "pending_review"
    rows = db.all_(
        """SELECT id, legal_name, trade, class_code, county, annual_payroll, losses_5yr, limits,
                  effective_date, status, email_sent_at, email_error, created_at
             FROM submissions
            WHERE (%(st)s = 'all' OR status = %(st)s)
            ORDER BY created_at DESC LIMIT 200""",
        {"st": status},
    )
    counts = {r["status"]: r["n"] for r in db.all_("SELECT status, count(*) AS n FROM submissions GROUP BY status")}
    return render(request, "list.html", admin=admin, rows=rows, status=status, statuses=STATUSES, counts=counts)


@router.get("/submissions/{sid}", response_class=HTMLResponse)
def view_submission(request: Request, sid: int, admin=Depends(require_admin)):
    sub = db.one("SELECT * FROM submissions WHERE id = %s", (sid,))
    if not sub:
        raise HTTPException(status_code=404)
    docs = db.all_("SELECT kind, filename, created_at FROM submission_documents WHERE submission_id = %s ORDER BY kind", (sid,))
    log = db.all_(
        """SELECT l.action, l.detail, l.created_at, a.email
             FROM audit_log l LEFT JOIN admins a ON a.id = l.admin_id
            WHERE l.submission_id = %s ORDER BY l.created_at""", (sid,))
    fein_masked = mask_fein(decrypt(sub["fein_enc"]).decode()) if sub["fein_enc"] else "Not provided"
    quotes = db.all_("""SELECT * FROM carrier_quotes WHERE submission_id = %s ORDER BY created_at DESC""", (sid,))
    carriers = {"coterie": {"name": "Coterie", "issues": coterie.eligibility(sub["answers"]),
                            "connected": bool(get_settings().coterie_publishable_key),
                            "env": get_settings().coterie_env},
                "next": {"name": "NEXT", "issues": next_ins.eligibility(sub["answers"]),
                         "sheet": next_ins.answer_sheet(sub["answers"])}}
    audit(admin["id"], "view_submission", sid, request=request)
    policies = db.all_("""SELECT p.*, (SELECT count(*) FROM certificates c WHERE c.policy_id = p.id) AS n_certs,
                                 (SELECT count(*) FROM certificates c WHERE c.policy_id = p.id AND c.status = 'pending_approval') AS n_pending
                            FROM policies p WHERE submission_id = %s ORDER BY id""", (sid,))
    return render(request, "detail.html", admin=admin, s=sub, docs=docs, log=log, fein=fein_masked,
                  statuses=STATUSES, carriers=carriers, quotes=quotes, policies=policies)


@router.post("/submissions/{sid}/carriers/coterie/quote")
def coterie_quote(request: Request, sid: int, csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    require_csrf(admin, csrf)
    sub = db.one("SELECT status, answers FROM submissions WHERE id = %s", (sid,))
    if not sub:
        raise HTTPException(status_code=404)
    if sub["status"] not in ("approved", "sent_to_market", "quoted"):
        raise HTTPException(status_code=409, detail="Approve the submission before sending it to a carrier.")
    res = coterie.create_quote(sub["answers"])
    premium = res.get("premium")
    try:
        premium = float(str(premium).replace("$", "").replace(",", "")) if premium is not None else None
    except ValueError:
        premium = None
    db.run("""INSERT INTO carrier_quotes (submission_id, carrier, environment, ok, premium, quote_id,
                                          application_url, error, raw, requested_by)
              VALUES (%s,'coterie',%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
           (sid, res.get("env"), res["ok"], premium, res.get("quote_id"), res.get("application_url"),
            res.get("error"), json.dumps(res.get("raw") or {}, default=str), admin["id"]))
    audit(admin["id"], "carrier_quote", sid, {"carrier": "coterie", "ok": res["ok"]}, request)
    return RedirectResponse(f"{get_settings().admin_path}/submissions/{sid}#carriers", status_code=303)


@router.post("/submissions/{sid}/review")
def review(request: Request, sid: int, action: str = Form(max_length=20), note: str = Form("", max_length=1000),
           csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    require_csrf(admin, csrf)
    new_status = {"approve": "approved", "reject": "rejected", "sent": "sent_to_market", "bound": "bound",
                  "reopen": "pending_review"}.get(action)
    if not new_status:
        raise HTTPException(status_code=400)
    with db.tx() as cur:
        cur.execute("SELECT status FROM submissions WHERE id = %s FOR UPDATE", (sid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404)
        cur.execute("""UPDATE submissions SET status = %s, reviewed_by = %s, reviewed_at = now(),
                              review_note = NULLIF(%s, '') WHERE id = %s""", (new_status, admin["id"], note.strip(), sid))
        cur.execute("""INSERT INTO audit_log (admin_id, action, submission_id, detail, ip_hash)
                       VALUES (%s, %s, %s, %s::jsonb, %s)""",
                    (admin["id"], "status_change", sid,
                     json.dumps({"from": row["status"], "to": new_status, "note": note.strip()[:200]}), ip_hash(request)))
    return RedirectResponse(f"{get_settings().admin_path}/submissions/{sid}", status_code=303)


@router.get("/submissions/{sid}/docs/{kind}")
def download_doc(request: Request, sid: int, kind: str, admin=Depends(require_admin)):
    if kind not in ("summary", "acord125", "acord126", "acord125_fillable", "acord126_fillable", "fee_agreement"):
        raise HTTPException(status_code=404)
    doc = db.one("SELECT filename, content_enc FROM submission_documents WHERE submission_id = %s AND kind = %s", (sid, kind))
    if not doc:
        raise HTTPException(status_code=404)
    audit(admin["id"], "download_" + kind, sid, request=request)
    return Response(decrypt(doc["content_enc"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'})


@router.get("/submissions/{sid}/export.json")
def export(request: Request, sid: int, admin=Depends(require_admin)):
    """Approved submissions only. This is the hand-off point for First Connect / your XPT rater:
    replace or extend with their API client once you have credentials and their field spec."""
    sub = db.one("SELECT id, public_id, status, answers, acord125, acord126, fein_enc FROM submissions WHERE id = %s", (sid,))
    if not sub:
        raise HTTPException(status_code=404)
    if sub["status"] not in ("approved", "sent_to_market", "bound"):
        raise HTTPException(status_code=409, detail="Approve the submission before exporting it.")
    audit(admin["id"], "export_json", sid, request=request)
    acord125 = dict(sub["acord125"])
    if sub["fein_enc"]:
        f = decrypt(sub["fein_enc"]).decode()
        acord125["ACORD_Policy_Insured1_FEINSSN"] = f"{f[:2]}-{f[2:]}"
    payload = {"submission_id": sub["id"], "ref": str(sub["public_id"]), "line": "GL",
               "answers": sub["answers"], "acord125": acord125, "acord126": sub["acord126"]}
    return Response(json.dumps(payload, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="Bindswift_{sid}.json"'})



# ---------------- quote -> bind workflow ----------------
def _quote(sid: int, qid: int):
    q = db.one("SELECT * FROM carrier_quotes WHERE id = %s AND submission_id = %s", (qid, sid))
    if not q:
        raise HTTPException(status_code=404)
    return q


def _back(sid: int):
    return RedirectResponse(f"{get_settings().admin_path}/submissions/{sid}#carriers", status_code=303)


@router.post("/submissions/{sid}/quotes/manual")
def add_manual_quote(request: Request, sid: int, carrier: str = Form(max_length=60), premium: str = Form(max_length=20),
                     quote_ref: str = Form("", max_length=80), csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    """For carriers quoted in their own portal (NEXT, wholesalers): record the quote so you can send it."""
    require_csrf(admin, csrf)
    try:
        amount = float(premium.replace("$", "").replace(",", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="Premium must be a number")
    db.run("""INSERT INTO carrier_quotes (submission_id, carrier, environment, ok, premium, quote_id, requested_by)
              VALUES (%s, %s, 'manual', TRUE, %s, NULLIF(%s,''), %s)""",
           (sid, carrier.strip().lower(), amount, quote_ref.strip(), admin["id"]))
    audit(admin["id"], "manual_quote", sid, {"carrier": carrier, "premium": amount}, request)
    return _back(sid)


@router.post("/submissions/{sid}/quotes/{qid}/send")
def send_quote(request: Request, sid: int, qid: int, csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    require_csrf(admin, csrf)
    from .mailer import send_offer
    from .offers import new_token
    q = _quote(sid, qid)
    if not q["ok"]:
        raise HTTPException(status_code=409, detail="Only successful quotes can be sent.")
    if q["environment"] == "upload" and not q["reviewed_at"]:
        raise HTTPException(status_code=409, detail="Review the extracted figures before sending this quote.")
    sub = db.one("SELECT legal_name, contact_name, contact_email, answers, effective_date, limits FROM submissions WHERE id = %s", (sid,))
    s = get_settings()
    token, th = new_token()
    limits = "$2M per occurrence / $4M aggregate" if sub["limits"] == "2/4" else "$1M per occurrence / $2M aggregate"
    # the link never outlives the insurer's own quote expiration
    from datetime import datetime, timedelta, timezone
    exp_at = datetime.now(timezone.utc) + timedelta(days=s.offer_days)
    qexp = (q["details"] or {}).get("quote_expires")
    try:
        if qexp:
            exp_at = min(exp_at, datetime.strptime(qexp, "%m/%d/%Y").replace(hour=23, minute=59, tzinfo=timezone.utc))
    except ValueError:
        pass
    with db.tx() as cur:
        cur.execute("""UPDATE carrier_quotes SET offer_token_hash = %s, offer_token_enc = %s, offer_expires_at = %s,
                              sent_at = now(), limits_text = %s, effective_date = %s WHERE id = %s
                       RETURNING offer_expires_at""", (th, encrypt(token.encode()), exp_at, limits, sub["effective_date"], qid))
        exp = cur.fetchone()["offer_expires_at"]
        cur.execute("UPDATE submissions SET status = 'quoted' WHERE id = %s AND status <> 'bound'", (sid,))
    link = f"{s.public_base_url.rstrip('/')}/q/{token}"
    shown = q["total_due"] or q["premium"]
    send_offer(sub["contact_email"], sub["contact_name"].split()[0], link,
               ((q["details"] or {}).get("carrier") or q["carrier"]).title(),
               float(shown) if shown else None, s.fee_amount, exp.strftime("%m/%d/%Y"), bool(q["total_due"]))
    audit(admin["id"], "quote_sent", sid, {"quote": qid}, request)
    return _back(sid)


@router.post("/submissions/{sid}/quotes/{qid}/fee")
def record_fee(request: Request, sid: int, qid: int, method: str = Form(max_length=20),
               reference: str = Form("", max_length=80), csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    require_csrf(admin, csrf)
    if method not in ("card", "zelle", "ach", "check", "cash", "other"):
        raise HTTPException(status_code=400)
    _quote(sid, qid)
    db.run("UPDATE carrier_quotes SET fee_paid_at = now(), fee_method = %s, fee_reference = NULLIF(%s,'') WHERE id = %s",
           (method, reference.strip(), qid))
    audit(admin["id"], "fee_collected", sid, {"quote": qid, "amount": get_settings().fee_amount, "method": method,
                                              "reference": reference.strip()[:80]}, request)
    return _back(sid)


def _mark_bound(request, admin, sid, q, policy_number, billing, raw=None):
    from .mailer import send_bound
    with db.tx() as cur:
        cur.execute("""UPDATE carrier_quotes SET bound_at = now(), policy_number = NULLIF(%s,''), premium_billing = %s,
                              bind_raw = %s::jsonb, bind_error = NULL WHERE id = %s""",
                    (policy_number or "", billing, json.dumps(raw or {}, default=str), q["id"]))
        cur.execute("UPDATE submissions SET status = 'bound' WHERE id = %s", (sid,))
    from .certs import policy_from_bind
    q = {**q, "policy_number": policy_number}
    if not db.one("SELECT 1 FROM policies WHERE quote_id = %s", (q["id"],)):
        policy_from_bind(sid, q, policy_number)
    audit(admin["id"], "bound", sid, {"quote": q["id"], "carrier": q["carrier"], "policy": policy_number,
                                      "billing": billing}, request)
    sub = db.one("SELECT contact_email, contact_name FROM submissions WHERE id = %s", (sid,))
    try:
        send_bound(sub["contact_email"], sub["contact_name"].split()[0], q["carrier"].title(), policy_number)
    except Exception:
        pass


@router.post("/submissions/{sid}/quotes/{qid}/bind")
def bind(request: Request, sid: int, qid: int, interval: str = Form("Annual", max_length=10),
         csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    """Binds through Coterie's API on Agency Bill. Only after the contractor asked and the fee is recorded."""
    require_csrf(admin, csrf)
    q = _quote(sid, qid)
    if not q["bind_requested_at"]:
        raise HTTPException(status_code=409, detail="The contractor hasn't requested to bind this quote.")
    if not q["fee_paid_at"]:
        raise HTTPException(status_code=409, detail="Record the $50 fee before binding.")
    if q["bound_at"]:
        return _back(sid)
    if q["carrier"] != "coterie":
        raise HTTPException(status_code=400, detail="API binding is only set up for Coterie. Use 'Mark bound'.")
    res = coterie.bind_quote(q["quote_id"], "Monthly" if interval == "Monthly" else "Annual")
    if not res["ok"]:
        db.run("UPDATE carrier_quotes SET bind_error = %s, bind_raw = %s::jsonb WHERE id = %s",
               (res["error"], json.dumps(res.get("raw") or {}, default=str), qid))
        audit(admin["id"], "bind_failed", sid, {"quote": qid, "error": res["error"][:200]}, request)
        return _back(sid)
    _mark_bound(request, admin, sid, q, res.get("policy_number"), "agency", res.get("raw"))
    return _back(sid)


@router.post("/submissions/{sid}/quotes/{qid}/mark-bound")
def mark_bound(request: Request, sid: int, qid: int, policy_number: str = Form("", max_length=60),
               billing: str = Form("direct", max_length=10), csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    """For policies you bound in the carrier's own dashboard or portal."""
    require_csrf(admin, csrf)
    q = _quote(sid, qid)
    if not q["fee_paid_at"]:
        raise HTTPException(status_code=409, detail="Record the $50 fee before marking bound.")
    _mark_bound(request, admin, sid, q, policy_number.strip(), "agency" if billing == "agency" else "direct")
    return _back(sid)



# ---------------- uploaded underwriter quotes ----------------
@router.post("/submissions/{sid}/quotes/upload")
async def upload_quote(request: Request, sid: int, file: UploadFile = File(...), use_ai: str = Form(""),
                       csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    """Upload the PDF a wholesaler/underwriter sent you. Figures are extracted, then you review them."""
    from fastapi.concurrency import run_in_threadpool
    from . import quote_extract
    require_csrf(admin, csrf)
    if not db.one("SELECT 1 AS x FROM submissions WHERE id = %s", (sid,)):
        raise HTTPException(status_code=404)
    data = await file.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(status_code=413, detail="PDF is too large (4 MB max).")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="That file isn't a PDF.")
    try:
        if use_ai and get_settings().anthropic_api_key:
            try:
                d = await run_in_threadpool(quote_extract.extract_ai, data)
            except Exception as e:  # fall back to the rules
                d = await run_in_threadpool(quote_extract.extract_rules, data)
                d["ai_error"] = f"AI extraction failed ({type(e).__name__}); used the built-in reader."
        else:
            d = await run_in_threadpool(quote_extract.extract_rules, data)
    except Exception:
        d = {"source": "none", "limits": {}, "internal": {}, "missing": ["everything"],
             "note": "Couldn't read this PDF automatically. Fill in the figures below."}
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "quote.pdf")[:80]
    with db.tx() as cur:
        cur.execute("""INSERT INTO carrier_quotes (submission_id, carrier, environment, ok, premium, total_due, quote_id,
                                                   details, source_pdf_enc, source_filename, requested_by)
                       VALUES (%s, %s, 'upload', TRUE, %s, %s, %s, %s::jsonb, %s, %s, %s) RETURNING id""",
                    (sid, (d.get("carrier") or "underwriter").lower(), d.get("premium"), d.get("total"),
                     d.get("quote_number"), json.dumps(d, default=str), encrypt(data), fname, admin["id"]))
        qid = cur.fetchone()["id"]
    audit(admin["id"], "quote_uploaded", sid, {"quote": qid, "source": d.get("source"), "file": fname}, request)
    return RedirectResponse(f"{get_settings().admin_path}/submissions/{sid}/quotes/{qid}/review", status_code=303)


@router.get("/submissions/{sid}/quotes/{qid}/review", response_class=HTMLResponse)
def review_quote_form(request: Request, sid: int, qid: int, admin=Depends(require_admin)):
    q = _quote(sid, qid)
    sub = db.one("SELECT id, legal_name FROM submissions WHERE id = %s", (sid,))
    return render(request, "quote_review.html", admin=admin, s=sub, q=q, d=q["details"] or {})


def _money_or_none(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").strip()) if str(v).strip() else None
    except ValueError:
        return None


def _rows(text: str) -> list[dict]:
    """'Label | 123.45' per line -> [{'label','amount'}]"""
    out = []
    for line in (text or "").splitlines():
        if "|" in line:
            lab, amt = line.rsplit("|", 1)
            a = _money_or_none(amt)
            if lab.strip() and a is not None:
                out.append({"label": lab.strip()[:80], "amount": a})
    return out[:12]


@router.post("/submissions/{sid}/quotes/{qid}/review")
async def review_quote_save(request: Request, sid: int, qid: int, admin=Depends(require_admin)):
    form = await request.form()
    require_csrf(admin, form.get("csrf"))
    q = _quote(sid, qid)
    f = lambda k, n=120: str(form.get(k) or "").strip()[:n]
    d = dict(q["details"] or {})
    d.update({
        "wholesaler": f("wholesaler"), "carrier": f("carrier"), "admitted": f("admitted"), "am_best": f("am_best"),
        "quote_number": f("quote_number"), "coverage": f("coverage"), "policy_form": f("policy_form"),
        "policy_start": f("policy_start", 12), "policy_end": f("policy_end", 12), "quote_expires": f("quote_expires", 12),
        "premium_lines": _rows(form.get("premium_lines")), "premium": _money_or_none(form.get("premium")),
        "fees": _rows(form.get("fees")), "taxes": _rows(form.get("taxes")), "total": _money_or_none(form.get("total")),
        "deductible": f("deductible", 30), "billing": f("billing", 40),
        "limits": {k: f("limit_" + k, 30) for k in ("each_occurrence", "general_aggregate", "products_aggregate",
                                                    "personal_advertising", "damage_rented", "medical")},
        "prior_to_bind": [x.strip()[:300] for x in str(form.get("prior_to_bind") or "").splitlines() if x.strip()][:20],
        "client_note": f("client_note", 600),
    })
    share = form.get("share_original") == "yes"
    db.run("""UPDATE carrier_quotes SET details = %s::jsonb, carrier = %s, premium = %s, total_due = %s, quote_id = NULLIF(%s,''),
                     share_original = %s, reviewed_at = now() WHERE id = %s""",
           (json.dumps(d, default=str), (d["carrier"] or "underwriter").lower(), d["premium"], d["total"],
            d["quote_number"], share, qid))
    audit(admin["id"], "quote_reviewed", sid, {"quote": qid, "share_original": share}, request)
    return _back(sid)


@router.get("/submissions/{sid}/quotes/{qid}/original.pdf")
def original_pdf(request: Request, sid: int, qid: int, admin=Depends(require_admin)):
    q = _quote(sid, qid)
    if not q["source_pdf_enc"]:
        raise HTTPException(status_code=404)
    audit(admin["id"], "download_quote_pdf", sid, {"quote": qid}, request)
    return Response(decrypt(q["source_pdf_enc"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{q["source_filename"] or "quote.pdf"}"'})



# ---------------- bound policies -> instant certificates ----------------
LIMIT_KEYS = ["each_occurrence", "general_aggregate", "products_aggregate", "personal_advertising", "damage_rented", "medical"]


def _policy(sid: int, pid: int):
    p = db.one("SELECT * FROM policies WHERE id = %s AND submission_id = %s", (pid, sid))
    if not p:
        raise HTTPException(status_code=404)
    return p


@router.get("/submissions/{sid}/policies/{pid}", response_class=HTMLResponse)
def policy_page(request: Request, sid: int, pid: int, admin=Depends(require_admin)):
    p = _policy(sid, pid)
    certs = db.all_("""SELECT id, number, holder_name, holder_address, holder_email, project, want_ai, want_pnc, want_wos,
                              status, created_at, issued_at FROM certificates WHERE policy_id = %s ORDER BY created_at DESC""", (pid,))
    return render(request, "policy.html", admin=admin, p=p, sid=sid, certs=certs, keys=LIMIT_KEYS)


@router.post("/submissions/{sid}/policies/{pid}")
async def policy_save(request: Request, sid: int, pid: int, admin=Depends(require_admin)):
    from .certs import new_portal_token
    from .mailer import send_cert_portal
    f = await request.form()
    require_csrf(admin, str(f.get("csrf", "")))
    p = _policy(sid, pid)
    g = lambda k, n=200: str(f.get(k, "")).strip()[:n]
    from datetime import datetime as _dt
    def d(k):
        try:
            return _dt.strptime(g(k, 10), "%Y-%m-%d").date()
        except ValueError:
            return None
    limits = {k: g("lim_" + k, 30) for k in LIMIT_KEYS if g("lim_" + k, 30)}
    ready = f.get("ready") == "1"
    if ready and not (g("policy_number") and d("effective_date") and d("expiration_date") and limits.get("each_occurrence")):
        raise HTTPException(status_code=400, detail="Policy number, dates and the each-occurrence limit are required before certificates can go out.")
    db.run("""UPDATE policies SET carrier = %s, naic = NULLIF(%s,''), policy_number = NULLIF(%s,''), effective_date = %s,
                     expiration_date = %s, policy_form = %s, limits = %s::jsonb, blanket_ai = %s, blanket_pnc = %s,
                     blanket_wos = %s, ops_description = %s, insured_name = %s, insured_address = %s, ready = %s,
                     status = %s WHERE id = %s""",
           (g("carrier", 80) or p["carrier"], g("naic", 10), g("policy_number", 40), d("effective_date"), d("expiration_date"),
            "Claims-made" if g("policy_form") == "Claims-made" else "Occurrence", json.dumps(limits),
            f.get("blanket_ai") == "1", f.get("blanket_pnc") == "1", f.get("blanket_wos") == "1",
            g("ops_description", 300), g("insured_name", 120) or p["insured_name"], g("insured_address", 160) or p["insured_address"],
            ready, g("status", 10) if g("status", 10) in ("active", "cancelled", "expired") else "active", pid))
    audit(admin["id"], "policy_saved", sid, {"policy": pid, "ready": ready}, request)
    if ready and not p["portal_token_hash"]:
        token, th = new_portal_token()
        db.run("UPDATE policies SET portal_token_hash = %s, portal_token_enc = %s WHERE id = %s", (th, encrypt(token.encode()), pid))
        sub = db.one("SELECT contact_email, contact_name FROM submissions WHERE id = %s", (sid,))
        link = f"{get_settings().public_base_url.rstrip('/')}/c/{token}"
        try:
            send_cert_portal(sub["contact_email"], sub["contact_name"].split()[0], link)
        except Exception:
            pass
        audit(admin["id"], "cert_portal_sent", sid, {"policy": pid}, request)
    return RedirectResponse(f"{get_settings().admin_path}/submissions/{sid}/policies/{pid}", status_code=303)


@router.post("/submissions/{sid}/policies/{pid}/certs/{cid}")
def cert_decide(request: Request, sid: int, pid: int, cid: int, decision: str = Form(max_length=10),
                csrf: str = Form(max_length=100), admin=Depends(require_admin)):
    """Approve a certificate that needs an endorsement you've now had the carrier add, or decline it."""
    from .certs import certificate_pdf
    from .mailer import send_certificate
    require_csrf(admin, csrf)
    p = _policy(sid, pid)
    c = db.one("SELECT * FROM certificates WHERE id = %s AND policy_id = %s AND status = 'pending_approval'", (cid, pid))
    if not c:
        raise HTTPException(status_code=404)
    if decision == "approve":
        pdf = certificate_pdf(p, c, c["number"])
        db.run("UPDATE certificates SET status = 'issued', pdf_enc = %s, issued_at = now() WHERE id = %s", (encrypt(pdf), cid))
        sub = db.one("SELECT contact_email FROM submissions WHERE id = %s", (sid,))
        for to in {sub["contact_email"], c["holder_email"]} - {None}:
            try:
                send_certificate(to, p["insured_name"], c["holder_name"], pdf, c["number"])
            except Exception:
                pass
    else:
        db.run("UPDATE certificates SET status = 'declined' WHERE id = %s", (cid,))
    audit(admin["id"], f"cert_{decision}", sid, {"policy": pid, "cert": cid}, request)
    return RedirectResponse(f"{get_settings().admin_path}/submissions/{sid}/policies/{pid}", status_code=303)


@router.get("/submissions/{sid}/policies/{pid}/certs/{cid}.pdf")
def cert_pdf_admin(request: Request, sid: int, pid: int, cid: int, admin=Depends(require_admin)):
    _policy(sid, pid)
    c = db.one("SELECT number, pdf_enc FROM certificates WHERE id = %s AND policy_id = %s", (cid, pid))
    if not c or not c["pdf_enc"]:
        raise HTTPException(status_code=404)
    return Response(decrypt(c["pdf_enc"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="COI_{c["number"]}.pdf"'})
