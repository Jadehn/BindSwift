# Bindswift backend

Python (FastAPI) + PostgreSQL. It serves the Bindswift site and handles the rest:

1. A contractor submits the GL intake.
2. The server re-validates every answer and rebuilds the ACORD 125 and 126 field values itself (it never trusts the browser's copy). It fills both PDFs, builds an answer-summary PDF, and stores everything encrypted.
3. You get an email with the three PDFs attached (FEIN masked) and a link to review.
4. You sign in to the admin portal (password plus authenticator code) and approve, reject, or move the submission along: approved, then sent to market, then bound.
5. Approved submissions can be exported as JSON for First Connect and your XPT rater.

```
backend/
  app/
    main.py        public site + POST /api/submissions + security headers
    admin.py       admin portal routes
    security.py    argon2 passwords, TOTP 2FA, sessions, CSRF, lockout, encryption
    models.py      strict validation of every intake answer
    acord_map.py   answers -> ACORD 125/126 field names (all 134 verified against your PDFs)
    pdfs.py        fills the ACORDs, builds the summary PDF
    mailer.py      SMTP email to you
    templates/     admin pages (server-rendered, no JavaScript)
  forms/           ACORD_125.pdf, ACORD_126.pdf (your blank forms)
  schema.sql       tables
  roles.sql        least-privilege database permissions for production
  queries.sql      11 SQL practice queries on your own data
  scripts/create_admin.py
```

## Run it locally

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Postgres: install it (brew install postgresql@16 / Postgres.app on Mac), then:
createdb bindswift
psql bindswift -f schema.sql

cp .env.example .env      # fill it in; for local use set ENV=development
python -m scripts.create_admin you@howardanalytics.net   # scan the QR with your authenticator app
uvicorn app.main:app --reload
```

Open http://localhost:8000 for the contractor site. The admin portal is at http://localhost:8000/admin (or whatever you set `ADMIN_PATH` to).

For email while testing, a Gmail or Google Workspace **app password** works (`smtp.gmail.com`, port 587). For production, use a transactional provider (Postmark, Resend, Amazon SES) on your own domain, with SPF/DKIM set so the emails don't land in spam.

## Deploying on Vercel

The repo is ready for Vercel: `vercel.json` sends every request to `api/index.py` (the FastAPI app), and only the empty `public/` folder is served as static files, so your backend source is never downloadable.

1. **Use a Pro plan.** Vercel's Hobby plan is for personal, non-commercial use only.
2. **Database:** use managed Postgres with TLS (Neon or Supabase). Use the *pooled* connection string and add `?sslmode=require`. The app refuses to start in production without TLS.
3. **Environment variables:** add everything from `.env.example` in Vercel → Settings → Environment Variables. Mark the secrets as Sensitive, and scope the real database and keys to **Production only**, so preview deployments (public URLs for every branch) can't touch real data.
4. Set `TRUST_PROXY=vercel` (the app also detects Vercel automatically) and `ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com`.
5. Turn on **Deployment Protection** for previews, and add a Firewall rate-limit rule for `/api/submissions` and your admin login path.
6. Run `schema.sql` and `roles.sql` against the database once, and run `python -m scripts.create_admin` locally pointed at the production `DATABASE_URL` and `DATA_KEY`.

What the code handles for serverless:
- **Real visitor IPs** come from `x-vercel-forwarded-for`, which Vercel sets itself. Without that, every visitor would look like the same proxy IP, and one attacker could lock you out of the admin login or trip the submission limit for everyone.
- **Email is sent before the response** on Vercel, because a serverless function can be frozen right after it responds and background work would be lost.
- **Connection pool** holds no idle connections and works with pooled Postgres.

## Deploying elsewhere

Any host with HTTPS and managed Postgres works: Render, Railway, or Fly.io. Set the `.env` values as the host's environment variables (never commit `.env`). Start command:

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
```

Only use `--forwarded-allow-ips='*'` when the platform's proxy is the only way in, which is true on Render, Railway and Fly. Otherwise put the proxy's IP there.

## What's in place for security

| Area | What it does |
|---|---|
| Admin passwords | argon2id hashes, 14-character minimum. Wrong emails take the same time as wrong passwords, so valid emails can't be guessed. |
| Two-factor | TOTP authenticator code required on every login. Each code works only once (replay blocked). The secret is encrypted in the database. |
| Brute force | 5 failed logins per email (10 per IP) in 15 minutes locks login, even for the right password. Every attempt is logged. |
| Sessions | Random 256-bit token in an `HttpOnly`, `Secure`, `SameSite=Strict`, `__Host-` cookie. Only a SHA-256 hash is stored server-side. 30-minute idle timeout, 8-hour max, one session per admin, server-side logout. |
| CSRF | Per-session token on every admin form, plus a separate token on the login form. |
| Admin exposure | Configurable secret path, optional IP allowlist (returns 404 to everyone else), `noindex`, no public API docs. |
| Sensitive data | FEIN encrypted (Fernet/AES) in its own column and removed from the JSON copies. Generated PDFs are encrypted at rest. Raw IPs are never stored, only keyed hashes. Emailed copies mask the FEIN. |
| Input | Every field is type-checked, length-limited and limited to allowed values. Body size is capped. Only JSON is accepted. Hidden honeypot field for bots. 5 submissions per IP per hour. |
| SQL injection | Every query is parameterized; no string-built SQL. |
| Browser headers | Strict CSP: the public page only runs its own script (hash-pinned, no inline injection) and self-hosted pdf.js; admin pages allow no scripts at all. HSTS, `X-Frame-Options: DENY`, `nosniff`, `no-store` on admin and API responses. |
| Contractor's device | The FEIN is never saved in the browser, and the saved draft is wiped after submitting. |
| E-signature evidence | Each signature records the signer, the timestamp, the device and a keyed hash of the IP. |
| Audit trail | Logins, views, downloads, exports and status changes are logged with who and when. `roles.sql` makes the log append-only for the app's database user. |

Before real contractor data goes in:

- **NY DFS cybersecurity rule (23 NYCRR 500).** As a licensed NY producer you're a covered entity. Small agencies often qualify for a limited exemption under 500.19, but you still need a risk assessment, a written policy, and to file your annual compliance or exemption notice. Read the current rule or talk to DFS before launch.
- Turn on 2FA for the email account that receives submissions. Those emails still carry business details.
- Keep `DATA_KEY` somewhere safe outside the server, such as a password manager. If you lose it, the encrypted FEINs and PDFs can't be recovered.
- Turn on your host's automated Postgres backups.
- Verify the ISO class codes in `acord_map.py` against each carrier's class guide.

## Stopping spam and checking the business

**Email verification (on by default).** A contractor must confirm their email with a 6-digit code before they can submit. Codes are stored only as hashes, expire in 10 minutes, allow 5 tries, can't be reused, and are rate-limited per email and per IP. The submission must carry a signed token for the same email. Throwaway-inbox domains (Mailinator and similar) are refused.

**Optional bot check.** Set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET` (free Cloudflare Turnstile) to add an invisible "are you human" check before a code is sent.

Already in place: a hidden honeypot field, 5 submissions per IP per hour, a 64 KB size cap, and your manual approval of every submission before anything goes to a carrier.

**Business verification (New York).** The first step searches New York Department of State records (data.ny.gov, the free "Active Corporations" dataset). The contractor picks their business, which fills the legal name exactly as registered. If the record shows a different type (say they chose LLC but it's a corporation), it's flagged or corrected. The server re-checks at submission, and the admin page shows **verified / name variation / type mismatch / not found**. Notes:
- **S corporations** appear as business corporations in state records; S status is a federal tax election, so ask for the IRS acceptance letter (CP261).
- **Sole proprietors and DBAs** aren't in the state entity database. Contractors can say "my business isn't listed," and you verify by hand.
- The admin page links to the NJ, NC, DE and MD business searches for out-of-state entities. Those states don't offer a free API for this, so checks there are manual or need a paid data provider.

## $50 service fee

Bindswift charges a flat $50 broker service fee right before a policy is bound. New York Insurance Law §2119 requires a written memorandum signed by the insured that states the amount, kept for at least 3 years. The contractor signs that agreement on the sign step, and the backend stores a signed PDF (`fee_agreement`) with the submission. The fee itself isn't collected by this code yet; collect it at binding through your payment processor.

## Quote → bind → fee workflow

1. **Get a quote.** On an approved submission, click **Get Coterie quote**, or add a quote you got in a carrier portal such as NEXT.
2. **Send it.** **Send quote to contractor** emails them a private link (`/q/<token>`, valid 14 days, only a hash stored). The page shows the premium, the limits, and the **+$50 Bindswift fee charged once right before binding**.
3. **They press Bind.** They confirm nothing changed and type their name. You get an email, and the submission moves to *bind requested*. Nothing is charged on that page.
4. **Collect the $50.** Reach out and collect it however you like (card, Zelle, ACH, check), then click **Record $50 fee** with the method and receipt number. Binding is blocked until this is recorded.
5. **Bind.**
   - **Coterie through the API:** **Bind with Coterie (agency bill)** calls Coterie's bind endpoint with your secret key and no card token. That's their Agency Bill option: Coterie bills your agency, and you collect the premium from the contractor. Confirm the bind path in Coterie's sandbox first (`COTERIE_BIND_PATH`).
   - **Direct bill, or any carrier portal:** bind there, then click **Mark bound** with the policy number.
6. The contractor gets a "you're covered" email, and every step is in the audit log.

If you collect premiums yourself (agency bill), New York treats that money as held in trust for the insurer. Keep it separate from your operating money and remit on time.

## Carriers

**Coterie (API).** `app/integrations/coterie.py` calls Coterie's commercial API v1.6 (`POST /quotes`, header `Authorization: token <publishable key>`). On an approved submission, the admin page shows a **Get Coterie quote** button. It sends the answers in Coterie's documented field names, records the premium, and links to the application in Coterie's dashboard so you can finish binding there. To connect:
1. Ask your Coterie rep for API access. You'll get sandbox keys, your `agencyExternalId`, and the **AKHash** class codes for the trades you write.
2. Fill in the `COTERIE_*` variables. Start with `COTERIE_ENV=sandbox`, then switch to `live`.
3. Binding is deliberately not automated yet, since it commits real coverage. The secret key is only for a bind call you add later.

**NEXT (portal).** NEXT has no public API; programmatic quoting is limited to its NEXT Connect partner program. The admin page pre-checks each submission against NEXT's published contractor rules and gives you an answer sheet to key into NEXT's agent portal. NEXT's contractor guide lists *work performed in New York* as ineligible, so check your portal's appetite checker for each class before quoting NY contractors there.

Every quote attempt is saved in `carrier_quotes` and the audit log.

## First Connect and XPT

`GET {ADMIN_PATH}/submissions/{id}/export.json` returns the answers plus the full ACORD 125/126 field map (with FEIN) for approved submissions only. That's the hand-off point. Once you have API access or an import spec from First Connect or your XPT rater, write a small client module (for example `app/integrations/first_connect.py`) that maps this payload to their fields. Call it from the review route when you click "Mark sent to market", and store their quote or reference number on the submission. Keep their API keys in environment variables, like everything else.

## Why Python and not n8n

The core of Bindswift is filling PDFs, validating applications, encrypting data and running a secure login. That's real application code, and you already write Python. n8n is good glue for non-sensitive automations (a Slack ping when a submission comes in, renewal reminders, pulling new permits from NYC Open Data). If you add it later, have n8n call this API rather than putting contractor data or credentials inside n8n.

## Underwriter quotes (XPT, Morstan, any wholesaler)

1. Submission page → **Upload the underwriter's quote PDF**. The built-in reader pulls carrier, admitted status,
   A.M. Best, policy period, limits, premium lines, fees, taxes, total, billing and prior-to-bind items, and checks
   that premium + fees + taxes = total. Set `ANTHROPIC_API_KEY` to use the AI reader instead for unfamiliar layouts.
2. **Review** every figure (required before sending). Your commission and the wholesaler never reach the client.
   Tick "Let the client download the insurer's original PDF" only if you're fine with them seeing what's on it.
3. **Send**. The client's link shows the full breakdown, a Bindswift summary PDF and (if allowed) the original.
   The link expires at the insurer's quote expiration date or OFFER_DAYS, whichever is first.

## Instant certificates of insurance

Neither Coterie nor NEXT has a public certificate API (Coterie agents download COIs from the Coterie Dashboard;
NEXT's Live Certificate lives in NEXT's own portal). So Bindswift issues certificates itself, as the producing
agency, for every carrier, including XPT/Morstan placements.

1. Marking a quote bound creates a **policy** record. Open it from the submission page.
2. Check carrier, NAIC, policy number, dates and limits against the dec page. Tick the blanket endorsements the
   policy actually has (additional insured, primary & non-contributory, waiver of subrogation). Tick "Details
   match" and save: the contractor gets a private link, `/c/<token>`.
3. The contractor issues certificates 24/7: holder name and address, optional email (we send it to them),
   project. Anything the policy covers issues instantly as a PDF. A request for an endorsement the policy doesn't
   have waits for you; you get it endorsed by the carrier, then press "Issue it".
4. Guardrails: certificates stop when the policy is cancelled or expired; 25 per day per policy; every certificate
   is numbered, stored encrypted and audit-logged.

The certificate is a Bindswift layout, not the ACORD 25 form itself. ACORD forms require an ACORD license
(agencies usually get one through ACORD membership or their agency software). Once you have one, drop the
ACORD 25 template in `forms/` and fill it in `certs.certificate_pdf` the same way the 125/126 are filled.
Before going live, also confirm with each carrier that you're authorized to issue certificates on their policies.

## Logging in to the admin panel

1. Create your admin once (and again any time you lose your phone, which resets the password and 2FA):
   ```bash
   python -m scripts.create_admin you@howardanalytics.net
   ```
   Pick a password of 14+ characters, then scan the QR code it prints with Google Authenticator, 1Password,
   Authy or Microsoft Authenticator. The key is shown once.
2. Go to `https://yourdomain.com` + `ADMIN_PATH` (default `/admin`; set it to something unguessable in production,
   like `/ops-7k2m9`). Enter email, password and the 6-digit code from the app.
