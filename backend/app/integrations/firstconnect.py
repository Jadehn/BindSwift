"""First Connect: the producer portal that hands Bindswift's signed-in session to carriers (Blitz today).

Sign-in is Auth0 with a password, a text-message code and bot protection, so Bindswift never signs in
by itself. The broker connects once with scripts/firstconnect_connect.py: it opens a normal browser,
the broker signs in by hand, and the script keeps the long-lived refresh token Auth0 returns. The
token is stored encrypted (Fernet, DATA_KEY) in app_settings and swapped for a one-hour access
token whenever a quote runs. Auth0 rotates refresh tokens, so every refresh saves the new one.

Endpoints (from a recorded session, docs/blitz-api-notes.md):
  POST https://auth.firstconnectinsurance.com/oauth/token          grant_type=refresh_token
  GET  https://portal-bff.firstconnectinsurance.com/api/v1/producer/me
  GET  .../api/v1/producer/dashboard-carriers/{producer_id}/carrier/{carrier_id}/link
       -> {"success": true, "carrierLink": "<carrier SSO url>", ...}
"""
from __future__ import annotations

import base64
import json
import time

import httpx

AUTH = "https://auth.firstconnectinsurance.com"
BFF = "https://portal-bff.firstconnectinsurance.com/api/v1"
PORTAL = "https://portal.firstconnectinsurance.com"
CLIENT_ID = "mj8CZ5Lyp8vtQK2fpVh685cdUa36WcRX"     # First Connect portal's public Auth0 client
SETTING_KEY = "firstconnect_token"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

CARRIER_IDS = {"blitz": 834}


class FirstConnectError(Exception):
    pass


class NotConnected(FirstConnectError):
    """No saved sign-in, or First Connect no longer accepts it: run scripts/firstconnect_connect.py."""


# ---------------------------------------------------------------- token storage (encrypted)
def save_refresh_token(token: str) -> None:
    from .. import db
    from ..security import encrypt
    blob = base64.b64encode(encrypt(token.encode())).decode()
    db.run("""INSERT INTO app_settings (key, value, updated_by, updated_at)
              VALUES (%s, %s::jsonb, NULL, now())
              ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                              updated_at = now()""",
           (SETTING_KEY, json.dumps({"enc": blob, "saved": int(time.time())})))


def load_refresh_token() -> str | None:
    from .. import db
    from ..security import decrypt
    try:
        row = db.one("SELECT value FROM app_settings WHERE key = %s", (SETTING_KEY,))
    except Exception:
        return None
    if not row or not isinstance(row["value"], dict) or not row["value"].get("enc"):
        return None
    try:
        return decrypt(base64.b64decode(row["value"]["enc"])).decode()
    except Exception:
        return None


def connected() -> bool:
    return bool(load_refresh_token())


# ---------------------------------------------------------------- session
class FirstConnect:
    def __init__(self, client: httpx.Client | None = None, refresh_token: str | None = None,
                 save=save_refresh_token):
        self.http = client or httpx.Client(timeout=30, headers={"User-Agent": UA})
        self._own = client is None
        self.refresh_token = refresh_token
        self.save = save
        self.access_token = None
        self.producer_id = None

    def close(self):
        if self._own:
            self.http.close()

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}", "Origin": PORTAL, "Referer": PORTAL + "/",
                "Accept": "application/json"}

    def authenticate(self) -> None:
        rt = self.refresh_token or load_refresh_token()
        if not rt:
            raise NotConnected("First Connect isn't connected. Run scripts/firstconnect_connect.py once.")
        r = self.http.post(f"{AUTH}/oauth/token", data={
            "client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt},
            headers={"Origin": PORTAL, "Referer": PORTAL + "/"})
        if r.status_code in (400, 401, 403):
            raise NotConnected("First Connect's saved sign-in expired. Run scripts/firstconnect_connect.py again.")
        r.raise_for_status()
        tok = r.json()
        self.access_token = tok["access_token"]
        new_rt = tok.get("refresh_token")
        if new_rt and new_rt != rt:                     # rotation: the old one stops working
            self.refresh_token = new_rt
            if self.save:
                self.save(new_rt)
        else:
            self.refresh_token = rt

    def me(self) -> dict:
        r = self.http.get(f"{BFF}/producer/me", headers=self._headers())
        r.raise_for_status()
        d = r.json()
        self.producer_id = d.get("producer_id") or (d.get("producer") or {}).get("id")
        if not self.producer_id:
            raise FirstConnectError("First Connect didn't return a producer id.")
        return d

    def carrier_link(self, carrier: str) -> str:
        """The one-time SSO link First Connect hands the browser when you click Go to carrier."""
        if not self.access_token:
            self.authenticate()
        if not self.producer_id:
            self.me()
        cid = CARRIER_IDS[carrier]
        r = self.http.get(f"{BFF}/producer/dashboard-carriers/{self.producer_id}/carrier/{cid}/link",
                          headers=self._headers())
        r.raise_for_status()
        d = r.json()
        link = d.get("carrierLink")
        if not d.get("success") or not link:
            raise FirstConnectError(f"First Connect didn't return a {carrier} link.")
        return link
