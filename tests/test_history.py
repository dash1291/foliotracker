from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from foliotracker import history
from foliotracker.db import Base


def test_one_row_per_day_latest_value_wins():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        history.record(s, Decimal("100"), Decimal("90"), {"debt": Decimal("100")}, {"Bonds": Decimal("100")}, date(2026, 11, 8))
        history.record(s, Decimal("110"), Decimal("90"), {"debt": Decimal("110")}, {"Bonds": Decimal("110")}, date(2026, 11, 9))
        history.record(s, Decimal("120"), Decimal("95"), {"debt": Decimal("120")}, {"Bonds": Decimal("120")}, date(2026, 11, 9))
        rows = history.series(s)
        assert [(r.date.isoformat(), str(r.total)) for r in rows] == [("2026-11-08", "100"), ("2026-11-09", "120")]
        assert rows[1].classes == {"debt": Decimal("120")}
        assert str(rows[1].statement_total) == "95"
