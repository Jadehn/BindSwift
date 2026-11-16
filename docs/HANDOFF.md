# Bindswift — handoff for the next AI agent

Written 2026-11-15 by Claude for Jaden Howard. Read all of this before touching code, the
browser, or First Connect.

## 1. Who and what

- **Jaden Howard**: owner of **Howard Analytics LLC**, a licensed NY property & casualty broker
  (NY License #1931665). Also licensed in NJ, NC, DE and MD. Junior at LIU.
- **Bindswift**: his venture, a web app that gets small NYC businesses General Liability
  quotes fast and lets them send certificates of insurance 24/7. He's a finalist in the
  **LIU Innovation & Venture Challenge, pitching Friday Nov 20, 2026, 1:30–4:30 PM**.
- **Business model**: the standard carrier commission, plus a flat **$50 Bindswift service fee**
  charged right before a policy is bound. It requires a signed fee agreement (NY Insurance Law
  §2119, kept 3 years). No fee if nothing is bound.
- **Strategy**: lead with the classes insurers quote and bind instantly (main-street
  businesses), not contractors. NY contractor GL is hard to place: Coterie, Hiscox (artisan),
  MGT and AmTrustONE exclude NY contractors, and NEXT's contractor guide flags NY work.
  Contractors go to THREE, biBerk, or E&S (Pathpoint, Blitz) through the broker.

## 2. Where the code is

- Jaden's PC: `C:\Users\jaden\Documents\New folder\Python\Stock Machine\BindSwift`
- GitHub: `https://github.com/Jadehn/BindSwift` (private). `main` is current as of commit
  `5b94039` "Lockout fix, Windows start script, README password wording". Jaden pushes
  himself; an agent working in the folder can't push.
- Files use **Windows CRLF line endings** in the repo. Keep them CRLF when you edit.

### Stack
- `index.html`: the whole customer site in one file: home page, instant-quote check, "Who we
  insure" appetite page, 10-step quote form, email-code login, dashboard, demo mode. Opened
  without the backend, it runs as a demo with John Doe sample data.
- `backend/`: Python **FastAPI** + **PostgreSQL** (psycopg 3, raw SQL). Main files:
  - `app/main.py`: routes, security headers, `/api/submissions`
  - `app/models.py`: answer validation (GLAnswers)
  - `app/acord_map.py`: answers → ACORD 125/126 fields
  - `app/pdfs.py`: fills the ACORDs, signatures, fee agreement, quote summary
  - `app/admin.py`: admin panel (password + TOTP 2FA)
  - `app/client.py`: customer accounts and the dashboard API
  - `app/offers.py`: the customer's quote and bind page `/q/<token>`
  - `app/cert_portal.py` and `app/certs.py`: instant certificates `/c/<token>`
  - `app/quote_extract.py`: reads wholesaler quote PDFs (rules, with optional AI)
  - `app/verify.py`: email codes and NY Department of State business lookup
  - `app/integrations/coterie.py` and `next_ins.py`: carrier eligibility and Coterie's API
  - `schema.sql`: tables
  - `roles.sql`: the limited `bindswift_app` database user
  - `README.md`: full documentation
- `api/index.py` + `vercel.json`: Vercel deploy entry point. Not deployed yet; plan is
  Vercel Pro + Neon Postgres.

### Running it on Jaden's PC (Windows, Command Prompt)
```
cd "C:\Users\jaden\Documents\New folder\Python\Stock Machine\BindSwift\backend"
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```
Or double-click `backend\start.bat`. Site: http://localhost:8000. Admin: http://localhost:8000/admin.

- Always call `.venv\Scripts\python.exe`. Plain `python` picks up an old MSYS2 Python 3.10
  on his PATH.
- The venv is Python 3.13.
- PostgreSQL 18 runs as a Windows service. Command Prompt needs
  `set PATH=%PATH%;C:\Program Files\PostgreSQL\18\bin` before using `psql`.
- The database `bindswift` exists with the schema. The app connects as `bindswift_app`
  (limited rights); the superuser is `postgres`. Passwords are in Jaden's password manager.
  **Never ask him to paste them into chat.**
- `.env` lives in `backend\.env` (gitignored). Its values: `ENV=development`,
  `ALLOWED_HOSTS=localhost,127.0.0.1`, `REQUIRE_EMAIL_VERIFICATION`, `DATA_KEY` (Fernet
  encryption key for FEINs, PDFs and TOTP secrets: **never change it once real data
  exists**), `IP_HASH_SALT`, and SMTP via **Resend**:
  - `SMTP_HOST=smtp.resend.com`, `SMTP_USER=resend`, `SMTP_PASSWORD=<Resend API key>`
  - `MAIL_FROM=Bindswift <verification@bindswift.com>`, `BROKER_EMAIL=broker@bindswift.com`
  - bindswift.com is verified in Resend (DNS on Namecheap). Google Workspace runs the
    @bindswift.com inboxes.
  - Never print or commit `.env`.
- The admin account is `jaden@howardanalytics.net`. If a TOTP code is ever rejected, the PC
  clock was off before; sync Windows time. Create or reset the admin with
  `set ADMIN_PASSWORD=...` and then `.venv\Scripts\python.exe -m scripts.create_admin <email>`.
  There's no minimum password length; Jaden chose that.
- Login, email codes and the dashboard all work locally (verified 11/15).

## 3. What's built

- **Quote form:** builds the ACORD 125/126 (flattened and fillable copies), the answers PDF
  and the signed fee agreement. Everything is encrypted in Postgres and the broker is emailed.
- **Business check:** live NY Department of State lookup (data.ny.gov dataset `n9v6-gdp6`).
  NJ, NC, DE and MD are a manual check with the official search link and the entity ID. **Never
  invent a lookup result**; the demo once did and it was removed.
- **Admin panel:**
  - Review and approve submissions; Coterie sandbox quote.
  - Manual quote entry, or upload a wholesaler quote PDF (XPT, Morstan, Hull). The figures are
    extracted and must be reviewed.
  - Send the quote link, record the $50 fee, mark bound.
  - Confirm policy details; certificates then turn on.
- **Customer:** passwordless email-code login, then a dashboard with quotes (view & bind),
  policies (send a certificate), applications and documents.
- **Instant certificates:** Bindswift's own layout, not the ACORD 25 (that needs an ACORD
  license). AI, P&NC and WOS only issue instantly if the policy has the blanket endorsement;
  otherwise the request goes to the broker for approval.
