"""Current vs. non-current classification on a Campfire-style balance sheet.

Expected figures for the chart of accounts below (as of 2026-10):

  Current assets      1000 Cash 4,000,000 + 1200 AR 900,000 + 1210 Intercompany AR 100,000
                      + 1300 Prepaid 150,000 + 1600 Unbilled AR 80,000          = 5,230,000
  Non-current assets  1510 300,000 + 1520 50,000 + 1590 Acc. Depr. (120,000)
                      + 1700 ROU 400,000                                         =   630,000
  Current liabs       2000 AP 300,000 + 2010 IC AP 100,000
                      + 2100 own 5,000 + 2110-2140 145,000 + 2210 Ramp 45,000
                      + 2300 Deferred Revenue 700,000 + 2600 Payroll Tax 30,000  = 1,325,000
  Non-current liabs   2400 Long-term Note Payable 500,000 (name rule)
                      + 2800 Lease Liability Non-current 350,000                 =   850,000

"1500 - Property and Equipment" and "2200 - Credit Card Payable" are roll-up
parents (their row equals their children) and must not be double counted;
"2100 - Accrued Expenses" carries its own 5,000 balance and must be counted.
"""

import json

import pytest

import balance_sheet
import campfire

MONTH = "2026-10"
COLUMNS = [{"end_date": "2026-10-31"}]

# (label, value or None for a header, children)
TREE = [
    ("Asset", None, [
        ("1000 - Operating Cash", 4_000_000, []),
        ("Accounts Receivable", None, [
            ("1200 - Accounts Receivable", 900_000, []),
            ("1210 - Intercompany AR", 100_000, []),
        ]),
        ("1300 - Prepaid Expenses", 150_000, []),
        ("1500 - Property and Equipment", 230_000, [
            ("1510 - Computer Equipment", 300_000, []),
            ("1520 - Furniture and Fixtures", 50_000, []),
            ("1590 - Accumulated Depreciation", -120_000, []),
        ]),
        ("1600 - Unbilled AR", 80_000, []),
        ("1700 - Right-of-Use Asset", 400_000, []),
    ]),
    ("Liability", None, [
        ("2000 - Accounts Payable", 300_000, []),
        ("2010 - Intercompany AP", 100_000, []),
        ("2100 - Accrued Expenses", 5_000, [
            ("2110 - Accrued Payroll", 60_000, []),
            ("2120 - Accrued Bonus", 40_000, []),
            ("2130 - Accrued Commissions", 25_000, []),
            ("2140 - Accrued PTO", 20_000, []),
        ]),
        ("2200 - Credit Card Payable", 45_000, [
            ("2210 - Credit Card Payable (Ramp)", 45_000, []),
        ]),
        ("2300 - Deferred Revenue", 700_000, []),
        ("2400 - Long-term Note Payable", 500_000, []),
        ("2600 - Payroll Tax Liabilities", 30_000, []),
        ("2800 - Lease Liability Non-current", 350_000, []),
    ]),
    ("Equity", None, [
        ("3000 - Common Stock", 1_000, []),
        ("3900 - Retained Earnings", 2_000_000, []),
    ]),
]

CURRENT_ASSETS = 5_230_000
NONCURRENT_ASSETS = 630_000
CURRENT_LIABS = 1_325_000
NONCURRENT_LIABS = 850_000


def _subtotal(node):
    label, value, children = node
    if not children:
        return value or 0
    own = value if value is not None and value != sum(_subtotal(c) for c in children) else 0
    return own + sum(_subtotal(c) for c in children)


def _group_name(label):
    m = balance_sheet.ACCOUNT_RE.match(label)
    return m.group(2) if m else label


def flat_report():
    """Rows in document order with "Total for X" closing each group (Campfire style)."""
    rows = []

    def emit(node):
        label, value, children = node
        row = {"name": label}
        if value is not None:
            row["values"] = [value]
        rows.append(row)
        if children:
            for c in children:
                emit(c)
            rows.append({"name": f"Total for {_group_name(label)}", "values": [_subtotal(node)]})

    for top in TREE:
        emit(top)
    return {"columns": COLUMNS, "rows": rows}


def nested_report(with_totals=True):
    def build(node):
        label, value, children = node
        row = {"name": label}
        if value is not None:
            row["values"] = [value]
        if children:
            row["rows"] = [build(c) for c in children]
            if with_totals:
                row["rows"].append({"name": f"Total for {_group_name(label)}", "values": [_subtotal(node)]})
        return row

    return {"columns": COLUMNS, "rows": [build(n) for n in TREE]}


def classify(report, types=None):
    return balance_sheet.classify(campfire.collect_entries(report), MONTH, types)


def test_fixture_totals_are_consistent():
    assert _subtotal(TREE[0]) == CURRENT_ASSETS + NONCURRENT_ASSETS
    assert _subtotal(TREE[1]) == CURRENT_LIABS + NONCURRENT_LIABS


@pytest.mark.parametrize("report", [flat_report(), nested_report(True), nested_report(False)],
                         ids=["flat", "nested-with-totals", "nested-no-totals"])
def test_classifies_by_number_ranges_without_double_counting(report):
    wc = classify(report)
    assert wc is not None
    assert wc.current_assets == CURRENT_ASSETS
    assert wc.noncurrent_assets == NONCURRENT_ASSETS
    assert wc.current_liabilities == CURRENT_LIABS
    assert wc.noncurrent_liabilities == NONCURRENT_LIABS
    assert wc.unclassified == []


def test_total_for_rows_are_never_counted_as_accounts():
    accounts = balance_sheet.find_accounts(campfire.collect_entries(flat_report()), MONTH)
    labels = {a.name for a in accounts.values()}
    assert not any(name.lower().startswith("total") for name in labels)
    numbers = sorted(a.number for a in accounts.values())
    assert 1590 in numbers and 2210 in numbers and 2800 in numbers


