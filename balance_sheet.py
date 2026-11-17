"""Current vs. non-current classification for Campfire balance sheets.

Campfire's balance sheet has no "Total Current Assets" / "Total Current
Liabilities" rows. It lists accounts as "<number> - <name>", grouped under
parents with "Total for <group>" rows, and only top-level "Total for Asset" /
"Total for Liability". So we sum leaf accounts ourselves:

1. Find account rows ("1210 - Intercompany AR") and their children, either from
   the report's nesting or from "Total for X" rows that close a group in a flat
   list. "Total for" rows are never summed.
2. A parent account row is counted only when its value is its own balance, not a
   roll-up of its children (i.e. it differs from the sum of its direct children).
3. Classify each counted account, in order of precedence:
   a. name says "non-current" / "long-term"  -> non-current
   b. account type/subtype from the accounts endpoint, when it says which
   c. account number ranges:
        assets       1000-1499 current, 1500-1599 non-current (PP&E incl.
                     accumulated depreciation), 1600-1699 current (unbilled AR),
                     1700-1999 non-current (ROU and other long-term)
        liabilities  2000-2699 current, 2700-2999 non-current
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ACCOUNT_RE = re.compile(r"^\s*(\d{3,6})(?:\.\d+)?\s*[-–—:]\s*(.+?)\s*$")
TOTAL_RE = re.compile(r"^\s*total\s+(?:for\s+)?(.+?)\s*$", re.IGNORECASE)
NONCURRENT_NAME_RE = re.compile(r"non[\s-]?current|long[\s-]?term", re.IGNORECASE)
TOLERANCE = 0.5  # dollars


@dataclass
class Entry:
    """One labelled row of a report, in document order."""
    label: str
    values: dict[str, float]
    parent: int | None  # index of the nearest labelled ancestor, if nested


@dataclass
class Account:
    index: int
    number: int
    name: str
    value: float
    children: list[int] = field(default_factory=list)  # indexes of direct child accounts


@dataclass
class WorkingCapital:
    current_assets: float
    current_liabilities: float
    noncurrent_assets: float
    noncurrent_liabilities: float
    total_assets: float
    total_liabilities: float
    unclassified: list[str]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def find_accounts(entries: list[Entry], month: str) -> dict[int, Account]:
    accounts: dict[int, Account] = {}
    for i, e in enumerate(entries):
        if TOTAL_RE.match(e.label):
            continue
        m = ACCOUNT_RE.match(e.label)
        if m:
            accounts[i] = Account(i, int(m.group(1)), m.group(2), e.values.get(month, 0.0))

    def nearest_account_ancestor(i: int) -> int | None:
        p = entries[i].parent
        while p is not None:
            if p in accounts:
                return p
            p = entries[p].parent
        return None

    parent_of: dict[int, int] = {}

    # Nested layout: an account's parent is its nearest account ancestor.
    for i in accounts:
        p = nearest_account_ancestor(i)
        if p is not None:
            parent_of[i] = p

    # Flat layout: "Total for X" closes the group opened by the latest row named X;
    # accounts in between with no parent yet belong to X (innermost groups close first).
    for t, e in enumerate(entries):
        m = TOTAL_RE.match(e.label)
        if not m:
            continue
        name = _norm(m.group(1))
        # The group's opener is the EARLIEST row named X since the previous
        # "Total for X" (a child account may share the group's name, e.g. an
        # "Accounts Receivable" header containing "1200 - Accounts Receivable").
        opener = None
        for j in range(t - 1, -1, -1):
            tm = TOTAL_RE.match(entries[j].label)
            if tm:
                if _norm(tm.group(1)) == name:
                    break
                continue
            acct = accounts.get(j)
            if (acct and _norm(acct.name) == name) or _norm(entries[j].label) == name:
                opener = j
        if opener is not None and opener in accounts:
            for k in range(opener + 1, t):
                if k in accounts and k not in parent_of:
                    parent_of[k] = opener

    for child, parent in parent_of.items():
        accounts[parent].children.append(child)
    return accounts


def counted_value(acct: Account, accounts: dict[int, Account]) -> float:
    """A leaf's value, or a parent's own balance (0 if its row is a roll-up).

    A parent row is a roll-up when it equals everything beneath it (the subtree
    total of its children) or the raw sum of its direct child rows.
    """
    if not acct.children:
        return acct.value
    below = sum(subtree_total(accounts[c], accounts) for c in acct.children)
    direct = sum(accounts[c].value for c in acct.children)
    if abs(acct.value - below) <= TOLERANCE or abs(acct.value - direct) <= TOLERANCE:
        return 0.0
    return acct.value


def subtree_total(acct: Account, accounts: dict[int, Account]) -> float:
    return counted_value(acct, accounts) + sum(subtree_total(accounts[c], accounts) for c in acct.children)


def classify_by_type(type_text: str) -> str | None:
    t = _norm(type_text)
    if not t:
        return None
    if NONCURRENT_NAME_RE.search(t) or any(k in t for k in (
        "fixed asset", "property", "equipment", "accumulated depreciation",
        "right of use", "right-of-use", "intangible", "goodwill",
    )) or re.search(r"\bother assets?\b", t):
        return "noncurrent"
    if "current" in t or any(k in t for k in (
        "bank", "cash", "receivable", "payable", "credit card", "inventory", "prepaid",
    )):
        return "current"
    return None


def side_of(number: int, type_text: str = "") -> str | None:
    t = _norm(type_text)
    if "liabilit" in t or "payable" in t or "credit card" in t:
        return "liability"
    if "asset" in t or "receivable" in t or "bank" in t:
        return "asset"
    if 1000 <= number <= 1999:
        return "asset"
    if 2000 <= number <= 2999:
        return "liability"
    return None


def classify_by_number(number: int, side: str) -> str | None:
    if side == "asset" and 1000 <= number <= 1999:
        return "current" if number <= 1499 or 1600 <= number <= 1699 else "noncurrent"
    if side == "liability" and 2000 <= number <= 2999:
        return "current" if number <= 2699 else "noncurrent"
    return None


def classify(entries: list[Entry], month: str, account_types: dict[int, str] | None = None) -> WorkingCapital | None:
    """Return current/non-current totals, or None if classification fails."""
    account_types = account_types or {}
    accounts = find_accounts(entries, month)
    totals = {("asset", "current"): 0.0, ("asset", "noncurrent"): 0.0,
              ("liability", "current"): 0.0, ("liability", "noncurrent"): 0.0}
    found = {k: False for k in totals}
    unclassified: list[str] = []

    for acct in accounts.values():
        type_text = account_types.get(acct.number, "")
        side = side_of(acct.number, type_text)
        if side is None:
            continue  # equity, income, etc.
        if NONCURRENT_NAME_RE.search(acct.name):
            term = "noncurrent"
        else:
            term = classify_by_type(type_text) or classify_by_number(acct.number, side)
        if term is None:
            unclassified.append(f"{acct.number} - {acct.name}")
            continue
        totals[(side, term)] += counted_value(acct, accounts)
        found[(side, term)] = True

    if not (found[("asset", "current")] and found[("liability", "current")]):
        return None

    # Liabilities may be reported as negative balances; present them as positive.
    liab_total = totals[("liability", "current")] + totals[("liability", "noncurrent")]
    sign = -1.0 if liab_total < 0 else 1.0

    result = WorkingCapital(
        current_assets=totals[("asset", "current")],
        current_liabilities=sign * totals[("liability", "current")],
        noncurrent_assets=totals[("asset", "noncurrent")],
        noncurrent_liabilities=sign * totals[("liability", "noncurrent")],
        total_assets=totals[("asset", "current")] + totals[("asset", "noncurrent")],
        total_liabilities=sign * liab_total,
        unclassified=unclassified,
    )
    _cross_check(entries, month, result)
    return result


def _cross_check(entries: list[Entry], month: str, wc: WorkingCapital) -> None:
    """Log (not show) a mismatch with the report's own "Total for Asset/Liability" rows."""
    for e in entries:
        m = TOTAL_RE.match(e.label)
        if not m or month not in e.values:
            continue
        name = _norm(m.group(1))
        ours = {"asset": wc.total_assets, "assets": wc.total_assets,
                "liability": wc.total_liabilities, "liabilities": wc.total_liabilities}.get(name)
        if ours is not None and abs(abs(e.values[month]) - abs(ours)) > 1.0:
            log.warning("Balance sheet check: %s is %.2f but classified accounts sum to %.2f",
                        e.label, e.values[month], ours)
    if wc.unclassified:
        log.warning("Balance sheet accounts not classified: %s", ", ".join(wc.unclassified))


