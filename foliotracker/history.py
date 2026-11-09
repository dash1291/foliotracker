from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import NetWorthHistory


def record(session: Session, total: Decimal, statement_total: Decimal, by_class: dict[str, Decimal],
           by_wrapper: dict[str, Decimal], on: date | None = None) -> NetWorthHistory:
    on = on or date.today()
    row = session.scalar(select(NetWorthHistory).where(NetWorthHistory.date == on))
    if row is None:
        row = NetWorthHistory(date=on)
        session.add(row)
    row.total = total
    row.statement_total = statement_total
    row.by_class = json.dumps({k: str(v) for k, v in by_class.items()})
    row.by_wrapper = json.dumps({k: str(v) for k, v in by_wrapper.items()})
    row.recorded_at = datetime.now()
    session.commit()
    return row


def series(session: Session) -> list[NetWorthHistory]:
    rows = session.scalars(select(NetWorthHistory).order_by(NetWorthHistory.date)).all()
    for r in rows:
        r.classes = {k: Decimal(v) for k, v in json.loads(r.by_class).items()}
    return rows


# --- growth from statement points -------------------------------------------------

from dataclasses import dataclass, field as _field

from .db import Account, ImportRun, Position
from .manual import deposit_value


@dataclass
class Point:
    date: date
    positions: dict[tuple[int, int], tuple[Decimal, Decimal, Decimal]]  # (account, instrument) -> (qty, price, value)
    deposits: dict[int, Decimal]  # account id -> accrued value
    balances: dict[int, Decimal]  # account id -> snapshot balance
    label: str = "statement"

    @property
    def total(self) -> Decimal:
        return (sum((v for _, _, v in self.positions.values()), Decimal(0))
                + sum(self.deposits.values(), Decimal(0)) + sum(self.balances.values(), Decimal(0)))


@dataclass
class Change:
    start: date
    end: date
    start_total: Decimal
    end_total: Decimal
    new_money: Decimal = Decimal(0)
    gains: Decimal = Decimal(0)
    unsplit: Decimal = Decimal(0)

    @property
    def change(self) -> Decimal:
        return self.end_total - self.start_total

    @property
    def return_pct(self) -> Decimal | None:
        base = self.start_total + self.new_money / 2
        return (self.gains / base * 100).quantize(Decimal("0.01")) if base else None


def _manual_as_of(session: Session, on: date) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    deposits, balances = {}, {}
    for acct in session.scalars(select(Account).where(Account.manual.is_(True))):
        if acct.deposit:
            if acct.deposit.start_date <= on:
                deposits[acct.id] = deposit_value(acct.deposit, on)
        else:
            snaps = [s for s in acct.snapshots if s.date <= on]
            if snaps:
                balances[acct.id] = max(snaps, key=lambda s: s.date).value
    return deposits, balances


def statement_points(session: Session) -> list[Point]:
    """One point per full statement run (per period end), valued at statement prices,
    plus manual accounts as they stood on that date."""
    runs = session.scalars(
        select(ImportRun).where(ImportRun.covers.is_(None)).order_by(ImportRun.period_to, ImportRun.id)
    ).all()
    by_date: dict[date, list[ImportRun]] = {}
    for r in runs:
        by_date.setdefault(r.period_to, []).append(r)
    points = []
    for on, group in sorted(by_date.items()):
        positions = {}
        for run in group:
            for p in run.positions:
                positions[(p.account_id, p.instrument_id)] = (p.quantity, p.price or Decimal(0), p.value)
        deposits, balances = _manual_as_of(session, on)
        points.append(Point(on, positions, deposits, balances))
    return points


def current_point(session: Session, groups, manuals) -> Point:
    positions = {}
    for g in groups:
        for p in g.positions:
            positions[(p.account_id, p.instrument_id)] = (p.quantity, p.current_price or p.price or Decimal(0), p.current_value)
    deposits = {m.account.id: m.value for m in manuals if m.account.deposit}
    balances = {m.account.id: m.value for m in manuals if not m.account.deposit}
    return Point(date.today(), positions, deposits, balances, label="today")


def decompose(a: Point, b: Point) -> Change:
    c = Change(a.date, b.date, a.total, b.total)
    for key in set(a.positions) | set(b.positions):
        qa, pa, va = a.positions.get(key, (Decimal(0), Decimal(0), Decimal(0)))
        qb, pb, vb = b.positions.get(key, (Decimal(0), Decimal(0), Decimal(0)))
        if key in a.positions and key in b.positions and qa and qb:
            c.gains += qa * (pb - pa)
            c.new_money += (qb - qa) * pb
            c.gains += (vb - va) - (qa * (pb - pa) + (qb - qa) * pb)
        else:
            c.new_money += vb - va
    for acct_id in set(a.deposits) | set(b.deposits):
        da, db = a.deposits.get(acct_id), b.deposits.get(acct_id)
        if da is not None and db is not None:
            c.gains += db - da
        else:
            c.new_money += (db or Decimal(0)) - (da or Decimal(0))
    for acct_id in set(a.balances) | set(b.balances):
        c.unsplit += b.balances.get(acct_id, Decimal(0)) - a.balances.get(acct_id, Decimal(0))
    return c


def _sum(changes: list[Change]) -> Change | None:
    if not changes:
        return None
    total = Change(changes[0].start, changes[-1].end, changes[0].start_total, changes[-1].end_total)
    for ch in changes:
        total.new_money += ch.new_money
        total.gains += ch.gains
        total.unsplit += ch.unsplit
    return total


def fy_label(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY {start}-{str(start + 1)[2:]}"


def growth_by_year(points: list[Point]) -> list[tuple[str, Change]]:
    """Consecutive point-to-point changes bucketed by the financial year (Apr-Mar) of the
    period end. A period that straddles 31 March counts in the year it ends."""
    if len(points) < 2:
        return []
    steps = [decompose(points[i], points[i + 1]) for i in range(len(points) - 1)]
    buckets: dict[str, list[Change]] = {}
    for st in steps:
        buckets.setdefault(fy_label(st.end), []).append(st)
    return [(label, _sum(chs)) for label, chs in buckets.items()]


def trailing(points: list[Point], days: int = 365) -> Change | None:
    if len(points) < 2:
        return None
    cutoff = date.fromordinal(points[-1].date.toordinal() - days)
    start = min(points[:-1], key=lambda p: abs((p.date - cutoff).days))
    idx = points.index(start)
    return _sum([decompose(points[i], points[i + 1]) for i in range(idx, len(points) - 1)])


def backfill(session: Session, points: list[Point]) -> int:
    added = 0
    for p in points:
        if session.scalar(select(NetWorthHistory).where(NetWorthHistory.date == p.date)) is None:
            session.add(NetWorthHistory(
                date=p.date, total=p.total, statement_total=sum((v for _, _, v in p.positions.values()), Decimal(0)),
                by_class="{}", by_wrapper="{}", recorded_at=datetime.now(),
            ))
            added += 1
    session.commit()
    return added
