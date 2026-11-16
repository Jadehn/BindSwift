"""Validation for what the contractor submits. The server trusts nothing from the
browser: every field is type-checked and length-limited here, and the ACORD
field map is rebuilt on the server from these answers."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

YN = Literal["Y", "N"]
Short = Field(max_length=120)
Long = Field(default="", max_length=2000)

TRADE_IDS = Literal["carpentry", "paint_int", "paint_ext", "electrical", "plumbing", "hvac",
                    "masonry", "tile", "drywall", "handyman", "remodel", "roofing",
                    "salon", "barber", "beauty", "consultant", "marketing", "bookkeeping",
                    "doula", "wellness", "retail"]
CONTRACTOR_TRADES = {"carpentry", "paint_int", "paint_ext", "electrical", "plumbing", "hvac",
                     "masonry", "tile", "drywall", "handyman", "remodel", "roofing"}
OPS = Literal["design", "blasting", "excavation", "demolition", "structural", "hazmat", "flammables",
              "leaseequip", "leaseemp", "jv", "drones", "otherbiz", "parent", "violations"]
AREAS = Literal["Brooklyn", "Queens", "Manhattan", "Bronx", "Staten Island", "Long Island", "Westchester"]


def _money(v) -> float:
    s = "".join(c for c in str(v) if c.isdigit() or c == ".")
    n = float(s) if s else 0.0
    if n < 0 or n > 500_000_000:
        raise ValueError("Amount out of range")
    return n


INTEREST_TYPES = Literal["AdditionalInsured", "EmployeeAsLessor", "LendersLossPayable",
                         "Lienholder", "LossPayee", "Mortgagee"]


class Interest(BaseModel):
    """Someone who must be named on the policy: a GC or owner (additional insured),
    a lender on equipment (lienholder / loss payee), a mortgagee, etc."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    type: INTEREST_TYPES
    name: str = Field(min_length=2, max_length=120)
    street: str = Field(min_length=3, max_length=120)
    city: str = Field(min_length=2, max_length=60)
    state: str = Field(default="NY", pattern=r"^[A-Za-z]{2}$")
    zip: str = Field(pattern=r"^\d{5}$")
    ref: str = Field(default="", max_length=40)       # loan / account / contract number
    cert: bool = True                                # send them a certificate

    @field_validator("state")
    @classmethod
    def upper(cls, v):
        return v.upper()


