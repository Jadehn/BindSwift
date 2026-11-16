"""Offline check of the Hiscox integration against a recorded portal session (HAR).

  python scripts/hiscox_replay_check.py "fc captures/portal.hiscox.com.har"

Replays the recorded portal responses to integrations/hiscox.py (no network, no sign-in) and
compares every request the code builds with the request the browser sent. The application
answers are read back out of the recording, so no personal data lives in this script.
"""
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402

from app.integrations import hiscox as hx  # noqa: E402

IGNORE = {"HTTP_SESSION_ID", "google_api_key"}   # redacted in the sanitized capture


def main(path):
    E = json.load(open(path, encoding="utf-8"))["log"]["entries"]
    sso = next(e for e in E if e["request"]["url"].endswith("/oneshield/sso"))
    gw = [e for e in E if e["request"]["url"].endswith("GatewayServlet")]
    fp = [e for e in E if e["request"]["url"].endswith("FieldProcessorServlet")]
    rec = [parse_qsl(e["request"]["postData"]["text"], keep_blank_values=True) for e in gw]
    allv = {k.split("_", 2)[-1]: v for r in rec for k, v in r if k.startswith("bv_")}

    a = {"zip": allv[hx.F_ZIP_START], "hiscoxClass": allv[hx.F_PROFESSION], "entity": "LLC",
         "effDate": "-".join(reversed(allv[hx.F_START_DATE].split("/")[2:] + allv[hx.F_START_DATE].split("/")[:2])),
         "contactName": f"{allv[hx.F_FIRST]} {allv[hx.F_LAST]}", "legalName": allv[hx.F_BIZ_NAME],
         "phone": allv[hx.F_PHONE], "email": allv[hx.F_EMAIL],
         "started": f"{allv[hx.F_BIZ_BEGAN][3:]}-{allv[hx.F_BIZ_BEGAN][:2]}-01",
         "insured": "Y" if allv[hx.F_HAS_POLICY] == "1" else "N", "street": allv[hx.F_STREET],
         "suite": allv.get(hx.F_SUITE, ""), "city": allv[hx.F_CITY], "county": allv.get(hx.F_COUNTY) or "Other",
         "premises": "Home" if allv[hx.F_HOME_BASED] == "1" else "Tenant", "install": "N",
         "sqft": allv[hx.F_SQFT].replace(",", ""), "revenue": allv[hx.F_REVENUE].replace(",", ""),
         "limits": "1/2" if allv[hx.F_OCCURRENCE] == "1000000" else "2/4"}
    # effDate as ISO
    m, d, y = allv[hx.F_START_DATE].split("/")
    a["effDate"] = f"{y}-{m}-{d}"

    sent_gw, sent_fp = [], []
    fp_left = list(fp)

    def handler(req: httpx.Request):
        form = parse_qsl(req.content.decode(), keep_blank_values=True)
        if req.url.path.endswith("GatewayServlet"):
            i = len(sent_gw)
            sent_gw.append(form)
            return httpx.Response(200, text=gw[i]["response"]["content"]["text"])
        if req.url.path.endswith("FieldProcessorServlet"):
            sent_fp.append(form)
            bv = dict(form).get("AJX_RULE_BV")
            hit = next((e for e in fp_left if dict(parse_qsl(e["request"]["postData"]["text"])).get("AJX_RULE_BV") == bv), None)
            if hit:
                fp_left.remove(hit)
                return httpx.Response(200, text=hit["response"]["content"]["text"])
            return httpx.Response(200, json={"ins": [], "success": True})
        return httpx.Response(404)

    sess = hx.HiscoxSession(httpx.Client(transport=httpx.MockTransport(handler)), pause=0)
    html = sso["response"]["content"]["text"]
    sess.screen = hx.Screen(hx._page_json(html))
    sess.skin = hx._js_var(html, "skin", hx.DEFAULT_SKIN)
    sess.viewport = hx._js_var(html, "os_viewport_id", hx.DEFAULT_VIEWPORT)
    sess.viewport_assign = hx._js_var(html, "os_viewport_assignment_id", hx.DEFAULT_VIEWPORT_ASSIGNMENT)
    sess.trail.append({"step": "signed_in"})
    try:
        res = hx.run_quote(sess, a)
    except hx.HiscoxError as e:
        res = {"ok": False, "error": str(e)}

    problems = 0
    for n, (ours, theirs) in enumerate(zip(sent_gw, rec), 1):
        o, t = dict(ours), dict(theirs)
        diffs = [(k, o.get(k), t.get(k)) for k in sorted(set(o) | set(t))
                 if k not in IGNORE and not k.startswith("oshiddenfield") and o.get(k) != t.get(k)]
        okeys = [k for k, _ in ours if k.startswith("bv_")]
        tkeys = [k for k, _ in theirs if k.startswith("bv_")]
        print(f"step {n} {t.get('TX_NAME')}: {'OK' if not diffs and okeys == tkeys else 'DIFF'}")
        for k, x, y in diffs:
            problems += 1
            print(f"    {k}: ours={str(x)!r}\n      recorded={str(y)!r}" if k=="namefields" else f"    {k}: ours={str(x)[:60]!r} recorded={str(y)[:60]!r}")
        if okeys != tkeys and sorted(okeys) == sorted(tkeys):
            print("    (same fields, different order)")
        elif okeys != tkeys:
            problems += 1
            print("    fields only ours:", [k[-14:] for k in okeys if k not in tkeys],
                  "only recorded:", [k[-14:] for k in tkeys if k not in okeys])
    print(f"\nGateway requests: ours {len(sent_gw)}, recorded {len(rec)}; field-rule calls: ours {len(sent_fp)}, recorded {len(fp)}")
    print("Result:", {k: v for k, v in res.items() if k not in ("trail",)})
    return problems


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1]) else 0)