- **Security:** argon2 passwords, TOTP with replay protection, CSRF, `__Host-` cookies,
  CSP, a lockout (fixed so attempts made while locked don't extend it), an append-only audit
  log and encrypted documents. Full list in `backend/README.md`.

## 4. Rules you must follow

1. **First Connect Product Access Agreement** (signed 08/17/2025):
   - §2(e): don't use First Connect's or any carrier's name or logo publicly without written
     consent. The public site uses generic labels ("Online admitted insurers"). The admin
     panel may show carrier names.
   - §4(e): don't collect premium for First Connect markets except through the carrier's
     own website. Premium is paid on the carrier's site; only the $50 fee goes to Jaden.
   - §4(c): broker fees must follow the law and each market's rules.
   - §2(e): no binding authority on First Connect's behalf. Binding is always a human action
     on the carrier's platform.
   - §5(c): Jaden's password is only for him and his employees. **Never store his First
     Connect or carrier passwords** in code, files or chat.
   - §6(d): fraud or misrepresentation is grounds for termination.
   - Jaden says First Connect approved this work. Keep their approval in writing.
2. **Only submit truthful quotes.** Quote Howard Analytics LLC only as what it really is.
   For other classes, go through each carrier's form up to the final quote button without
   submitting. **Never bind anything.**
3. **No real client data** in the repo, screenshots or published pages. Use the fictional
   John Doe / Doe Home Services Inc. sample data. Peter's Handyman and other real clients
   must never appear.
4. Never print secrets: `.env`, API keys, passwords, session tokens, cookies.
5. **Captured traffic (HAR files)** contains Jaden's session. Keep it outside the repo
   (e.g. `Documents\FC-captures`) and redact tokens before analyzing.
6. Jaden prefers direct answers without hedging.

## 5. Work in progress: the first appetite tier

Goal: the quote form must fully support the nine "Quoted in minutes" classes:
**hair salons, barbershops, nail/lash/braiding, consultants, marketing agencies,
bookkeepers & tax preparers, doulas, wellness coaches, retail stores.**

**Backend: done, untested, NOT applied.** It's in `docs/wip-main-street-backend.patch` (apply
with `git apply`; the repo is CRLF, so check line endings). It adds:
- New trade IDs: `salon, barber, beauty, consultant, marketing, bookkeeping, doula, wellness,
  retail`, each with a `group`: `beauty`, `office`, `wellness`, `retail` (contractors are
  `trade`).
