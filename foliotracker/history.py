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
