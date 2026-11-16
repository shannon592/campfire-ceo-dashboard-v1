"""Sample data for demo mode.

Numbers are invented but internally consistent: a growing company with ~70%
gross margin that is still burning cash. The shape matches what campfire.py
builds from live data, so every page renders the same way in both modes.
"""

from __future__ import annotations

from datetime import date, timedelta

from metrics import month_labels


def build_demo_dataset(today: date | None = None) -> dict:
    today = today or date.today()
    months = month_labels(today, 12)

    revenue, cogs, opex = [], [], []
    for i in range(12):
        rev = round(1_620_000 * (1.032 ** i) + (35_000 if i % 3 == 2 else 0), -3)
        revenue.append(rev)
        cogs.append(round(rev * (0.305 - 0.002 * i), -3))
        opex.append(round(1_900_000 + 30_000 * i + (90_000 if i == 6 else 0), -3))
    net_income = [r - c - o for r, c, o in zip(revenue, cogs, opex)]

    # Month-end cash: net income plus working-capital and non-cash swing.
    swing = [40_000, -60_000, 25_000, -15_000, 55_000, -30_000,
             10_000, -45_000, 35_000, 20_000, -25_000, 15_000]
    cash = []
    balance = 21_600_000.0
    for ni, s in zip(net_income, swing):
        balance += ni + s
        cash.append(round(balance, -3))

    as_of = today.replace(day=1) - timedelta(days=1)

    open_invoices = [
        ("INV-2291", "Northwind Health", 184_500, 74),
        ("INV-2310", "Brightline Logistics", 96_200, 48),
        ("INV-2334", "Atlas Robotics", 142_000, 33),
        ("INV-2342", "Juniper Foods", 58_750, 19),
        ("INV-2350", "Keystone Media", 211_300, 6),
        ("INV-2361", "Halcyon Capital", 77_900, -4),
        ("INV-2366", "Riverbend Schools", 64_100, -12),
        ("INV-2371", "Orion Analytics", 305_000, -21),
        ("INV-2375", "Summit Dental Group", 41_250, -27),
        ("INV-2379", "Pioneer Energy", 268_400, -29),
        ("INV-2380", "Lumen Partners", 122_600, -30),
        ("INV-2384", "Cobalt Retail", 189_900, -30),
    ]
    open_bills = [
        ("BILL-8812", "Amazon Web Services", 711_500, -9),
        ("BILL-8815", "WeWork", 430_000, -14),
        ("BILL-8820", "Gusto", 192_000, -3),
        ("BILL-8798", "Snowflake", 323_750, 11),
        ("BILL-8801", "Salesforce", 261_000, 24),
        ("BILL-8823", "Cooley LLP", 239_000, -21),
        ("BILL-8826", "Datadog", 115_750, -25),
        ("BILL-8779", "Brex Travel", 63_000, 38),
        ("BILL-8830", "Figma", 47_000, -28),
    ]

    def doc(number, party, amount, days_overdue):
        return {
            "number": number,
            "party": party,
            "amount_due": float(amount),
            "due_date": (today - timedelta(days=days_overdue)).isoformat(),
            "days_overdue": days_overdue,
        }

    current_assets = cash[-1] + sum(a for _, _, a, _ in open_invoices) + 1_150_000
    current_liabilities = sum(a for _, _, a, _ in open_bills) + 2_850_000

    return {
        "source": "demo",
        "as_of": as_of.isoformat(),
        "months": months,
        "revenue": revenue,
        "cogs": cogs,
        "opex": opex,
        "net_income": net_income,
        "cash": cash,
        "current_assets": float(current_assets),
        "current_liabilities": float(current_liabilities),
        "open_invoices": [doc(*row) for row in open_invoices],
        "open_bills": [doc(*row) for row in open_bills],
        "warnings": [],
    }
