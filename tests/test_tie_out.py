"""AR/AP tie to the GL; invoice detail excludes future-dated docs and uses USD amounts."""

from datetime import date

import pytest

import balance_sheet
import campfire
import metrics
from tests.test_balance_sheet_structured import REPORT


def test_ar_ap_balances_match_balance_sheet_group_totals():
    # Campfire Balance Sheet, Oct 2026: Total for Accounts Receivable / Accounts Payable
    assert balance_sheet.subtype_balance(REPORT, "2026-10", "ACCOUNTS_RECEIVABLE") == pytest.approx(1748423.45)
    assert balance_sheet.subtype_balance(REPORT, "2026-10", "ACCOUNTS_PAYABLE") == pytest.approx(2444412.32)


def test_subtype_balance_none_without_structured_report():
    assert balance_sheet.subtype_balance({"rows": []}, "2026-10", "ACCOUNTS_RECEIVABLE") is None


def test_open_docs_skip_future_dated_and_use_consolidated_amount():
    today = date(2026, 11, 17)
    items = [
        {"status": "open", "invoice_date": "2028-10-01", "due_date": "2028-10-31", "amount_due": 125000},
        {"status": "past_due", "invoice_date": "2026-09-01", "due_date": "2026-10-01",
         "amount_due": 30000, "amount_due_consolidation": 40639.11, "currency": "GBP"},
        {"status": "open", "invoice_date": "2026-11-17", "due_date": "2026-12-17", "amount_due": 1000},
    ]
    docs = campfire._open_docs(items, ("client_name",), ("invoice_number",), today)
    assert [d["amount_due"] for d in docs] == [pytest.approx(40639.11), 1000]


def test_headline_uses_gl_balance_and_aging_uses_open_docs():
    data = {
        "months": ["2026-10"], "revenue": [900.0], "cogs": [0.0], "opex": [0.0], "net_income": [0.0],
        "cash": [10.0], "source": "live", "warnings": [], "current_assets": 1.0, "current_liabilities": 1.0, "as_of": "2026-10-31",
        "open_invoices": [{"amount_due": 300.0, "days_overdue": 10}, {"amount_due": 100.0, "days_overdue": 0}],
        "open_bills": [{"amount_due": 50.0, "days_overdue": -5}],
        "ar_balance": 1000.0, "ap_balance": 70.0,
    }
    k = metrics.compute(data)["kpi"]
    assert k["ar_total"] == 1000.0 and k["ap_total"] == 70.0
    assert k["ar_open"] == 400.0 and k["ar_overdue"] == 300.0
    assert k["ar_overdue_pct"] == pytest.approx(0.75)
