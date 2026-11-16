# Bindswift — Agent Handoff (as of 2026-11-15, evening ET)

You are picking up work on **Bindswift**, a general liability (GL) insurance and certificate-of-insurance (COI) platform for New York / NYC small and mid-size businesses. It's built by Jaden Howard (Howard Analytics, licensed NY P&C broker). **Pitch deadline: LIU Innovation & Venture Challenge, Nov 20, 2026.**

Read this whole file before touching anything. The rules in section 1 still apply.

---

## 1. Hard rules (do not break)

- **Never print, log, copy or commit `backend/.env`.** It holds Hiscox/First Connect credentials, DB URLs and API keys. Credentials live **only** in `.env`.
- **No personal info** of Jaden's on public pages or in the ACORD preview (no license number, home address or phone on the site).
- **No real client data** in tests, samples or screenshots. Use the sample "Howard Housing Solutions, Inc" profile or invented businesses.
- **HAR captures stay out of git** (`*.har` and `backend/fc captures/` are git-ignored). Redact tokens and passwords; blur passwords in screenshots.
- **Only truthful quotes. Never bind automatically.** The Hiscox driver refuses buttons labelled bind / pay / purchase.
- **First Connect agreement §2(e):** carrier names stay generic on public marketing pages. Naming the insurer is allowed only on the client's own quote.
- **Don't drive the user's browser** unless he asks. Give him DevTools steps instead.
- **Commit or push to git only when the user asks.** Nothing from this session has been committed yet.

---

## 2. Where things live

| What | Path |
|---|---|
| Repo (Windows desktop) | `C:\Users\jaden\Documents\New folder\Python\Stock Machine\BindSwift` |
| Planned OneDrive copy (for the laptop) | `C:\Users\jaden\Desktop\Howard Analytics - Howard Indemnity Group\BindSwift` (access granted; **copy not done yet**, see §8) |
| Howard Analytics site source | `C:\Users\jaden\Documents\New folder\Python\Stock Machine\howardanalyticssite` |
| Existing handoff notes | `docs/HANDOFF.md` (has an "Update 2026-11-15 (evening)" section), `docs/hiscox-api-notes.md` |

Repo files use **CRLF** line endings, so keep them.

### Stack
- **Backend:** FastAPI + psycopg3 (raw SQL) + PostgreSQL. Admin uses Jinja2 templates, with Fernet encryption, argon2 + TOTP, CSRF and a strict CSP.
- **Frontend:** a single-file SPA `index.html` with a hash router. It has demo vs live modes: `?demo=1` / `?demo=0`, and a `body.live` class.
- **Hosting target:** Vercel serves static files from `public/` and rewrites to `api/index.py`, with `maxDuration` 300 on the Pro plan. **bindswift.com is not deployed yet** (NXDOMAIN; Namecheap DNS is only set up for email).
- **Local run:** `start.bat` on the desktop, served at `http://127.0.0.1:8000`.

---

## 3. What was built this session

### 3.1 Hiscox live quoting (`backend/app/integrations/hiscox.py`)
- Hiscox's portal is OneShield "Dragon UI" (server-driven JSON). It uses the `GatewayServlet` and `FieldProcessorServlet` endpoints (`AJX_*` params). Fields are named `bv_<objectId>_<fieldId>`, and the field IDs are stable.
- **Login:** Azure B2C (`prodb2chiscoxus.b2clogin.com`, policy `B2C_1A_signin_only`) → SelfAsserted → confirmed → `POST /oneshield/sso` with the id_token. **Sign-in must start at** `https://portal.hiscox.com/oneshield/GatewayExternalLinkServlet?partner=https%3A%2F%2Fwww.hiscox.com%2Fpartner-agent%2Ffirstconnectinsurance&oswt=LoginAgent`, or you get the OneShield error page.
- **Request-shape quirks:**
  - Hidden sections use `visibility:false`.
  - Labels live on the cell's `label`.
  - `posted_value` has quirks for checkboxes, IncludeExclude fields, and the `'0'` default on selects.
  - `validateFlag` comes from the button's `validationType` when `savePageContents` is set.
  - `CURRENT_OBJECT` comes from the button id `obj_<id>`.
  - `namefields` excludes Dragon Text and ComboBox.
- **Run-time behaviour:**
  - `Screen.outcome()` detects declines ("Coverage not available") and raises `HiscoxDecision`.
  - An unanswered-required-question guard stops a run that would leave questions blank.
  - `QUESTION_ANSWERS` covers the property-management questions.
  - `wait_for_price` handles the premium appearing after a delay.
  - `leave_quote` clicks Home at the end to release the "quote is locked" state.
  - A Postgres advisory lock `pg_advisory_xact_lock(hashtext('bindswift-hiscox'))` serialises runs.
