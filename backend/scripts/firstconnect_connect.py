"""Connect Bindswift to First Connect once, so it can open Blitz (and other First Connect carriers).

  .venv\\Scripts\\python.exe scripts\\firstconnect_connect.py

A normal browser window opens on First Connect's sign-in page. Sign in yourself: email, password and
the text-message code. Bindswift never sees your password or the code; it only keeps the long-lived
sign-in token First Connect issues after you sign in, encrypted in the database.

Run it again whenever a quote says First Connect needs reconnecting.

Needs Playwright on this computer (one time):
  .venv\\Scripts\\python.exe -m pip install playwright
  .venv\\Scripts\\python.exe -m playwright install chromium
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations import firstconnect as fcmod  # noqa: E402


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright isn't installed. Run:\n"
              "  .venv\\Scripts\\python.exe -m pip install playwright\n"
              "  .venv\\Scripts\\python.exe -m playwright install chromium")
        return 1
    got = {}

    def on_response(resp):
        if resp.url.startswith(fcmod.AUTH + "/oauth/token") and resp.request.method == "POST":
            try:
                body = resp.json()
            except Exception:
                return
            if body.get("refresh_token"):
                got["rt"] = body["refresh_token"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.on("response", on_response)
        page.goto(fcmod.PORTAL + "/signin")
        print("Sign in to First Connect in the browser window (password and text code).")
        print("This window closes by itself once you're signed in.")
        for _ in range(600):                      # up to 10 minutes
            if got.get("rt"):
                break
            page.wait_for_timeout(1000)
        browser.close()
    if not got.get("rt"):
        print("Didn't see a finished sign-in. Nothing was saved. Try again.")
        return 1
    fc = fcmod.FirstConnect(refresh_token=got["rt"])
    try:
        fc.authenticate()                         # also saves the (rotated) token
        me = fc.me()
        fc.carrier_link("blitz")
    except Exception as e:
        print(f"Signed in, but First Connect didn't respond as expected: {type(e).__name__}. Nothing was saved.")
        return 1
    finally:
        fc.close()
    if fc.refresh_token == got["rt"]:             # no rotation happened, save it ourselves
        fcmod.save_refresh_token(got["rt"])
    print(f"Connected. First Connect producer {me.get('producer_id')}, Blitz link OK. Saved encrypted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
