"""Run one live Blitz quote from a JSON file of answers. Never binds.

  .venv\Scripts\python.exe scripts\blitz_test_quote.py "fc captures\blitz-sample.json"

Needs First Connect connected first (scripts\firstconnect_connect.py). If Blitz asks something the
file doesn't answer, the questions are printed: add them to "blitzAnswers" and run again.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.integrations import blitz  # noqa: E402


def main(path):
    a = json.load(open(path, encoding="utf-8"))
    res = blitz.create_quote(a)
    if res.get("ok"):
        print(f"Blitz quoted ${res['premium']:,.2f}/yr  (ref {res.get('quote_number')}, coverages {res.get('amounts')})")
    elif res.get("decision") == "needs_input":
        print("Blitz needs these answered (add to blitzAnswers):")
        for q in res["questions"]:
            print(f"  {q['id']}  [{q['type']}]  {q['label']}" + (f"  options={list(q['options'])}" if q.get("options") else ""))
    else:
        print("No quote:", res.get("decision") or "", res.get("error"))
    print("trail:", " | ".join(res.get("trail", [])[-8:]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fc captures/blitz-sample.json")
