"""Run one live Hiscox quote from a JSON file of sample answers, without the website or database.

  .venv\\Scripts\\python.exe scripts\\hiscox_test_quote.py "fc captures\\howard-housing-sample.json"

Uses HISCOX_EMAIL / HISCOX_PASSWORD from backend/.env. Stops at the price; never binds.
Hiscox saves the quote in your agent portal and may email the address in the sample.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.integrations import hiscox  # noqa: E402


def main(path: str) -> int:
    answers = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = hiscox.eligibility(answers)
    if problems:
        print("Not ready for Hiscox:", *problems, sep="\n  - ")
        return 1
    print(f"Quoting {answers.get('legalName')} with Hiscox (about 30-90 seconds)...")
    res = hiscox.create_quote(answers)
    for t in res.get("trail", []):
        print("  step:", t.get("label") or t.get("step"), "->", t.get("screen", ""))
    if not res["ok"] and res.get("decision"):
        print(f"\nHISCOX {res['decision'].upper()}: {res['error']}")
        print("That's Hiscox's underwriting answer, not a Bindswift error. Try another class or carrier.")
        return 1
    if not res["ok"]:
        print("\nNO QUOTE:", res["error"])
        for line in res.get("debug", []):
            print("  debug:", line)
        if res.get("debug_html"):
            out = Path(path).with_name("hiscox-debug-sso.html")
            out.write_text(res["debug_html"], encoding="utf-8")
            print(f"  saved the portal's reply to {out} (git-ignored)")
        if res.get("debug_screen"):
            out = Path(path).with_name("hiscox-debug-screen.json")
            out.write_text(json.dumps(res["debug_screen"]), encoding="utf-8")
            print(f"  saved the last Hiscox screen to {out} (git-ignored)")
        return 1
    print(f"\nHiscox premium: ${res['premium']:,.2f} per year"
          + (f"  (monthly installment ${res['monthly']:,.2f})" if res.get("monthly") else ""))
    for label, amt in res.get("amounts", []):
        print(f"  {label}: ${amt:,.2f}")
    print("\nNothing was bound. Open the quote in the Hiscox portal to review or bind it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "fc captures/howard-housing-sample.json"))
