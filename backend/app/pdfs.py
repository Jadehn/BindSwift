"""Fill the ACORD PDFs and build the one-page-per-section summary PDF."""
import io
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, NameObject
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .acord_map import INTEREST_LABELS, LIMITS, TRADES, mdy
from .config import get_settings

NY = ZoneInfo("America/New_York")


_FONT_READY = False


def _script_font() -> str:
    global _FONT_READY
    if not _FONT_READY:
        pdfmetrics.registerFont(TTFont("Signature", str(get_settings().signature_font)))
        _FONT_READY = True
    return "Signature"


def _widget_rects(reader: PdfReader) -> dict[str, tuple[int, list[float]]]:
    """field name -> (page index, [x0, y0, x1, y1]) for every widget in the template."""
    out = {}
    for i, page in enumerate(reader.pages):
        for annot in page.get("/Annots") or []:
            o = annot.get_object()
            name = o.get("/T") or (o.get("/Parent") or {}).get("/T")
            if name and "/Rect" in o:
                x0, y0, x1, y1 = [float(v) for v in o["/Rect"]]
                out[name] = (i, [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)])
    return out


def _draw_signature(cv, rect, text: str) -> None:
    font = _script_font()
    x0, y0, x1, y1 = rect
    size = 20.0 if (y1 - y0) > 18 else 16.0
    while size > 9 and pdfmetrics.stringWidth(text, font, size) > (x1 - x0) - 6:
        size -= 0.5
    cv.setFont(font, size)
    cv.setFillColorRGB(0.05, 0.08, 0.2)
    cv.drawString(x0 + 4, y0 + 2.5, text)


def _overlay(template: PdfReader, rects: dict, form: str, built: dict) -> dict[int, bytes]:
    """Draws what the blank form has no fillable field for. Returns page index -> one-page PDF."""
    by_page: dict[int, list] = {}
    add = lambda page, fn: by_page.setdefault(page, []).append(fn)

    if form == "125":
        # X in the Lines of Business box for COMMERCIAL GENERAL LIABILITY (row below Business Owners)
        auto = rects["ACORD_LOB_BusinessAuto"][1]
        row = 24.0  # two rows down
        add(0, lambda cv: (cv.setFont("Helvetica", 11), cv.drawString(auto[0] + 3.5, auto[1] - row + 2.5, "X")))
        for field, val in built["yn125"].items():
            pg, r = rects[f"ACORD_General_{field}"]
            add(pg, lambda cv, r=r, val=val: (cv.setFont("Helvetica-Bold", 10),
                                              cv.drawCentredString((r[0] + r[2]) / 2, (r[1] + r[3]) / 2 - 3.5, val)))
        sig = {"ACORD_Signatures_Producer_Signature": built["producer"],
               "ACORD_Signatures_Applicant_Signature": built["signer"]}
    else:
        sig = {"Producer_AuthorizedRepresentative_Signature_A": built["producer"],
               "NamedInsured_Signature_A": built["signer"]}
    for field, text in sig.items():
        if text and field in rects:
            pg, r = rects[field]
            add(pg, lambda cv, r=r, text=text: _draw_signature(cv, r, text))

    out = {}
    for pg, fns in by_page.items():
        box = template.pages[pg].mediabox
        buf = io.BytesIO()
        cv = canvas.Canvas(buf, pagesize=(float(box.width), float(box.height)))
        for fn in fns:
            fn(cv)
        cv.save()
        out[pg] = buf.getvalue()
    return out


def _draw_value(cv, rect, value: str, multiline: bool, is_check: bool) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    cv.setFillColorRGB(0, 0, 0)
    if is_check:
        if min(w, h) < 9:  # tiny phone-type boxes: fill them in, like the sample
            cv.rect(x0 + 0.5, y0 + 0.5, w - 1, h - 1, stroke=0, fill=1)
            return
        size = min(h, w) * 0.9
        cv.setFont("Helvetica", size)
        cv.drawCentredString((x0 + x1) / 2, y0 + (h - size * 0.72) / 2, "X")
        return
    if multiline and h > 20:
        size = 10.0
        lines = []
        while True:
            lines = [ln for para in value.split("\n") for ln in (simpleSplit(para, "Helvetica", size, w - 4) or [""])]
            if len(lines) * size * 1.15 <= h - 2 or size <= 6:
                break
            size -= 0.5
        cv.setFont("Helvetica", size)
        y = y1 - size - 1
        for ln in lines:
            if y < y0 - 1:
                break
            cv.drawString(x0 + 2, y, ln)
            y -= size * 1.15
        return
    size = max(6.0, min(11.0, h * 0.72))
    while size > 6 and stringWidth(value, "Helvetica", size) > w - 4:
        size -= 0.25
    cv.setFont("Helvetica", size)
    cv.drawString(x0 + 2, y0 + (h - size * 0.7) / 2, value)


