"""Hiscox small-business General Liability quotes through Hiscox's agent portal (OneShield),
for agents appointed through First Connect.

How it works (details and the captured traffic it's based on: docs/hiscox-api-notes.md):

  1. Sign in to Hiscox's Azure AD B2C tenant with the agency's portal login
     (HISCOX_EMAIL / HISCOX_PASSWORD, environment only, never the repo or the database).
  2. B2C posts an id_token to portal.hiscox.com/oneshield/sso, which opens a OneShield session.
  3. OneShield is server-driven: every screen is a JSON layout. We read the fields on each screen,
     fill the ones we know from the Bindswift application, fire the same field rules the browser
     fires, and press the same button the agent would press.
  4. We stop at the price screen and return the premium. Nothing is bound or purchased here:
     any button that looks like bind / purchase / pay / issue is refused.

Field IDs (the number after the object ID in bv_<object>_<field>) are stable across sessions;
object IDs are new for every quote, so they're always read from the current screen.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote, urlencode, urlsplit

import httpx

from ..config import get_settings

PORTAL = "https://portal.hiscox.com/oneshield"
B2C_HOST = "https://prodb2chiscoxus.b2clogin.com"
B2C_TENANT = "prodb2chiscoxus.onmicrosoft.com"
B2C_POLICY = "B2C_1A_signin_only"
B2C_CLIENT_ID = "6e3b735d-ca61-43fe-b99f-ef7afc63951f"
# What the "Sign in" button on the First Connect partner page opens (login_url_plain in the page's
# settings). The portal sets up its session here, then sends the browser to the B2C sign-in.
PARTNER_PAGE = "https://www.hiscox.com/partner-agent/firstconnectinsurance"
LOGIN_URL = ("https://portal.hiscox.com/oneshield/GatewayExternalLinkServlet?partner="
             "https%3A%2F%2Fwww.hiscox.com%2Fpartner-agent%2Ffirstconnectinsurance&oswt=LoginAgent")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

# Values the portal's page script sends with every screen (from the sso page's script block).
DEFAULT_SKIN = "4165"
DEFAULT_VIEWPORT = "12265"
DEFAULT_VIEWPORT_ASSIGNMENT = "48465"

# Buttons, by the screen they belong to. Identified by a field that only appears on that screen.
ACTION_START = "Action.1514465"          # home: ZIP -> start a quote
ACTION_BUSINESS_TYPE = "Action.1385265"  # profession / ownership / start date -> Next
ACTION_PRODUCTS = "Action.1431065"       # product selection (GL) -> continue
ACTION_CONTACT = "Action.1600361"        # client name, business, phone, email -> Next
ACTION_VALIDATE_ADDR = "Action.1207665"  # Validate address
ACTION_ADDR_CONTINUE = "Action.1433965"  # continue after the address validates
ACTION_NEXT = "Action.113"               # application questions -> next
ACTION_LIMITS = "Action.1494265"         # limits -> Next (prices the quote)
ACTION_HOME = "Action.1386365"           # Home link on every screen: leaves the quote so Hiscox unlocks it

# Never press anything whose label looks like it commits coverage or money.
FORBIDDEN_LABEL = re.compile(r"\b(bind|purchase|buy|pay|payment|issue|checkout|submit\s+application)\b", re.I)

# Field IDs (see docs/hiscox-api-notes.md §4)
F_ZIP_START = "31810846"
F_PROFESSION = "211195p33087465p34108046"
F_OWNERSHIP = "29236514"
F_START_DATE = "211195p33087465p4095"
F_GL_PRODUCT = "33144965"
F_FIRST = "32874364"
F_LAST = "32874564"
F_BIZ_NAME = "29238814"
F_PHONE = "32681846p33645365"
F_EMAIL = "32681746p30028946"
F_BIZ_BEGAN = "33684261"
F_HAS_DBA = "32874964p32875164"
F_DBA_NAME = "32874964p32875364"
F_HAS_POLICY = "211195p34102546p34101946"
F_STREET = "29325214"
F_SUITE = "210417"
F_ZIP = "29327414"
F_CITY = "29326314"
F_STATE = "210419"
F_COUNTY = "210422"
F_PRODUCTS = "32646064"
F_PRODUCTS_WHAT = "32646264"
F_PRODUCTS_PCT = "32646364"
F_PRODUCTS_USE = "32646464"
F_PRODUCTS_WHERE = "32646564"
F_PRODUCTS_WHO = "32646664"
F_PRODUCTS_SITE = "32646764"
F_DESIGN_BUILD = "32647264"
F_HOME_BASED = "32663364"
F_SQFT = "32669664"
F_REVENUE = "33109364"
F_AGGREGATE = "1591"
F_OCCURRENCE = "1594"

# Bindswift class -> Hiscox profession code (from the portal's profession list;
# full list in backend/fc captures/hiscox-professions.json). An application can override
# with answers["hiscoxClass"] = "<code>".
PROFESSION_BY_TRADE = {
    "salon": "1143545",        # Beautician/cosmetology services
    "barber": "1143445",       # Barber/hair stylist services
    "beauty": "1144745",       # Nail technician services (lash: Esthetician 1143945)
    "consultant": "1145645",   # Business consulting
    "marketing": "1151945",    # Marketing/media consulting
    "bookkeeping": "1147045",  # Bookkeeping (tax prep: 1147845, accounting: 1146845)
    "wellness": "1153145",     # Life/career/executive coaching
    "retail": "1150445",       # Other stores (without food/drinks); pick a specific store class when known
    # "doula": no Hiscox profession matches; ask Hiscox before quoting doulas
}
SELLS_PRODUCTS = {"retail"}

OWNERSHIP = {"LLC": "Limited Liability Company", "Corporation": "Corporation", "SCorp": "Corporation",
             "Individual": "Sole Proprietor", "Partnership": "Partnership", "JointVenture": "Joint Venture"}
LIMITS = {"1/2": ("1000000", "2000000"), "2/4": ("2000000", "4000000")}


class HiscoxError(Exception):
    """Something went wrong talking to the portal. The message is safe to show in the admin."""


class HiscoxDecision(HiscoxError):
    """Hiscox declined or referred the risk. Not an error: it's the carrier's answer."""

    def __init__(self, kind: str, wording: str):
        self.kind = kind
        m = (re.search(r"Unfortunately[^.]*\.", wording, re.I) or re.search(r"[^.]*\brefer[^.]*\.", wording, re.I)
             or re.search(r"Coverage not available[^.]*", wording, re.I))
        self.wording = _clean(m.group(0) if m else wording)[:400]
        super().__init__(f"Hiscox {kind} this risk: {self.wording}")


# ---------------------------------------------------------------- answers -> portal values

def eligibility(a: dict) -> list[str]:
    """Reasons Hiscox may not quote this risk online. Empty list = try it."""
    out = []
    trade = a.get("trade", "")
    if not (a.get("hiscoxClass") or PROFESSION_BY_TRADE.get(trade)):
        out.append("No Hiscox class for this business type yet" +
                   (" (Hiscox has no doula class; ask your underwriter)." if trade == "doula" else "."))
    if trade in SELLS_PRODUCTS and not a.get("productsDesc"):
        out.append("Retail quotes need the products they sell (Hiscox asks what, where it's made, and by whom).")
    if not a.get("sqft"):
        out.append("Hiscox asks for square footage; the application doesn't have it.")
    return out


def _phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else p


def _d(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


# Class-specific questions Hiscox asks for some professions, matched by wording. Each Bindswift answer
# key gives the reply; an application can also pass answers["hiscoxAnswers"] = {"<words in the
# question>": "<option text>"} for anything else. Unanswered required questions stop the quote.
QUESTION_ANSWERS = [
    (r"real estate agent services", lambda a: _yn(a.get("realEstateAgent"))),
    (r"provide any of the following services", lambda a: a.get("otherServices") or None),
    (r"hotel properties or health care", lambda a: _yn(a.get("managesHotelsHealthcare"))),
    (r"construction, renovation or reconstruction projects", lambda a: _yn(a.get("managesConstruction"))),
]


def _yn(v) -> str | None:
    return {"Y": "Yes", "N": "No", True: "Yes", False: "No"}.get(v)


def question_answers(a: dict) -> list[tuple[str, str]]:
    out = [(k, str(v)) for k, v in (a.get("hiscoxAnswers") or {}).items()]
    for pat, fn in QUESTION_ANSWERS:
        v = fn(a)
        if v:
            out.append((pat, v))
    return out


def quote_inputs(a: dict) -> dict[str, str]:
    """Field ID -> value we want on the portal. Select lists can be given as the display text;
    they're matched against the screen's options when filled."""
    first, _, last = (a.get("contactName") or "").strip().partition(" ")
    eff = _d(a.get("effDate")) or date.today()
    began = _d(a.get("started"))
    occ, agg = LIMITS.get(a.get("limits"), LIMITS["1/2"])
    sells = a.get("trade") in SELLS_PRODUCTS or a.get("sellsProducts") in ("Y", True)
    v = {
        F_ZIP_START: a.get("zip", ""),
        F_PROFESSION: a.get("hiscoxClass") or PROFESSION_BY_TRADE.get(a.get("trade", ""), ""),
        F_OWNERSHIP: "text:" + OWNERSHIP.get(a.get("entity", ""), "Other"),
        F_START_DATE: eff.strftime("%m/%d/%Y"),
        F_GL_PRODUCT: "1",
        F_FIRST: first,
        F_LAST: last or first,
        F_BIZ_NAME: a.get("legalName", ""),
        F_PHONE: _phone(a.get("phone", "")),
        F_EMAIL: a.get("email", ""),
        F_BIZ_BEGAN: began.strftime("%m/%Y") if began else "",
        F_HAS_DBA: "1" if a.get("dba") else "0",
        F_DBA_NAME: a.get("dba", ""),
        F_HAS_POLICY: "1" if a.get("insured") == "Y" else "2",
        F_STREET: a.get("street", ""),
        F_SUITE: a.get("suite", ""),
        F_ZIP: a.get("zip", ""),
        F_CITY: a.get("city", ""),
        F_STATE: "text:New York",
        F_PRODUCTS: "1" if sells else "2",
        F_DESIGN_BUILD: "1" if a.get("install") == "Y" else "2",
        F_HOME_BASED: "1" if a.get("premises") == "Home" else "2",
        F_SQFT: f"{int(a['sqft']):,}" if str(a.get("sqft") or "").isdigit() else "",
        F_REVENUE: f"{round(float(a.get('revenue') or 0)):,}",
        F_AGGREGATE: agg,
        F_OCCURRENCE: occ,
    }
    if sells:
        v.update({F_PRODUCTS_WHAT: a.get("productsDesc", ""), F_PRODUCTS_PCT: str(a.get("productsPct", "")),
                  F_PRODUCTS_USE: a.get("productsUse", ""), F_PRODUCTS_WHERE: a.get("productsMadeWhere", ""),
                  F_PRODUCTS_WHO: a.get("productsMaker", ""), F_PRODUCTS_SITE: a.get("website", "")})
    return {k: str(x) for k, x in v.items() if x not in (None,)}


# ---------------------------------------------------------------- screens

LABEL_FES = {8}                       # read-only text
NON_INPUT_FES = {8, 42, 11708, 12008, 10409}   # labels, action links/buttons, images, hyperlinks
NAMEFIELD_FES = {2, 3, 5, 6, 7}       # inputs the page script lists in "namefields"
TEXT_WIDGETS = ("TextWithHelp", "TextAreaWithHelp", "TextAreaDynamic", "DateTextWithHelp")


@dataclass
class Cell:
    var: str
    bv: str
    obj: str
    fes: int
    ui: str
    value: str
    visible: bool
    read_only: bool
    ajax: bool
    lookups: list[tuple[str, str]] = field(default_factory=list)
    mandatory: bool = False
    label: str = ""
    explicit: bool = False      # we set it (vs. the screen's default)
    rule_list: bool = False     # a label a field rule turned into a list: named, but not posted

    @property
    def is_input(self) -> bool:
        return self.fes not in NON_INPUT_FES or self.rule_list

    def posted_value(self) -> str:
        """What the page script posts for this widget."""
        if self.ui.endswith("BoxedCheckbox"):
            return "1" if self.value == "1" else ""
        if self.ui.endswith("IncludeExcludeOption") and not self.explicit:
            return ""
        if not self.value and self.ui.endswith(("SingleSelectListWithHelp", "SingleSelectListWithHelpWithError")) \
                and any(code == "0" for code, _ in self.lookups):
            return "0"
        return self.value

    @property
    def is_option(self) -> bool:
        return self.ui.endswith("OptionButtonWithHelp")


class Screen:
    """One OneShield page (from the sso page's pageJSON or a GatewayServlet response)."""

    def __init__(self, data: dict):
        self.data = data
        self.hvars = {h["name"]: "" if h.get("value") is None else str(h["value"]) for h in data.get("hvars", [])}
        self.name = ((data.get("pageSubTitle") or {}).get("pageName") or "").strip()
        self.tab = data.get("browserTabId") or self.hvars.get("USER_SESSION_GUID", "")
        self.workflow = data.get("workflowContext") or (data.get("diagnostics") or {}).get("workflowContext", "")
        self.object_tree = data.get("objectTree", "")
        diag = data.get("diagnostics") or {}
        self.current_object = str(diag.get("currentObjectId") or "")
        self.transaction = str(diag.get("transactionId") or self.hvars.get("DRAGON_TRANSACTION_ID", ""))
        self.cells: list[Cell] = []
        self.labels: dict[str, str] = {}
        self.actions: dict[str, tuple[str, int]] = {}   # "Action.123" -> (label, validationType)
        self.action_obj: dict[str, str] = {}             # "Action.123" -> object the button belongs to
        self._walk(data)

    def _walk(self, o, hidden: bool = False):
        if isinstance(o, dict):
            hidden = hidden or o.get("visibility") is False      # a whole section the page doesn't show
            var = o.get("varName")
            if isinstance(var, str) and var.startswith("bv_") and not (
                    not o.get("uiProvider") and any(c.var == var for c in self.cells)):
                obj, _, bv = var[3:].partition("_")
                self.cells.append(Cell(
                    var=var, bv=bv, obj=obj, fes=int(o.get("fes") or 0), ui=str(o.get("uiProvider") or ""),
                    value="" if o.get("value") is None else str(o.get("value")),
                    visible=o.get("visible") is not False and not hidden, read_only=bool(o.get("readOnly")),
                    ajax=bool(o.get("ajaxFlag")), mandatory=bool(o.get("mandatory")),
                    label=_clean(o.get("label") or "") if isinstance(o.get("label"), str) else "",
                    lookups=[(str(x.get("code")), str(x.get("displayValue", "")).strip()) for x in o.get("lookups") or []]))
            if o.get("bvId") and isinstance(o.get("label"), str) and o["label"].strip():
                self.labels.setdefault(str(o["bvId"]), _clean(o["label"]))
            elif str(o.get("id", "")).startswith("Label_") and o.get("bvId") and o.get("value"):
                self.labels.setdefault(str(o["bvId"]), _clean(o["value"]))
            act = o.get("actionIdText") or o.get("actionOne")
            if isinstance(act, str) and act.startswith("Action."):
                lab = _clean(o.get("label") or o.get("content") or o.get("value") or "")
                prev_lab, prev_vt = self.actions.get(act, ("", 1))
                vt = int(o["validationType"]) if o.get("savePageContents") and o.get("validationType") else 1
                self.actions[act] = (prev_lab or lab, max(prev_vt, vt))
                m = re.search(r"obj_(\d+)$", str(o.get("id", "")))
                if m:
                    self.action_obj.setdefault(act, m.group(1))
            for v in o.values():
                self._walk(v, hidden)
        elif isinstance(o, list):
            for v in o:
                self._walk(v, hidden)

    def cell(self, bv: str) -> Cell | None:
        return next((c for c in self.cells if c.bv == bv and c.is_input), None)

    def has(self, bv: str) -> bool:
        return self.cell(bv) is not None

    def messages(self) -> list[str]:
        ml = self.data.get("messageList") or {}
        return [_clean(m.get("content", "")) for m in ml.get("messages") or [] if m.get("content")]

    def errors(self) -> list[str]:
        out = []
        ml = self.data.get("messageList") or {}
        for m in ml.get("messages") or []:
            if str(m.get("mode")) not in ("1", "") and m.get("content"):   # mode 1 = information
                out.append(_clean(m["content"]))
        for c in self.cells:
            if c.visible and c.is_input and isinstance(c, Cell) and c.bv in self._bv_errors():
                out.append(f"{self.labels.get(c.bv, c.bv)}: needs a valid answer")
        if self.data.get("errorPage"):
            out.append("The portal returned an error page.")
        return out

    def _bv_errors(self) -> set[str]:
        bad = set()

        def walk(o):
            if isinstance(o, dict):
                if o.get("bvError") and o.get("bvId"):
                    bad.add(str(o["bvId"]))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(self.data)
        return bad

    def text(self) -> str:
        """All visible wording on the screen (questions, labels, messages)."""
        bits = []

        def walk(o):
            if isinstance(o, dict):
                if o.get("visible") is not False:
                    for k in ("label", "value", "content"):
                        v = o.get(k)
                        if isinstance(v, str) and re.search(r"[A-Za-z]{3}", v):
                            bits.append(_clean(v))
                    for v in o.values():
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(self.data.get("layout") or {})
        walk(self.data.get("messageList") or {})
        return " ".join(dict.fromkeys(b for b in bits if b))

    def outcome(self) -> tuple[str, str] | None:
        """('declined' | 'referred', Hiscox's wording) when the screen is an underwriting decision."""
        t = self.text()
        if re.search(r"coverage not available|outside of our (current )?underwriting appetite|unable to (offer|provide) (a )?(quote|coverage)", t, re.I):
            return "declined", t
        if re.search(r"referred to (an |our )?underwrit|underwriter (will|needs to) review|requires underwriting review", t, re.I):
            return "referred", t
        return None

    def unanswered(self) -> list[Cell]:
        """Visible required questions with no answer (radio buttons, lists, checkboxes, text)."""
        out = []
        for c in self.cells:
            if not (c.is_input and c.visible and not c.read_only and c.fes != 1):
                continue
            empty = c.value in ("", "0") if (c.lookups or c.is_option) else c.value == ""
            if c.mandatory and empty:
                out.append(c)
        return out

    def money(self) -> list[tuple[str, float]]:
        """Dollar amounts on the screen with the nearest label, e.g. [("Annual premium", 4335.0), ...]."""
        out, seen = [], set()

        def walk(o, ctx=""):
            if isinstance(o, dict):
                for k in ("cells", "blocks", "blockRows", "rows"):
                    pass
                txt = o.get("value") if isinstance(o.get("value"), str) else ""
                lab = o.get("label") if isinstance(o.get("label"), str) else ""
                for m in re.finditer(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2}|\d{3,})\b", _clean(txt)):
                    amt = float(m.group(1).replace(",", ""))
                    key = (lab or ctx, amt)
                    if key not in seen:
                        seen.add(key)
                        out.append((_clean(lab or ctx)[:80], amt))
                if txt and not re.search(r"\$\s?\d", txt):
                    ctx = _clean(txt)[:80] or ctx
                for v in o.values():
                    ctx = walk(v, ctx) or ctx
                return ctx
            if isinstance(o, list):
                for v in o:
                    ctx = walk(v, ctx) or ctx
                return ctx
            return ctx
        walk(self.data.get("layout") or self.data)
        return out


