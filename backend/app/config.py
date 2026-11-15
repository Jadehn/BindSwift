"""All configuration comes from environment variables (see .env.example).
Nothing secret lives in the code or the repo."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    env: str = Field("production", description="'development' relaxes the Secure-cookie rule for localhost only")
    database_url: str

    # 32-byte url-safe base64 Fernet key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    data_key: str
    # Random secret used to hash IP addresses (we never store raw IPs)
    ip_hash_salt: str

    # Email (SMTP with STARTTLS, e.g. Google Workspace, Microsoft 365, Resend, Postmark)
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str
    smtp_password: str
    mail_from: str
    broker_email: str  # where new-submission emails go (you)
    public_base_url: str = "http://localhost:8000"  # used for the "review it" link in emails
    # The emailed copies mask the FEIN. Full copies are only in the admin portal.
    email_attach_pdfs: bool = True

    # Admin security
    admin_path: str = "/admin"                 # change to something unguessable
    admin_ip_allowlist: str = ""               # comma-separated IPs; empty = allow all
    session_idle_minutes: int = 30
    session_absolute_hours: int = 8
    login_max_failures: int = 5                # per email or per IP...
    login_window_minutes: int = 15             # ...within this window

    # Public submission endpoint
    submissions_per_ip_per_hour: int = 5
    max_body_bytes: int = 64_000

    # Paths
    acord125_pdf: Path = BASE_DIR / "forms" / "ACORD_125.pdf"
    acord126_pdf: Path = BASE_DIR / "forms" / "ACORD_126.pdf"
    frontend_dir: Path = BASE_DIR.parent  # serves ../index.html

    # Producer info printed on the forms
    agency_name: str = "Jaden Howard"         # matches the AGENCY box on your sample submission
    agency_address: str = ""                   # printed under the agency name; use \n between lines
    producer_name: str = "Jaden Howard"
    producer_phone: str = ""
    producer_email: str = ""
    producer_npn: str = ""                     # National Producer Number
    producer_license: str = "1931665"
    signature_font: Path = BASE_DIR / "fonts" / "GreatVibes-Regular.ttf"

    # Carriers
    coterie_env: str = "sandbox"              # "sandbox" until Coterie approves you for live
    coterie_publishable_key: str = ""         # creates quotes
    coterie_secret_key: str = ""              # binds; keep server-side only (not used yet)
    coterie_agency_external_id: str = ""
    coterie_akhash: str = ""
    coterie_base_url: str = ""                # override only for testing
    coterie_bind_path: str = ""               # default "/{quote_id}/bind"; confirm in sandbox
    fee_amount: int = 50
    anthropic_api_key: str = ""               # optional: AI reading of unfamiliar quote formats
    anthropic_model: str = "claude-sonnet-5-5"
    max_upload_bytes: int = 4_000_000         # Vercel caps request bodies at 4.5 MB
    offer_days: int = 14                      # how long a quote link stays valid                  # JSON: {"handyman":"<AKHash>", "plumbing":"<AKHash>", ...}

    # Anti-spam + verification
    require_email_verification: bool = True   # contractor must confirm their email with a code
    turnstile_site_key: str = ""              # optional Cloudflare Turnstile (invisible bot check)
    turnstile_secret: str = ""
    socrata_app_token: str = ""               # optional: higher rate limits on data.ny.gov
    ny_entities_url: str = ""                 # override only for testing; defaults to data.ny.gov

    # Hosting
    trust_proxy: str = ""                 # "vercel" -> read the client IP from x-vercel-forwarded-for
    allowed_hosts: str = "*"              # comma-separated, e.g. "bindswift.com,www.bindswift.com"
    email_inline: bool | None = None      # send email before responding (required on serverless); auto on Vercel
    db_pool_max: int = 5

    @property
    def on_vercel(self) -> bool:
        import os
        return bool(os.environ.get("VERCEL")) or self.trust_proxy == "vercel"

    @property
    def send_email_inline(self) -> bool:
        return self.on_vercel if self.email_inline is None else self.email_inline

    @property
    def is_dev(self) -> bool:
        return self.env == "development"

    @property
    def allowlist(self) -> set[str]:
        return {ip.strip() for ip in self.admin_ip_allowlist.split(",") if ip.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