- NAICS codes only where exact (812112, 812111, 812113, 541611, 541613, 541219). GL class
  codes are left blank for the broker to assign per carrier. **Don't invent ISO class codes.**
- New answer fields:
  - `stations`, `services[]` (cut, color, nails, lash, braid, wax)
  - `boothRental`, `licensed`, `clientVisits`, `wantEO`
  - `workPlaces[]` (homes, hospital, studio, online), `certified`
  - `ownBrand`, `sellOnline`, and the existing `sqft`
- Contractor-only fields (`height`, `areas`, subcontractors, `install`, `safety`) are
  optional, and required only for contractor trades.
- ACORD 125 "Nature of business" ticks Service / Office / Retail / Contractor by group. Class
  details go into the description. Installation percentage is only for contractors.
- Coterie eligibility: main-street classes are in appetite; "Coterie excludes contractor
  classes in NY" is flagged for contractors. NEXT: the NY contractor flag is skipped for
  main-street classes.

**Front end: TODO** (`index.html`):
- Add the nine classes to `TRADES` with a `group`. Use demo-only rates for the fictional demo
  carriers.
- In the "Your work" step, show contractor questions (`offsitePct`, `height`, `areas`) only
  for trades. Add group questions:
  - **Beauty:** stations; services (checks); "Do independent stylists rent chairs or booths?";
    "Is everyone licensed by NY State?"
  - **Office:** "Do clients visit your office?"; "Also want professional liability (E&O)?"
  - **Wellness:** where you work (checks); certified?; E&O?
  - **Retail:** square footage; sell online?; "Do you import or sell under your own label?"
- Hide the subcontractor fields and the contractor items in `opsChecks` (keep otherbiz,
  parent, violations, drones), plus `install` and `osha`, for main-street classes.
- **Mirror every change in the JS `buildAcord()`** so the ACORD preview matches the Python
  output exactly; there's a parity test.
- Test with Playwright on a 390px mobile viewport. Check dark mode stays readable
  (`color-scheme: light` is set).

**Then, also requested by Jaden:**
- **More info at sign-up:** the "Create account" form asks for business name, business type
  (one of the classes), mobile phone and ZIP. Save them to a new `client_profiles` table
  (email primary key), prefill the quote form, and have the dashboard greet by name.
  Currently it shows the email prefix, e.g. "Hi jaden3702".
- **Admin "Data" page:** tabs for Accounts, Applications, Quotes, Policies and Certificates.
  Add search, a status filter, counts and CSV export. Read-only, FEIN masked. Add a nav link
  in `templates/base.html`.
- **Explain to Jaden how data is saved:** Postgres tables linked by ID; sensitive fields
  Fernet-encrypted with `DATA_KEY`; passwords as argon2 hashes; append-only `audit_log`.
- After schema changes, Jaden runs as postgres:
  `psql -U postgres -h 127.0.0.1 -d bindswift -f schema.sql`, then `-f roles.sql`. Add
  GRANTs for any new table to `roles.sql`.

## 6. First Connect / Hiscox capture: where we stopped

Goal: run real or sample quotes in First Connect for the instant-bind classes, watch the
web/API requests, and then reproduce them in code (Bindswift backend → carrier).

**Found so far:**
- Agent portal: `https://portal.firstconnectinsurance.com/dashboard`. Jaden is already signed in
  inside the Claude desktop built-in browser. Each carrier is a tile on the dashboard; clicking
  it opens a side panel with **Go to carrier** and **Quote via rater**.
- First Connect's private API (backend-for-frontend):
  `https://portal-bff.firstconnectinsurance.com/api/v1/...`. Endpoints seen:
  - `producer/me`, `producer/settings`, `producer-flags`
  - `quote-connector/states/lobs/available-carriers`, `quote-connector/credentials/WC`
  - `producer/dashboard-carriers/<producerId>`, `carrier/carrier/<carrierId>` (Hiscox = **357**)
  - `navigation/counters`, `recent-wins`, `carrier/trending`, `carrier-incentives/active`
  - Also `https://api.firstconnectinsurance.com/api/v2/...`
- Requests carry an auth token added by the app. A plain `fetch` from the console with only
  cookies fails, so the token lives in app memory or storage. **Don't extract or store it.**