class GLAnswers(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # business
    legalName: str = Field(min_length=2, max_length=120)
    dosId: str = Field(default="", max_length=12, pattern=r"^\d*$")    # NY DOS ID picked from state records
    bizUnlisted: bool = False                                          # contractor said they're not in NY records
    regState: Literal["NY", "NJ", "NC", "DE", "MD"] = "NY"             # state the business is registered in
    regId: str = Field(default="", max_length=20, pattern=r"^[A-Za-z0-9-]*$")  # that state's entity ID
    dba: str = Field(default="", max_length=120)
    entity: Literal["LLC", "Corporation", "SCorp", "Individual", "Partnership", "JointVenture"]
    members: str = Field(default="", max_length=3, pattern=r"^\d*$")
    fein: str = Field(default="", max_length=12, pattern=r"^(\d{2}-?\d{7})?$")
    started: date
    # contact
    contactName: str = Field(min_length=2, max_length=80)
    contactTitle: Literal["Owner", "Partner", "Office manager", "Other"]
    phone: str = Field(max_length=20)
    email: EmailStr
    website: str = Field(default="", max_length=120)
    # address
    street: str = Field(min_length=3, max_length=120)
    city: str = Field(min_length=2, max_length=60)
    zip: str = Field(pattern=r"^\d{5}$")
    county: Literal["Kings", "Queens", "New York", "Bronx", "Richmond", "Nassau", "Suffolk", "Westchester", "Other"]
    premises: Literal["Owner", "Tenant", "Home"]
    cityLimits: Literal["Inside", "Outside"] | None = None   # asked outside NYC; NYC is always inside
    sqft: str = Field(default="", max_length=7, pattern=r"^\d*$")
    # work
    trade: TRADE_IDS
    opsDesc: str = Field(min_length=10, max_length=1000)
    resPct: Literal["100", "75", "50", "25"] | None = None
    height: Literal["1-3", "4-6", "7+", "ext7"] | None = None      # contractors only
    areas: list[AREAS] = Field(default_factory=list, max_length=7)  # contractors only
    # main-street classes
    stations: str = Field(default="", max_length=3, pattern=r"^\d*$")
    services: list[Literal["cut", "color", "nails", "lash", "braid", "wax"]] = Field(default_factory=list, max_length=6)
    boothRental: YN | None = None
    licensed: YN | None = None
    clientVisits: YN | None = None
    wantEO: YN | None = None
    workPlaces: list[Literal["homes", "hospital", "studio", "online"]] = Field(default_factory=list, max_length=4)
    certified: YN | None = None
    ownBrand: YN | None = None
    sellOnline: YN | None = None
    offsitePct: int = Field(default=100, ge=0, le=100)  # share of work done at customers' sites
    # crew
    ft: int = Field(ge=0, le=500)
    pt: int = Field(ge=0, le=500)
    revenue: float
    payroll: float
    subCost: float = 0
    subPct: str = Field(default="", max_length=3, pattern=r"^\d*$")
    subWork: str = Field(default="", max_length=200)
    subsInsured: YN | None = None
    # coverage
    effDate: date
    limits: Literal["1/2", "2/4"]
    deduct: Literal["0", "500", "1000", "2500"] = "0"
    payPlan: Literal["Monthly", "Annual"] = "Monthly"
    gcReq: YN
    interests: list[Interest] = Field(default_factory=list, max_length=10)
    holderName: str = Field(default="", max_length=120)   # legacy single-holder fields
    holderAddr: str = Field(default="", max_length=200)
    # history
    insured: YN
    priorCarrier: str = Field(default="", max_length=120)
    priorPolicy: str = Field(default="", max_length=60)
    priorExp: date | None = None
    priorPrem: float = 0
    losses: Literal["0", "1", "2", "3"]
    lossDesc: str = Long
    lossPaid: float = 0
    cancelled: YN
    cancelWhy: Literal["NonPayment", "NonRenewal", "AgentStopRepresenting", "Underwriting"] | None = None
    # ops / safety
    safety: YN = "N"
    osha: YN | None = None
    install: YN = "N"
    installDesc: str = Field(default="", max_length=300)
    holdHarmless: YN
    opsChecks: list[OPS] = Field(default_factory=list, max_length=14)
    opsExplain: str = Long
    # legal
    bankrupt: YN
    judgment: YN
    abuseClaims: YN
    fraud: YN
    legalExplain: str = Long
    profileRef: str = Field(default="", max_length=36, pattern=r"^[0-9a-f-]*$")   # the short profile this continues
    # signature
    initials: str = Field(default="", max_length=5)
    signature: str = Field(min_length=2, max_length=80)
    feeAgree: Literal[True]        # signed §2119 broker service fee agreement
    attest: Literal[True]

    @field_validator("revenue", "payroll", "subCost", "priorPrem", "lossPaid", mode="before")
    @classmethod
    def money(cls, v):
        return _money(v)

    @field_validator("phone")
    @classmethod
    def phone_digits(cls, v):
        d = "".join(c for c in v if c.isdigit())
        if len(d) != 10:
            raise ValueError("Phone must have 10 digits")
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"

    @field_validator("priorExp", mode="before")
    @classmethod
    def blank_date(cls, v):
        return v or None

    @model_validator(mode="after")
    def cross_checks(self):
        if self.effDate < date.today():
            raise ValueError("Effective date can't be in the past")
        if self.started > date.today():
            raise ValueError("Start date can't be in the future")
        if self.gcReq == "Y" and not self.interests:
            if self.holderName:  # older clients sent a single holder
                self.interests = [Interest(type="AdditionalInsured", name=self.holderName,
                                           street=self.holderAddr or "Address on file", city="New York", zip="10001")]
            else:
                raise ValueError("Add at least one person or company to list on the policy")
        if self.trade in CONTRACTOR_TRADES:
            if not self.height or not self.areas:
                raise ValueError("Tell us how tall the buildings are and where you work")
        else:   # main-street businesses: contractor-only answers don't apply
            self.subCost, self.subsInsured, self.offsitePct, self.install = 0, None, 100, "N"
        if self.subCost > 0 and self.subsInsured is None:
            raise ValueError("Tell us whether subcontractors carry insurance")
        return self


PROFILE_CLASSES = Literal["bookkeeping", "consultant", "marketing", "salon", "barber", "beauty", "wellness",
                          "retail", "doula"]
COUNTY = Literal["Kings", "Queens", "New York", "Bronx", "Richmond", "Nassau", "Suffolk", "Westchester", "Other"]


class ProfileAnswers(BaseModel):
    """The short business profile that decides whether we can quote online right away."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    trade: PROFILE_CLASSES
    kind: str = Field(min_length=1, max_length=20, pattern=r"^[a-z_]+$")
    legalName: str = Field(min_length=2, max_length=120)
    dba: str = Field(default="", max_length=120)
    entity: Literal["LLC", "Corporation", "SCorp", "Individual", "Partnership"]
    started: date
    website: str = Field(default="", max_length=120)
    street: str = Field(min_length=3, max_length=120)
    suite: str = Field(default="", max_length=20)
    city: str = Field(min_length=2, max_length=60)
    zip: str = Field(pattern=r"^\d{5}$")
    county: COUNTY
    premises: Literal["Owner", "Tenant", "Home"]
    sqft: str = Field(default="", max_length=7, pattern=r"^\d*$")
    revenue: float
    effDate: date
    limits: Literal["1/2", "2/4"]
    insured: YN
    claims: Literal["0", "1", "2", "3"]
    install: YN
    productsDesc: str = Field(default="", max_length=300)
    productsPct: str = Field(default="", max_length=3, pattern=r"^\d*$")
    productsUse: str = Field(default="", max_length=300)
    productsMadeWhere: str = Field(default="", max_length=120)
    productsMaker: str = Field(default="", max_length=120)
    contactName: str = Field(min_length=2, max_length=80)
    phone: str = Field(max_length=20)
    email: EmailStr
    signature: str = Field(min_length=2, max_length=80)
    feeAgree: Literal[True]
    attest: Literal[True]

    @field_validator("revenue", mode="before")
    @classmethod
    def money(cls, v):
        return _money(v)

    @field_validator("phone")
    @classmethod
    def phone_digits(cls, v):
        d = "".join(c for c in v if c.isdigit())
        if len(d) != 10:
            raise ValueError("Phone must have 10 digits")
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"

    @model_validator(mode="after")
    def cross_checks(self):
        from .appetite import CLASSES
        if self.kind not in {k for k, _, _ in CLASSES[self.trade]["kinds"]}:
            raise ValueError("Pick what kind of business this is")
        if self.effDate < date.today():
            raise ValueError("Effective date can't be in the past")
        if self.started > date.today():
            raise ValueError("Start date can't be in the future")
        if not self.sqft and self.premises != "Home":
            raise ValueError("Tell us how many square feet the business uses")
        return self


class ProfileIn(BaseModel):
    answers: ProfileAnswers
    verification: str = Field(default="", max_length=400)
    website_url: str = ""  # honeypot


class SubmissionIn(BaseModel):
    answers: GLAnswers
    verification: str = Field(default="", max_length=400)   # signed email-verification token
    website_url: str = ""  # honeypot: real users never see or fill this


class VerifyStart(BaseModel):
    email: EmailStr
    turnstile: str = Field(default="", max_length=4096)


class VerifyCheck(BaseModel):
    email: EmailStr
    code: str = Field(max_length=10)
