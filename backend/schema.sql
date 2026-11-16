-- Bindswift database schema (PostgreSQL 14+)
-- Run once:  psql "$DATABASE_URL" -f schema.sql

CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------
-- Admin users (you). Passwords are argon2id hashes; TOTP secrets
-- are encrypted with the app's Fernet key before they're stored.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admins (
    id              SERIAL PRIMARY KEY,
    email           CITEXT      NOT NULL UNIQUE,
    password_hash   TEXT        NOT NULL,
    totp_secret_enc BYTEA       NOT NULL,
    totp_last_step  BIGINT      NOT NULL DEFAULT 0,   -- blocks reuse of a 6-digit code
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ
);

-- Server-side sessions. Only a SHA-256 hash of the cookie token is stored,
-- so a database leak doesn't hand out live sessions.
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash   BYTEA       PRIMARY KEY,
    admin_id     INT         NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    csrf_token   TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    ip_hash      TEXT,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS admin_sessions_admin_idx ON admin_sessions(admin_id);

-- Every login attempt, for lockout and for your own review.
CREATE TABLE IF NOT EXISTS login_attempts (
    id          BIGSERIAL PRIMARY KEY,
    email       CITEXT,
    ip_hash     TEXT        NOT NULL,
    succeeded   BOOLEAN     NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_email_idx ON login_attempts(email, created_at);
CREATE INDEX IF NOT EXISTS login_attempts_ip_idx    ON login_attempts(ip_hash, created_at);

-- ---------------------------------------------------------------
-- Submissions from contractors
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS submissions (
    id              BIGSERIAL PRIMARY KEY,
    public_id       UUID        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    line            TEXT        NOT NULL DEFAULT 'GL' CHECK (line IN ('GL')),
    status          TEXT        NOT NULL DEFAULT 'pending_review'
                    CHECK (status IN ('pending_review','approved','rejected','sent_to_market','quoted',
                                      'bind_requested','bound')),

    -- searchable, non-sensitive columns (handy for SQL practice)
    legal_name      TEXT        NOT NULL,
    contact_name    TEXT        NOT NULL,
    contact_email   CITEXT      NOT NULL,
    contact_phone   TEXT        NOT NULL,
    trade           TEXT        NOT NULL,
    class_code      TEXT        NOT NULL,
    county          TEXT        NOT NULL,
    annual_revenue  NUMERIC(14,2) NOT NULL,
    annual_payroll  NUMERIC(14,2) NOT NULL,
    sub_cost        NUMERIC(14,2) NOT NULL DEFAULT 0,
    employees_ft    INT         NOT NULL,
    employees_pt    INT         NOT NULL,
    losses_5yr      INT         NOT NULL DEFAULT 0,
    limits          TEXT        NOT NULL,
    effective_date  DATE        NOT NULL,

    -- full answers (FEIN removed) and the ACORD field maps
    answers         JSONB       NOT NULL,
    acord125        JSONB       NOT NULL,
    acord126        JSONB       NOT NULL,
    fein_enc        BYTEA,                -- encrypted; never stored in plain text

    ip_hash         TEXT        NOT NULL,
    email_sent_at   TIMESTAMPTZ,
    email_error     TEXT,
    reviewed_by     INT REFERENCES admins(id),
    reviewed_at     TIMESTAMPTZ,
    review_note     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS submissions_status_idx  ON submissions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS submissions_ip_idx      ON submissions(ip_hash, created_at);

-- Generated PDFs, encrypted at rest (they contain the FEIN).
CREATE TABLE IF NOT EXISTS submission_documents (
    id             BIGSERIAL PRIMARY KEY,
    submission_id  BIGINT      NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    kind           TEXT        NOT NULL CHECK (kind IN ('summary','acord125','acord126',
                                                      'acord125_fillable','acord126_fillable','fee_agreement')),
    filename       TEXT        NOT NULL,
    content_enc    BYTEA       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (submission_id, kind)
);

-- Email verification codes (stored only as HMACs)
CREATE TABLE IF NOT EXISTS email_verifications (
    id          BIGSERIAL PRIMARY KEY,
    email       CITEXT      NOT NULL,
    code_hash   BYTEA       NOT NULL,
    ip_hash     TEXT        NOT NULL,
    attempts    INT         NOT NULL DEFAULT 0,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS email_verifications_email_idx ON email_verifications(email, created_at DESC);
CREATE INDEX IF NOT EXISTS email_verifications_ip_idx    ON email_verifications(ip_hash, created_at);

-- Generic per-IP rate-limit log for public lookups
CREATE TABLE IF NOT EXISTS api_hits (
    bucket     TEXT        NOT NULL,
    ip_hash    TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_hits_idx ON api_hits(bucket, ip_hash, created_at);

ALTER TABLE submissions ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS business_verification JSONB;

-- Quotes requested from carriers (Coterie via API; NEXT etc. recorded by hand)
CREATE TABLE IF NOT EXISTS carrier_quotes (
    id             BIGSERIAL PRIMARY KEY,
    submission_id  BIGINT      NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    carrier        TEXT        NOT NULL,
    environment    TEXT,                       -- sandbox / live
    ok             BOOLEAN     NOT NULL,
    premium        NUMERIC(12,2),
    quote_id       TEXT,
    application_url TEXT,
    error          TEXT,
    raw            JSONB,
    requested_by   INT REFERENCES admins(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS carrier_quotes_sub_idx ON carrier_quotes(submission_id, created_at DESC);

-- Who did what, when. Never updated or deleted by the app.
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    admin_id      INT REFERENCES admins(id),
    action        TEXT        NOT NULL,
    submission_id BIGINT REFERENCES submissions(id),
    detail        JSONB,
    ip_hash       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_sub_idx ON audit_log(submission_id);

-- Upgrading a database created before fillable copies existed:
-- ALTER TABLE submission_documents DROP CONSTRAINT submission_documents_kind_check;
-- ALTER TABLE submission_documents ADD CONSTRAINT submission_documents_kind_check
--   CHECK (kind IN ('summary','acord125','acord126','acord125_fillable','acord126_fillable','fee_agreement'));

-- Quote -> bind workflow (safe to re-run)
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS offer_token_hash  BYTEA UNIQUE;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS offer_expires_at  TIMESTAMPTZ;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS sent_at           TIMESTAMPTZ;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS bind_requested_at TIMESTAMPTZ;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS bind_request      JSONB;        -- confirmation, signer, device
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS fee_paid_at       TIMESTAMPTZ;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS fee_method        TEXT;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS fee_reference     TEXT;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS premium_billing   TEXT;         -- agency / direct
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS bound_at          TIMESTAMPTZ;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS policy_number     TEXT;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS bind_raw          JSONB;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS bind_error        TEXT;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS limits_text       TEXT;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS effective_date    DATE;

-- Upgrading an older database: widen the status list
-- ALTER TABLE submissions DROP CONSTRAINT submissions_status_check;
-- ALTER TABLE submissions ADD CONSTRAINT submissions_status_check CHECK (status IN
--   ('pending_review','approved','rejected','sent_to_market','quoted','bind_requested','bound'));

-- Uploaded underwriter quotes (safe to re-run)
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS details          JSONB;      -- reviewed client-facing figures
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS total_due        NUMERIC(12,2);
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS source_pdf_enc   BYTEA;      -- the wholesaler's PDF, encrypted
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS source_filename  TEXT;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS share_original   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS reviewed_at      TIMESTAMPTZ;

-- Bound policies and the certificates issued on them
CREATE TABLE IF NOT EXISTS policies (
    id                BIGSERIAL PRIMARY KEY,
    submission_id     BIGINT      NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    quote_id          BIGINT      REFERENCES carrier_quotes(id),
    carrier           TEXT        NOT NULL,
    naic              TEXT,
    policy_number     TEXT,
    effective_date    DATE,
    expiration_date   DATE,
    policy_form       TEXT        NOT NULL DEFAULT 'Occurrence',
    limits            JSONB       NOT NULL DEFAULT '{}',
    blanket_ai        BOOLEAN     NOT NULL DEFAULT FALSE,   -- policy has a blanket additional-insured endorsement
    blanket_pnc       BOOLEAN     NOT NULL DEFAULT FALSE,   -- primary & non-contributory
    blanket_wos       BOOLEAN     NOT NULL DEFAULT FALSE,   -- waiver of subrogation
    ops_description   TEXT,
    insured_name      TEXT        NOT NULL,
    insured_address   TEXT        NOT NULL,
    ready             BOOLEAN     NOT NULL DEFAULT FALSE,   -- you confirmed the details; certificates can be issued
    portal_token_hash BYTEA       UNIQUE,
    status            TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled','expired')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS certificates (
    id              BIGSERIAL PRIMARY KEY,
    policy_id       BIGINT      NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    holder_name     TEXT        NOT NULL,
    holder_address  TEXT        NOT NULL,
    holder_email    CITEXT,
    project         TEXT,
    want_ai         BOOLEAN     NOT NULL DEFAULT FALSE,
    want_pnc        BOOLEAN     NOT NULL DEFAULT FALSE,
    want_wos        BOOLEAN     NOT NULL DEFAULT FALSE,
    status          TEXT        NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','pending_approval','declined')),
    pdf_enc         BYTEA,
    number          TEXT,
    ip_hash         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    issued_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS certificates_policy_idx ON certificates(policy_id, created_at DESC);

-- Contractor accounts: passwordless (emailed 6-digit code). One row per signed-in device.
CREATE TABLE IF NOT EXISTS client_sessions (
    id            BIGSERIAL PRIMARY KEY,
    token_hash    BYTEA       NOT NULL UNIQUE,
    email         CITEXT      NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    ip_hash       TEXT,
    user_agent    TEXT
);
CREATE INDEX IF NOT EXISTS client_sessions_email_idx ON client_sessions(email);
CREATE INDEX IF NOT EXISTS submissions_email_idx ON submissions(contact_email);
-- Encrypted copies of the client's own links, so the signed-in dashboard can open them
ALTER TABLE carrier_quotes ADD COLUMN IF NOT EXISTS offer_token_enc BYTEA;
ALTER TABLE policies       ADD COLUMN IF NOT EXISTS portal_token_enc BYTEA;

-- Short business profiles and instant quotes (safe to re-run)
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS kind        TEXT  NOT NULL DEFAULT 'full';   -- 'profile' or 'full'
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS match       JSONB;                           -- which carriers could quote it, and why not
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS profile_id  BIGINT REFERENCES submissions(id); -- full application that continues a profile
ALTER TABLE submissions DROP CONSTRAINT IF EXISTS submissions_status_check;
ALTER TABLE submissions ADD CONSTRAINT submissions_status_check CHECK (status IN
  ('pending_review','approved','rejected','sent_to_market','quoted','bind_requested','bound',
   'quoting','needs_application','superseded'));

-- Switches the broker flips in the admin (e.g. live instant quotes on/off). Safe to re-run.
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT        PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_by  INT REFERENCES admins(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
