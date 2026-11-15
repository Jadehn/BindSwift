"""Instant certificates of insurance.

Bindswift issues certificates as the agency that placed the policy, for any carrier (Coterie, NEXT,
wholesaler/E&S). Neither Coterie nor NEXT offers a public certificate API: Coterie agents download
COIs from the Coterie Dashboard, and NEXT policyholders make them in NEXT's portal. Bindswift gives
the contractor one place, 24/7, for every policy.

Rules that keep certificates honest:
  * Only for policies you've confirmed (carrier, NAIC, policy number, dates, limits) in the admin.
  * A certificate is information only; it never changes coverage.
  * "Additional insured", "primary & non-contributory" and "waiver of subrogation" are shown only if
    the policy actually has the blanket endorsement. Otherwise the request waits for your approval
    (you'd get the endorsement added by the carrier first).
"""
import hashlib
import io
import json
import secrets
from datetime import date, datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from . import db
from .config import get_settings
from .pdfs import NY, _script_font

LIMIT_ROWS = [("each_occurrence", "Each occurrence"), ("damage_rented", "Damage to rented premises"),
              ("medical", "Medical exp (any one person)"), ("personal_advertising", "Personal & advertising injury"),
              ("general_aggregate", "General aggregate"), ("products_aggregate", "Products-comp/op agg")]


def new_portal_token() -> tuple[str, bytes]:
    t = secrets.token_urlsafe(32)
    return t, hashlib.sha256(t.encode()).digest()


def policy_from_bind(sid: int, q: dict, policy_number: str | None) -> int:
    """Creates the policy record when a quote is marked bound. You confirm details before certificates go out."""
    sub = db.one("SELECT legal_name, answers, effective_date, limits FROM submissions WHERE id = %s", (sid,))
    a = sub["answers"]
    d = q.get("details") or {}
    lim = d.get("limits") or {}
    if not lim.get("each_occurrence"):
        occ, agg = ("$2,000,000", "$4,000,000") if sub["limits"] == "2/4" else ("$1,000,000", "$2,000,000")
        lim = {"each_occurrence": occ, "general_aggregate": agg, "products_aggregate": agg,
               "personal_advertising": occ, "damage_rented": "$100,000", "medical": "$5,000"}
    eff = sub["effective_date"]
    try:
        exp = eff.replace(year=eff.year + 1)
    except ValueError:
        exp = eff.replace(year=eff.year + 1, day=28)
    row = db.one("""INSERT INTO policies (submission_id, quote_id, carrier, policy_number, effective_date, expiration_date,
                                          policy_form, limits, ops_description, insured_name, insured_address)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s) RETURNING id""",
                 (sid, q["id"], (d.get("carrier") or q["carrier"]).title(), policy_number or q.get("policy_number"),
                  eff, exp, d.get("policy_form") or "Occurrence", json.dumps(lim),
                  a.get("opsDesc", "")[:300], sub["legal_name"],
                  f"{a.get('street','')}, {a.get('city','')}, NY {a.get('zip','')}"))
    return row["id"]


def needs_approval(p: dict, ai: bool, pnc: bool, wos: bool) -> list[str]:
    out = []
    if ai and not p["blanket_ai"]:
        out.append("additional insured")
    if pnc and not p["blanket_pnc"]:
        out.append("primary & non-contributory")
    if wos and not p["blanket_wos"]:
        out.append("waiver of subrogation")
    return out


