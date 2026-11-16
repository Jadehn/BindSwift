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
    """Certificate of Liability Insurance: the ACORD 25 when forms/ACORD_25.pdf is present, else Bindswift's layout."""
    if get_settings().acord25_pdf.exists():
        return acord25_pdf(coi_data(p, c, number), sample=bool(p.get("sample")))
    return certificate_pdf_basic(p, c, number)


def certificate_pdf_basic(p: dict, c: dict, number: str) -> bytes:
    """One-page Certificate of Liability Insurance (Bindswift layout, used when there's no ACORD 25 template)."""
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


# ---------------------------------------------------------------- ACORD 25
def producer_block(sample: bool = False) -> dict:
    """The PRODUCER box. Real certificates use your .env (AGENCY_NAME, AGENCY_ADDRESS, PRODUCER_*).
    Samples use a made-up agency so no personal details appear on a demo."""
    if sample:
        return {"name": "Bindswift Demo Agency", "lines": ["100 Sample Street", "New York, NY 10001"],
                "contact": "Demo Producer", "phone": "(000) 000-0000", "email": "demo@example.com", "signature": "Demo Producer"}
    s = get_settings()
    lines = [ln.strip() for ln in (s.agency_address or "").replace("\\n", "\n").split("\n") if ln.strip()]
    return {"name": s.agency_name, "lines": lines[:2], "contact": s.producer_name, "phone": s.producer_phone,
            "email": s.producer_email, "signature": s.producer_name}


def _split_addr(addr: str) -> list[str]:
    parts = [x.strip() for x in (addr or "").split(",") if x.strip()]
    if len(parts) <= 1:
        return parts
    return [parts[0], ", ".join(parts[1:])]


def coi_data(p: dict, c: dict, number: str) -> dict:
    """Everything an ACORD 25 needs, from a policy row and a certificate row."""
    def mdy(d):
        return d.strftime("%m/%d/%Y") if d else ""
    remarks = []
    if p.get("ops_description"):
        remarks.append(p["ops_description"])
    if c.get("project"):
        remarks.append(f"Re: {c['project']}")
    extras = [n for n, on in (("an additional insured", c.get("want_ai")), ("primary and non-contributory", c.get("want_pnc")),
                              ("entitled to a waiver of subrogation", c.get("want_wos"))) if on]
    if extras:
        remarks.append("The certificate holder is " + "; ".join(extras) +
                       " where required by written contract, per the policy's endorsement(s).")
    return {"sample": bool(p.get("sample")), "number": number, "date": datetime.now(NY).strftime("%m/%d/%Y"),
            "producer": producer_block(bool(p.get("sample"))),
            "insured": {"name": p["insured_name"], "lines": _split_addr(p["insured_address"])},
            "insurer": {"name": p["carrier"], "naic": p.get("naic") or ""},
            "gl": {"form": p.get("policy_form") or "Occurrence", "number": p.get("policy_number") or "",
                   "eff": mdy(p.get("effective_date")), "exp": mdy(p.get("expiration_date")),
                   "limits": p.get("limits") or {}, "ai": bool(c.get("want_ai")), "wos": bool(c.get("want_wos"))},
            "remarks": " ".join(x.rstrip(".") + "." for x in remarks),
            "holder": {"name": c["holder_name"], "lines": _split_addr(c["holder_address"])}}


GL_LIMIT_FIELDS = {"each_occurrence": "GeneralLiability_EachOccurrence_LimitAmount_A",
                   "damage_rented": "GeneralLiability_FireDamageRentedPremises_EachOccurrenceLimitAmount_A",
                   "medical": "GeneralLiability_MedicalExpense_EachPersonLimitAmount_A",
                   "personal_advertising": "GeneralLiability_PersonalAndAdvertisingInjury_LimitAmount_A",
                   "general_aggregate": "GeneralLiability_GeneralAggregate_LimitAmount_A",
                   "products_aggregate": "GeneralLiability_ProductsAndCompletedOperations_AggregateLimitAmount_A"}


def _money(v) -> str:
    t = str(v or "").replace("$", "").replace(",", "").strip()
    try:
        return f"{float(t):,.0f}"
    except ValueError:
        return str(v or "")


