from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Account, ImportRun, Position, RTA_SOURCES, Valuation


@dataclass
class AccountHoldings:
    account: Account
    as_of: date
    import_run: ImportRun
    positions: list[Position]

    @property
    def total(self) -> Decimal:
        return sum((p.value for p in self.positions), Decimal(0))

    @property
    def current_total(self) -> Decimal:
        return sum((p.current_value for p in self.positions), Decimal(0))

    @property
    def current_date(self) -> date:
        return max((p.current_date for p in self.positions), default=self.as_of)


def _attach_current(session: Session, positions: list[Position]) -> None:
    ids = {p.instrument_id for p in positions}
    latest: dict[int, Valuation] = {}
    for v in session.scalars(select(Valuation).where(Valuation.instrument_id.in_(ids)).order_by(Valuation.date)):
        latest[v.instrument_id] = v
    for p in positions:
        v = latest.get(p.instrument_id)
        if v and v.date > p.as_of:
            p.current_price, p.current_date = v.price, v.date
            p.current_value = (p.quantity * v.price).quantize(Decimal("0.01"))
        else:
            p.current_price, p.current_date, p.current_value = None, p.as_of, p.value


def latest_holdings(session: Session) -> list[AccountHoldings]:
    rows = session.execute(
        select(Position, ImportRun)
        .join(ImportRun, Position.import_run_id == ImportRun.id)
        .order_by(Position.account_id)
    ).all()
    by_account: dict[int, list[tuple[Position, ImportRun]]] = {}
    for pos, run in rows:
        by_account.setdefault(pos.account_id, []).append((pos, run))

    result = []
    for account_id, items in by_account.items():
        def rank(item):
            pos, run = item
            return (pos.as_of, run.file_type in RTA_SOURCES, run.id)
        best_pos, best_run = max(items, key=rank)
        chosen = [p for p, r in items if r.id == best_run.id and p.as_of == best_pos.as_of]
        account = session.get(Account, account_id)
        result.append(AccountHoldings(account, best_pos.as_of, best_run, chosen))
    _attach_current(session, [p for h in result for p in h.positions])
    result.sort(key=lambda h: (h.account.wrapper, h.account.display_name))
    return result
