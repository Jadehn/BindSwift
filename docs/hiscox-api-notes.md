# Hiscox (via First Connect) — portal traffic notes

Source: one sanitized HAR of a sample GL quote for Jaden's own LLC, captured 2026-11-15
(`backend/fc captures/portal.hiscox.com.har`, git-ignored, password/tokens/cookies scrubbed).
No real client data. Quote stopped at the price page — nothing was bound.

## 1. Platform

- Hiscox's agent portal is **OneShield** (Java servlets + an ExtJS front end called "Dragon UI"),
  at `https://portal.hiscox.com/oneshield/`.
- The page is **server-driven**: every screen arrives as a JSON layout from `GatewayServlet`;
  field-level rules run through `FieldProcessorServlet`. There is no clean REST API.
- Session timeout redirects to `https://www.hiscox.com/partner-agent/firstconnectinsurance`.

## 2. Sign-in chain

| # | Request | What it does |
|---|---------|--------------|
| 1 | Partner page → **Sign in** | Sends the browser to Hiscox's Azure AD B2C tenant (`prodb2chiscoxus.b2clogin.com`, policy `B2C_1A_signin_only`). The initial `authorize` GET was not captured (happened before recording). |
| 2 | `POST …/B2C_1A_signin_only/SelfAsserted?tx=StateProperties=…&p=B2C_1A_signin_only` | Form body `request_type=RESPONSE&signInName=<email>&password=<pw>`; header `X-CSRF-TOKEN` (from the B2C page's `x-ms-cpim-csrf` cookie). Returns `{"status":"200"}`. |
| 3 | `GET …/api/CombinedSigninAndSignup/confirmed?rememberMe=false&csrf_token=…&tx=…&p=…` | Returns an auto-submitting HTML form containing `state` and `id_token`. |
| 4 | `POST https://portal.hiscox.com/oneshield/sso` | Form post `state`, `id_token`. Sets the OneShield session cookie and returns the app shell. |
| 5 | `GET /oneshield/HxDesktop.json`, `POST /oneshield/SessionExtendServlet` | App boot + keep-alive (`USER_SESSION_GUID`, `actionId`). |

No MFA step appeared. The login is Hiscox's own B2C account (not a First Connect token exchange);
First Connect only supplies the entry link.

## 3. Session variables every call carries

- `USER_SESSION_GUID` (also sent as `osst`, and returned as `browserTabId`)
- `HTTP_SESSION_ID`, `EXCHANGE_ID` (1218), `MT_LOCALE_ID` (101), `CURRENT_SKIN`
- `DRAGON_TRANSACTION_ID` — returned in each response (`diagnostics.transactionId`), sent back on the next call
- `WORKFLOW_CONTEXT` = `<pageActionId>,<currentObjectId>,1`, `OBJECT_TREE`, `CURRENT_OBJECT`, `TYPE_PROCESSING`
- Field names are `bv_<objectId>_<fieldId>`. **fieldIds are stable**; **objectIds are created per
  submission**, so each step must read them from the previous response (`varName` on each cell).
- Buttons are `TX_NAME=Action.<id>` (taken from `layoutBlockButtons[].actionIdText`).

## 4. Quote flow (GatewayServlet calls, in order)

| Step | Page | Button (`TX_NAME`) | Fields sent (fieldId → meaning) |
|------|------|--------------------|---------------------------------|
| 1 | Start quote | `Action.1514465` | `31810846` ZIP; `210356p33275765`=1 |
| 2 | Business type | `Action.1385265` Next | `211195p33087465p34108046` **profession code** (see §5); `29236514` ownership structure (3 = LLC in this run); `211195p33087465p4095` start date MM/DD/YYYY |
| 3 | Product selection | `Action.1431065` | `33144965`=1 selects GL (field rule sets `33145065`=2) |
| 4 | Load Submission Tree | `Action.1600361` Next | `32874364` first name; `32874564` last name; `29238814` business name; `32681846p33645365` phone; `32681746p30028946` email; `33684261` business start MM/YYYY; `32874964p32875164` DBA? (0/1); `211195p34102546p34101946` current policy in force? (1 yes / 2 no) |
| 5 | Address | `Action.1207665` Validate address | `29325214` street; `210417` suite; `29327414` ZIP; `29326314` city; `210419` state code (32 = NY); `210422` county |
| 6 | (continue) | `Action.1433965` | — |
| 7 | GL Application Questions | `Action.113` next | `32646064` sells/makes products? (2 = No, opens follow-ups `32646264`–`32647164` if Yes); `32647264` design/construction/installation work? (2 = No, follow-ups `32647364`–`32647664`); `32663364` home-based? (2 = No) |
| 8 | Business size | `Action.113` next | `32669664` square feet; `33109364` next-12-month revenue |
| 9 | GL Quote Limits Options | `Action.1494265` Next | `1591` aggregate (2000000); `1594` per-occurrence (1000000); `33535165` deductible ($0); optional add-ons: `32615945p32615345` business property, `32615945p32615145` hired/non-owned auto, `32615945p32614845` waiver of subrogation, `32615945p32614945` primary & non-contributory |

Result of step 9 (accounting, $1M/$2M, $0 deductible, $75K revenue, 2,000 sq ft, Nassau County):
**$4,335.00/yr, or $361.11/mo** (a $723.90 figure also appears — likely a down payment; confirm on the page).

Every field change also fires `FieldProcessorServlet` (`AJX_RULE_OBJ`, `AJX_RULE_BV`, `AJX_RULE_BV_VAL`, …),
which returns show/hide and value rules (`{"ins":[{type,bv:{nm,val}}]}`). Some steps may reject the
Gateway call if the matching field-rule calls weren't made first — test this.

## 5. Profession codes for the first appetite tier

Full list (543 entries): `backend/fc captures/hiscox-professions.json`.

| Bindswift class | Hiscox profession (code) |
|-----------------|--------------------------|
| Hair salons | Beautician/cosmetology services (1143545) |
| Barbershops | Barber/hair stylist services (1143445) |
| Nail / lash / braiding | Nail technician services (1144745); Esthetician services (1143945); braiding → Beautician/cosmetology (1143545) |
| Consultants | Business consulting (1145645); Management consulting (1145945); Strategy consultant (1146445); Other consulting (1146545) |
| Marketing agencies | Marketing/media consulting (1151945); Digital marketing (1151745); Advertising (1151545); Social media consultant (1152345) |
| Bookkeepers & tax preparers | Bookkeeping (1147045); Tax preparation (1147845); Accounting (1146845) |
| Doulas | **No match** in the list — ask Hiscox / First Connect (closest: Personal care aide 1144945, not a fit) |
| Wellness coaches | Life/career/executive coaching (1153145); Diet/nutrition services (1143745); Yoga/Pilates (1145545); Personal training (1145045) |
| Retail stores | Many specific store classes, e.g. Clothing/apparel (1150045), Cosmetic & beauty supply (1165345), Other stores without food/drinks (1150445) |

## 6. Build notes for `integrations/hiscox.py`

- Raw replay is possible but brittle: object IDs, transaction IDs and field-rule ordering change per
  session. Recommended: **Playwright (headless Chromium) for the B2C sign-in**, then drive the
  Gateway/FieldProcessor calls with the same cookie jar, reading `varName`s from each response
  instead of hard-coding object IDs. Fall back to clicking through the UI if a step rejects replay.
- Credentials come from the server's secret store, never the repo; one session per worker; respect
  the keep-alive and timeout.
- Stop at the price. Binding stays a human step (admin approval).
- Before running this in production, confirm with Hiscox / First Connect that automated use of the
  portal is allowed under the agency agreement, and ask whether Hiscox offers a partner quoting API —
  that would replace all of the above.
