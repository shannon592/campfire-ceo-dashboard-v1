# Campfire CEO Dashboard

A read-only Campfire OAuth app (Python/Flask) that shows four pages inside
Campfire under **3rd Party Apps**:

| Page | URL | What it shows |
|---|---|---|
| Overview | `/frame/overview` | Runway, revenue, margin, net income, AR/AP, revenue vs. expenses, cash trend |
| Revenue & Profitability | `/frame/revenue` | Monthly revenue, gross/net margin, net income, cost mix, 12-month P&L summary |
| Cash & Runway | `/frame/cash` | Cash on hand, runway, average net burn, monthly net cash flow |
| Working Capital | `/frame/working-capital` | Working capital, current ratio, DSO/DPO, AR and AP aging, overdue invoices, upcoming bills |

It follows the *How to Build a Campfire OAuth App with GitHub + Render* guide.

## Routes

- `/connect`: sends the user to Campfire to approve read-only access
- `/callback`: receives the OAuth code and stores the token
- `/healthz`: health check for Render
- `/manage`: connection status (the manifest's `manage_url`)
- `/frame/...`: the four pages above

## Scopes (read-only)

`reports:read`, `accounts:read`, `invoices:read`, `bills:read`, set in
`campfire.py` (`SCOPES`) and in `manifest.json` (`allowed_scopes`). The two lists
must match; a test checks this.

## Demo mode

- Set `DEMO_MODE=true` to show sample data on every page, or
- add `?demo=1` to any page URL, e.g. `/frame/overview?demo=1`.

Demo pages show a "Demo mode" banner and never call Campfire.

## Environment variables

Set these on the Render service's **Environment** page. Never commit them.

| Variable | Secret | Where it comes from |
|---|---|---|
| `CLIENT_ID` | | Campfire → Settings → Developer → Apps → Manage → View credentials |
| `CLIENT_SECRET` | yes | Same place (shown once when the app is created) |
| `SIGNING_SECRET` | yes | Same place → Signing secret → Rotate Secret |
| `PUBLIC_BASE_URL` | | Your Render URL, e.g. `https://campfire-ceo-dashboard.onrender.com` |
| `FLASK_SECRET` | yes | Render generates it (see `render.yaml`) |
| `DB_PATH` | | `/var/data/app.sqlite3` (on the Render disk) |
| `DEMO_MODE` | | Optional: `true` for sample data everywhere |
| `CAMPFIRE_API_BASE` | | Optional: defaults to `https://api.meetcampfire.com` |
| `FRAME_ANCESTORS` | | Optional: defaults to `https://app.meetcampfire.com` |

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a; . ./.env; set +a
python app.py
```

Then open http://localhost:5000/frame/overview?demo=1.

Run the tests:

```bash
pip install pytest && pytest
```

## Deploy

1. **Render:** create a Blueprint from this repo. `render.yaml` defines a Starter web
   service (~$7/month) with a 1 GB disk (~$0.25/month) at `/var/data`, health check
   `/healthz`, and auto-deploy from `main`.
2. **Manifest:** in `manifest.json`, replace `<APP_URL>` with the Render URL
   (e.g. `https://campfire-ceo-dashboard.onrender.com`) and `<APP_DOMAIN>` with its
   domain (e.g. `campfire-ceo-dashboard.onrender.com`). Paste it into Campfire →
   Settings → Developer → Apps → **From Manifest**.
3. **Credentials:** copy the client ID, client secret and signing secret from
   Campfire into Render's Environment page by hand, along with `PUBLIC_BASE_URL`.
4. **Promote to production**, then click **Connect** in Campfire and approve the scopes.
5. Open the pages from **3rd Party Apps** in Campfire's left navigation.

## How it works

- **Framing:** every response sends
  `Content-Security-Policy: frame-ancestors 'self' https://app.meetcampfire.com`
  and no `X-Frame-Options`, so Campfire can show the pages in its frame.
- **Who is viewing:** Campfire appends `?cf_ctx=<JWT>` to each page URL. The app
  verifies it (HS256, `SIGNING_SECRET`) and uses its `customer_id` to choose the
  workspace's stored token. Pages don't rely on cookies, because the frame is sandboxed.
- **Tokens:** stored in SQLite at `DB_PATH`, one row per workspace. Access tokens
  (1 hour) are refreshed automatically with the rotating 30-day refresh token.
- **Workspace binding:** connecting from inside Campfire (the in-frame Connect
  button) binds the token to that workspace. If you connect from outside Campfire
  and the token response doesn't name the workspace, the first verified page view
  claims the connection, and no other workspace can use it afterwards.
- **Data:** income statement and balance sheet (`/ca/api/get_income_statement`,
  `/ca/api/get_balance_sheet`, monthly, last 12 closed months) plus open invoices
  (`/coa/api/v1/invoice`) and bills (`/coa/api/v1/bill`). Results are cached per
  workspace for 5 minutes. Charts use Chart.js 4.4.1 (MIT), served from `static/`.

## Current assets and liabilities

Campfire's balance sheet has no "Total Current Assets/Liabilities" rows, so
`balance_sheet.py` sums leaf accounts (`<number> - <name>`) itself, never the
"Total for …" rows, and skips parent rows that only roll up their children. Each
account is classified in this order:

1. Name contains "non-current" or "long-term" → non-current.
2. Account type/subtype from the accounts endpoint (`accounts:read`), when it says
   current or non-current (e.g. "Other Current Asset", "Fixed Asset", "Long Term Liability").
3. Number ranges: assets 1000–1499 and 1600–1699 current, 1500–1599 and 1700–1999
   non-current; liabilities 2000–2699 current, 2700–2999 non-current.

The Working Capital banner appears only if no current asset or current liability
accounts can be classified. Mismatches against the report's "Total for Asset/Liability"
rows are logged to Render's logs.

## Not yet verified against live Campfire

These came from the guide and Campfire engineering notes but haven't been run
against a real workspace yet. Check them on the first live connect:

- **Report response layout.** The parser in `campfire.py` reads several plausible
  shapes and picks rows by name (e.g. "Total Revenue", "Net Income", "Total Cash and
  Cash Equivalents"). If it can't find a row, the page shows a yellow
  "Some figures may be incomplete" banner naming it. It never substitutes sample
  data. If you see that banner, adjust the names in `build_live_dataset`.
- **Invoice and bill list paths and field names** (`amount_due`/`balance_due`,
  `due_date`, `client_name`/`vendor`).
- **Navigation icons:** the manifest uses `scale` (the icon in the guide's sample)
  for every entry. Change them on the Navigation tab if Campfire offers better ones.
