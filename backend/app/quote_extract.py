"""Pull the client-facing numbers out of a wholesaler/underwriter quote PDF.

Step 1 (always, free): layout-aware text extraction (pdfplumber) + label-based rules. Tested on
Hull & Co (Bridge Specialty / Western World) and XPT Specialty (USLI) quote formats.
Step 2 (optional): if ANTHROPIC_API_KEY is set, you can ask Claude to read the PDF instead, for
wholesaler formats the rules don't know. Either way you review and correct every field before the
client sees anything. Nothing is sent to the client automatically.

Internal-only fields (producer commission) are kept out of everything the client sees.
"""
import base64
import io
import json
import re

import httpx

from .config import get_settings

MONEY = r"\$\s?([\d,]+(?:\.\d{2})?)"
DATE = r"(\d{1,2}/\d{1,2}/\d{4})"


def _money(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def pdf_text(data: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(p.extract_text(layout=True) or "") for p in pdf.pages[:30]]
    t = "\n".join(pages)
    t = re.sub(r"[ \t]{2,}", "  ", t)
    t = t.replace("Q uote", "Quote").replace("Stampi ng", "Stamping")
    return t


def _first(t, *patterns, flags=re.I | re.M):
    for p in patterns:
        m = re.search(p, t, flags)
        if m:
            return m.group(1).strip()
    return None


LIMIT_LABELS = {
    "each_occurrence": [r"Each Occurrence(?: Limit)?"],
    "general_aggregate": [r"General Aggregate(?: Limit)?(?: \(Other Than Products-Completed Ops\))?"],
    "products_aggregate": [r"Products[-/ ]Completed Op(?:eration)?s Aggregate(?: Limit)?"],
    "personal_advertising": [r"Personal (?:Injury )?and Advertising Injury(?: Limit)?"],
    "damage_rented": [r"Damage To Premises Rented To You"],
    "medical": [r"Medical Expense(?: Limit)?(?: \(Any One Person\))?"],
}


def _limit(t, labels):
    for lab in labels:
        m = re.search(lab + r"\s*(\$[\d,]+|Included|Not Covered|Excluded|N/A)", t, re.I)
        if m:
            return m.group(1)
    return None


def _lines_after(t, header, stop, max_lines=15):
    """Requirement bullets after a header; joins lines that wrapped mid-sentence."""
    m = re.search(header, t, re.I)
    if not m:
        return []
    out = []
    for line in t[m.end():].splitlines():
        s = line.strip(" •o-\t")
        if not s:
            continue
        if re.search(stop, s, re.I):
            break
        if len(s) <= 12 or re.search(r"Page \d+ of \d+|Please contact us|FREE AND DISCOUNTED|Read the quote carefully|"
                                     r"^[A-Za-z ]+ Requirements$", s, re.I):
            continue
        if out and s[0].islower():
            out[-1] = out[-1] + " " + s
        else:
            if len(out) >= max_lines:
                break
            out.append(s)
    return out


def extract_rules(data: bytes) -> dict:
    t = pdf_text(data)
    head = "\n".join(t.strip().splitlines()[:8])
    d = {"source": "rules"}

    d["wholesaler"] = _first(head, r"^\s*(.*?(?:LLC|Inc\.?|Company|Agency|Brokerage)[^\n]*?)\s*$")
    d["carrier"] = _first(t, r"^\s*CARRIER:\s*(.+?)\s*$", r"^\s*Carrier:\s*(.+?)\s*$", r"Quoted By\s*\n\s*(.+?)\s*\(")
    if d["carrier"]:
        d["carrier"] = re.sub(r"\s{2,}.*$", "", d["carrier"])
    if re.search(r"Non-?Admitted", t, re.I):
        d["admitted"] = "Non-admitted (surplus lines)"
    elif re.search(r"Status:\s*Admitted|an admitted", t, re.I):
        d["admitted"] = "Admitted"
    d["am_best"] = _first(t, r"BEST RATING:\s*([^;)\n]+)", r"A\.M\. Best Rating:\s*([^\n]+?)\s*$")
    if d["am_best"] and d["am_best"].count("(") > d["am_best"].count(")"):
        d["am_best"] += ")"
    d["quote_number"] = _first(t, r"quote number is\s+([A-Z0-9]+(?:\s+Version\s+\d+)?)", r"Quote No:\s*([A-Z]?\s?[\d-]+)",
                               r"SUBMISSION NUMBER:\s*(\S+)")
    d["coverage"] = _first(t, r"Policy Type:\s*(Commercial General Liability)", r"an? (?:non-)?admitted (.+?) quote for",
                           r"^\s*COVERAGE:\s*(.+?)\s*$")
    d["policy_form"] = _first(t, r"Policy Type:\s*(Occurrence|Claims[- ]Made)")
    period = re.search(r"POLICY PERIOD:\s*" + DATE + r"\s*(?:TO|-)\s*" + DATE, t, re.I) or \
        re.search(r"Policy Period:\s*" + DATE + r"\s*(?:TO|-)\s*" + DATE, t, re.I)
    d["policy_start"], d["policy_end"] = (period.group(1), period.group(2)) if period else (None, None)
    d["quote_expires"] = _first(t, r"QUOTE EXPIRATION DATE:\s*" + DATE, r"Quote is valid until\s*" + DATE)
    d["issued"] = _first(t, r"DATE ISSUED:\s*" + DATE)

    # premium lines
    lines = []
    for m in re.finditer(r"^\s*(Commercial General Liability|General Liability|Directors & Officers[^$\n]*?|"
                         r"Professional[^$\n]*?|Property[^$\n]*?)\s+" + MONEY + r"\s*$", t, re.I | re.M):
        lab, amt = m.group(1).strip().rstrip(":"), _money(m.group(2))
        if (amt and len(lab) <= 80 and not re.search(r"Premium for|Incl\b|\d{4,}", lab)
                and not any(x["label"] == lab or x["amount"] == amt for x in lines)):
            lines.append({"label": lab, "amount": amt})
    d["premium_lines"] = lines
    prem = _first(t, r"^\s*Premium:\s*" + MONEY, r"TOTAL PREMIUM DUE TO CARRIER\s*" + MONEY, r"Total Premium\s*" + MONEY)
    d["premium"] = _money(prem) if prem else (sum(x["amount"] for x in lines) or None)

    fees, taxes = [], []
    for m in re.finditer(r"^\s*((?:Wholesaler )?Broker Fee|Inspection Fee|Policy Fee|Carrier Fee|Underwriting Fee)\s*" + MONEY,
                         t, re.I | re.M):
        a = _money(m.group(2))
        if a and not any(f["label"] == m.group(1) for f in fees):
            fees.append({"label": m.group(1), "amount": a})
    for m in re.finditer(r"^\s*((?:NY )?SL Tax[^$\n]*|Stamping Fee[^$\n]*|SL Stamp Fee|Surplus Lines Tax[^$\n]*|"
                         r"Fire Marshal Tax[^$\n]*)\s*" + MONEY, t, re.I | re.M):
        lab = re.sub(r"\s+", " ", m.group(1)).strip()
        a = _money(m.group(2))
        key = "tax" if "tax" in lab.lower() else "stamp"
        if a and not any(("tax" if "tax" in x["label"].lower() else "stamp") == key for x in taxes):
            taxes.append({"label": lab, "amount": a})
    d["fees"], d["taxes"] = fees, taxes
    total = _first(t, r"^\s*Total:\s*" + MONEY, r"TOTAL AMOUNT DUE\s*" + MONEY, r"Grand Total\s*" + MONEY)
    d["total"] = _money(total)

    d["limits"] = {k: _limit(t, v) for k, v in LIMIT_LABELS.items()}
    d["deductible"] = _first(t, r"General Liability Deductible\s*(\$[\d,]+|N/A|None)", r"^\s*Deductible\s*(\$[\d,]+|N/A|None)")
    if re.search(r"Agency Billed", t, re.I):
        d["billing"] = "Agency bill"
    elif re.search(r"eligible to be Direct Billed|Direct Bill", t, re.I):
        d["billing"] = "Direct bill available"
    tr = _first(t, r"Terrorism Coverage:\s*" + MONEY, r"Terrorism[^$\n]{0,40}\(add:\s*\*?" + MONEY)
    d["terrorism_optional"] = _money(tr)
    d["prior_to_bind"] = (_lines_after(t, r"Prior To Bind Requirements:", r"^[B-Z]\.\s|^Subject to underwriter", 12)
                          or _lines_after(t, r"quote subject to:", r"^Application List|^Location", 6))
    d["internal"] = {"commission": _first(t, r"Producer Commission:\s*([\d.]+\s*%)")}

    # sanity check: premium + fees + taxes should equal the total
    if d["premium"] and d["total"]:
        calc = round(d["premium"] + sum(f["amount"] for f in fees) + sum(x["amount"] for x in taxes), 2)
        d["checks"] = {"sum_matches_total": abs(calc - d["total"]) < 0.02, "calculated": calc}
    d["missing"] = [k for k in ("carrier", "premium", "total", "quote_expires") if not d.get(k)] + \
                   [f"limit:{k}" for k in ("each_occurrence", "general_aggregate") if not d["limits"].get(k)]
    return d


AI_SCHEMA_HINT = {
    "wholesaler": "string", "carrier": "string", "admitted": "Admitted | Non-admitted (surplus lines)",
    "am_best": "string", "quote_number": "string", "coverage": "string", "policy_form": "Occurrence | Claims-made",
    "policy_start": "MM/DD/YYYY", "policy_end": "MM/DD/YYYY", "quote_expires": "MM/DD/YYYY",
    "premium_lines": [{"label": "string", "amount": 0}], "premium": 0,
    "fees": [{"label": "string", "amount": 0}], "taxes": [{"label": "string", "amount": 0}], "total": 0,
    "limits": {"each_occurrence": "$", "general_aggregate": "$", "products_aggregate": "$",
               "personal_advertising": "$", "damage_rented": "$", "medical": "$"},
    "deductible": "string", "billing": "Agency bill | Direct bill | Direct bill available",
    "terrorism_optional": 0, "prior_to_bind": ["string"], "internal": {"commission": "string"},
}


def extract_ai(data: bytes, client: httpx.Client | None = None) -> dict:
    """Optional: Claude reads the PDF and returns the same JSON shape. Requires ANTHROPIC_API_KEY."""
    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY to use AI extraction.")
    prompt = ("This is an insurance quote from a wholesaler/underwriter to a retail broker. Extract the fields below. "
              "Use null for anything not stated; never guess. Amounts are numbers without $ or commas. "
              "Put producer/broker commission ONLY in internal.commission. Reply with JSON only, matching:\n"
              + json.dumps(AI_SCHEMA_HINT))
    body = {"model": s.anthropic_model, "max_tokens": 2000,
            "messages": [{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(data).decode()}},
                {"type": "text", "text": prompt}]}]}
    own = client is None
    client = client or httpx.Client(timeout=90)
    try:
        r = client.post("https://api.anthropic.com/v1/messages", json=body,
                        headers={"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    finally:
        if own:
            client.close()
    m = re.search(r"\{.*\}", text, re.S)
    d = json.loads(m.group(0)) if m else {}
    d["source"] = "ai"
    d.setdefault("limits", {})
    d.setdefault("internal", {})
    d["missing"] = [k for k in ("carrier", "premium", "total", "quote_expires") if not d.get(k)]
    return d