def fill_acord(template: Path, values: dict, built: dict | None = None, form: str = "125",
               flatten: bool = True) -> bytes:
    """flatten=True (default): every answer is drawn onto the page and the form fields are
    removed, like a final submission. It looks identical in every viewer and can't be edited.
    flatten=False: fills the live form fields instead, so it stays editable in Acrobat."""
    reader = PdfReader(str(template))
    fields = reader.get_fields() or {}
    rects = _widget_rects(reader)
    writer = PdfWriter()
    writer.append(reader)
    known = {k: v for k, v in values.items() if k in fields}
    overlays = _overlay(reader, rects, form, built) if built is not None else {}

    if flatten:
        by_page: dict[int, list] = {}
        for k, v in known.items():
            if k not in rects:
                continue
            pg, r = rects[k]
            is_check = fields[k].get("/FT") == "/Btn"
            multiline = bool(int(fields[k].get("/Ff", 0)) & 4096) or (r[3] - r[1]) > 30
            by_page.setdefault(pg, []).append((r, str(v), multiline, is_check))
        for pg, items in by_page.items():
            box = reader.pages[pg].mediabox
            buf = io.BytesIO()
            cv = canvas.Canvas(buf, pagesize=(float(box.width), float(box.height)))
            for r, v, ml, chk in items:
                _draw_value(cv, r, v, ml, chk)
            cv.save()
            writer.pages[pg].merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
        _strip_widgets(writer)
    else:
        text = {k: str(v) for k, v in known.items() if fields[k].get("/FT") != "/Btn"}
        checks = {k: v for k, v in known.items() if fields[k].get("/FT") == "/Btn"}
        for page in writer.pages:
            if text:
                writer.update_page_form_field_values(page, text, auto_regenerate=False)
            for annot in page.get("/Annots") or []:
                obj = annot.get_object()
                name = obj.get("/T") or (obj.get("/Parent") or {}).get("/T")
                if name in checks:
                    on = NameObject("/" + checks[name])
                    obj[NameObject("/V")] = on
                    obj[NameObject("/AS")] = on
        writer.set_need_appearances_writer(True)

    for pg, data in overlays.items():
        writer.pages[pg].merge_page(PdfReader(io.BytesIO(data)).pages[0])
    if form == "126" and built and built.get("schedule"):
        writer.append(PdfReader(io.BytesIO(interest_schedule_pdf(built))))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _strip_widgets(writer: PdfWriter) -> None:
    for page in writer.pages:
        annots = page.get("/Annots")
        if annots:
            keep = ArrayObject([a for a in annots if a.get_object().get("/Subtype") != "/Widget"])
            page[NameObject("/Annots")] = keep
    if "/AcroForm" in writer._root_object:
        del writer._root_object["/AcroForm"]


def interest_schedule_pdf(built: dict) -> bytes:
    """Attachment listing every additional interest when there's more than the form's one slot."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title="Additional Interest Schedule")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9, leading=11.5)
    head = ParagraphStyle("h", parent=body, fontName="Helvetica-Bold")
    p = lambda x, st=body: Paragraph(str(x).replace("&", "&amp;").replace("<", "&lt;"), st)
    story = [Paragraph("ADDITIONAL INTEREST SCHEDULE", ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                                                                       fontSize=14, alignment=0, spaceAfter=2)),
             p(f"Attachment to ACORD 126 · Applicant: {built['insured']} · Effective {built['effective']}"),
             Spacer(1, 10)]
    rows = [[p(h, head) for h in ("Rank", "Interest", "Name and address", "Reference / loan #", "Certificate")]]
    for i in built["schedule"]:
        rows.append([p(i["rank"]), p(i["type"]), p(f"{i['name']}\n{i['address']}".replace("\n", " · ")),
                     p(i["ref"] or "—"), p("Yes" if i["cert"] else "No")])
    t = Table(rows, colWidths=[0.5 * inch, 1.4 * inch, 3.3 * inch, 1.2 * inch, 0.8 * inch], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    doc.build(story)
    return buf.getvalue()


def fee_agreement_pdf(legal_name: str, text: str, signer: str, signed_at: datetime, ref: str) -> bytes:
    """Signed copy of the §2119 service fee agreement, kept with the submission."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.9 * inch, bottomMargin=0.9 * inch, title="Broker Service Fee Agreement")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=8)
    small = ParagraphStyle("s", parent=body, fontSize=8.5, textColor=colors.HexColor("#5B6878"))
    esc = lambda x: x.replace("&", "&amp;").replace("<", "&lt;")
    story = [Paragraph("BROKER SERVICE FEE AGREEMENT", ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                                                                     fontSize=15, alignment=0, spaceAfter=2)),
             Paragraph("New York Insurance Law §2119 · Bindswift", small), Spacer(1, 10)]
    story += [Paragraph(esc(line), body) for line in text.split("\n")]
    story += [Spacer(1, 18), Paragraph("Signed electronically by the insured:", body)]
    sig = ParagraphStyle("sig", parent=body, fontName="Signature" if _script_font() else "Helvetica", fontSize=26, leading=30)
    story += [Paragraph(esc(signer), sig),
              Paragraph(f"{esc(signer)} for {esc(legal_name)} · {signed_at.astimezone(NY):%m/%d/%Y %I:%M %p} ET", body),
              Spacer(1, 8), Paragraph(f"Reference {ref}. Retain for at least 3 years after services are performed.", small)]
    doc.build(story)
    return buf.getvalue()


