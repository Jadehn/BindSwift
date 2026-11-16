"""Offline check of the Blitz integration against a recorded portal session (HAR).

  python scripts/blitz_replay_check.py "fc captures/app.blitzinsurance.com.har"

Replays the recorded Blitz responses to integrations/blitz.py (no network, no sign-in) and compares
every page the code submits with the page the browser submitted. The answers are read back out of
the recording, so no personal data lives in this script. A recorded page Blitz refused (an
ineligible class code tried first) is skipped: the code goes straight to the class that was quoted.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402

from app.integrations import blitz as bz  # noqa: E402


def multipart_fields(text):
    out = []
    for part in re.split(r"-{4,}WebKitFormBoundary\w+", text)[1:]:
        m = re.search(r'name="([^"]+)"\r?\n\r?\n(.*?)\r?\n$', part, re.S)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def body_fields(req: httpx.Request):
    ct = req.headers.get("content-type", "")
    raw = req.content.decode()
    if "multipart" in ct:
        b = ct.split("boundary=")[1]
        out = []
        for part in raw.split("--" + b)[1:]:
            m = re.search(r'name="([^"]+)"\r?\n(?:[^\r\n]*\r?\n)*?\r?\n(.*?)\r?\n$', part, re.S)
            if m:
                out.append((m.group(1), m.group(2)))
        return out
    return parse_qsl(raw, keep_blank_values=True)


SKIP = {"uuid", "authenticity_token", "submit_turbo"}


def main(path):
    E = json.load(open(path, encoding="utf-8"))["log"]["entries"]
    url = lambda e: e["request"]["url"]
    subs = [i for i, e in enumerate(E) if url(e).endswith("/submit_submissions_data")]
    refused = {i for i in subs if "global_validations" in url(E[i + 1])}
    good = [i for i in subs if i not in refused]
    prod = next(i for i, e in enumerate(E) if url(e).endswith("/choose_products"))
    # recorded page each submit leads to, in order, with the refused attempts removed
    pages_after = {i: i + 1 for i in good + [prod]}
    start = next(i for i, e in enumerate(E) if "/apply/start_application" in url(e))
    app_uuid = re.search(r"/apply/([0-9a-f-]{36})", next(h["value"] for h in E[start]["response"]["headers"]
                                                          if h["name"].lower() == "location")).group(1)
    # the dry_run answer for each step = the last dry_run before that step's submit
    def last_dry(before):
        return max(k for k in range(before) if url(E[k]).endswith("dry_run_submissions_data.json"))
    rec_fields = {i: [(k, v) for k, v in multipart_fields(E[i]["request"]["postData"]["text"]) if k not in SKIP]
                  for i in good}
    rec_prod = [(k, v) for k, v in parse_qsl(E[prod]["request"]["postData"]["text"], keep_blank_values=True)
                if k not in SKIP]

    # answers the client gave, read back from the recording
    allv = {}
    for i in good:
        for k, v in rec_fields[i]:
            allv.setdefault(k, []).append(v)
    one = {k: v[0] for k, v in allv.items()}
    g = lambda k: one.get(k, "")
    tf = lambda k: one.get(k) == "true"
    money = lambda k: float(g(k).replace("$", "").replace(",", "") or 0)
    from datetime import date
    a = {"legalName": g("first_named_insured[legal_business_name]"), "dba": g("first_named_insured[dba]"),
         "street": g("first_named_insured[business_address_1]"), "suite": g("first_named_insured[business_address_2]"),
         "city": g("first_named_insured[business_city]"), "state": g("first_named_insured[business_state]"),
         "zip": g("first_named_insured[business_zip]"),
         "entity": {v: k for k, v in bz.CORP.items()}[g("first_named_insured[corporation_type]")],
         "contactName": f"{g('insured_contact[first_name]')} {g('insured_contact[last_name]')}",
         "phone": g("insured_contact[phone]"), "email": g("insured_contact[email]"),
         "blitzClass": allv["primary_class_codes[]"][-1], "operations": g("operations_description"),
         "started": date.today().isoformat() if tf("first_time_buyer") else "2015-01-01",
         "revenue": money("total_annual_sales"),
         "payroll": money("total_annual_payroll_of_employees_not_including_subcontracting_costs"),
         "limits": "1/1" if g("general_liability_occurrence_limit_v2") == "occurrence_1m_aggregate_1m" else "1/2",
         "claims": "1" if tf("claims_past_three_years_package") else "0",
         "effDate": parse_qsl(urlparse(url(E[start])).query)[-1][1]}
    derived = set(bz.profile_answers(a))
    extra = {}
    for k, vals in allv.items():
        name = k[:-2] if k.endswith("[]") else k
        if name in derived:
            continue
        v = vals[-1]
        extra[name] = True if v == "true" else False if v == "false" else v
    for k in ("total_annual_payroll_of_employees_not_including_subcontracting_costs", "total_annual_sales"):
        extra.pop(k, None)
    a["blitzAnswers"] = extra
    a["blitzAnswers"]["operations_description"] = allv["operations_description"][-1]

    step = {"i": 0}
    sent, sent_prod = [], []
    order = good[:]                      # submits in order (refused attempt removed)
    seq_pages = [E[pages_after[i]] for i in sorted(pages_after)]

    def handler(req: httpx.Request):
        p = req.url.path
        if p == "/apply" and req.method == "GET":
            return httpx.Response(200, text=E[next(i for i, e in enumerate(E) if url(e).rstrip("/").endswith("/apply"))]
                                  ["response"]["content"]["text"])
        if p == "/apply/start_application":
            return httpx.Response(302, headers={"location": f"{bz.BASE}/apply/{app_uuid}"})
        if p == f"/apply/{app_uuid}":
            k = step["i"]
            if k == 0:
                return httpx.Response(200, text=E[start + 1]["response"]["content"]["text"])
            return httpx.Response(200, text=seq_pages[k - 1]["response"]["content"]["text"])
        if p.endswith("dry_run_submissions_data.json"):
            nxt = sorted(good + [prod])[step["i"]]
            return httpx.Response(200, text=E[last_dry(nxt)]["response"]["content"]["text"])
        if p.endswith("submit_submissions_data"):
            sent.append([(k, v) for k, v in body_fields(req) if k not in SKIP])
            step["i"] += 1
            return httpx.Response(302, headers={"location": f"{bz.BASE}/apply/{app_uuid}"})
        if p.endswith("choose_products"):
            sent_prod.extend((k, v) for k, v in body_fields(req) if k not in SKIP)
            step["i"] += 1
            return httpx.Response(302, headers={"location": f"{bz.BASE}/apply/{app_uuid}"})
        return httpx.Response(404, text=f"unexpected {req.method} {p}")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    sess = bz.BlitzSession(client, pause=0)
    res = bz.run_quote(sess, a)

    ok = True
    for n, (rec_i, mine) in enumerate(zip(order, sent), 1):
        want = [(k, v) for k, v in rec_fields[rec_i]]
        if sorted(mine) != sorted(want):
            ok = False
            print(f"page {n}: DIFFERENT")
            from collections import Counter
            cw, cm = Counter(want), Counter(mine)
            print("   only recorded:", sorted((cw - cm).items())[:12])
            print("   only ours:    ", sorted((cm - cw).items())[:12])
        else:
            print(f"page {n}: same ({len(mine)} fields)")
    same_prod = sorted(sent_prod) == sorted(rec_prod)
    ok &= same_prod
    print(f"product selection: {'same' if same_prod else 'DIFFERENT'} ({len(sent_prod)} fields)")
    if not same_prod:
        from collections import Counter
        print("   counts:", sorted((Counter(rec_prod)-Counter(sent_prod)).items())[:6], sorted((Counter(sent_prod)-Counter(rec_prod)).items())[:6])
        print("   only ours:    ", sorted(set(sent_prod) - set(rec_prod))[:10])
    print(f"pages submitted: {len(sent)} of {len(order)} recorded")
    print("result:", {k: v for k, v in res.items() if k in ("ok", "premium", "amounts", "quote_number")})
    print("ALL MATCH" if ok and len(sent) == len(order) and res.get("ok") else "MISMATCH")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fc captures/app.blitzinsurance.com.har")
