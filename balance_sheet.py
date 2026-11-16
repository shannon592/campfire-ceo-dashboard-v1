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
