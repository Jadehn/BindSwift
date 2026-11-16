# NEXT Insurance — what the agent-portal recording shows (2026-11-15)

## Verdict: don't automate the portal; get NEXT Connect partner API access
- app.nextinsurance.com is protected by **PerimeterX** bot detection (`_px3`, `_pxhd`, `px-cloud.net`). It even served Jaden's real browser a captcha once (`GET /api/validate-email` → 403 with a PerimeterX block script). Driving it from a server would mean getting around bot protection, which Bindswift doesn't do.
- The session also rides on the agent portal's sign-in cookies (`ni-portal-agent-access-token` / `-refresh-token`).
- So NEXT goes through its partner program (NEXT Connect), as `integrations/next_ins.py` already says. Until then: answer sheet + broker keys it in.

## How the flow works (useful once API access exists; same concepts)
- Start: agent quote link `track.nextinsurance.com/links?serial=…&channel=affiliation&agent=<agent id>&account_token=<token>` → `app.nextinsurance.com/quote/get-started`.
- `GET /api/cobs-search?term=…&channel=agent` → classes of business (e.g. Accountant 110212, General Contractor 100001).
- `POST /api/lead-form/create-and-link-opportunity {cobId, stateCode, email}` → opportunityId.
- `POST /api/hub-lob-selection/update {lobSelection:["GL"]}`.
- Questionnaire v2 (schema-driven): `get-step-id` → `step-and-hub-data` (questions: name, type, text, mandatory, values, inclusion rules) → `extended-submit-answers {answers:{question name: value}}`. Steps: personal details, business details, multi-state, payroll, waiver of subrogation, owners/employees, revenue, cancellations/fraud, claims + start date.
- `POST /api/quote/load-data` → `lobsForPurchase.GL.offer.quotes.<package>.totalPremium` / monthly, coverages, fees.

## Appetite / pricing seen (NY)
- **General Contractor (100001), NY: unsupported state** (matches NEXT's contractor guide: NY contractors ineligible).
- **Accountant (110212), NY, $50K revenue, LLC since 2023, 1 owner, no employees, no claims: GL $300/yr ($25/mo, $50 down)**. Minimum premium applied, package "proPlus", rating id 185838752.
- NY minimum premiums shown: GL $300/yr, PL $250, BOP $505, WC $216.59.