def test_rollup_parents_skipped_own_balance_parents_counted():
    entries = campfire.collect_entries(flat_report())
    accounts = balance_sheet.find_accounts(entries, MONTH)
    by_num = {a.number: a for a in accounts.values()}
    assert balance_sheet.counted_value(by_num[1500], accounts) == 0  # roll-up of 1510-1590
    assert balance_sheet.counted_value(by_num[2200], accounts) == 0  # roll-up of 2210
    assert balance_sheet.counted_value(by_num[2100], accounts) == 5_000  # own balance
    # "1200 - Accounts Receivable" sits inside the unnumbered "Accounts Receivable"
    # group: it is a leaf, not the parent of 1210.
    assert by_num[1200].children == []
    assert balance_sheet.counted_value(by_num[1200], accounts) == 900_000


def test_name_rule_beats_number_range_for_liabilities():
    wc = classify(flat_report())
    # 2400 is in the 2000-2699 range but named "Long-term": non-current.
    assert wc.noncurrent_liabilities == 500_000 + 350_000


def test_account_type_takes_precedence_over_ranges():
    types = {
        1700: "Asset Other Current Asset",      # range says non-current; type says current
        1300: "Asset Other Asset",              # range says current; type says non-current
        2300: "Liability Long Term Liability",  # range says current; type says non-current
        2000: "Liability Accounts Payable",
    }
    wc = classify(flat_report(), types)
    assert wc.current_assets == CURRENT_ASSETS + 400_000 - 150_000
    assert wc.noncurrent_assets == NONCURRENT_ASSETS - 400_000 + 150_000
    assert wc.current_liabilities == CURRENT_LIABS - 700_000
    assert wc.noncurrent_liabilities == NONCURRENT_LIABS + 700_000


def test_name_rule_beats_account_type():
    wc = classify(flat_report(), {2800: "Liability Other Current Liability"})
    assert wc.current_liabilities == CURRENT_LIABS


def test_generic_types_fall_back_to_ranges():
    types = {n: "Asset" for n in (1000, 1300, 1700)} | {n: "Liability" for n in (2000, 2800)}
    wc = classify(flat_report(), types)
    assert (wc.current_assets, wc.current_liabilities) == (CURRENT_ASSETS, CURRENT_LIABS)


def test_negative_liability_convention_is_normalised():
    report = flat_report()
    for row in report["rows"]:
        num = balance_sheet.ACCOUNT_RE.match(row["name"])
        is_liab = (num and num.group(1).startswith("2")) or row["name"] in ("Total for Liability",)
        if is_liab and "values" in row:
            row["values"] = [-row["values"][0]]
    wc = classify(report)
    assert wc.current_liabilities == CURRENT_LIABS


def test_classification_fails_without_numbered_accounts():
    report = {"columns": COLUMNS, "rows": [
        {"name": "Cash", "values": [100]},
        {"name": "Total for Asset", "values": [100]},
    ]}
    assert classify(report) is None


def _fake_api(balance_report, accounts=None):
    from tests.test_app import BILLS, INCOME, INVOICES

    def fake_api_get(customer_id, path, params=None):
        if path in ("/coa/api/v1/account", "/coa/api/account"):
            if accounts is None:
                raise campfire.CampfireError(f"{path}: HTTP 404")
            return {"results": accounts, "next": None}
        return {
            "/ca/api/get_income_statement": INCOME,
            "/ca/api/get_balance_sheet": balance_report,
            "/coa/api/v1/invoice": INVOICES,
            "/coa/api/v1/bill": BILLS,
        }[path]

    return fake_api_get


@pytest.fixture()
def fixed_months(monkeypatch):
    monkeypatch.setattr(campfire, "month_labels", lambda today, n: [f"2025-{m:02d}" for m in range(11, 13)]
                        + [f"2026-{m:02d}" for m in range(1, 11)])


def test_live_dataset_uses_classification_without_banner(monkeypatch, fixed_months):
    monkeypatch.setattr(campfire, "api_get", _fake_api(flat_report()))
    data = campfire.build_live_dataset("cust")
    assert data["current_assets"] == CURRENT_ASSETS
    assert data["current_liabilities"] == CURRENT_LIABS
    assert not any("current assets" in w for w in data["warnings"])


def test_live_dataset_reads_account_types(monkeypatch, fixed_months):
    accounts = [{"number": "1700", "name": "Right-of-Use Asset", "account_type": "Other Current Asset"}]
    monkeypatch.setattr(campfire, "api_get", _fake_api(flat_report(), accounts))
    data = campfire.build_live_dataset("cust")
    assert data["current_assets"] == CURRENT_ASSETS + 400_000


def test_live_dataset_banner_only_when_classification_fails(monkeypatch, fixed_months):
    bad = {"columns": COLUMNS, "rows": [{"name": "Cash", "values": [100]}]}
    monkeypatch.setattr(campfire, "api_get", _fake_api(bad))
    data = campfire.build_live_dataset("cust")
    assert any("current assets/liabilities" in w for w in data["warnings"])


def test_leaf_named_current_assets_is_not_taken_as_total(monkeypatch, fixed_months):
    report = flat_report()
    report["rows"].insert(2, {"name": "1350 - Other Current Assets", "values": [10_000]})
    monkeypatch.setattr(campfire, "api_get", _fake_api(report))
    data = campfire.build_live_dataset("cust")
    assert data["current_assets"] == CURRENT_ASSETS + 10_000


def test_manifest_has_no_category():
    with open("manifest.json") as f:
        assert "category" not in json.load(f)["app"]