def acord25_values(d: dict) -> dict:
    """Field name (as on the ACORD 25 widget) -> value."""
    pr, ins, gl, h = d["producer"], d["insured"], d["gl"], d["holder"]
    v = {"Form_CompletionDate_A[0]": d["date"],
         "Producer_FullName_A[0]": pr["name"],
         "Producer_MailingAddress_LineOne_A[0]": (pr["lines"] + ["", ""])[0],
         "Producer_MailingAddress_LineTwo_A[0]": (pr["lines"] + ["", ""])[1],
         "Producer_ContactPerson_FullName_A[0]": pr["contact"],
         "Producer_ContactPerson_PhoneNumber_A[0]": pr["phone"],
         "Producer_ContactPerson_EmailAddress_A[0]": pr["email"],
         "NamedInsured_FullName_A[0]": ins["name"],
         "NamedInsured_MailingAddress_LineOne_A[0]": (ins["lines"] + ["", ""])[0],
         "NamedInsured_MailingAddress_LineTwo_A[0]": (ins["lines"] + ["", ""])[1],
         "Insurer_FullName_A[0]": d["insurer"]["name"],
         "Insurer_NAICCode_A[0]": d["insurer"]["naic"],
         "CertificateOfInsurance_CertificateNumberIdentifier_A[0]": d["number"],
         "GeneralLiability_InsurerLetterCode_A[0]": "A",
         "GeneralLiability_CoverageIndicator_A[0]": "X",
         ("GeneralLiability_ClaimsMadeIndicator_A[0]" if gl["form"].lower().startswith("claims")
          else "GeneralLiability_OccurrenceIndicator_A[0]"): "X",
         "GeneralLiability_GeneralAggregate_LimitAppliesPerPolicyIndicator_A[0]": "X",
         "CertificateOfInsurance_GeneralLiability_AdditionalInsuredCode_A[0]": "Y" if gl["ai"] else "",
         "Policy_GeneralLiability_SubrogationWaivedCode_A[0]": "Y" if gl["wos"] else "",
         "Policy_GeneralLiability_PolicyNumberIdentifier_A[0]": gl["number"],
         "Policy_GeneralLiability_EffectiveDate_A[0]": gl["eff"],
         "Policy_GeneralLiability_ExpirationDate_A[0]": gl["exp"],
         "CertificateOfLiabilityInsurance_ACORDForm_RemarkText_A[0]": d["remarks"],
         "CertificateHolder_FullName_A[0]": h["name"],
         "CertificateHolder_MailingAddress_LineOne_A[0]": (h["lines"] + ["", ""])[0],
         "CertificateHolder_MailingAddress_LineTwo_A[0]": (h["lines"] + ["", ""])[1]}
    for k, f in GL_LIMIT_FIELDS.items():
        if gl["limits"].get(k):
            v[f + "[0]"] = _money(gl["limits"][k])
    return {k: x for k, x in v.items() if x}


def acord25_pdf(d: dict, sample: bool = False) -> bytes:
    """Fill the ACORD 25 and flatten it: every value drawn on the page, form fields removed."""
    from pypdf import PdfReader, PdfWriter
    from .pdfs import _draw_signature, _draw_value, _strip_widgets, _widget_rects
    reader = PdfReader(str(get_settings().acord25_pdf))
    rects = _widget_rects(reader)
    kinds = {}
    for annot in reader.pages[0].get("/Annots") or []:
        o = annot.get_object()
        par = o.get("/Parent").get_object() if o.get("/Parent") else {}
        name = o.get("/T") or par.get("/T")
        kinds[name] = (o.get("/FT") or par.get("/FT"), int(o.get("/Ff") or par.get("/Ff") or 0))
    writer = PdfWriter()
    writer.append(reader)
    box = reader.pages[0].mediabox
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=(float(box.width), float(box.height)))
    sig = "Producer_AuthorizedRepresentative_Signature_A[0]"
    for name, val in acord25_values(d).items():
        if name not in rects:
            continue
        _, r = rects[name]
        ft, ff = kinds.get(name, (None, 0))
        _draw_value(cv, r, str(val), bool(ff & 4096) or (r[3] - r[1]) > 30, ft == "/Btn")
    if sig in rects:
        _draw_signature(cv, rects[sig][1], d["producer"]["signature"])
    if sample or d.get("sample"):
        cv.saveState()
        cv.setFillColorRGB(0.85, 0.1, 0.1, alpha=0.18)
        cv.setFont("Helvetica-Bold", 54)
        cv.translate(float(box.width) / 2, float(box.height) / 2)
        cv.rotate(35)
        cv.drawCentredString(0, 20, "SAMPLE ONLY")
        cv.setFont("Helvetica-Bold", 22)
        cv.drawCentredString(0, -18, "NOT EVIDENCE OF INSURANCE")
        cv.restoreState()
    cv.save()
    writer.pages[0].merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
    _strip_widgets(writer)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
