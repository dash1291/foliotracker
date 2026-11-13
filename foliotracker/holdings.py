from __future__ import annotations

from dataclasses import dataclass, field
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
    sources: list[ImportRun] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((p.value for p in self.positions), Decimal(0))

    @property
    def current_total(self) -> Decimal:
        return sum((p.current_value for p in self.positions), Decimal(0))

    @property
    def current_date(self) -> date:
        return max((p.current_date for p in self.positions), default=self.as_of)

    @property
    def has_overlay(self) -> bool:
        return len(self.sources) > 1


def _rank(run: ImportRun, as_of: date):
    return (as_of, run.file_type in RTA_SOURCES, run.id)


def resolve_positions(items: list[tuple[Position, ImportRun]]) -> tuple[list[Position], ImportRun, list[ImportRun]]:
    """A full statement run is the baseline for an account; later runs that cover only
    some instrument kinds replace those kinds and leave the rest from the baseline."""
    by_run: dict[int, tuple[ImportRun, date, list[Position]]] = {}
    for pos, run in items:
        entry = by_run.setdefault(run.id, (run, pos.as_of, []))
        by_run[run.id] = (run, max(entry[1], pos.as_of), entry[2])
        entry[2].append(pos)
    full = [(run, as_of) for run, as_of, _ in by_run.values() if run.covered_kinds is None]
    if full:
        base_run, base_date = max(full, key=lambda ra: _rank(ra[0], ra[1]))
    else:
        base_run, base_date = max(((r, d) for r, d, _ in by_run.values()), key=lambda ra: _rank(ra[0], ra[1]))
    positions = [p for p in by_run[base_run.id][2] if p.as_of == base_date]
    sources = [base_run]
    overlays = sorted(
        ((run, as_of, ps) for run, as_of, ps in by_run.values()
         if run.covered_kinds is not None and (as_of, run.id) > (base_date, base_run.id)),
        key=lambda t: (t[1], t[0].id),
    )
    newest_for_kind: dict[str, tuple[ImportRun, list[Position]]] = {}
    for run, _as_of, ps in overlays:
        for kind in run.covered_kinds:
            newest_for_kind[kind] = (run, ps)
    for kind, (run, ps) in newest_for_kind.items():
        positions = [p for p in positions if p.instrument.kind != kind]
        positions += [p for p in ps if p.instrument.kind == kind]
        if run not in sources:
            sources.append(run)
    return positions, base_run, sources


def closed_dates(rows: list[tuple[Position, ImportRun]], accounts: dict[int, Account]) -> dict[int, date]:
    """An account missing from the newest full statement of the source that used to list it
    is closed as of that statement's period end. A date set by hand on the account also closes
    it; positions dated before the closing date are stale, later ones stand."""
    full = [(pos, run) for pos, run in rows if run.covered_kinds is None]
    newest: dict[bool, ImportRun] = {}
    for _pos, run in full:
        latest = newest.get(run.is_rta)
        if latest is None or (run.period_to, run.id) > (latest.period_to, latest.id):
            newest[run.is_rta] = run
    listed = {(run.id, pos.account_id) for pos, run in full}
    closed: dict[int, date] = {}
    for pos, run in full:
        latest = newest[run.is_rta]
        if (latest.id, pos.account_id) not in listed:
            closed[pos.account_id] = max(closed.get(pos.account_id, latest.period_to), latest.period_to)
    for acct in accounts.values():
        if acct.closed_as_of:
            closed[acct.id] = max(closed.get(acct.id, acct.closed_as_of), acct.closed_as_of)
    return closed


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
    accounts = {a.id: a for a in session.scalars(select(Account).where(Account.id.in_(by_account)))}
    closed = closed_dates(rows, accounts)

    result = []
    for account_id, items in by_account.items():
        if account_id in closed:
            items = [(p, r) for p, r in items if p.as_of >= closed[account_id]]
        if not items:
            continue
        positions, base_run, sources = resolve_positions(items)
        positions = [p for p in positions if p.quantity]
        if not positions:
            continue
        as_of = max(p.as_of for p in positions)
        result.append(AccountHoldings(accounts[account_id], as_of, base_run, positions, sources))
    _attach_current(session, [p for h in result for p in h.positions])
    result.sort(key=lambda h: (h.account.wrapper, h.account.display_name))
    return result
