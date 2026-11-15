"""Answers -> ACORD 125 (2016/03) and ACORD 126 (2016/09).

Modeled on a real final GL submission (Peter's Handyman Solutions): only the boxes
an actual submission fills are mapped. Field names are the real ones in your blank
PDFs. Checkboxes: ACORD 125 uses 'On', ACORD 126 uses '1'.

Some things on that sample aren't fillable fields in the blank 125, so pdfs.py draws
them on the page instead ("overlays"):
  - the X next to COMMERCIAL GENERAL LIABILITY under Lines of Business
  - the Y/N letters for General Information questions 1a-15 on page 3
  - the script signatures on both forms
"""
from datetime import date

from .config import get_settings
from .models import GLAnswers

TRADES = {
    "carpentry":  dict(l="Carpentry",                   code="91342", naics="238350"),
    "paint_int":  dict(l="Interior Painting",           code="92478", naics="238320"),
    "paint_ext":  dict(l="Exterior Painting",           code="92453", naics="238320"),
    "electrical": dict(l="Electrical Contractor",       code="92215", naics="238210"),
    "plumbing":   dict(l="Plumbing Contractor",         code="98482", naics="238220"),
    "hvac":       dict(l="HVAC Contractor",             code="95647", naics="238220"),
    "masonry":    dict(l="Masonry Contractor",          code="97447", naics="238140"),
    "tile":       dict(l="Tile, Stone & Marble",        code="99746", naics="238340"),
    "drywall":    dict(l="Drywall Contractor",          code="92338", naics="238310"),
    "handyman":   dict(l="Handyman Services",           code="91342", naics="236118"),
    "remodel":    dict(l="Residential Remodeling",      code="91583", naics="236118"),
    "roofing":    dict(l="Roofing Contractor",          code="98677", naics="238160"),
}
INTEREST_LABELS = {"AdditionalInsured": "Additional Insured", "EmployeeAsLessor": "Employee as Lessor",
                   "LendersLossPayable": "Lender's Loss Payable", "Lienholder": "Lienholder",
                   "LossPayee": "Loss Payee", "Mortgagee": "Mortgagee"}
INTEREST_126 = {"AdditionalInsured": "AdditionalInsured", "EmployeeAsLessor": "EmployeeAsLessor",
                "LendersLossPayable": "LendersLossPayable", "Lienholder": "Lienholder",
                "LossPayee": "LossPayee", "Mortgagee": "Mortgagee"}
INTEREST_125 = {"AdditionalInsured": "ACORD_125_Interest_1_AdditionalInsured",
                "EmployeeAsLessor": "ACORD_125_Interest_1_EmpAsLessor",
                "Lienholder": "ACORD_125_Interest_1_Lienholder",
                "LossPayee": "ACORD_125_Interest_1_LossPayee"}   # others use "Other" + description
NYC_COUNTIES = {"Kings", "Queens", "New York", "Bronx", "Richmond"}
LIMITS = {"1/2": (1_000_000, 2_000_000), "2/4": (2_000_000, 4_000_000)}

# ACORD 125 page 3, questions 1a-15, in order. Values are the checkbox field each answer sits on.
GENERAL_125 = ["Subsidiary", "Parent", "Safety", "Exposure", "OtherInsurance", "CoverageTerminated",
               "PastAllegations", "IndictedOrConvicted", "SafetyViolations", "NegativeFinancialAction",
               "JudgementLien", "Trust", "Foreign", "OtherVentures", "PossessDrones", "HireDrones"]

# ACORD 126 Y/N text fields, in the order they appear on the form
CONTRACTORS_126 = ["Contractors_Question_AABCode_A",   # 1 draws plans for others
                   "Contractors_Question_AAICode_A",   # 2 blasting / explosives
                   "Contractors_Question_AACCode_A",   # 3 excavation / underground
                   "Contractors_Question_ABBCode_A",   # 4 subs carry lower limits
                   "Contractors_Question_AADCode_A",   # 5 subs work without a COI
                   "Contractors_Question_AAECode_A"]   # 6 leases equipment to others
