from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import amfi
from .db import Instrument, Valuation
from .importer import _clean


@dataclass
class RefreshResult:
    updated: int = 0
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)
    latest: date | None = None


def refresh_navs(session: Session, index: amfi.AmfiIndex | None = None) -> RefreshResult:
    index = index or amfi.load()
    result = RefreshResult()
    for inst in session.scalars(select(Instrument).where(Instrument.kind == "mf")):
        scheme = index.get(inst.isin)
        if scheme is None or not scheme.nav or scheme.nav in ("N.A.", "-"):
            result.missing.append(inst.name)
            continue
        inst.name, inst.amc, inst.category, inst.amfi_code = _clean(scheme.name), scheme.amc, scheme.category, scheme.code
        nav_date = datetime.strptime(scheme.nav_date, "%d-%b-%Y").date()
        existing = session.scalar(
            select(Valuation).where(Valuation.instrument_id == inst.id, Valuation.date == nav_date)
        )
        if existing:
            result.unchanged += 1
        else:
            session.add(Valuation(instrument_id=inst.id, date=nav_date, price=scheme.nav, source="amfi"))
            result.updated += 1
        result.latest = max(result.latest, nav_date) if result.latest else nav_date
    session.commit()
    return result


def latest_nav_date(session: Session) -> date | None:
    return session.scalar(select(Valuation.date).order_by(Valuation.date.desc()).limit(1))