- **Verified live:** Howard Housing Solutions, Inc as Accounting (class 1146845) quoted **$1,301/yr**, ref S112.767.614.
- **Scripts:**
  - `backend/scripts/hiscox_replay_check.py` replays the captured HAR exactly (9/9 gateway requests, 20/20 field-rule calls).
  - `backend/scripts/hiscox_test_quote.py` runs a live test from a JSON file.
  - Samples are in `backend/fc captures/` (git-ignored).

### 3.2 Appetite matching and the client flow
- `backend/app/appetite.py`:
  - `CLASSES` covers bookkeeping, consultant, marketing, salon, barber, beauty, wellness, retail and doula; each kind maps to a Hiscox code.
  - `MAX_REVENUE = 5_000_000`.
  - `match(answers)` returns `{route: "instant" | "application", carriers, reasons}`.
  - `hiscox_ready()` requires the Hiscox credentials **and** the admin switch.
- **Flow:** the client fills a short business profile.
  - If the profile matches Hiscox appetite, `POST /api/profile/{ref}/instant-quote` runs Hiscox. The client sees a loading screen, then a quote card that names the insurer and links to an offer at `/q/<token>`.
  - If nothing matches, the status becomes `needs_application`. The client then fills the **full ACORD application**, which goes to MGA partners. The full application's `profileRef` supersedes the profile.
- **Statuses added:** `quoting`, `needs_application`, `superseded`. Profiles are stored as submissions with `kind='profile'` plus a `match` JSONB column and a `profile_id` link.
- **Instant-quote calls are protected by an HMAC ticket** (`verify._key("profile-ticket")`).

### 3.3 Admin
- The admin was redesigned: black and white, KPI tiles, and status pills for every status.
- The list shows Source, Revenue and Quote columns. The detail page has an instant-quote callout, and quote rows show the Hiscox reference and the monthly amount.
- **Live instant quotes switch:** stored in the `app_settings` table (key `instant_quotes`). It is **off by default** and changes are audited as `setting_changed`. Turn it on for the judges' demo.

### 3.4 Free "Can I get an instant quote?" checker
- `backend/app/eligibility.py` handles `POST /api/eligibility`.
- It uses a Claude forced tool call (`classify_business`, model `claude-haiku-5-5` via `ELIGIBILITY_MODEL`). Without `ANTHROPIC_API_KEY` it falls back to keyword matching; nail/lash/braid keywords are checked before salon.
- Verdicts are instant / soon / broker. Broker-quoted wording is "Usually quoted the same day, within 1 to 2 business days."

### 3.5 Site and UI
- **Header** (latest version):
  - A single bar with a black gradient and a hairline bottom border.
  - The logo sits at the far left, with the tagline **"HANDLING YOUR INSURANCE NEEDS"** after a thin divider. The tagline is hidden on mobile.
  - Nav links are small spaced-out capitals with an underline on hover. "Get a quote" is a white button with squared corners.
  - The top strip was removed at the user's request.
- **Other UI:**
  - Text selection is white on black (the default blue was removed).
  - A custom scrollbar; the text caret is hidden outside form fields; the click/press animations were removed.
  - Page transitions use `startViewTransition`.
  - The phone preview works: frontend `frame-ancestors 'self'` with X-Frame-Options SAMEORIGIN, and `frontend_csp()` recomputes the inline-script hashes whenever index.html changes.
  - The "Expect a few days" wording was changed to "Usually quoted the same day, within 1 to 2 business days."

### 3.6 Howard Analytics site under Bindswift
- The files are in `public/howard-analytics/`, served at `/howard-analytics/` with its own `SITE_CSP`.
- **Redirects (308)** go to `/howard-analytics/` from `/howard-analytics`, `/howardanalytics`, `/howardanalytics/`, `/howard`, `/ha`, `/HA`, `/Ha`, `/jay` and `/Jay`. They're set in both `main.py` and `vercel.json`; Vercel is case-sensitive, so each case is listed.

### 3.7 Logo ("seal in motion")
- The design: speed lines + a scalloped seal whose teeth sit on the right side only + an inner ring + a check whose long stroke is a lightning bolt.
- The plain seal-with-check was dropped because it looked like generic verified badges, and a USPTO application (serial 50159613, a check in a scalloped circle) exists for that shape. Recommend a USPTO design search in class 36 or an attorney before filing. A search for the name "Bindswift" found no conflicts, but that isn't a clearance.
- **Integrated into:** the site header, favicon (data URI), admin `base.html`, `quote.html` and `certs.html`. Files are also in `public/brand/`.
- **Brand kit:** `bindswift-brand-kit.zip` contains:
  - `logo/`: 4 PNGs.
  - `mark/`: SVG + 512/1024 PNGs, black and white.
  - `favicon/`.
  - `email-profile/`: black, white, grid and gradient at 1024px.
