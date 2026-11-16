# Blitz (via First Connect) — integration notes

Recorded 2026-11-15 from the producer portal (HAR in `backend/fc captures/`, git-ignored, tokens and cookies removed).

## Sign-in chain
1. **First Connect** (Auth0, `auth.firstconnectinsurance.com`): password + SMS code + bot protection. Bindswift never signs in by itself.
   `scripts/firstconnect_connect.py` opens a real browser; the broker signs in; the script keeps the **refresh token** (scope `offline_access`) from `POST /oauth/token`, stored Fernet-encrypted in `app_settings.firstconnect_token`. Auth0 rotates it on each use; the new one is saved every time.
2. Each quote: `POST /oauth/token grant_type=refresh_token` → 1-hour access token →
   `GET portal-bff…/api/v1/producer/me` (producer_id) →
   `GET portal-bff…/api/v1/producer/dashboard-carriers/{producer_id}/carrier/834/link` → `{carrierLink}` (Blitz SSO URL with a 2-hour JWT).
3. Opening the link sets Blitz's `_blitz_session` cookie (302 → `/partners/applications`).

## Blitz application (Rails)
- `GET /apply` → program picker (`artisan_contractors`, `retail_services`, `restaurants`, `auto_services`, `special_events`, `commercial_lessors_risk`, `vacants`, `builders_risk`, `cannabis_line`), producer uuid, CSRF meta.
- `GET /apply/start_application?application[producer_uuid]&[program_identifier]&[state]&[requested_effective_date]` → 302 `/apply/<uuid>`.
- Every question page: `POST /apply/dry_run_submissions_data.json` (multipart) returns the page's question schema: `label`, `field_type` (text, select, radio, boolean, attestation, currency, integer, multi_select, textarea, file), `options`, `hidden`, `conditional_relevant_states/products`, `expected_answer` (attestations), `errors`.
  `POST /apply/submit_submissions_data` saves it → 302 next page, or back with `?global_validations[]=<message>` (e.g. an ineligible class code).
- Product selection: `POST /apply/choose_products` (urlencoded checkbox pairs; hidden `false` + checked `true`).
- Stages: First Named Insured → Product Selection → Basic Information → Additional Information → Underwriting Review → **Review Quote** (Quote, Fees, Checkout) → Sign Contracts. Bindswift stops at Review Quote; `/lock_in_offer_selections`, checkout and signing are refused in code.

## Contractors program (NY) test
Class 91555 Computer Service or Repair, $150K sales, $50K payroll, 1M/1M, no deductible → **$1,281** ($1,149 GL + $132 products/completed ops), ref AC-NY-1116-EDE3. 91341 Carpentry - interior was refused as ineligible.
580 class codes are offered in the contractors program (`fc captures/blitz-classes.json`).

## Truthful answers
`integrations/blitz.py` answers only from the profile (`profile_answers`) and the client's explicit `blitzAnswers`. Any other visible question, or an attestation the client hasn't confirmed, raises `BlitzNeedsInput` with the questions (labels, types, options, attestation clauses) so the site can ask the client.

## Checks
- `python scripts/blitz_replay_check.py "fc captures/app.blitzinsurance.com.har"` → ALL MATCH (10 pages + product selection identical, same $1,281).
- Live: `python scripts/firstconnect_connect.py` once, then `python scripts/blitz_test_quote.py "fc captures/blitz-sample.json"`.

## Coterie (recorded the same night)
Coterie's portal signs in through Okta using First Connect as the identity provider, with 1-hour tokens and no refresh token, and needs First Connect's browser session. Not practical to automate unattended; use Coterie's partner API keys instead (`integrations/coterie.py`). The recording stopped before a price (contact fields still missing). Useful: `POST portalapi…/v2/agentDashboard/appetite/full {akHash,state}` returns allowed products and class codes (e.g. Beauty Salons NY: GL, BOP, PL).
