"""Blitz producer portal (app.blitzinsurance.com): live general liability quotes, reached through First Connect.

How the portal works (from a recorded session, docs/blitz-api-notes.md):
  * Sign-in: First Connect gives a one-time SSO link (integrations/firstconnect.py). Opening it sets
    Blitz's Rails session cookie.
  * GET  /apply                       program picker; carries the producer uuid and the CSRF token
  * GET  /apply/start_application?... 302 -> /apply/<application uuid>
  * Each page of questions:
      POST /apply/dry_run_submissions_data.json   (multipart) answers so far -> JSON describing every
           question on the page: label, field_type, options, whether it is hidden, errors.
      POST /apply/submit_submissions_data         (multipart) save the page -> 302 to the next page,
           or back to the same page with ?global_validations[]=<message> when Blitz refuses it.
  * Product selection: POST /apply/choose_products (form-urlencoded checkboxes).
  * The application ends at the "Review Quote" stage, which shows the quoted total.
Nothing past Review Quote is ever touched (offer lock-in, checkout, contract signing).

Answers are truthful only: every question on a page must be answered from the client's profile
or from an explicit answer the client gave (a["blitzAnswers"]). Anything else stops the run with
BlitzNeedsInput, listing the questions, so the website can ask the client or a broker can step in.
"""
from __future__ import annotations

import html as htmlmod
import re
import time
import uuid as uuidmod
from datetime import date
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .firstconnect import UA, FirstConnect

BASE = "https://app.blitzinsurance.com"
FORBIDDEN_PATH = re.compile(r"/(lock_in_offer_selections|checkout|sign|bind|payment|purchase)", re.I)
CONTRACTORS = "artisan_contractors"
STATE = "NY"


class BlitzError(Exception):
    pass


class BlitzDecline(BlitzError):
    """Blitz refused the risk or a class code (its own words in .messages)."""
    def __init__(self, messages):
        self.messages = list(messages)
        super().__init__("Blitz can't quote this: " + " ".join(self.messages))


class BlitzReferred(BlitzError):
    """The application went to a Blitz underwriter instead of pricing instantly."""


class BlitzNeedsInput(BlitzError):
    """Questions Bindswift can't answer truthfully from the profile."""
    def __init__(self, questions):
        self.questions = questions
        super().__init__("Blitz needs answers to: " + "; ".join(q["label"] for q in questions))


# ---------------------------------------------------------------- the question schema
def visible_questions(schema: dict, state: str = STATE, products=("general_liability",)) -> list[tuple[str, dict]]:
    """Flatten Blitz's page schema into (field_id, question) pairs the form would show."""
    out = []
    for qid, q in schema.items():
        if not isinstance(q, dict) or not ("field_type" in q or "questions" in q or "aliased_fields" in q):
            continue
        if q.get("hidden") is True:
            continue
        states = q.get("conditional_relevant_states")
        if states and state not in states:
            continue
        prods = q.get("conditional_relevant_products")
        if prods and not set(prods) & set(products):
            continue
        out.append((qid, q))
    return out


def field_names(qid: str, q: dict) -> list[tuple[str, dict]]:
    """Form field names for one question: groups become group[sub], addresses expand to lines."""
    if q.get("question_group"):
        names = []
        for sub, sq in q["questions"].items():
            if "aliased_fields" in sq:
                stem = sub[:-len("address")] if sub.endswith("address") else sub + "_"
                for ak, aq in sq["aliased_fields"].items():
                    names.append((f"{qid}[{stem}{ak}]", aq))
            else:
                names.append((f"{qid}[{sub}]", sq))
        return names
    return [(qid, q)]


def encode(q: dict, v) -> list[str]:
    ft = q.get("field_type")
    if ft == "multi_select":
        vals = v if isinstance(v, (list, tuple)) else ([v] if v else [])
        return [str(x) for x in vals] or [""]
    if v is None or v == "":
        return [""]
    if ft in ("boolean", "attestation"):
        return ["true" if v in (True, "true", "Y", "yes") else "false"]
    if ft == "currency":
        return [f"${float(v):,.0f}"]
    return [str(v)]


# ---------------------------------------------------------------- answers from a Bindswift profile
CORP = {"LLC": "llc", "Corporation": "corporation", "SCorp": "corporation", "Individual": "individual",
        "Partnership": "partnership", "Sole proprietor": "sole_proprietor"}