- The generator script lives outside the repo (`mk.py` / `export.py`, function `motion_mark(fg, bg)`).

### 3.8 First Connect Carriers spreadsheet
- Claude Docs artifact `facce954-2fce-4771-8543-a01d234faea9`.
- Updated the Blitz, Hiscox and Coterie rows, added a "Bindswift automation" column, and added a "Main-street appetite" tab.

---

## 4. Key files changed (uncommitted)

- `index.html`
- `vercel.json`
- `backend/app/`: `main.py`, `appetite.py` (new), `eligibility.py` (new), `models.py`, `admin.py`, `client.py`, `config.py`, `acord_map.py`
- `backend/app/integrations/`: `hiscox.py`, `coterie.py`, `next_ins.py`
- `backend/app/templates/`: `base.html`, `list.html`, `detail.html`, `quote.html`, `certs.html`
- `backend/`: `schema.sql`, `roles.sql`, `.env.example`
- `backend/scripts/hiscox_*.py`
- `public/brand/*`, `public/howard-analytics/*`
- `docs/hiscox-api-notes.md`, `docs/HANDOFF.md`

---

## 5. Setup the user must do (before the demo)

1. Run `backend/schema.sql`, then `backend/roles.sql`, against the local Postgres. They add the `kind`, `match` and `profile_id` columns, the status CHECK constraint, and the `app_settings` table. **Without this, `/admin/submissions` throws Internal Server Error.**
2. Fill these in `.env` (names only; never print the values): `HISCOX_EMAIL`, `HISCOX_PASSWORD`, `SITE_MODE=live`, `ANTHROPIC_API_KEY`, and optionally `ELIGIBILITY_MODEL`.
3. Restart `start.bat`.
4. In the admin, turn **Live instant quotes** on.

---

## 6. Testing notes

- **Local test harness (cloud sandbox, not the user's PC):** Postgres on port 5433, DB `bs_test`. `serve.py` runs a fake Hiscox on port 8811 with an 8-second delay.
- **Tests:**
  - `t_profile.py`: profile → instant flow.
  - `t_ui.py`: Playwright UI flow.
  - `t_parity.py`: ACORD build parity, 0 differences across 5 classes.
  - `t_switch.py`: admin toggle.
  - All passed after the latest changes.
- Rate limits (`submission_rate`, verify codes) can block repeated test runs; truncate the test tables between runs.

---

## 7. Gotchas

- **File transfer to the PC:** writing files through the desktop bridge sometimes leaves an old copy. Always compare the md5 on the device after writing and retry until it matches. Writing with `force` or to a temp name then `cp` has worked.
- **Leftover file:** `index.new.html` sits in the repo root as a copy of `index.html`. It's safe to delete (the bridge couldn't delete it).
- **CSP:** editing inline scripts in `index.html` changes their hashes, so `frontend_csp()` must keep recomputing them (it keys on the file's mtime). The admin, `/q`, `/c` and `/api` keep `frame-ancestors` DENY.

---

## 8. Open items / next steps

1. **OneDrive sync for the laptop.** The user wants the Bindswift files in `C:\Users\jaden\Desktop\Howard Analytics - Howard Indemnity Group\BindSwift`, kept in step with the repo ("a link between the two folders"). OneDrive does **not** sync the contents of symlinks or junctions. Recommended:
   - Move the repo into the OneDrive folder.
   - Put a junction at the old path so existing scripts still work, from an admin `cmd`: `mklink /J "<old path>" "<OneDrive path>"`.
   - **Exclude `backend/.env`** (and ideally `.venv/`, `__pycache__/`, `node_modules/`) from the shared folder. If the OneDrive is shared with others, keep `.env` out and copy it to the laptop by hand.
   - The user stopped this mid-way; confirm the approach with him first.
2. **Email profile pictures.**
   - Gmail shows a sender photo only if the sending address has a Google Account photo, or through BIMI (DMARC at enforcement plus a VMC/CMC certificate).
   - For a non-Google mailbox (e.g. Namecheap), the quick fix is to create a Google Account "using your existing email" for that address and set the photo there.
   - Use `email-profile/` from the brand kit: black for the main address, white for support or billing.
3. **Deploy:** set up the Vercel project, set the env vars there, and add the Namecheap DNS records for bindswift.com. After that, `/howard-analytics/`, `/ha` and `/jay` go live.
4. Commit to git when the user asks; the commit message needs the attribution lines.
5. **Optional:** add more carriers (Coterie, Blitz, NEXT) to the instant route as their quoting gets automated, and widen `appetite.CLASSES`.