# --- Structured rows (the real /ca/api/get_balance_sheet shape) -------------
#
# The live endpoint returns {"balance_sheet": [row, ...]} where each row has
# separate fields: account_id, account_number, account_name, account_type
# (ASSET / LIABILITY / EQUITY), account_subtype (BANK, FIXED_ASSET, ...),
# parent (an account_id, or a section name like "ASSET"), and period values
# keyed "<YYYY-MM-DD>_<column>" (e.g. "2026-10-01_Total"). A parent row's value
# is the roll-up of itself plus its children.

CURRENT_SUBTYPES = {
    "BANK", "CASH", "ACCOUNTS_RECEIVABLE", "PREPAID", "OTHER_CURRENT_ASSET", "INVENTORY",
    "ACCOUNTS_PAYABLE", "CREDIT_CARD", "OTHER_CURRENT_LIABILITY", "DEFERRED_REVENUE",
    "ACCRUED_LIABILITY", "ACCRUED_EXPENSES",
}
NONCURRENT_SUBTYPES = {
    "FIXED_ASSET", "OTHER_ASSET", "INTANGIBLE_ASSET", "RIGHT_OF_USE_ASSET",
    "LONG_TERM_LIABILITY", "OTHER_LIABILITY",
}


def structured_rows(report) -> list[dict] | None:
    rows = report.get("balance_sheet") if isinstance(report, dict) else None
    if not isinstance(rows, list) or not any(
        isinstance(r, dict) and "account_subtype" in r for r in rows
    ):
        return None
    return [r for r in rows if isinstance(r, dict)]