def certificate_pdf(p: dict, c: dict, number: str) -> bytes:
    """One-page Certificate of Liability Insurance (Bindswift layout).
    If you have an ACORD 25 license and template, this is where it would be filled instead."""
    s = get_settings()
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=letter)
    W, H = letter
    x0, x1 = 0.6 * inch, W - 0.6 * inch
    y = H - 0.65 * inch

    def box(y_top, h, title=None):
        cv.setStrokeColor(colors.black)
        cv.setLineWidth(0.8)
        cv.rect(x0, y_top - h, x1 - x0, h)
        if title:
            cv.setFont("Helvetica-Bold", 7.5)
            cv.drawString(x0 + 5, y_top - 10, title.upper())

    def text(x, yy, t, size=9, bold=False):
        cv.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        cv.drawString(x, yy, t)

    cv.setFont("Helvetica-Bold", 15)
    cv.drawString(x0, y, "CERTIFICATE OF LIABILITY INSURANCE")
    cv.setFont("Helvetica", 9)
    cv.drawRightString(x1, y, f"Date issued: {datetime.now(NY):%m/%d/%Y}   ·   Certificate {number}")
    y -= 14
    cv.setFont("Helvetica", 7.2)
    for line in ("This certificate is issued as a matter of information only and confers no rights upon the certificate holder.",
                 "It does not amend, extend or alter the coverage afforded by the policy below. The policy's terms, exclusions and conditions control."):
        cv.drawString(x0, y, line)
        y -= 9
    y -= 4
    half = (x0 + x1) / 2
    box(y, 70)
    cv.line(half, y, half, y - 70)
    text(x0 + 5, y - 11, "PRODUCER", 7.5, True)
    yy = y - 24
    for line in [s.agency_name, *(s.agency_address.replace("\\n", "\n").split("\n") if s.agency_address else []),
                 f"NY License #{s.producer_license}"][:4]:
        text(x0 + 5, yy, line, 9)
        yy -= 11
    text(half + 5, y - 11, "INSURED", 7.5, True)
    text(half + 5, y - 24, p["insured_name"][:52], 9, True)
    text(half + 5, y - 36, p["insured_address"][:60], 9)
    y -= 70
    box(y, 30)
    text(x0 + 5, y - 11, "INSURER", 7.5, True)
    text(x0 + 5, y - 23, p["carrier"], 9.5, True)
    text(half + 5, y - 11, "NAIC #", 7.5, True)
    text(half + 5, y - 23, p["naic"] or "", 9.5)
    y -= 30
    # coverage table
    rows = [(lab, (p["limits"] or {}).get(k, "")) for k, lab in LIMIT_ROWS]
    h = 22 + 13 * len(rows)
    box(y, h, "Coverage: Commercial general liability")
    cols = [x0 + 5, x0 + 1.75 * inch, x0 + 3.0 * inch, x0 + 3.9 * inch]
    text(cols[0], y - 22, f"Form: {p['policy_form']}", 8.5)
    text(cols[0], y - 34, "AI: " + ("Y" if c["want_ai"] else "N") + "    WOS: " + ("Y" if c["want_wos"] else "N"), 8.5)
    text(cols[1], y - 22, "Policy number", 7.5, True)
    text(cols[1], y - 34, (p["policy_number"] or "")[:22], 9)
    text(cols[2], y - 22, "Effective", 7.5, True)
    text(cols[2], y - 34, p["effective_date"].strftime("%m/%d/%Y") if p["effective_date"] else "", 9)
    text(cols[3], y - 22, "Expires", 7.5, True)
    text(cols[3], y - 34, p["expiration_date"].strftime("%m/%d/%Y") if p["expiration_date"] else "", 9)
    lx = x0 + 4.95 * inch
    yy = y - 22
    for lab, val in rows:
        cv.setFont("Helvetica", 7.6)
        cv.drawString(lx, yy, lab)
        cv.setFont("Helvetica-Bold", 8.5)
        cv.drawRightString(x1 - 6, yy, val or "")
        yy -= 13
    y -= h
    # description
    desc = [f"Operations: {p['ops_description'] or ''}"]
    if c.get("project"):
        desc.append(f"Project / location: {c['project']}")
    extras = [n for n, on in (("additional insured", c["want_ai"]), ("primary and non-contributory", c["want_pnc"]),
                              ("waiver of subrogation", c["want_wos"])) if on]
    if extras:
        desc.append("The certificate holder is " + ", ".join(extras[:-1]) + (" and " if len(extras) > 1 else "") + extras[-1]
                    + " where required by written contract, per the policy's blanket endorsement(s).")
    from reportlab.lib.utils import simpleSplit
    lines = [ln for d in desc for ln in simpleSplit(d, "Helvetica", 8.5, x1 - x0 - 12)][:7]
    h = 16 + 11 * max(2, len(lines))
    box(y, h, "Description of operations / locations / additional remarks")
    yy = y - 22
    for ln in lines:
        text(x0 + 6, yy, ln, 8.5)
        yy -= 11
    y -= h
    box(y, 92)
    cv.line(half, y, half, y - 92)
    text(x0 + 5, y - 11, "CERTIFICATE HOLDER", 7.5, True)
    yy = y - 26
    for line in [c["holder_name"], *c["holder_address"].split(", ", 1)][:4]:
        text(x0 + 8, yy, line[:55], 9.5, line == c["holder_name"])
        yy -= 12
    text(half + 5, y - 11, "CANCELLATION", 7.5, True)
    cv.setFont("Helvetica", 7.4)
    yy = y - 24
    for ln in simpleSplit("If the policy above is cancelled before its expiration date, notice will be delivered as the "
                          "policy provisions require.", "Helvetica", 7.4, x1 - half - 12):
        cv.drawString(half + 5, yy, ln)
        yy -= 9
    text(half + 5, y - 62, "AUTHORIZED REPRESENTATIVE", 7.5, True)
    try:
        cv.setFont(_script_font(), 18)
        cv.setFillColorRGB(0.05, 0.08, 0.2)
        cv.drawString(half + 8, y - 84, s.producer_name)
        cv.setFillColorRGB(0, 0, 0)
    except Exception:
        text(half + 8, y - 84, s.producer_name, 10)
    y -= 92
    cv.setFont("Helvetica", 7)
    cv.drawString(x0, y - 12, f"Issued through Bindswift · {s.agency_name} · Verify this certificate by contacting the producer.")
    cv.save()
    return buf.getvalue()
