"""Derived CEO metrics, computed the same way for demo and live data."""

from __future__ import annotations

from datetime import date

AGING_BUCKETS = ["Current", "1-30", "31-60", "61-90", "90+"]


def month_labels(today: date, count: int) -> list[str]:
    """The `count` most recent fully closed months, oldest first, as YYYY-MM."""
    year, month = today.year, today.month
    labels = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        labels.append(f"{year:04d}-{month:02d}")
    return list(reversed(labels))


def _safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def _pct_change(new: float, old: float) -> float | None:
    return (new - old) / abs(old) if old else None


def aging(docs: list[dict]) -> dict[str, float]:
    buckets = dict.fromkeys(AGING_BUCKETS, 0.0)
    for d in docs:
        days = d.get("days_overdue") or 0
        if days <= 0:
            key = "Current"
        elif days <= 30:
            key = "1-30"
        elif days <= 60:
            key = "31-60"
        elif days <= 90:
            key = "61-90"
        else:
            key = "90+"
        buckets[key] += d.get("amount_due") or 0.0
    return buckets


def compute(data: dict) -> dict:
    rev, cogs, opex, ni, cash = (
        data["revenue"], data["cogs"], data["opex"], data["net_income"], data["cash"],
    )
    n = len(rev)
    gross_profit = [r - c for r, c in zip(rev, cogs)]
    gross_margin = [_safe_div(g, r) for g, r in zip(gross_profit, rev)]
    net_margin = [_safe_div(x, r) for x, r in zip(ni, rev)]
    total_expenses = [c + o for c, o in zip(cogs, opex)]

    # Net burn = month-over-month decrease in cash (positive means burning).
    burn = [None] + [cash[i - 1] - cash[i] for i in range(1, len(cash))]
    recent_burn = [b for b in burn[-3:] if b is not None]
    avg_burn = sum(recent_burn) / len(recent_burn) if recent_burn else 0.0
    cash_now = cash[-1] if cash else 0.0
    runway_months = cash_now / avg_burn if avg_burn > 0 else None

    # Open invoice/bill detail (as of today) drives aging and overdue figures.
    ar_open = sum(d["amount_due"] for d in data["open_invoices"])
    ap_open = sum(d["amount_due"] for d in data["open_bills"])
    # Headline balances tie to the Balance Sheet (GL) when available.
    ar_total = data["ar_balance"] if data.get("ar_balance") is not None else ar_open
    ap_total = data["ap_balance"] if data.get("ap_balance") is not None else ap_open
    ar_overdue = sum(d["amount_due"] for d in data["open_invoices"] if (d.get("days_overdue") or 0) > 0)
    ap_due_30 = sum(d["amount_due"] for d in data["open_bills"] if (d.get("days_overdue") or 0) > -30)

    last3_rev = sum(rev[-3:])
    last3_spend = sum(total_expenses[-3:])
    dso = _safe_div(ar_total, last3_rev / 90) if last3_rev else None
    dpo = _safe_div(ap_total, last3_spend / 90) if last3_spend else None

    ttm_rev = sum(rev)
    ttm_ni = sum(ni)

    return {
        "months": data["months"],
        "series": {
            "revenue": rev,
            "cogs": cogs,
            "opex": opex,
            "total_expenses": total_expenses,
            "gross_profit": gross_profit,
            "gross_margin": gross_margin,
            "net_income": ni,
            "net_margin": net_margin,
            "cash": cash,
            "burn": burn,
        },
        "kpi": {
            "revenue_last": rev[-1] if n else 0.0,
            "revenue_mom": _pct_change(rev[-1], rev[-2]) if n >= 2 else None,
            "revenue_ttm": ttm_rev,
            "revenue_growth_3m": _pct_change(sum(rev[-3:]), sum(rev[-6:-3])) if n >= 6 else None,
            "gross_margin_last": gross_margin[-1] if n else None,
            "gross_margin_ttm": _safe_div(sum(gross_profit), ttm_rev),
            "net_income_last": ni[-1] if n else 0.0,
            "net_income_ttm": ttm_ni,
            "net_margin_ttm": _safe_div(ttm_ni, ttm_rev),
            "opex_last": opex[-1] if n else 0.0,
            "cash": cash_now,
            "cash_change_3m": (cash_now - cash[-4]) if len(cash) >= 4 else None,
            "avg_burn_3m": avg_burn,
            "runway_months": runway_months,
            "ar_total": ar_total,
            "ar_overdue": ar_overdue,
            "ar_overdue_pct": _safe_div(ar_overdue, ar_open),
            "ar_open": ar_open,
            "ap_open": ap_open,
            "ap_total": ap_total,
            "ap_due_30": ap_due_30,
            "dso": dso,
            "dpo": dpo,
            "current_ratio": _safe_div(data["current_assets"], data["current_liabilities"]),
            "working_capital": data["current_assets"] - data["current_liabilities"],
            "current_assets": data["current_assets"],
            "current_liabilities": data["current_liabilities"],
        },
        "ar_aging": aging(data["open_invoices"]),
        "ap_aging": aging(data["open_bills"]),
        "open_invoices": sorted(data["open_invoices"], key=lambda d: -(d.get("days_overdue") or 0)),
        "open_bills": sorted(data["open_bills"], key=lambda d: d.get("due_date") or ""),
        "source": data["source"],
        "as_of": data["as_of"],
        "warnings": data.get("warnings", []),
    }