def row_value(row: dict, month: str) -> float:
    """Value for the period starting in `month`; prefers the "_Total" column."""
    total, parts = None, 0.0
    for key, val in row.items():
        if not str(key).startswith(month) or not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        if str(key).lower().endswith("_total"):
            total = float(val)
        else:
            parts += float(val)
    return total if total is not None else parts


def _term_for(number: int | None, name: str, subtype: str, side: str) -> str | None:
    if NONCURRENT_NAME_RE.search(name):
        return "noncurrent"
    st = subtype.upper()
    if st in NONCURRENT_SUBTYPES:
        return "noncurrent"
    if st in CURRENT_SUBTYPES:
        return "current"
    term = classify_by_type(st.replace("_", " "))
    if term is None and number is not None:
        term = classify_by_number(number, side)
    return term


def classify_structured(report, month: str) -> WorkingCapital | None:
    """Current/non-current totals from the structured balance sheet, or None."""
    rows = structured_rows(report)
    if rows is None:
        return None
    accounts = [r for r in rows if r.get("account_number") not in (None, "")
                and str(r.get("account_type", "")).upper() in {"ASSET", "LIABILITY"}]
    ids = {str(r.get("account_id")) for r in accounts}
    child_sum: dict[str, float] = {}
    for r in accounts:
        p = str(r.get("parent"))
        if p in ids:
            child_sum[p] = child_sum.get(p, 0.0) + row_value(r, month)

    totals = {("asset", "current"): 0.0, ("asset", "noncurrent"): 0.0,
              ("liability", "current"): 0.0, ("liability", "noncurrent"): 0.0}
    found = {k: False for k in totals}
    unclassified: list[str] = []
    for r in accounts:
        side = "asset" if str(r["account_type"]).upper() == "ASSET" else "liability"
        name = str(r.get("account_name") or "")
        try:
            number = int(str(r["account_number"]).split(".")[0])
        except ValueError:
            number = None
        # A parent's own balance = its roll-up minus its children's roll-ups.
        own = row_value(r, month) - child_sum.get(str(r.get("account_id")), 0.0)
        term = _term_for(number, name, str(r.get("account_subtype") or ""), side)
        if term is None:
            unclassified.append(f"{r['account_number']} - {name}")
            continue
        totals[(side, term)] += own
        found[(side, term)] = True

    if not (found[("asset", "current")] and found[("liability", "current")]):
        return None
    liab_total = totals[("liability", "current")] + totals[("liability", "noncurrent")]
    sign = -1.0 if liab_total < 0 else 1.0
    wc = WorkingCapital(
        current_assets=totals[("asset", "current")],
        current_liabilities=sign * totals[("liability", "current")],
        noncurrent_assets=totals[("asset", "noncurrent")],
        noncurrent_liabilities=sign * totals[("liability", "noncurrent")],
        total_assets=totals[("asset", "current")] + totals[("asset", "noncurrent")],
        total_liabilities=sign * liab_total,
        unclassified=unclassified,
    )
    for r in rows:  # cross-check against the report's own section totals
        if r.get("account_number") in (None, "") and str(r.get("account_name", "")).upper() in {"ASSET", "LIABILITY"}:
            ours = wc.total_assets if r["account_name"].upper() == "ASSET" else wc.total_liabilities
            if abs(abs(row_value(r, month)) - abs(ours)) > 1.0:
                log.warning("Balance sheet check: %s is %.2f but classified accounts sum to %.2f",
                            r["account_name"], row_value(r, month), ours)
    if unclassified:
        log.warning("Balance sheet accounts not classified: %s", ", ".join(unclassified))
    return wc
