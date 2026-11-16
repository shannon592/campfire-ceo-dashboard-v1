import importlib
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

SIGNING_SECRET = "whsec_test_signing_secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_ID", "cid_test")
    monkeypatch.setenv("CLIENT_SECRET", "csecret_test")
    monkeypatch.setenv("SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dash.example.com")
    monkeypatch.setenv("FLASK_SECRET", "test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    import campfire
    import app as app_module
    importlib.reload(app_module)
    campfire._cache.clear()
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def ctx(customer_id="cust_1", secret=SIGNING_SECRET, exp_in=300):
    return jwt.encode(
        {"customer_id": customer_id, "user_id": "user_1", "exp": int(time.time()) + exp_in},
        secret,
        algorithm="HS256",
    )


PAGES = ["overview", "revenue", "cash", "working-capital"]


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


@pytest.mark.parametrize("page", PAGES)
def test_demo_pages_render_and_allow_campfire_frame(client, page):
    resp = client.get(f"/frame/{page}?demo=1")
    assert resp.status_code == 200
    assert b"Demo mode" in resp.data
    assert "X-Frame-Options" not in resp.headers
    assert "frame-ancestors 'self' https://app.meetcampfire.com" in resp.headers["Content-Security-Policy"]


def test_demo_mode_env(client, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    resp = client.get("/frame/overview")
    assert resp.status_code == 200 and b"Demo mode" in resp.data


def test_unknown_page_404(client):
    assert client.get("/frame/nope?demo=1").status_code == 404


def test_frame_without_ctx_asks_to_open_in_campfire(client):
    resp = client.get("/frame/overview")
    assert b"Open this page from Campfire" in resp.data


def test_bad_and_expired_ctx_rejected(client):
    assert client.get(f"/frame/overview?cf_ctx={ctx(secret='wrong')}").status_code == 401
    assert client.get(f"/frame/overview?cf_ctx={ctx(exp_in=-120)}").status_code == 401


def test_valid_ctx_not_connected_offers_connect(client):
    resp = client.get(f"/frame/overview?cf_ctx={ctx()}")
    assert resp.status_code == 200
    assert b"Connect Campfire" in resp.data


def test_connect_redirects_with_readonly_scopes(client):
    resp = client.get("/connect")
    assert resp.status_code == 302
    url = urlparse(resp.headers["Location"])
    assert url.netloc == "api.meetcampfire.com" and url.path == "/ca/oauth/authorize"
    q = parse_qs(url.query)
    assert q["client_id"] == ["cid_test"]
    assert q["redirect_uri"] == ["https://dash.example.com/callback"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["reports:read accounts:read invoices:read bills:read"]
    assert q["state"][0]


def test_scopes_match_manifest():
    import json
    import campfire
    with open("manifest.json") as f:
        manifest = json.load(f)
    assert sorted(manifest["app"]["allowed_scopes"]) == sorted(campfire.SCOPES)
    assert all(s.endswith(":read") for s in campfire.SCOPES)


def test_callback_rejects_unknown_state(client):
    assert client.get("/callback?code=abc&state=forged").status_code == 400


def _connect(client, monkeypatch, customer_ctx=None):
    import campfire
    monkeypatch.setattr(campfire, "exchange_code", lambda code: {
        "access_token": "cfa_test", "refresh_token": "r1", "expires_in": 3600, "scope": " ".join(campfire.SCOPES),
    })
    path = "/connect" + (f"?cf_ctx={customer_ctx}" if customer_ctx else "")
    state = parse_qs(urlparse(client.get(path).headers["Location"]).query)["state"][0]
    resp = client.get(f"/callback?code=abc&state={state}")
    assert resp.status_code == 200 and b"Connected" in resp.data
    # state is single use
    assert client.get(f"/callback?code=abc&state={state}").status_code == 400


INCOME = {
    "columns": [{"end_date": f"2026-{m:02d}-28"} for m in range(1, 13)],
    "rows": [
        {"name": "Revenue", "rows": [{"name": "Total Revenue", "values": [100_000 + i * 1000 for i in range(12)]}]},
        {"name": "Total Cost of Revenue", "values": [30_000] * 12},
        {"name": "Total Operating Expenses", "values": [90_000] * 12},
        {"name": "Net Income", "values": [-20_000 + i * 1000 for i in range(12)]},
    ],
}
BALANCE = {
    "columns": [{"end_date": f"2026-{m:02d}-28"} for m in range(1, 13)],
    "rows": [
        {"name": "Total Cash and Cash Equivalents", "values": [5_000_000 - i * 20_000 for i in range(12)]},
        {"name": "Total Current Assets", "values": [6_000_000] * 12},
        {"name": "Total Current Liabilities", "values": [1_500_000] * 12},
    ],
}
INVOICES = {"results": [
    {"invoice_number": "INV-1", "client_name": "Acme", "amount_due": "1,200.00", "due_date": "2026-11-01", "status": "open"},
    {"invoice_number": "INV-2", "client_name": "Paid Co", "amount_due": 0, "due_date": "2026-10-01", "status": "paid"},
], "next": None}
BILLS = {"results": [
    {"bill_number": "B-1", "vendor": {"name": "AWS"}, "balance_due": 500, "due_date": "2026-12-15"},
], "next": None}


def test_live_flow_end_to_end(client, monkeypatch):
    import campfire
    _connect(client, monkeypatch, customer_ctx=ctx("cust_live"))
    calls = []

    def fake_api_get(customer_id, path, params=None):
        calls.append((customer_id, path))
        return {
            "/ca/api/get_income_statement": INCOME,
            "/ca/api/get_balance_sheet": BALANCE,
            "/coa/api/v1/invoice": INVOICES,
            "/coa/api/v1/bill": BILLS,
        }[path]

    monkeypatch.setattr(campfire, "api_get", fake_api_get)
    monkeypatch.setattr(campfire, "month_labels", lambda today, n: [f"2026-{m:02d}" for m in range(1, 13)])

    resp = client.get(f"/frame/working-capital?cf_ctx={ctx('cust_live')}")
    assert resp.status_code == 200, resp.data
    body = resp.data.decode()
    assert "Demo mode" not in body
    assert "Acme" in body and "AWS" in body and "Paid Co" not in body
    assert all(c[0] == "cust_live" for c in calls)

    data = campfire.live_dataset("cust_live")
    assert data["revenue"][0] == 100_000 and data["cash"][-1] == 4_780_000
    assert data["current_assets"] == 6_000_000 and data["warnings"] == []


def test_default_connection_is_claimed_by_first_workspace_only(client, monkeypatch):
    import campfire
    import db
    _connect(client, monkeypatch)  # no cf_ctx -> stored as "default"
    monkeypatch.setattr(campfire, "live_dataset", lambda cid: (_ for _ in ()).throw(campfire.CampfireError("stop")))
    client.get(f"/frame/overview?cf_ctx={ctx('cust_a')}")
    assert db.get_connection("cust_a") is not None
    resp = client.get(f"/frame/overview?cf_ctx={ctx('cust_b')}")
    assert b"Connect Campfire" in resp.data
    assert db.get_connection("cust_b") is None


def test_parser_handles_period_dict_layout():
    import campfire
    report = {"sections": [{"label": "Net Income", "amounts": {"2026-01-31": "(1,000)", "2026-02-28": 2500}}]}
    rows = campfire.collect_rows(report)
    assert rows["net income"] == {"2026-01": -1000.0, "2026-02": 2500.0}