PRODUCTS_126 = [f"GeneralLiabilityLineOfBusiness_Question_{c}Code_A"
                for c in ("AAJ", "ABA", "ABB", "ABC", "ABD", "ABE", "ABF", "ABG", "ABH", "ABI")]
GENERAL_126 = ([f"GeneralLiabilityLineOfBusiness_Question_{c}Code_A"
                for c in ("ABJ", "ACA", "ACJ", "ACB", "ACC")]                 # 1-5
               + ["Contractors_Question_AAHCode_A"]                          # 6 watercraft
               + [f"GeneralLiabilityLineOfBusiness_Question_{c}Code_A"
                  for c in ("ACD", "KAG", "ACE", "KAA", "KAH", "ACF", "KAB", "ACG", "ACH",   # 7-15
                            "AAB", "AAC", "AAD", "AAE", "AAF")]                              # 16-20
               + ["Contractors_Question_AAGCode_A",                          # 21 written safety policy
                  "GeneralLiabilityLineOfBusiness_Question_AAGCode_A"])      # 22 promo literature


def mdy(d: date | None) -> str:
    return d.strftime("%m/%d/%Y") if d else ""


def plus_year(d: date) -> str:
    try:
        return mdy(d.replace(year=d.year + 1))
    except ValueError:
        return mdy(d.replace(year=d.year + 1, day=28))


def c(n: float) -> str:
    return f"{n:,.0f}"