- Hiscox through First Connect: access granted. **14% new / 12% renewal commission.** GL, BOP,
  PL and cyber available in NY, with instant bind up to $5M revenue.
- "Go to carrier" sends you to Hiscox's partner portal:
  **https://www.hiscox.com/partner-agent/firstconnectinsurance**, then **Sign in**. That's
  Hiscox's own site and API, so Hiscox's terms apply to anything automated there.

**Tooling notes:**
- The built-in browser's network reader didn't show cross-origin XHR/fetch calls, only static
  files. Two ways to see API traffic:
  1. `performance.getEntriesByType('resource')` filtered to `fetch`/`xmlhttprequest` lists the
     endpoint URLs (no bodies).
  2. Inject a recorder that wraps `window.fetch` and `XMLHttpRequest` and stores
     method/url/body/status/response in `window.__cap`, **redacting any header matching
     auth|token|cookie**. It must be re-injected after every page load or navigation.
- Or Jaden captures a HAR in Chrome DevTools (Network → Preserve log → Fetch/XHR → Save all
  as HAR) and the agent reads it with tokens redacted.
- Each new site needs Jaden's approval in the browser pane. He declined access to
  hiscox.com at 4:29 PM because of a cooldown. **Ask before opening Hiscox again.**

**Next steps for the capture:**
1. With Jaden's go-ahead, open the Hiscox partner page above. Jaden signs in himself.
2. Start a **General Liability** quote for **Howard Analytics LLC**, truthfully:
   - NY DOS ID 7571650, Domestic LLC, first filed 03/28/2025, Nassau County.
   - Address: 1191 Slabey Avenue, Valley Stream, NY 11580.
   - Class: consulting / bookkeeping / professional services (whichever Hiscox class actually
     describes it).
   - **Ask Jaden** for revenue, employees and any other answer you don't know. Never guess.
3. Record every quote API call: endpoint, method, request JSON field names, response fields,
   and how auth and session work. Save the findings (redacted) to `docs/hiscox-api-notes.md`.
4. Stop at the quote. **Do not bind.**
5. Repeat for **Coterie** and **ERGO NEXT** tiles (both in the first tier). Coterie also has a
   documented public API (`docs.coterieinsurance.com`, v1.6) already wired in
   `integrations/coterie.py`. Confirm whether quotes through Coterie's own API count under the
   First Connect appointment.
6. Design `backend/app/integrations/hiscox.py` (and others) from the notes. Keep a human
   approval step before any bind.

## Update 2026-11-15 (evening): live instant quotes

- **Hiscox automation works end to end** (`backend/app/integrations/hiscox.py`). Live test: Howard Housing Solutions, Inc (Accounting) quoted $1,301/yr, ref S112.767.614. Sign-in starts at the partner page's `login_url_plain` (GatewayExternalLinkServlet); the quote never binds, stops on unanswered questions, recognizes declines, and clicks Home at the end to release the quote lock. Offline check: `scripts/hiscox_replay_check.py`; live check: `scripts/hiscox_test_quote.py`.
- **Short business profile → instant quote** (live mode). `appetite.py` is the single list of classes and Hiscox codes (sent to the page via `/api/config`). `POST /api/profile` saves a submission with `kind='profile'` and a `match`; `POST /api/profile/{ref}/instant-quote` runs Hiscox (one at a time, `pg_advisory_xact_lock`), saves the quote with a `/q/` bind link already sent. No match or a decline → status `needs_application`; the client continues to the full ACORD app (prefilled, `profileRef`), which marks the profile `superseded`.
- **Admin switch** "Live instant quotes" (table `app_settings`, key `instant_quotes`), off by default.
- **Free eligibility check** `POST /api/eligibility` (`eligibility.py`): Claude (`ELIGIBILITY_MODEL`, forced tool call) maps a description to a class; `appetite.match` decides. Keyword fallback without `ANTHROPIC_API_KEY`.
- **Main-street classes** in the full application: the WIP patch (`docs/wip-main-street-backend.patch`) is now applied; `index.html` TRADES/STEPS/buildAcord mirror it (parity checked for 5 classes).
- **Demo vs live**: `SITE_MODE=demo` or `?demo=1` shows the demo on a live backend.
- **Howard Analytics site** served at `/howard-analytics/` from `public/howard-analytics/` (static on Vercel, FastAPI route locally, its own CSP).
- After pulling: run `schema.sql` and `roles.sql` again (new columns, statuses, `app_settings`).
