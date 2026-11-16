"""Which carriers can quote a business automatically, from a short business profile.

One list for the server and the website (sent to the page through /api/config), so the classes a
client can pick and the carrier codes behind them never drift apart.

Today the automatic market is Hiscox (agent portal, see integrations/hiscox.py). A profile that
doesn't match, or that Hiscox declines, goes to the full ACORD application, which the broker
sends to MGA partners. Add Coterie here once its API keys are in (its appetite is in
docs and the First Connect Carriers sheet).
"""
from .config import get_settings

# Bindswift class -> kinds of business a client picks from, each with the Hiscox profession code.
# Codes come from Hiscox's own profession list (backend/fc captures/hiscox-professions.json).
CLASSES = {
    "bookkeeping": {"l": "Bookkeeping & tax preparation", "kinds": [
        ("bookkeeping", "Bookkeeping", "1147045"),
        ("tax", "Tax preparation", "1147845"),
        ("accounting", "Accounting", "1146845")]},
    "consultant": {"l": "Business consulting", "kinds": [
        ("business", "Business consulting", "1145645"),
        ("management", "Management consulting", "1145945"),
        ("strategy", "Strategy consulting", "1146445"),
        ("other", "Other consulting", "1146545")]},
    "marketing": {"l": "Marketing & advertising", "kinds": [
        ("media", "Marketing / media consulting", "1151945"),
        ("digital", "Digital marketing", "1151745"),
        ("advertising", "Advertising", "1151545"),
        ("social", "Social media", "1152345")]},
    "salon": {"l": "Hair salon", "kinds": [("salon", "Hair salon", "1143545")]},
    "barber": {"l": "Barbershop", "kinds": [("barber", "Barbershop", "1143445")]},
    "beauty": {"l": "Nails, lashes & braiding", "kinds": [
        ("nails", "Nail services", "1144745"),
        ("lash", "Lashes, brows & esthetics", "1143945"),
        ("braid", "Braiding & locs", "1143545")]},
    "wellness": {"l": "Wellness & coaching", "kinds": [
        ("coach", "Life / career coaching", "1153145"),
        ("nutrition", "Diet & nutrition", "1143745"),
        ("yoga", "Yoga or Pilates instruction", "1145545"),
        ("trainer", "Personal training", "1145045")]},
    "retail": {"l": "Retail store", "kinds": [
        ("clothing", "Clothing & apparel", "1150045"),
        ("beauty_supply", "Cosmetics & beauty supply", "1165345"),
        ("other", "Other store (no food or drinks)", "1150445")]},
    "doula": {"l": "Doula services", "kinds": [("doula", "Doula services", None)]},
}

MAX_REVENUE = 5_000_000      # past this, a broker looks first
PAUSED = "Online quoting is paused right now, so your broker will quote this one for you."


def hiscox_code(cls: str, kind: str) -> str | None:
    for k, _, code in CLASSES.get(cls, {}).get("kinds", []):
        if k == kind:
            return code
    return None


def kind_label(cls: str, kind: str) -> str:
    for k, label, _ in CLASSES.get(cls, {}).get("kinds", []):
        if k == kind:
            return label
    return CLASSES.get(cls, {}).get("l", cls)


def for_page() -> list[dict]:
    """The class list the website shows (no carrier codes)."""
    return [{"v": cls, "l": c["l"], "kinds": [{"v": k, "l": label, "instant": bool(code)} for k, label, code in c["kinds"]]}
            for cls, c in CLASSES.items()]


def hiscox_connected() -> bool:
    s = get_settings()
    return bool(getattr(s, "hiscox_email", "") and getattr(s, "hiscox_password", ""))


def instant_enabled() -> bool:
    """The broker's switch in the admin. Off until it's turned on, so no client triggers a live
    carrier quote by accident."""
    from . import db
    try:
        row = db.one("SELECT value FROM app_settings WHERE key = 'instant_quotes'")
    except Exception:
        return False
    return bool(row and row["value"] is True)


def hiscox_ready() -> bool:
    """Live instant quotes run only with Hiscox connected AND the admin switch on."""
    return hiscox_connected() and instant_enabled()


def match(a: dict) -> dict:
    """{'route': 'instant' | 'application', 'carriers': [...], 'reasons': [...]}.
    reasons explain an 'application' route in plain words for the client and the admin."""
    reasons = []
    code = hiscox_code(a.get("trade", ""), a.get("kind", ""))
    if not code:
        reasons.append("We don't have an online insurer for this kind of business yet.")
    if float(a.get("revenue") or 0) > MAX_REVENUE:
        reasons.append("Revenue over $5M goes to a broker first.")
    if str(a.get("claims") or "0") != "0":
        reasons.append("Recent claims mean a broker reviews this first.")
    if a.get("install") == "Y":
        reasons.append("Design, construction or installation work needs a specialty insurer.")
    if a.get("trade") == "retail" and not (a.get("productsDesc") or "").strip():
        reasons.append("Tell us what the store sells.")
    if not reasons and not hiscox_ready():
        reasons.append(PAUSED)
    if reasons:
        return {"route": "application", "carriers": [], "reasons": reasons}
    return {"route": "instant", "carriers": [{"id": "hiscox", "name": "Hiscox", "code": code}], "reasons": []}