def build_acord(a: GLAnswers, fein: str) -> dict:
    """Returns {'acord125': {...}, 'acord126': {...}, 'yn125': {...}, 'signer': ..., 'producer': ...}.
    fein is passed in so callers choose full (stored, encrypted) or masked (email)."""
    s = get_settings()
    t = TRADES[a.trade]
    occ, agg = LIMITS[a.limits]
    ops = set(a.opsChecks)
    yn = lambda b: "Y" if b else "N"
    today = mdy(date.today())
    name = a.legalName.upper() + (f" DBA {a.dba.upper()}" if a.dba else "")
    has_subs = a.subCost > 0

    desc = [t["l"], f"Operations: {a.opsDesc}"]
    if a.premises == "Home":
        desc.append("Provided location is the home office.")

    f125 = {
        # header / producer
        "ACORD_CurrentDate": today,
        "ACORD_AgencyName": "\n".join(x for x in (s.agency_name, s.agency_address.replace("\\n", "\n")) if x),
        "ACORD_ProducerContact": s.producer_name, "ACORD_ProducerPhoneNumber": s.producer_phone,
        "ACORD_ProducerEmailAddress": s.producer_email,
        "ACORD_Transaction_Quote": "On", "ACORD_Transaction_Date": today,
        # policy
        "ACORD_Policy_EffectiveDate": mdy(a.effDate), "ACORD_Policy_ExpirationDate": plus_year(a.effDate),
        "ACORD_Policy_DirectPlan": "On", "ACORD_Policy_PaymentPlan": a.payPlan,
        # applicant
        "ACORD_Policy_Insured1_Name": name,
        "ACORD_Policy_Insured1_MailingAddress": f"{a.street}, {a.city}, NY {a.zip}",
        "ACORD_Policy_Insured1_NAICS": t["naics"],
        "ACORD_Policy_Insured1_FEINSSN": fein, "ACORD_Policy_Insured1_PhoneNumber": a.phone,
        f"ACORD_Policy_Insured1_Type_{a.entity}": "On",
        # contact
        "ACORD_Contact1_Type": a.contactTitle, "ACORD_Contact1_Name": a.contactName,
        "ACORD_Contact1_PrimaryPhoneNumber": a.phone, "ACORD_Contact1_PrimaryCellPhone": "On",
        "ACORD_Contact1_PrimaryEmailAddress": str(a.email),
        # premises
        "ACORD_Location1_LocationNumber": "1", "ACORD_Location1_BuildingNumber": "1",
        "ACORD_Location1_Street": a.street, "ACORD_Location1_City": a.city, "ACORD_Location1_State": "NY",
        "ACORD_Location1_County": a.county, "ACORD_Location1_ZIP": a.zip,
        f"ACORD_Location1_CityLimits_{'Inside' if a.county in NYC_COUNTIES else (a.cityLimits or 'Inside')}": "On",
        "ACORD_Location1_Employees_FullTime": str(a.ft),
        "ACORD_Location1_AnnualRevenue": c(a.revenue),
        # nature of business
        "ACORD_NatureOfBusiness_Contractor": "On",
        "ACORD_NatureOfBusiness_Description": "\n".join(desc),
        "ACORD_NatureOfBusiness_InstallationPercentage": str(100 - a.offsitePct),
        "ACORD_NatureOfBusiness_OffPremisesInstallation": str(a.offsitePct),
        # signature block
        "ACORD_Signatures_Producer_Name": s.producer_name,
        "ACORD_Signatures_Producer_NationalNumber": s.producer_npn,
        "ACORD_Signatures_Applicant_Date": today,
    }
    if a.pt:
        f125["ACORD_Location1_Employees_PartTime"] = str(a.pt)
    if a.premises in ("Owner", "Tenant"):
        f125[f"ACORD_Location1_Interest_{a.premises}"] = "On"
    elif a.premises == "Home":
        f125["ACORD_Location1_Interest_Owner"] = "On"
    if a.entity == "LLC" and a.members:
        f125["ACORD_Policy_Insured1_Type_NumberofMembers"] = a.members
    if a.safety == "Y":
        f125["ACORD_General_Safety_SafetyManual"] = "On"
        if a.osha == "Y":
            f125["ACORD_General_Safety_OSHA"] = "On"
    if a.cancelled == "Y" and a.cancelWhy:
        f125[f"ACORD_General_CoverageTerminated_{a.cancelWhy}"] = "On"
    if a.losses == "0":
        f125["ACORD_LossHistory_None"] = "On"
    else:
        f125.update({"ACORD_LossHistory_NumberOfYears": "5", "ACORD_LossHistory_TotalLosses": c(a.lossPaid),
                     "ACORD_LossHistory_1_LOB": "GL", "ACORD_LossHistory_1_Description": a.lossDesc,
                     "ACORD_LossHistory_1_AmountPaid": c(a.lossPaid)})
    remarks = []
    if a.insured == "Y" and a.priorCarrier:
        remarks.append(f"Prior GL: {a.priorCarrier}" + (f" #{a.priorPolicy}" if a.priorPolicy else "")
                       + (f", exp {mdy(a.priorExp)}" if a.priorExp else "") + ".")
    if a.opsExplain:
        remarks.append(a.opsExplain)
    if a.legalExplain:
        remarks.append(a.legalExplain)
    if remarks:
        f125["ACORD_General_Remarks"] = " ".join(remarks)

    yn125 = dict(zip(GENERAL_125, map(yn, [
        "parent" in ops, False, a.safety == "Y", bool(ops & {"flammables", "blasting"}), False,
        a.cancelled == "Y", a.abuseClaims == "Y", a.fraud == "Y", "violations" in ops, a.bankrupt == "Y",
        a.judgment == "Y", False, False, "otherbiz" in ops, "drones" in ops, "drones" in ops])))

    f126 = {
        "Producer_FullName_A": s.agency_name, "Form_CompletionDate_A": today,
        "Policy_EffectiveDate_A": mdy(a.effDate), "NamedInsured_FullName_A": name,
        "GeneralLiability_CoverageIndicator_A": "1", "GeneralLiability_OccurrenceIndicator_A": "1",
        "GeneralLiability_GeneralAggregate_LimitAppliesPerPolicyIndicator_A": "1",
        "GeneralLiability_GeneralAggregate_LimitAmount_A": c(agg),
        "GeneralLiability_EachOccurrence_LimitAmount_A": c(occ),
        "GeneralLiabilityLineOfBusiness_TypeOfWorkSubcontractedDescription_A": a.subWork if has_subs else "N/A",
        "Producer_AuthorizedRepresentative_FullName_A": s.producer_name,
        "Producer_NationalIdentifier_A": s.producer_npn,
        "NamedInsured_SignatureDate_A": today,
    }
    if has_subs:
        f126["Contractors_SubcontractorsPaidAmount_A"] = c(a.subCost)
        if a.subPct:
            f126["Contractors_WorkSubcontractedPercent_A"] = a.subPct
    for k, v in zip(CONTRACTORS_126, [
            "design" in ops, "blasting" in ops, "excavation" in ops,
            has_subs and a.subsInsured == "N", has_subs and a.subsInsured == "N", "leaseequip" in ops]):
        f126[k] = yn(v)
    for i, k in enumerate(PRODUCTS_126):
        f126[k] = yn(a.install == "Y") if i == 0 else yn(i == 3 and a.holdHarmless == "Y")
    if a.install == "Y" and a.installDesc:
        f126["GeneralLiabilityLineOfBusiness_ApplicantInstallServiceProductsExplanation_A"] = a.installDesc
    general_vals = {2: "hazmat" in ops, 4: "leaseequip" in ops, 13: "structural" in ops, 14: "demolition" in ops,
                    15: "jv" in ops, 16: "leaseemp" in ops, 20: a.safety == "Y"}
    for i, k in enumerate(GENERAL_126):
        f126[k] = yn(general_vals.get(i, False))
    # Claims-made questions 3 and 4 (occurrence policy, so No unless your broker says otherwise)
    f126["GeneralLiabilityLineOfBusiness_Question_AAHCode_A"] = "N"
    f126["GeneralLiabilityLineOfBusiness_Question_AAICode_A"] = "N"

    # Additional interests: first one goes in the form's own section on both forms,
    # everyone (if more than one) goes on an attached schedule page.
    interests = list(a.interests) if a.gcReq == "Y" else []
    notes126 = []
    if interests:
        i1 = interests[0]
        f125.update({
            "ACORD_125_Interest_1_NameAddress": f"{i1.name}\n{i1.street}\n{i1.city}, {i1.state} {i1.zip}",
            "ACORD_125_Interest_1_ReferenceNumber": i1.ref,
        })
        if i1.type in INTEREST_125:
            f125[INTEREST_125[i1.type]] = "On"
        else:
            f125["ACORD_125_Interest_1_Other"] = "On"
            f125["ACORD_125_Interest_1_OtherDesc"] = INTEREST_LABELS[i1.type]
        if i1.cert:
            f125["ACORD_125_Interest_1_Certificate"] = "On"
        f126.update({
            f"AdditionalInterest_Interest_{INTEREST_126[i1.type]}Indicator_A": "1",
            "AdditionalInterest_InterestRank_A": "1",
            "AdditionalInterest_FullName_A": i1.name,
            "AdditionalInterest_MailingAddress_LineOne_A": i1.street,
            "AdditionalInterest_MailingAddress_CityName_A": i1.city,
            "AdditionalInterest_MailingAddress_StateOrProvinceCode_A": i1.state,
            "AdditionalInterest_MailingAddress_PostalCode_A": i1.zip,
            "AdditionalInterest_AccountNumberIdentifier_A": i1.ref,
        })
        if i1.cert:
            f126["AdditionalInterest_CertificateRequiredIndicator_A"] = "1"
        if any(i.type == "AdditionalInsured" for i in interests):
            notes126.append("Additional Insured requested (blanket AI, primary & non-contributory, waiver of "
                            "subrogation where required by written contract).")
        if len(interests) > 1:
            note = f"{len(interests)} additional interests: see attached Additional Interest Schedule."
            notes126.append(note)
            f125["ACORD_General_Remarks"] = (f125.get("ACORD_General_Remarks", "") + " " + note).strip()
    if notes126:
        f126["GeneralLiabilityLineOfBusiness_RemarkText_A"] = " ".join(notes126)

    clean = lambda d: {k: v for k, v in d.items() if v not in ("", None)}
    schedule = [dict(rank=n, type=INTEREST_LABELS[i.type], name=i.name,
                     address=f"{i.street}, {i.city}, {i.state} {i.zip}", ref=i.ref, cert=i.cert)
                for n, i in enumerate(interests, 1)] if len(interests) > 1 else []
    return {"acord125": clean(f125), "acord126": clean(f126), "yn125": yn125,
            "signer": a.signature, "producer": s.producer_name, "schedule": schedule,
            "insured": name, "effective": mdy(a.effDate)}