def quote_summary_pdf(q: dict, legal_name: str, fee: int) -> bytes:
    """Client-facing Bindswift quote summary. Never includes commission or the wholesaler."""
    d = q.get("details") or {}
    lim = d.get("limits") or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.7 * inch, title="Bindswift Quote Summary")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=body, fontSize=8, textColor=colors.HexColor("#6E6E6E"))
    esc = lambda x: str(x).replace("&", "&amp;").replace("<", "&lt;")
    money = lambda v: f"${float(v):,.2f}"

    def table(rows):
        t = Table([[Paragraph(esc(a), body), Paragraph(esc(b), body)] for a, b in rows], colWidths=[3.4 * inch, 3.4 * inch])
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        return t
    carrier = d.get("carrier") or q.get("carrier", "").title()
    story = [Paragraph("BINDSWIFT · QUOTE SUMMARY", small), Paragraph(esc(legal_name), h1),
             Paragraph(esc(f"{carrier}" + (f" · {d['admitted']}" if d.get("admitted") else "")
                           + (f" · A.M. Best {d['am_best']}" if d.get("am_best") else "")), body)]
    info = [(k, v) for k, v in [("Coverage", (d.get("coverage") or "") + (f" ({d['policy_form']})" if d.get("policy_form") else "")),
                                ("Policy period", f"{d.get('policy_start')} – {d.get('policy_end')}" if d.get("policy_start") else ""),
                                ("Quote number", d.get("quote_number") or q.get("quote_id") or ""),
                                ("Quote valid until", d.get("quote_expires") or ""), ("Billing", d.get("billing") or "")] if v]
    if info:
        story += [Paragraph("Policy", h2), table(info)]
    names = [("each_occurrence", "Each occurrence"), ("general_aggregate", "General aggregate"),
             ("products_aggregate", "Products / completed operations"), ("personal_advertising", "Personal & advertising injury"),
             ("damage_rented", "Damage to rented premises"), ("medical", "Medical expense")]
    lrows = [(lab, lim[k]) for k, lab in names if lim.get(k)] + ([("Deductible", d["deductible"])] if d.get("deductible") else [])
    if lrows:
        story += [Paragraph("Limits", h2), table(lrows)]
    prows = [(x["label"], money(x["amount"])) for x in (d.get("premium_lines") or [])]
    prows += [(x["label"], money(x["amount"])) for x in (d.get("fees") or [])]
    prows += [(x["label"], money(x["amount"])) for x in (d.get("taxes") or [])]
    total = q.get("total_due") or q.get("premium")
    if total:
        prows.append(("Total due to the insurer", money(total)))
    prows.append((f"Bindswift service fee (charged once at binding)", money(fee)))
    story += [Paragraph("Price", h2), table(prows)]
    if d.get("prior_to_bind"):
        story += [Paragraph("Before binding, the insurer needs", h2)] + [Paragraph("• " + esc(x), body) for x in d["prior_to_bind"]]
    story += [Spacer(1, 14), Paragraph("This summary is for convenience. The insurer's quote and the policy itself control the "
                                       "actual terms. Coverage is not in force until your broker confirms it is bound. "
                                       "Bindswift is a service of Howard Analytics, NY licensed P&amp;C broker.", small)]
    doc.build(story)
    return buf.getvalue()


