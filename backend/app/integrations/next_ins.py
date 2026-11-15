"""NEXT (ERGO NEXT) for contractors' General Liability.

NEXT has no public quoting API. Programmatic quote/bind runs through its NEXT Connect partner
program, which requires a partnership agreement. Until then, Bindswift:
  1) pre-checks the risk against NEXT's published contractor eligibility rules, and
  2) produces an answer sheet you can key into NEXT's agent portal in a couple of minutes.

Rules below come from NEXT's Contractors Coverage Guide (undated). Re-check them in your NEXT
portal's appetite checker; they change.
"""

NEXT_TRADES = {"remodel", "carpentry", "handyman", "paint_int", "paint_ext", "roofing", "tile", "hvac"}


def eligibility(a: dict) -> list[str]:
    out = []
    areas = set(a.get("areas") or [])
    # NEXT's contractor guide lists "performs work in the state of New York" as ineligible.
    if areas & {"Brooklyn", "Queens", "Manhattan", "Bronx", "Staten Island", "Long Island", "Westchester"}:
        out.append("Works in New York State: NEXT's contractor guide lists this as ineligible. "
                   "Check NEXT's appetite checker for this class before quoting.")
    if max(float(a.get("revenue") or 0), float(a.get("payroll") or 0)) > 5_000_000:
        out.append("Revenue or payroll over $5M.")
    if int(a.get("losses") or 0) > 2:
        out.append("More than 2 claims (NEXT's limit is 2 in the last 3 years).")
    if a.get("bankrupt") == "Y" or a.get("judgment") == "Y":
        out.append("Bankruptcy, tax or credit lien (NEXT excludes these in the last 3 years).")
    if a.get("fraud") == "Y":
        out.append("Fraud, bribery or arson conviction in the last 5 years.")
    if a.get("height") in ("ext7",):
        out.append("Exterior work above 30 feet (3 stories).")
    if "hazmat" in (a.get("opsChecks") or []):
        out.append("Hazardous material abatement or remediation.")
    if a.get("trade") not in NEXT_TRADES:
        out.append("Trade isn't on NEXT's highlighted contractor list. Confirm in the appetite checker.")
    return out


def answer_sheet(a: dict) -> list[tuple[str, str]]:
    """The answers NEXT's contractor GL flow asks for, in plain order, ready to key in."""
    occ = "$2M / $4M" if a.get("limits") == "2/4" else "$1M / $2M"
    ais = [i for i in (a.get("interests") or []) if i.get("type") == "AdditionalInsured"] if a.get("gcReq") == "Y" else []
    return [
        ("Business name", a.get("legalName", "")),
        ("Business type", a.get("entity", "")),
        ("Owner / contact", f"{a.get('contactName','')} · {a.get('phone','')} · {a.get('email','')}"),
        ("Address", f"{a.get('street','')}, {a.get('city','')}, NY {a.get('zip','')}"),
        ("Year started", str(a.get("started", ""))[:4]),
        ("Profession", a.get("trade", "")),
        ("What the business does", a.get("opsDesc", "")),
        ("Annual revenue", f"${float(a.get('revenue') or 0):,.0f}"),
        ("Annual payroll", f"${float(a.get('payroll') or 0):,.0f}"),
        ("Employees (FT / PT)", f"{a.get('ft', 0)} / {a.get('pt', 0)}"),
        ("Subcontractor costs", f"${float(a.get('subCost') or 0):,.0f}"),
        ("Claims in last 5 yrs", str(a.get("losses", "0"))),
        ("Limits", occ),
        ("Start date", str(a.get("effDate", ""))),
        ("Additional insureds", "; ".join(f"{i['name']}, {i['street']}, {i['city']}, {i['state']} {i['zip']}" for i in ais) or "None"),
    ]
