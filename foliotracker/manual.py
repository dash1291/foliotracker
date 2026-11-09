from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Account, Deposit, Snapshot

MANUAL_WRAPPERS = {
    "fd": "Fixed deposit",
    "bank": "Cash and bank",
    "ppf": "PPF",
    "epf": "EPF",
    "nps": "NPS",
    "sgb": "Sovereign gold bond",
    "other": "Other",
}
DEFAULT_CLASS = {"fd": "debt", "bank": "cash", "ppf": "debt", "epf": "debt", "nps": "debt", "sgb": "gold", "other": "other"}
COMPOUNDING = {
    "quarterly": "Cumulative, compounded quarterly",
    "monthly": "Cumulative, compounded monthly",
    "half_yearly": "Cumulative, compounded half-yearly",
    "annual": "Cumulative, compounded yearly",
    "simple": "Cumulative, simple interest",
    "payout_monthly": "Non-cumulative, interest paid monthly",
    "payout_quarterly": "Non-cumulative, interest paid quarterly",
    "payout_half_yearly": "Non-cumulative, interest paid half-yearly",
    "payout_annual": "Non-cumulative, interest paid yearly",
}
LEGACY_LABELS = {"payout": "Non-cumulative, payout frequency not set"}
PERIODS = {"monthly": 12, "quarterly": 4, "half_yearly": 2, "annual": 1}


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last_day))


def last_payout_before(start: date, on: date, periods_per_year: int) -> date:
    step = 12 // periods_per_year
    last = start
    while _add_months(last, step) <= on:
        last = _add_months(last, step)
    return last


def deposit_value(dep: Deposit, on: date) -> Decimal:
    on = min(on, dep.maturity_date)
    if on <= dep.start_date:
        return dep.principal
    rate = dep.rate / Decimal(100)
    if dep.compounding == "payout":
        return dep.principal
    if dep.compounding.startswith("payout_"):
        n = PERIODS[dep.compounding.removeprefix("payout_")]
        since = last_payout_before(dep.start_date, on, n)
        accrued = dep.principal * rate * Decimal((on - since).days) / Decimal(365)
        return (dep.principal + accrued).quantize(Decimal("0.01"))
    years = Decimal((on - dep.start_date).days) / Decimal(365)
    if dep.compounding == "simple":
        factor = 1 + rate * years
    else:
        n = PERIODS[dep.compounding]
        factor = Decimal(str((1 + float(rate) / n) ** (n * float(years))))
    return (dep.principal * factor).quantize(Decimal("0.01"))


@dataclass
class ManualHolding:
    account: Account
    as_of: date
    value: Decimal
    detail: str
    matured: bool = False


def manual_holdings(session: Session, today: date | None = None) -> list[ManualHolding]:
    today = today or date.today()
    result = []
    for acct in session.scalars(select(Account).where(Account.manual.is_(True)).order_by(Account.wrapper, Account.label)):
        if acct.deposit:
            dep = acct.deposit
            value = deposit_value(dep, today)
            matured = today >= dep.maturity_date
            detail = (
                f"{dep.rate}% {(COMPOUNDING.get(dep.compounding) or LEGACY_LABELS.get(dep.compounding, dep.compounding)).lower()}, "
                f"{'matured' if matured else 'matures'} {dep.maturity_date:%d %b %Y}"
            )
            result.append(ManualHolding(acct, min(today, dep.maturity_date), value, detail, matured))
        else:
            snap = max(acct.snapshots, key=lambda s: s.date, default=None)
            if snap is None:
                result.append(ManualHolding(acct, today, Decimal(0), "no balance entered yet"))
            else:
                result.append(ManualHolding(acct, snap.date, snap.value, snap.note or ""))
    return result


def create_account(session: Session, wrapper: str, label: str, institution: str | None, holders: str | None,
                   asset_class: str | None) -> Account:
    if wrapper != "other" or not asset_class:
        asset_class = DEFAULT_CLASS.get(wrapper)
    acct = Account(
        wrapper=wrapper, identifier=f"manual-{uuid.uuid4().hex[:8]}", label=label, institution=institution or None,
        holders=holders or None, asset_class=asset_class, manual=True,
    )
    session.add(acct)
    session.flush()
    return acct


def add_snapshot(session: Session, acct: Account, on: date, value: Decimal, note: str | None) -> Snapshot:
    existing = session.scalar(select(Snapshot).where(Snapshot.account_id == acct.id, Snapshot.date == on))
    if existing:
        existing.value, existing.note = value, note or None
        return existing
    snap = Snapshot(account_id=acct.id, date=on, value=value, note=note or None)
    session.add(snap)
    return snap