def summary_pdf(sub_id: int, public_id: str, a: dict, fein_display: str, created: datetime) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"Bindswift submission #{sub_id}", author="Bindswift")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, alignment=0, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4,
                        textColor=colors.HexColor("#0E1B2C"))
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("s", parent=body, textColor=colors.HexColor("#5B6878"))

    def p(x):
        return Paragraph(str(x).replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>"), body)

    yn = lambda v: {"Y": "Yes", "N": "No"}.get(v, v or "—")
    money = lambda v: f"${float(v or 0):,.0f}"
    t = TRADES[a["trade"]]
    occ, agg = LIMITS[a["limits"]]
    sections = [
        ("Business", [("Legal name", a["legalName"]), ("DBA", a.get("dba") or "—"), ("Entity", a["entity"]),
                      ("LLC members", a.get("members") or "—"), ("FEIN", fein_display or "Not provided"),
                      ("Business started", a["started"])]),
        ("Contact", [("Name", f'{a["contactName"]} ({a["contactTitle"]})'), ("Phone", a["phone"]),
                     ("Email", a["email"]), ("Website", a.get("website") or "—")]),
        ("Address", [("Street", a["street"]), ("City / ZIP", f'{a["city"]}, NY {a["zip"]}'),
                     ("County", a["county"]), ("Premises", a["premises"]), ("Sq ft", a.get("sqft") or "—")]),
        ("Work", [("Trade", f'{t["l"]} (class {t["code"]})'), ("Description", a["opsDesc"]),
                  ("Work at customers' sites", f'{a.get("offsitePct", 100)}%'), ("Max height", a["height"]),
                  ("Areas", ", ".join(a["areas"]))]),
        ("Crew and revenue", [("Full-time / part-time", f'{a["ft"]} / {a["pt"]}'), ("Annual revenue", money(a["revenue"])),
                              ("Annual payroll", money(a["payroll"])), ("Paid to subs", money(a.get("subCost"))),
                              ("% subcontracted", a.get("subPct") or "—"), ("Work subbed", a.get("subWork") or "—"),
                              ("Subs provide COIs w/ equal limits", yn(a.get("subsInsured")))]),
        ("Coverage requested", [("Effective", a["effDate"]), ("Limits", f"${occ:,.0f} / ${agg:,.0f}"),
                                ("Payment plan", a.get("payPlan") or "—"), ("Additional interests", yn(a["gcReq"]))]
                               + [(f"#{n} {INTEREST_LABELS[i['type']]}",
                                   f"{i['name']}, {i['street']}, {i['city']}, {i['state']} {i['zip']}"
                                   + (f" · ref {i['ref']}" if i.get("ref") else "")
                                   + (" · certificate" if i.get("cert") else ""))
                                  for n, i in enumerate(a.get("interests") or [], 1)]),
        ("Insurance history", [("Currently insured", yn(a["insured"])), ("Prior carrier", a.get("priorCarrier") or "—"),
                               ("Prior policy #", a.get("priorPolicy") or "—"), ("Prior expiration", a.get("priorExp") or "—"),
                               ("Prior premium", money(a.get("priorPrem"))), ("Claims in 5 yrs", a["losses"]),
                               ("Claim details", a.get("lossDesc") or "—"), ("Paid on claims", money(a.get("lossPaid"))),
                               ("Declined/cancelled 3 yrs", yn(a["cancelled"])), ("Reason", a.get("cancelWhy") or "—")]),
        ("Safety and operations", [("Written safety program", yn(a["safety"])), ("OSHA / SST", yn(a.get("osha"))),
                                   ("Installs products", yn(a["install"])), ("What they install", a.get("installDesc") or "—"), ("Hold-harmless / warranties", yn(a["holdHarmless"])),
                                   ("Checked items", ", ".join(a.get("opsChecks") or []) or "None"),
                                   ("Explanation", a.get("opsExplain") or "—")]),
        ("Legal", [("Bankruptcy / foreclosure 5 yrs", yn(a["bankrupt"])), ("Judgment / lien 5 yrs", yn(a["judgment"])),
                   ("Abuse / discrimination / negligent hiring claims", yn(a["abuseClaims"])),
                   ("Fraud / bribery / arson charge 5 yrs", yn(a["fraud"])), ("Explanation", a.get("legalExplain") or "—")]),
        ("Signature", [("Signed", a["signature"]), ("Attested and adopted e-signature", "Yes"),
                       ("$50 broker service fee agreement (§2119)", "Signed" if a.get("feeAgree") else "Not signed")]),
    ]
    story = [Paragraph(f"Bindswift · GL submission #{sub_id}", h1),
             Paragraph(f"{created.astimezone(NY):%b %d, %Y %I:%M %p} ET · Ref {public_id}", small), Spacer(1, 6)]
    for title, rows in sections:
        story.append(Paragraph(title, h2))
        tbl = Table([[p(k), p(v)] for k, v in rows], colWidths=[2.2 * inch, 4.9 * inch])
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#5B6878")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E3E7EC")),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(tbl)
    story += [Spacer(1, 12), Paragraph("Generated by Bindswift for broker review. Contains business information; handle as confidential.", small)]
    doc.build(story)
    return buf.getvalue()