LIMIT = {"1/1": "occurrence_1m_aggregate_1m", "1/2": "occurrence_1m_aggregate_2m"}


def _d(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def profile_answers(a: dict) -> dict:
    """field_id -> value, for everything the Bindswift profile already says."""
    first, _, last = (a.get("contactName") or "").strip().partition(" ")
    started = _d(a.get("started"))
    today = date.today()
    ans = {
        "first_named_insured[legal_business_name]": a.get("legalName", ""),
        "first_named_insured[dba]": a.get("dba", ""),
        "first_named_insured[business_address_1]": a.get("street", ""),
        "first_named_insured[business_address_2]": a.get("suite", ""),
        "first_named_insured[business_city]": a.get("city", ""),
        "first_named_insured[business_state]": a.get("state") or STATE,
        "first_named_insured[business_zip]": a.get("zip", ""),
        "first_named_insured[corporation_type]": CORP.get(a.get("entity", ""), ""),
        "same_mailing_address_as_business_address": True,
        "insured_contact[first_name]": first,
        "insured_contact[last_name]": last.strip(),
        "insured_contact[phone]": "".join(c for c in a.get("phone", "") if c.isdigit())[-10:],
        "insured_contact[email]": a.get("email", ""),
        "business_owner_information_same_as_insured_contact": True,
        "primary_class_codes": [a["blitzClass"]] if a.get("blitzClass") else [],
        "if_any_class_codes": [],
        "operations_description": a.get("operations", ""),
        "general_liability_occurrence_limit_v2": LIMIT.get(a.get("limits", ""), ""),
        "products_and_completed_operations_occurrence_separate_aggregate_limit_v2": LIMIT.get(a.get("limits", ""), ""),
        "general_liability_occurrence_deductible": "no_deductible",
        "total_annual_sales": a.get("revenue"),
    }
    if a.get("payroll") not in (None, ""):
        ans["total_annual_payroll_of_employees_not_including_subcontracting_costs"] = a["payroll"]
    if started:
        ans["first_time_buyer"] = (today - started).days < 365
        ans["year_company_was_established"] = started.year
    if a.get("claims") is not None:
        ans["claims_past_three_years_package"] = str(a["claims"]) != "0"
    if a.get("insured") == "N":
        ans["renewal_quote_received"] = "no_prior_coverage"
    ans.update(a.get("blitzAnswers") or {})          # the client's own answers to Blitz's questions
    return ans


# ---------------------------------------------------------------- session
class BlitzSession:
    def __init__(self, client: httpx.Client | None = None, pause: float = 0.5):
        self.http = client or httpx.Client(timeout=45, headers={"User-Agent": UA}, follow_redirects=False)
        self._own = client is None
        self.pause = pause
        self.csrf = None
        self.token = None          # authenticity_token of the current page
        self.uuid = None
        self.html = ""
        self.trail = []

    def close(self):
        if self._own:
            self.http.close()

    def _req(self, method, url, **kw):
        if FORBIDDEN_PATH.search(urlparse(url).path):
            raise BlitzError(f"refusing to call {urlparse(url).path}: Bindswift never binds or checks out")
        url = url if url.startswith("http") else BASE + url
        r = self.http.request(method, url, **kw)
        self.trail.append(f"{method} {urlparse(url).path} {r.status_code}")
        return r

    def _get_follow(self, url, hops=6):
        for _ in range(hops):
            r = self._req("GET", url)
            if r.status_code in (301, 302, 303, 307, 308):
                url = r.headers["location"]
                continue
            r.raise_for_status()
            return r, url
        raise BlitzError("too many redirects")

    def _page(self, r):
        self.html = r.text
        m = re.search(r'name="csrf-token" content="([^"]+)"', self.html)
        if m:
            self.csrf = htmlmod.unescape(m.group(1))
        m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', self.html) or \
            re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', self.html)
        self.token = htmlmod.unescape(m.group(1)) if m else self.csrf

    def stage(self) -> str:
        m = re.search(r"Stage\s*</[^>]+>\s*<[^>]+>\s*([^<]+)", self.html)
        return m.group(1).strip() if m else ""

    # -- sign in through First Connect
    def login(self, fc: FirstConnect):
        link = fc.carrier_link("blitz")
        r, _ = self._get_follow(link)
        if "/partners/" not in str(r.url) and "Sign out" not in r.text:
            raise BlitzError("Blitz didn't accept the First Connect sign-in link.")

    # -- start an application
    def start(self, program: str = CONTRACTORS, state: str = STATE, eff: date | None = None) -> str:
        r, _ = self._get_follow("/apply")
        self._page(r)
        m = re.search(r'name="application\[producer_uuid\]"[^>]*value="([^"]+)"', self.html) or \
            re.search(r'value="([^"]+)"[^>]*name="application\[producer_uuid\]"', self.html)
        if not m:
            raise BlitzError("Blitz's program page didn't include the producer id.")
        params = {"application[producer_uuid]": m.group(1), "application[program_identifier]": program,
                  "application[state]": state, "application[requested_effective_date]": (eff or date.today()).isoformat()}
        r = self._req("GET", "/apply/start_application", params=params)
        loc = r.headers.get("location", "")
        m = re.search(r"/apply/([0-9a-f-]{36})", loc)
        if r.status_code not in (301, 302, 303) or not m:
            raise BlitzError("Blitz didn't open a new application.")
        self.uuid = m.group(1)
        self.open()
        return self.uuid

    def open(self, url: str | None = None):
        r, final = self._get_follow(url or f"/apply/{self.uuid}")
        self._page(r)
        q = parse_qs(urlparse(str(final)).query)
        return q.get("global_validations[]", [])

    # -- one page of questions
    def _multipart(self, fields: list[tuple[str, str]]):
        base = [("uuid", self.uuid), ("authenticity_token", self.token or ""), ("submit_turbo", "")]
        return [(k, (None, v)) for k, v in base + fields]

    def dry_run(self, fields) -> dict:
        r = self._req("POST", "/apply/dry_run_submissions_data.json", files=self._multipart(fields),
                      headers={"X-CSRF-Token": self.csrf or "", "Accept": "*/*"})
        r.raise_for_status()
        return r.json()

    def fill_page(self, answers: dict, state: str = STATE) -> list[tuple[str, str]]:
        """Answer every question the page shows, re-checking until the visible set stops changing."""
        fields, seen = [], None
        for _ in range(8):
            schema = self.dry_run(fields)
            vis = visible_questions(schema, state)
            missing, fields = [], []
            used = set()
            for qid, q in vis:
                for name, fq in field_names(qid, q):
                    if name in used:          # an address shows both as one smart field and as lines
                        continue
                    used.add(name)
                    optional = fq.get("optional") or q.get("optional")
                    if name in answers:
                        v = answers[name]
                    elif fq.get("field_type") == "file" or optional:
                        v = None
                    else:
                        missing.append({"id": name, "label": fq.get("label") or q.get("label") or name,
                                        "type": fq.get("field_type"), "options": fq.get("options") or q.get("options"),
                                        "clauses": q.get("attestation_clauses"), "expected": q.get("expected_answer")})
                        continue
                    if fq.get("field_type") == "file":
                        continue
                    if fq.get("field_type") == "attestation" and v is not True:
                        missing.append({"id": name, "label": q.get("label") or name, "type": "attestation",
                                        "clauses": q.get("attestation_clauses"), "expected": q.get("expected_answer")})
                        continue
                    fname = name + "[]" if fq.get("field_type") == "multi_select" else name
                    fields += [(fname, x) for x in encode(fq, v)]
            key = tuple(fields)
            if key == seen:
                break
            seen = key
        if missing:
            raise BlitzNeedsInput(missing)
        errs = [f"{qid}: {', '.join(q.get('errors') or [])}" for qid, q in vis if q.get("errors")]
        if errs and any(e for e in errs if "Required" not in e):
            raise BlitzError("Blitz flagged answers: " + "; ".join(errs))
        return fields

    def submit_page(self, fields):
        r = self._req("POST", "/apply/submit_submissions_data", files=self._multipart(fields),
                      headers={"Accept": "text/html"})
        if r.status_code not in (302, 303):
            raise BlitzError(f"Blitz didn't accept the page (HTTP {r.status_code}).")
        problems = self.open(r.headers["location"])
        if problems:
            raise BlitzDecline(problems)

    def choose_products(self):
        """General liability with its standard coverages; every other product off."""
        inputs = re.findall(r"<input[^>]*>", self.html)
        pairs = []
        for tag in inputs:
            name = re.search(r'name="([^"]+)"', tag)
            if not name or not name.group(1).startswith("products["):
                continue
            n = htmlmod.unescape(name.group(1))
            typ = (re.search(r'type="([^"]+)"', tag) or [None, ""])[1]
            if typ == "hidden":
                val = re.search(r'value="([^"]*)"', tag)
                pairs.append((n, htmlmod.unescape(val.group(1)) if val else "false"))
            elif typ == "checkbox" and (n, "true") in pairs:
                continue                                         # always-on coverage, already sent
            elif typ == "checkbox":
                if n == "products[general_liability][is_selected]":
                    on = True
                elif n.startswith("products[general_liability][coverages]"):
                    on = "excess_liability" not in n
                elif n.endswith("[is_selected]") and n.count("[") == 2:
                    on = False                                   # other products stay off
                else:
                    on = " checked" in tag                       # leave defaults of unselected products
                if on:
                    pairs.append((n, "true"))
        if not pairs:
            raise BlitzError("Blitz's product page had no products to pick.")
        body = urlencode([("uuid", self.uuid), ("authenticity_token", self.token or ""), ("submit_turbo", "")] + pairs)
        r = self._req("POST", "/apply/choose_products", content=body,
                      headers={"Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code not in (302, 303):
            raise BlitzError("Blitz didn't accept the product selection.")
        problems = self.open(r.headers["location"])
        if problems:
            raise BlitzDecline(problems)

    def quote(self) -> dict:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", self.html, flags=re.S)
        text = htmlmod.unescape(re.sub(r"<[^>]+>", "\n", text))
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        def after(label):
            for i, x in enumerate(lines):
                if x == label:
                    for y in lines[i + 1:i + 4]:
                        m = re.fullmatch(r"\$([\d,]+(?:\.\d\d)?)", y)
                        if m:
                            return float(m.group(1).replace(",", ""))
            return None
        total = after("Quoted total")
        if total is None:
            raise BlitzError("Blitz reached the quote page but no total was found.")
        prems = []
        for i, x in enumerate(lines):
            if x == "Total Premium:" and i + 1 < len(lines):
                m = re.fullmatch(r"\$([\d,]+)", lines[i + 1])
                if m:
                    prems.append(float(m.group(1).replace(",", "")))
        ref = re.search(r"\b([A-Z]{2}-[A-Z]{2}-\d{4}-[A-Z0-9]{4})\b", text)
        return {"premium": total, "coverage_premiums": prems, "reference": ref.group(1) if ref else None}


# ---------------------------------------------------------------- the whole run
def run_quote(sess: BlitzSession, a: dict, program: str = CONTRACTORS) -> dict:
    answers = profile_answers(a)
    state = a.get("state") or STATE
    sess.start(program, state, _d(a.get("effDate")))
    for _ in range(20):
        st = sess.stage()
        if st == "Review Quote":
            q = sess.quote()
            return {"ok": True, "carrier": "blitz", "premium": q["premium"], "monthly": None,
                    "amounts": q["coverage_premiums"], "quote_number": q["reference"], "application": sess.uuid}
        if st == "Underwriting Review":
            raise BlitzReferred("Blitz sent this application to an underwriter.")
        if "/apply/choose_products" in sess.html:
            sess.choose_products()
        else:
            sess.submit_page(sess.fill_page(answers, state))
        time.sleep(sess.pause)
    raise BlitzError("Blitz's application didn't reach a quote.")


def create_quote(a: dict, client: httpx.Client | None = None, fc: FirstConnect | None = None,
                 pause: float = 0.5) -> dict:
    """Sign in through First Connect, run the application to Review Quote, return the price. Never binds."""
    sess = BlitzSession(client, pause=pause)
    own_fc = fc is None
    fc = fc or FirstConnect()
    try:
        sess.login(fc)
        return run_quote(sess, a)
    except BlitzNeedsInput as e:
        return {"ok": False, "decision": "needs_input", "error": str(e), "questions": e.questions,
                "application": sess.uuid, "trail": sess.trail}
    except BlitzDecline as e:
        return {"ok": False, "decision": "decline", "error": str(e), "application": sess.uuid, "trail": sess.trail}
    except BlitzReferred as e:
        return {"ok": False, "decision": "referred", "error": str(e), "application": sess.uuid, "trail": sess.trail}
    except BlitzError as e:
        return {"ok": False, "error": str(e), "application": sess.uuid, "trail": sess.trail}
    except Exception as e:                         # FirstConnect errors, HTTP errors
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "trail": sess.trail}
    finally:
        sess.close()
        if own_fc:
            fc.close()
