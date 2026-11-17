"""Classification on the real /ca/api/get_balance_sheet response shape.

Rows mirror the live Campfire, LLC balance sheet for Oct 2026:
  current assets       21,191,514.99 cash + 1,748,423.45 AR + 306,500 prepaid
                       + 282,306.02 unbilled AR                         = 23,528,744.46
  non-current assets   (133,314.22) net PP&E + 5,482,710.36 ROU         =  5,349,396.14
  current liabilities  2,444,412.32 AP + 4,527,261.13 CC + 11,760,970.27 accrued
                       + 18,000 + 9,600 + 4,200 + 1,377 + 781,719.31    = 19,547,540.03
  non-current liabs    5,482,710.36 lease liability                     =  5,482,710.36
"""

import pytest

import balance_sheet
import campfire

K = "2026-10-01_Total"


def row(id_, num, name, typ, sub, parent, val):
    return {"account_id": id_, "account_number": num, "account_name": name, "account_type": typ,
            "account_subtype": sub, "parent": parent, K: val}


REPORT = {"balance_sheet": [
    row("1", "1000", "Cash and Cash Equivalents", "ASSET", "BANK", "ASSET", 21191514.99),
    row("2", "1010", "Summit Pacific Bank", "ASSET", "BANK", 1, 21063961.27),
    row("3", "1020", "Redwood Credit Union", "ASSET", "BANK", 1, 5604.11),
    row("4", "1200", "Accounts Receivable", "ASSET", "ACCOUNTS_RECEIVABLE", "ASSET", 1748423.45),
    row("5", "1300", "Prepaid Expenses", "ASSET", "PREPAID", "ASSET", 306500),
    row("6", "1600", "Unbilled Accounts Receivable", "ASSET", "OTHER_CURRENT_ASSET", "ASSET", 282306.02),
    row("7", "1500", "Property and Equipment", "ASSET", "FIXED_ASSET", "ASSET", -133314.22),
    row("8", "1510", "Computer Equipment", "ASSET", "FIXED_ASSET", 7, 98700),
    row("9", "1520", "Furniture and Fixtures", "ASSET", "FIXED_ASSET", 7, 27000),
    row("10", "1530", "Leasehold Improvements", "ASSET", "FIXED_ASSET", 7, 95000),
    row("11", "1590", "Accumulated Depreciation", "ASSET", "FIXED_ASSET", 7, -354014.22),
    row("12", "1700", "Right-of-Use Asset", "ASSET", "OTHER_ASSET", "ASSET", 5482710.36),
    {"account_name": "ASSET", "account_type": "ASSET", K: 28878140.6},
    row("13", "1210", "Intercompany Accounts Receivable", "ASSET", "ACCOUNTS_RECEIVABLE", 4, -405983.67),
    row("14", "2000", "Accounts Payable", "LIABILITY", "ACCOUNTS_PAYABLE", "LIABILITY", 2444412.32),
    row("15", "2010", "Intercompany Accounts Payable", "LIABILITY", "ACCOUNTS_PAYABLE", 14, -405983.67),
    row("16", "2200", "Credit Card Payable", "LIABILITY", "CREDIT_CARD", "LIABILITY", 4527261.13),
    row("17", "2210", "Ramp Card - Consolidated", "LIABILITY", "CREDIT_CARD", 16, 4489359.42),
    row("18", "2100", "Accrued Expenses", "LIABILITY", "OTHER_CURRENT_LIABILITY", "LIABILITY", 11760970.27),
    row("19", "2110", "Accrued Payroll", "LIABILITY", "OTHER_CURRENT_LIABILITY", 18, 10569800),
    row("20", "2120", "Accrued Bonus", "LIABILITY", "OTHER_CURRENT_LIABILITY", "LIABILITY", 18000),
    row("21", "2130", "Accrued Commissions", "LIABILITY", "OTHER_CURRENT_LIABILITY", "LIABILITY", 9600),
    row("22", "2140", "Accrued PTO", "LIABILITY", "OTHER_CURRENT_LIABILITY", "LIABILITY", 4200),
    row("23", "2600", "Payroll Tax Liabilities", "LIABILITY", "OTHER_CURRENT_LIABILITY", "LIABILITY", 1377),
    row("24", "2300", "Deferred Revenue", "LIABILITY", "DEFERRED_REVENUE", "LIABILITY", 781719.31),
    row("25", "2800", "Lease Liability Non-current", "LIABILITY", "LONG_TERM_LIABILITY", "LIABILITY", 5482710.36),
    {"account_name": "LIABILITY", "account_type": "LIABILITY", K: 25030250.39},
    row("26", "3100", "Additional Paid-In Capital", "EQUITY", "EQUITY", "EQUITY", 4607274.88),
], "headers": {"2026-10-01": ["Total"]}}


def test_structured_classification_matches_live_figures():
    wc = balance_sheet.classify_structured(REPORT, "2026-10")
    assert wc.current_assets == pytest.approx(23528744.46)
    assert wc.noncurrent_assets == pytest.approx(5349396.14)
    assert wc.total_assets == pytest.approx(28878140.60)
    assert wc.current_liabilities == pytest.approx(19547540.03)
    assert wc.noncurrent_liabilities == pytest.approx(5482710.36)
    assert wc.total_liabilities == pytest.approx(25030250.39)
    assert wc.unclassified == []


def test_non_structured_report_returns_none():
    assert balance_sheet.classify_structured({"rows": []}, "2026-10") is None


def test_live_dataset_has_no_banner_on_real_shape(monkeypatch):
    months = [f"2025-{m:02d}" for m in (11, 12)] + [f"2026-{m:02d}" for m in range(1, 11)]
    monkeypatch.setattr(campfire, "month_labels", lambda today, n: months)

    def fake(customer_id, path, params=None):
        if "balance_sheet" in path:
            return REPORT
        return {"rows": [{"name": "Total Revenue", "2026-10-01_Total": 1}, {"name": "Net Income", "2026-10-01_Total": 1}]}
    monkeypatch.setattr(campfire, "api_get", fake)
    monkeypatch.setattr(campfire, "api_list", lambda *a, **k: [])
    data = campfire.build_live_dataset("cust")
    assert not any("current assets" in w for w in data["warnings"])
    assert data["current_assets"] == pytest.approx(23528744.46)
    assert data["current_liabilities"] == pytest.approx(19547540.03)