3. Five wrong tries locks that email and IP for 15 minutes. Sessions end after 30 idle minutes or 8 hours.
4. For production, run the script on your laptop with `DATABASE_URL` and `DATA_KEY` set to the production values.
   Optionally set `ADMIN_IP_ALLOWLIST` to your home/office IP so the admin page doesn't exist for anyone else.

## Contractor accounts and dashboard

- **Login is passwordless.** The contractor enters their email and gets a 6-digit code (10-minute expiry, 5 tries,
  rate-limited, disposable emails blocked). The session is an HttpOnly cookie for 14 days; only a hash is stored.
- **What they see is matched by verified email:** applications and status, quotes (with a link to view and bind),
  policies (with a "Send a certificate" button once you've confirmed the policy), and documents: their answers,
  ACORD 125/126, fee agreement, quote summaries and certificates. Editable ACORD copies stay admin-only.
- A signed-in contractor's email counts as verified, so they don't get a second code when they start a quote.
- Tested: no session → 401, another account can't see or download anything, cross-site login and logout are
  blocked, logout kills the session.

## Business registration (NY, NJ, NC, DE, MD)

- **NY** is searched live (NY Department of State open data). The contractor picks their record and sees a preview:
  legal name, DOS ID, entity type, first filing date, county, and the state it was formed in.
- **NJ, NC, DE, MD** have no free public API, so the form opens that state's official search, takes the entity ID
  (NJ entity ID, NC SOSID, DE file number, MD Department ID), and previews which fields that record shows. The
  admin page marks these "manual check" with a link to the right state search. The backend also looks for a NY
  foreign registration, since an out-of-state company working in NY should be on file with New York too.