def _clean(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", htmllib.unescape(str(s)))).strip()


def _page_json(page_html: str) -> dict:
    k = page_html.find("var pageJSON = ")
    if k < 0:
        raise HiscoxError("Signed in, but the portal didn't return its start page. Hiscox may have changed the sign-in.")
    data, _ = json.JSONDecoder().raw_decode(page_html[k + len("var pageJSON = "):])
    return data


def _js_var(page_html: str, name: str, default: str) -> str:
    m = re.search(rf"var\s+{name}\s*=\s*'([^']*)'", page_html)
    return m.group(1) if m else default


# ---------------------------------------------------------------- session

class HiscoxSession:
    def __init__(self, client: httpx.Client | None = None, pause: float = 1.0):
        self.http = client or httpx.Client(timeout=40, follow_redirects=True,
                                           headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.pause = pause
        self.skin, self.viewport, self.viewport_assign = DEFAULT_SKIN, DEFAULT_VIEWPORT, DEFAULT_VIEWPORT_ASSIGNMENT
        self.screen: Screen | None = None
        self.first_submit = True
        self.trail: list[dict] = []      # what we did, for the audit log (no personal data)
        self.debug: list[str] = []       # sign-in diagnostics: hosts/paths/status only, never secrets
        self.debug_html = ""

    def close(self):
        self.http.close()

    # -- sign-in -------------------------------------------------------------
    def login(self, email: str, password: str) -> Screen:
        if not email or not password:
            raise HiscoxError("Hiscox isn't connected yet: add HISCOX_EMAIL and HISCOX_PASSWORD to your environment.")
        page = self._open_b2c()
        settings = _b2c_settings(page.text)
        base = f"{B2C_HOST}{settings['tenant_path']}"
        tx, csrf, pol = settings["transId"], settings["csrf"], settings["policy"]
        r = self.http.post(f"{base}/SelfAsserted?tx={quote(tx, safe='=')}&p={pol}",
                           data={"request_type": "RESPONSE", "signInName": email, "password": password},
                           headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest",
                                    "Origin": B2C_HOST, "Referer": str(page.url),
                                    "Accept": "application/json, text/javascript, */*; q=0.01"})
        try:
            status = r.json()
        except ValueError:
            status = {"status": str(r.status_code)}
        if str(status.get("status")) != "200":
            raise HiscoxError(f"Hiscox sign-in failed: {_clean(status.get('message') or 'check HISCOX_EMAIL / HISCOX_PASSWORD')}")
        r = self.http.get(f"{base}/api/CombinedSigninAndSignup/confirmed",
                          params={"rememberMe": "false", "csrf_token": csrf, "tx": tx, "p": pol},
                          headers={"Referer": str(page.url)})
        action, fields = _auto_form(r.text)
        if not action or "id_token" not in fields:
            raise HiscoxError("Hiscox sign-in didn't complete (no token came back). If the account needs a "
                              "verification code or a password change, sign in once in a browser first.")
        if urlsplit(action).netloc != urlsplit(PORTAL).netloc:
            raise HiscoxError("Hiscox sign-in tried to send the token somewhere unexpected; stopped.")
        r = self.http.post(action, data=fields, headers={"Origin": B2C_HOST, "Referer": B2C_HOST + "/"})
        self._note("sso", r)
        if "var pageJSON = " not in r.text:
            self.debug_html = re.sub(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*", "<token>", r.text)[:200000]
            title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
            self.debug.append("sso page title: " + _clean(title.group(1) if title else "(none)"))
            self.debug.append("sso page text: " + _clean(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I))[:400])
        if r.status_code >= 400:
            raise HiscoxError(f"Hiscox portal refused the sign-in (HTTP {r.status_code}).")
        self.skin = _js_var(r.text, "skin", DEFAULT_SKIN)
        self.viewport = _js_var(r.text, "os_viewport_id", DEFAULT_VIEWPORT)
        self.viewport_assign = _js_var(r.text, "os_viewport_assignment_id", DEFAULT_VIEWPORT_ASSIGNMENT)
        self.screen = Screen(_page_json(r.text))
        self.trail.append({"step": "signed_in"})
        return self.screen

    def _note(self, label: str, r: httpx.Response):
        hops = [f"{h.status_code} {h.url.host}{h.url.path}" for h in r.history] + [f"{r.status_code} {r.url.host}{r.url.path}"]
        self.debug.append(f"{label}: " + " -> ".join(hops))

    def _open_b2c(self) -> httpx.Response:
        """Reach the B2C sign-in page. Prefer letting the portal start the flow (it issues its own
        state); fall back to a standard authorize request."""
        s = get_settings()
        starts = [u for u in (getattr(s, "hiscox_start_url", ""), LOGIN_URL) if u]
        for u in starts:
            try:
                r = self.http.get(u, headers={"Referer": PARTNER_PAGE,
                                              "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
                for _ in range(3):                 # the portal may forward with a script or form, not a redirect
                    if "b2clogin.com" in str(r.url) or "var SETTINGS" in r.text:
                        break
                    nxt = _next_hop(r.text)
                    if not nxt:
                        break
                    self._note(f"forward from {r.url.host}{r.url.path}", r)
                    r = self.http.get(nxt, headers={"Referer": str(r.url)})
            except httpx.HTTPError as e:
                self.debug.append(f"start {u}: {type(e).__name__}")
                continue
            self._note(f"start {urlsplit(u).netloc}{urlsplit(u).path}", r)
            if "b2clogin.com" in str(r.url) and "var SETTINGS" in r.text:
                self.debug.append("sign-in started by the portal")
                return r
        params = {"p": B2C_POLICY, "response_type": "id_token", "client_id": B2C_CLIENT_ID,
                  "response_mode": "form_post", "redirect_uri": PORTAL + "/sso", "scope": "openid",
                  "state": str(secrets.randbelow(90_000_000) + 10_000_000), "nonce": "_" + str(uuid.uuid4())}
        r = self.http.get(f"{B2C_HOST}/{B2C_TENANT}/{B2C_POLICY.lower()}/oauth2/v2.0/authorize?" + urlencode(params))
        self._note("authorize (built by Bindswift)", r)
        if "var SETTINGS" not in r.text:
            raise HiscoxError(f"Couldn't open Hiscox's sign-in page (HTTP {r.status_code}). Set HISCOX_START_URL "
                              "to the link the partner page's Sign in button opens.")
        return r

    # -- screens ---------------------------------------------------------------
    def set_field(self, bv: str, value: str) -> bool:
        """Set a field on the current screen, resolving select-list text, and fire its rule like the
        browser does. Returns False if the field isn't on this screen."""
        sc = self.screen
        c = sc.cell(bv) if sc else None
        return self.set_cell(c, value) if c else False

    def set_cell(self, c: Cell, value: str) -> bool:
        if not c or c.read_only:
            return False
        value = _resolve(c, value)
        c.explicit = True
        if c.value == value:
            return True
        c.value = value
        if c.ajax:
            self._field_rule(c)
        elif c.ui.endswith(TEXT_WIDGETS):
            self._field_value(c)
        return True

    def _field_value(self, c: Cell):
        """Plain text fields: the page posts each new value as it's typed in."""
        sc = self.screen
        self._post("FieldProcessorServlet", {"value": c.value, "name": c.var,
                                             "MT_LOCALE_ID": sc.hvars.get("MT_LOCALE_ID", "101"),
                                             "EXCHANGE_ID": sc.hvars.get("EXCHANGE_ID", ""),
                                             "pageActionId": sc.workflow.split(",")[0], "USER_SESSION_GUID": sc.tab})

    def _field_rule(self, c: Cell):
        sc = self.screen
        same = [x for x in sc.cells if x.obj == c.obj and x.is_input and x.fes in NAMEFIELD_FES | {4}]
        tree = [p.split("_") for p in sc.object_tree.split(",") if p.count("_") == 2]
        form = {
            "value": c.value, "name": c.var, "MT_LOCALE_ID": sc.hvars.get("MT_LOCALE_ID", "101"),
            "EXCHANGE_ID": sc.hvars.get("EXCHANGE_ID", ""), "pageActionId": sc.workflow.split(",")[0],
            "USER_SESSION_GUID": sc.tab, "AJX_RULE_OBJ": c.obj, "AJX_RULE_BV": c.bv, "AJX_RULE_BV_VAL": c.value,
            "AJX_OBJ_IDS": "\x04".join(x.obj for x in same), "AJX_BV_IDS": "\x04".join(x.bv for x in same),
            "AJX_BV_VALS": "\x04".join(x.value or " " for x in same),
            "DRAGON_TRANSACTION_ID": sc.transaction, "TX_NAME": sc.workflow.split(",")[0], "PAGE_OBJ_ID": c.obj,
            "OBJECT_TREE": "\x04".join(t[2] for t in tree), "PAR_OBJECT_TREE": "\x04".join(t[0] for t in tree),
        }
        data = self._post("FieldProcessorServlet", form)
        echo = []                                          # rule-filled boxes the page posts back
        for ins in data.get("ins") or []:
            bv = ins.get("bv") or {}
            obj, _, bvid = str(bv.get("nm", "")).partition("_")
            tgt = next((x for x in sc.cells if x.obj == obj and x.bv == bvid), None)
            if not tgt:
                continue
            if ins.get("type") == 4:                       # show / hide
                tgt.visible = bv.get("val") != "H"
            elif "val" in bv:
                changed = tgt.value != str(bv["val"]) or (ins.get("list") and not tgt.rule_list)
                tgt.value = str(bv["val"])
                if changed and (tgt.ui.endswith(TEXT_WIDGETS) or tgt.fes in LABEL_FES):
                    echo.append(tgt)
                if ins.get("list") and bv.get("o"):
                    tgt.lookups = [(str(o.get("v")), str(o.get("d", "")).strip()) for o in bv["o"]]
                    if tgt.fes in LABEL_FES:
                        tgt.rule_list = True               # a label the rule turned into a list
        for tgt in echo:
            self._field_value(tgt)

    def press(self, action: str) -> Screen:
        sc = self.screen
        label, vtype = sc.actions.get(action, ("", 1))
        if FORBIDDEN_LABEL.search(label or ""):
            raise HiscoxError(f"Refused to press '{label}': Bindswift never binds or pays automatically.")
        form = build_gateway_form(sc, action, vtype, self.skin, self.viewport, self.viewport_assign, self.first_submit)
        data = self._post("GatewayServlet", form)
        self.first_submit = False
        if not data.get("success", True) and not data.get("layout"):
            raise HiscoxError("The portal rejected that step.")
        self.screen = Screen(data)
        self.trail.append({"step": action, "label": label, "screen": self.screen.name})
        return self.screen

    def wait_for_price(self, tries: int = 6, every: float = 5.0) -> list[tuple[str, float]]:
        """The premium can take a moment. Re-check the screen, using only its own auto-refresh step
        (never Next / Bind), for up to tries x every seconds."""
        for i in range(tries + 1):
            amounts = [(lab, amt) for lab, amt in self.screen.money() if amt >= 50]
            if amounts or i == tries:
                return amounts
            auto = _auto_action(self.screen.data)
            self.trail.append({"step": "waiting for price", "screen": self.screen.name})
            time.sleep(every if self.pause else 0)
            if auto and auto in self.screen.actions:
                self.press(auto)
            else:
                self._keep_alive()
        return []

    def leave_quote(self):
        """Go back to Home so the portal releases its lock on the quote. Best effort."""
        sc = self.screen
        if not sc or ACTION_HOME not in sc.actions:
            return
        try:
            self.press(ACTION_HOME)
            self.trail.append({"step": "left the quote (Home)"})
        except (HiscoxError, httpx.HTTPError):
            pass

    def _keep_alive(self):
        sc = self.screen
        try:
            self._post("SessionExtendServlet", {"USER_SESSION_GUID": sc.tab, "actionId": sc.workflow.split(",")[0]})
        except HiscoxError:
            pass

    def _post(self, servlet: str, form: dict) -> dict:
        time.sleep(self.pause)
        body = urlencode(list(form.items()) if isinstance(form, dict) else form)
        r = self.http.post(f"{PORTAL}/{servlet}", content=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                    "X-Requested-With": "XMLHttpRequest", "Origin": "https://portal.hiscox.com",
                                    "Referer": f"{PORTAL}/sso?osst={self.screen.tab if self.screen else ''}",
                                    "Accept": "*/*"})
        if r.status_code in (401, 403) or "b2clogin.com" in str(r.url):
            raise HiscoxError("The Hiscox session expired. Try again.")
        if r.status_code >= 400:
            raise HiscoxError(f"Hiscox portal error (HTTP {r.status_code}) on {servlet}.")
        try:
            return r.json()
        except ValueError:
            raise HiscoxError(f"Hiscox portal sent a page instead of data on {servlet}; the session may have expired.")


def build_gateway_form(sc: Screen, action: str, vtype: int, skin: str, viewport: str, viewport_assign: str,
                       first: bool) -> list[tuple[str, str]]:
    """The exact form the portal's page script posts to GatewayServlet for a button press."""
    hv = {k: v for k, v in sc.hvars.items() if k not in ("SESSION_INFO", "USER_ID")}
    cur_obj = hv.get("CURRENT_OBJECT", "") or sc.action_obj.get(action, "")
    inputs = [c for c in sc.cells if c.is_input]
    sent = []
    for c in inputs:
        if c.rule_list or (c.is_option and (not c.visible or c.value in ("", "0"))):
            continue                     # unchecked / hidden radio groups aren't posted
        sent.append(c)
    plain = ("Dragon.view.widget.Text", "Dragon.view.widget.ComboBox")
    names = [c.var for c in inputs if not c.rule_list and c.ui not in plain
             and (c.fes in NAMEFIELD_FES or (c.fes == 4 and c.value == "1"))]
    names += [c.var for c in inputs if c.rule_list]   # the script appends these last
    head = {"dragon-ui-page": "true", "osst": sc.tab, "OBJECT_TREE": sc.object_tree, "WORKFLOW_CONTEXT": sc.workflow,
            "FormIsSubmitted": "false", "SELECTED_NODE": "", "TX_NAME": action, "CURRENT_SKIN": skin}
    form = list(head.items())
    for k, v in hv.items():
        if k == "CURRENT_OBJECT":
            v = cur_obj
        elif k == "namefields":
            v = ",".join(dict.fromkeys(names))
        form.append((k, v))
    form += [("DateDisplayFormat", '"MM/DD/yyyy"'), ("current_viewport_id", viewport),
             ("current_viewport_assignment_id", viewport_assign), ("validateFlag", str(vtype)),
             ("refreshBrowser", "1" if first else "0")]
    form += [(c.var, c.posted_value()) for c in sent]
    return form


def _resolve(c: Cell, value: str) -> str:
    """'text:New York' -> that option's code; codes pass through."""
    if not c.lookups:
        return value.removeprefix("text:")
    if value.startswith("text:"):
        want = value[5:].strip().lower()
        for code, disp in c.lookups:
            if disp.lower() == want:
                return code
        for code, disp in c.lookups:
            if want in disp.lower():
                return code
        raise HiscoxError(f"'{value[5:]}' isn't an option for {c.bv} on Hiscox's form.")
    if value and value not in {code for code, _ in c.lookups} and c.ui.endswith(("ListWithHelp", "WithError")):
        raise HiscoxError(f"'{value}' isn't an option for {c.bv} on Hiscox's form.")
    return value


def _next_hop(page_html: str) -> str:
    """A b2clogin authorize URL the page forwards to by script, meta refresh or link."""
    m = re.search(r"""(https://[\w.-]*b2clogin\.com/[^"'<>\s]+)""", page_html)
    if m:
        return htmllib.unescape(m.group(1)).replace("\\/", "/").replace("\\u0026", "&")
    m = re.search(r"""http-equiv=["']refresh["'][^>]*url=([^"'>]+)""", page_html, re.I)
    return htmllib.unescape(m.group(1)) if m else ""


def _b2c_settings(page_html: str) -> dict:
    m = re.search(r"var\s+SETTINGS\s*=\s*(\{.*?\});\s*$", page_html, re.S | re.M)
    if not m:
        raise HiscoxError("Couldn't read Hiscox's sign-in page.")
    st = json.loads(m.group(1))
    hosts = st.get("hosts") or {}
    tenant_path = hosts.get("tenant") or f"/{B2C_TENANT}/{B2C_POLICY}"
    return {"csrf": st["csrf"], "transId": st["transId"], "policy": hosts.get("policy") or B2C_POLICY,
            "tenant_path": tenant_path if tenant_path.startswith("/") else "/" + tenant_path}


def _auto_form(page_html: str) -> tuple[str, dict]:
    m = re.search(r"<form[^>]*action=['\"]([^'\"]+)['\"]", page_html, re.I)
    fields = {}
    for tag in re.findall(r"<input[^>]*>", page_html, re.I):
        n = re.search(r"name=['\"]([^'\"]+)['\"]", tag)
        v = re.search(r"value=['\"]([^'\"]*)['\"]", tag)
        if n:
            fields[n.group(1)] = htmllib.unescape(v.group(1)) if v else ""
    return (htmllib.unescape(m.group(1)) if m else ""), fields


# ---------------------------------------------------------------- the quote

def run_quote(sess: HiscoxSession, a: dict, max_steps: int = 20) -> dict:
    """Walk an already signed-in session from the home screen to the price."""
    want = quote_inputs(a)
    by_words = question_answers(a)
    validated = False
    priced_from = None
    for _ in range(max_steps):
        sc = sess.screen
        errs = sc.errors()
        if errs and priced_from is None and sess.trail[-1]["step"] != "signed_in":
            raise HiscoxError("Hiscox flagged the application: " + "; ".join(errs[:4]))
        decision = sc.outcome()
        if decision:
            raise HiscoxDecision(*decision)
        for c in list(sc.cells):                 # page order, like a person tabbing through
            val = want.get(c.bv, "")
            if val == "" and c.label and c.is_input and c.visible and not c.explicit:
                val = next(("text:" + ans for pat, ans in by_words if re.search(pat, c.label, re.I)), "")
            if val != "" and c.is_input and c.visible:
                sess.set_cell(c, val)
        missing = [c for c in sc.unanswered() if c.bv not in (F_SUITE,)]
        if missing and not sc.has(F_ZIP_START):
            raise HiscoxError("Hiscox asked questions the application doesn't answer, so Bindswift stopped instead "
                              "of guessing: " + " | ".join(f"{c.label or c.bv} [{c.bv}]"
                                                          + (f" options: {', '.join(d for _, d in c.lookups if d and d != '- Select -')}" if c.lookups else "")
                                                          for c in missing[:8]))
        if sc.has(F_AGGREGATE):
            priced_from = sc
            time.sleep(sess.pause * 2)
            sess.press(ACTION_LIMITS)
            amounts = sess.wait_for_price()
            decision = sess.screen.outcome()
            if not amounts and decision:
                raise HiscoxDecision(*decision)
            if not amounts:
                nx = sess.screen
                said = nx.errors() + nx.messages()
                text = _clean(" ".join(str(c.value) for c in nx.cells if c.visible and c.fes in LABEL_FES and c.value))[:500]
                raise HiscoxError(f"Got past the limits screen, but the next screen ('{nx.name or 'untitled'}') has no price. "
                                  f"Portal says: {'; '.join(said)[:400] or 'nothing'}. Screen text: {text or '(none)'}")
            return _result(sess, amounts)
        if sc.has(F_ZIP_START) and ACTION_START in sc.actions:
            sess.press(ACTION_START)
        elif sc.has(F_PROFESSION):
            sess.press(ACTION_BUSINESS_TYPE)
        elif sc.has(F_GL_PRODUCT):
            sess.press(ACTION_PRODUCTS)
        elif sc.has(F_FIRST):
            sess.press(ACTION_CONTACT)
        elif sc.has(F_STREET) and not validated and ACTION_VALIDATE_ADDR in sc.actions:
            sess.press(ACTION_VALIDATE_ADDR)
            validated = True
        elif ACTION_ADDR_CONTINUE in sc.actions and validated and not sc.has(F_PRODUCTS):
            sess.press(ACTION_ADDR_CONTINUE)
        elif sc.has(F_PRODUCTS) or sc.has(F_SQFT) or sc.has(F_REVENUE):
            if sc.has(F_PRODUCTS) and want.get(F_PRODUCTS) == "1" and not want.get(F_PRODUCTS_WHAT):
                raise HiscoxError("Hiscox needs the products this business sells; add them to the application.")
            sess.press(ACTION_NEXT)
        elif ACTION_NEXT in sc.actions and _all_answered(sc):
            sess.press(ACTION_NEXT)                 # a question screen we filled completely
        else:
            asks = [f"{sc.labels.get(c.bv, '?')[:70]} [{c.bv}]" for c in sc.cells
                    if c.is_input and c.visible and not c.read_only and c.fes in NAMEFIELD_FES][:12]
            raise HiscoxError(f"Reached a Hiscox screen Bindswift doesn't know yet ('{sc.name or 'untitled'}'). "
                              f"It asks: {'; '.join(asks) or 'no visible questions'}. Buttons: "
                              f"{', '.join(f'{lab or act} [{act}]' for act, (lab, _) in sc.actions.items() if lab)[:300]}. "
                              "Finish this one in the portal and send me this message so I can add it.")
    raise HiscoxError("Too many steps without reaching a price; stopped.")


def _auto_action(data) -> str:
    """An action the screen asks the browser to run by itself (refresh / poll / timer)."""
    found = ""

    def walk(o):
        nonlocal found
        if found:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if re.search(r"auto|refresh|poll|timer|reload", k, re.I) and isinstance(v, str) \
                        and re.fullmatch(r"Action\.\d+", v):
                    found = v
                    return
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return "" if found and FORBIDDEN_LABEL.search(found) else found


def _all_answered(sc: Screen) -> bool:
    """Every visible question on the screen has an answer (and there is at least one question)."""
    qs = [c for c in sc.cells if c.is_input and c.visible and not c.read_only and c.fes in NAMEFIELD_FES]
    return bool(qs) and not sc.unanswered()


def _result(sess: HiscoxSession, amounts: list[tuple[str, float]]) -> dict:
    annual = next((amt for lab, amt in amounts if re.search(r"annual|total|year", lab, re.I)), None)
    monthly = next((amt for lab, amt in amounts if re.search(r"month", lab, re.I)), None)
    if annual is None:
        annual = max(amt for _, amt in amounts)
    ref = re.search(r"Reference\s*#\s*([A-Z]?[\d.]{6,})", sess.screen.text())
    return {"ok": True, "premium": annual, "monthly": monthly, "amounts": amounts,
            "quote_number": ref.group(1).rstrip(".") if ref else None,
            "messages": sess.screen.messages(), "trail": sess.trail, "env": "live"}


def create_quote(a: dict, client: httpx.Client | None = None, pause: float = 1.0) -> dict:
    """Sign in, run the quote to the price, return {ok, premium, monthly, amounts, ... | error}.
    Never binds."""
    s = get_settings()
    issues = [i for i in eligibility(a) if not i.startswith("Hiscox asks for square footage")]
    if issues:
        return {"ok": False, "error": " ".join(issues)}
    sess = HiscoxSession(client, pause=pause)
    try:
        sess.login(getattr(s, "hiscox_email", ""), getattr(s, "hiscox_password", ""))
        return run_quote(sess, a)
    except HiscoxDecision as e:
        return {"ok": False, "decision": e.kind, "error": str(e), "trail": sess.trail}
    except HiscoxError as e:
        return {"ok": False, "error": str(e), "trail": sess.trail, "debug": sess.debug, "debug_html": sess.debug_html,
                "debug_screen": sess.screen.data if sess.screen else None}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Couldn't reach Hiscox: {type(e).__name__}", "trail": sess.trail}
    finally:
        sess.leave_quote()                       # always release the quote, success or not
        if client is None:
            sess.close()
