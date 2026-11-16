"""Campfire OAuth, cf_ctx verification and read-only API access.

Endpoints (from the Campfire OAuth App Guide and Campfire engineering notes):
  authorize  GET  {CAMPFIRE_API_BASE}/ca/oauth/authorize
  token      POST {CAMPFIRE_API_BASE}/ca/oauth/token
  reports    GET  /ca/api/get_income_statement, /ca/api/get_balance_sheet
  invoices   GET  /coa/api/v1/invoice
  bills      GET  /coa/api/v1/bill

Access tokens last 1 hour; refresh tokens last 30 days and rotate on use.
Campfire appends ?cf_ctx=<JWT> to every frame URL: HS256, signed with the
app's signing secret, 5-minute life, claims include customer_id and user_id.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import jwt
import requests

import balance_sheet
import db
from balance_sheet import ACCOUNT_RE, Entry
from metrics import month_labels

log = logging.getLogger(__name__)

# Read-only scopes. Must match "allowed_scopes" in manifest.json exactly, or
# pages return 403 / connect fails with invalid_scope.
SCOPES = ["reports:read", "accounts:read", "invoices:read", "bills:read"]

HTTP_TIMEOUT = 20
CACHE_TTL_SECONDS = 300


class CampfireError(Exception):
    pass


class NotConnected(CampfireError):
    pass


def api_base() -> str:
    return os.environ.get("CAMPFIRE_API_BASE", "https://api.meetcampfire.com").rstrip("/")


def redirect_uri() -> str:
    return os.environ["PUBLIC_BASE_URL"].rstrip("/") + "/callback"


# --- cf_ctx -----------------------------------------------------------------

def verify_cf_ctx(token: str) -> dict:
    """Verify the signed context token Campfire passes to each frame page."""
    secret = os.environ.get("SIGNING_SECRET")
    if not secret:
        raise CampfireError("SIGNING_SECRET is not set")
    claims = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        options={"require": ["exp"], "verify_aud": False},
        leeway=30,
    )
    if not claims.get("customer_id"):
        raise jwt.InvalidTokenError("cf_ctx has no customer_id")
    claims["customer_id"] = str(claims["customer_id"])
    return claims


# --- OAuth ------------------------------------------------------------------

def authorize_url(state: str) -> str:
    params = {
        "client_id": os.environ["CLIENT_ID"],
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return f"{api_base()}/ca/oauth/authorize?{urlencode(params)}"


def _token_request(data: dict) -> dict:
    payload = {
        **data,
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
    }
    resp = requests.post(f"{api_base()}/ca/oauth/token", data=payload, timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        # Never log the response body: it may echo credentials.
        raise CampfireError(f"Token endpoint returned HTTP {resp.status_code}")
    token = resp.json()
    if "access_token" not in token:
        raise CampfireError("Token response had no access_token")
    return token


def exchange_code(code: str) -> dict:
    return _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
    })


def customer_id_from_token(token: dict) -> str | None:
    for key in ("customer_id", "workspace_id", "customer", "tenant_id"):
        if token.get(key):
            return str(token[key])
    return None


_refresh_lock = threading.Lock()


def access_token_for(customer_id: str) -> str:
    conn = db.get_connection(customer_id)
    if conn is None:
        raise NotConnected(customer_id)
    if conn["expires_at"] > time.time() + 60:
        return conn["access_token"]
    if not conn["refresh_token"]:
        raise NotConnected(customer_id)
    with _refresh_lock:
        conn = db.get_connection(customer_id)  # another thread may have refreshed
        if conn["expires_at"] > time.time() + 60:
            return conn["access_token"]
        try:
            token = _token_request({
                "grant_type": "refresh_token",
                "refresh_token": conn["refresh_token"],
            })
        except CampfireError as exc:
            log.warning("Token refresh failed for %s: %s", customer_id, exc)
            raise NotConnected(customer_id) from exc
        db.save_connection(customer_id, token)
        return token["access_token"]


def api_get(customer_id: str, path: str, params: dict | None = None):
    token = access_token_for(customer_id)
    resp = requests.get(
        f"{api_base()}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code == 401:
        raise NotConnected(customer_id)
    if resp.status_code == 403:
        raise CampfireError(
            f"{path}: 403 Forbidden. Check the scope is in the manifest and the "
            "connect request, then Promote and reconnect."
        )
    if resp.status_code >= 400:
        raise CampfireError(f"{path}: HTTP {resp.status_code}")
    return resp.json()


def api_list(customer_id: str, path: str, params: dict | None = None, max_pages: int = 20) -> list:
    """Fetch a list endpoint, following DRF-style `next` links or page numbers."""
    params = dict(params or {})
    params.setdefault("limit", 500)
    items: list = []
    offset = 0
    for _ in range(max_pages):
        body = api_get(customer_id, path, {**params, "offset": offset})
        if isinstance(body, list):
            items.extend(body)
            break
        page = body.get("results") or body.get("data") or []
        items.extend(page)
        if not body.get("next") or not page:
            break
        offset += len(page)
    return items


# --- Report parsing -----------------------------------------------------------
#
# The report endpoints return a nested tree of rows. Rather than depend on one
# exact layout, we walk the tree and collect (label -> per-period values), then
# pick the rows we need by name. Anything we can't find becomes a warning shown
# on the page, never a silently invented number.

_LABEL_KEYS = ("name", "label", "title", "account_name", "account", "display_name")
_VALUE_KEYS = ("values", "amounts", "totals", "columns", "data", "periods", "balances")
_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})")


def _to_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        try:
            return float(cleaned)
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("amount", "value", "total", "balance"):
            if key in value:
                return _to_float(value[key])
    return None


def _period_key(raw) -> str | None:
    m = _PERIOD_RE.match(str(raw))
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _values_by_period(node: dict, columns: list[str] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in _VALUE_KEYS:
        vals = node.get(key)
        if isinstance(vals, dict):
            for k, v in vals.items():
                p, f = _period_key(k), _to_float(v)
                if p and f is not None:
                    out[p] = out.get(p, 0.0) + f
        elif isinstance(vals, list):
            for i, v in enumerate(vals):
                if isinstance(v, dict):
                    p = _period_key(v.get("period") or v.get("date") or v.get("end_date")
                                    or v.get("start_date") or "")
                    f = _to_float(v)
                elif columns and i < len(columns):
                    p, f = columns[i], _to_float(v)
                else:
                    continue
                if p and f is not None:
                    out[p] = out.get(p, 0.0) + f
        if out:
            return out
    # Flat layout: period keys directly on the row.
    for k, v in node.items():
        p = _period_key(k)
        f = _to_float(v)
        if p and f is not None:
            out[p] = f
    return out


def _find_columns(report) -> list[str] | None:
    if isinstance(report, dict):
        for key in ("columns", "periods", "headers", "dates"):
            cols = report.get(key)
            if isinstance(cols, list) and cols:
                keys = []
                for c in cols:
                    raw = c
                    if isinstance(c, dict):
                        raw = c.get("end_date") or c.get("start_date") or c.get("period") or c.get("label")
                    keys.append(_period_key(raw or ""))
                if any(keys):
                    return keys
    return None


def collect_rows(report) -> dict[str, dict[str, float]]:
    columns = _find_columns(report)
    rows: dict[str, dict[str, float]] = {}

    def walk(node):
        if isinstance(node, dict):
            label = next((node[k] for k in _LABEL_KEYS if isinstance(node.get(k), str)), None)
            if label:
                vals = _values_by_period(node, columns)
                if vals:
                    rows.setdefault(label.strip().lower(), vals)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(report)
    return rows


def collect_entries(report) -> list[Entry]:
    """Every labelled row in document order, remembering its nearest labelled ancestor."""
    columns = _find_columns(report)
    entries: list[Entry] = []

    def walk(node, parent):
        if isinstance(node, dict):
            label = next((node[k] for k in _LABEL_KEYS if isinstance(node.get(k), str)), None)
            here = parent
            if label:
                entries.append(Entry(label.strip(), _values_by_period(node, columns), parent))
                here = len(entries) - 1
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, here)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent)

    walk(report, None)
    return entries


def account_types(customer_id: str) -> dict[int, str]:
    """Account number -> "type subtype" text from the accounts endpoint (accounts:read).

    Best effort: if the endpoint or its fields differ, classification falls back
    to account-number ranges.
    """
    for path in ("/coa/api/v1/account", "/coa/api/account"):
        try:
            items = api_list(customer_id, path)
        except (CampfireError, requests.RequestException, ValueError) as exc:
            log.info("Accounts endpoint %s unavailable: %s", path, exc)
            continue
        types: dict[int, str] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            raw_num = it.get("number") or it.get("account_number") or it.get("code")
            if raw_num is None:
                m = ACCOUNT_RE.match(str(it.get("name") or ""))
                raw_num = m.group(1) if m else None
            try:
                number = int(str(raw_num).split(".")[0])
            except (TypeError, ValueError):
                continue
            parts = []
            for key in ("account_type", "type", "account_subtype", "subtype", "detail_type", "classification"):
                val = it.get(key)
                if isinstance(val, dict):
                    val = val.get("name") or val.get("label") or val.get("value")
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
            if parts:
                types[number] = " ".join(parts)
        if types:
            return types
    return {}


def pick_exact(rows: dict[str, dict[str, float]], *candidates: str) -> dict[str, float] | None:
    for cand in candidates:
        if cand in rows:
            return rows[cand]
    return None


def pick(rows: dict[str, dict[str, float]], *candidates: str) -> dict[str, float] | None:
    for cand in candidates:
        if cand in rows:
            return rows[cand]
    for cand in candidates:
        for label, vals in rows.items():
            if cand in label:
                return vals
    return None


def _series(vals: dict[str, float] | None, months: list[str]) -> list[float] | None:
    if vals is None:
        return None
    return [vals.get(m, 0.0) for m in months]


def _parse_date(raw) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)[:10]).date()
    except ValueError:
        return None


def _open_docs(items: list, party_keys: tuple[str, ...], number_keys: tuple[str, ...], today: date) -> list[dict]:
    docs = []
    for it in items:
        if not isinstance(it, dict):
            continue
        status = str(it.get("status") or "").lower()
        if status in {"void", "voided", "draft", "paid", "deleted", "cancelled", "canceled"}:
            continue
        due = None
        for key in ("amount_due", "balance_due", "balance", "open_amount", "remaining_amount"):
            if key in it:
                due = _to_float(it[key])
                break
        if not due or due <= 0:
            continue
        party = None
        for key in party_keys:
            val = it.get(key)
            if isinstance(val, dict):
                val = val.get("name") or val.get("display_name")
            if val:
                party = str(val)
                break
        number = next((str(it[k]) for k in number_keys if it.get(k)), str(it.get("id", "")))
        due_date = _parse_date(it.get("due_date"))
        docs.append({
            "number": number,
            "party": party or "Unknown",
            "amount_due": due,
            "due_date": due_date.isoformat() if due_date else "",
            "days_overdue": (today - due_date).days if due_date else 0,
        })
    return docs


def build_live_dataset(customer_id: str, today: date | None = None) -> dict:
    today = today or date.today()
    months = month_labels(today, 12)
    start = f"{months[0]}-01"
    end = (today.replace(day=1) - timedelta(days=1)).isoformat()  # last closed month-end
    warnings: list[str] = []
    window = {"start_date": start, "end_date": end, "cadence": "monthly"}

    income = collect_rows(api_get(customer_id, "/ca/api/get_income_statement", window))
    balance_report = api_get(customer_id, "/ca/api/get_balance_sheet", window)
    balance = collect_rows(balance_report)

    revenue = _series(pick(income, "total revenue", "total income", "revenue", "income"), months)
    cogs = _series(pick(income, "total cost of goods sold", "total cost of revenue",
                        "cost of goods sold", "cost of revenue", "cogs"), months)
    opex = _series(pick(income, "total operating expenses", "total expenses", "operating expenses",
                        "expenses"), months)
    net_income = _series(pick(income, "net income", "net profit", "net loss"), months)
    cash = _series(pick_exact(balance, "total for cash and cash equivalents", "total for cash",
                              "total for bank accounts", "total for bank")
                   or pick(balance, "total cash and cash equivalents", "cash and cash equivalents",
                           "total bank accounts", "total cash", "cash"), months)
    # Exact match only: a leaf like "1350 - Other Current Assets" must not pass for the total.
    current_assets = pick_exact(balance, "total current assets", "total for current assets", "current assets")
    current_liabilities = pick_exact(balance, "total current liabilities", "total for current liabilities",
                                     "current liabilities")

    for name, value in (("Revenue", revenue), ("Net income", net_income), ("Cash", cash)):
        if value is None:
            warnings.append(f"Couldn't find a {name} row in the Campfire report; showing 0.")
    revenue = revenue or [0.0] * 12
    cogs = cogs or [0.0] * 12
    if opex is None:
        warnings.append("Couldn't find an operating expenses row; derived it from net income.")
    net_income = net_income or [0.0] * 12
    opex = opex or [r - c - n for r, c, n in zip(revenue, cogs, net_income)]
    cash = cash or [0.0] * 12

    invoices = _open_docs(
        api_list(customer_id, "/coa/api/v1/invoice"),
        ("client_name", "customer_name", "client", "customer"),
        ("invoice_number", "number", "doc_number"),
        today,
    )
    bills = _open_docs(
        api_list(customer_id, "/coa/api/v1/bill"),
        ("vendor_name", "vendor", "payee"),
        ("bill_number", "number", "doc_number"),
        today,
    )

    ca = current_assets.get(months[-1]) if current_assets else None
    cl = current_liabilities.get(months[-1]) if current_liabilities else None
    if ca is None or cl is None:
        # Campfire's balance sheet has no current/non-current subtotals: classify accounts.
        wc = balance_sheet.classify(collect_entries(balance_report), months[-1],
                                    account_types(customer_id))
        if wc is not None:
            ca, cl = wc.current_assets, wc.current_liabilities
    if ca is None or cl is None:
        warnings.append("Couldn't find current assets/liabilities; current ratio uses cash, AR and AP only.")
        ca = cash[-1] + sum(d["amount_due"] for d in invoices)
        cl = sum(d["amount_due"] for d in bills)

    return {
        "source": "live",
        "as_of": end,
        "months": months,
        "revenue": revenue,
        "cogs": cogs,
        "opex": opex,
        "net_income": net_income,
        "cash": cash,
        "current_assets": float(ca),
        "current_liabilities": float(cl),
        "open_invoices": invoices,
        "open_bills": bills,
        "warnings": warnings,
    }


_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def live_dataset(customer_id: str) -> dict:
    with _cache_lock:
        hit = _cache.get(customer_id)
        if hit and hit[0] > time.time():
            return hit[1]
    data = build_live_dataset(customer_id)
    with _cache_lock:
        _cache[customer_id] = (time.time() + CACHE_TTL_SECONDS, data)
    return data


def clear_cache(customer_id: str) -> None:
    with _cache_lock:
        _cache.pop(customer_id, None)
