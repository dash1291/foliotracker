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


def _row(d, total, source="live"):
    from foliotracker.db import NetWorthHistory
    return NetWorthHistory(date=d, total=Decimal(total), statement_total=Decimal(0), by_class="{}", by_wrapper="{}", source=source)


def test_resample_month_daily_year_weekly_all_monthly():
    today = date(2026, 11, 10)
    rows = [
        _row(date(2024, 12, 31), 100, "statement"),
        _row(date(2025, 11, 30), 200, "statement"),
        _row(date(2026, 10, 31), 300, "statement"),
        _row(date(2026, 11, 3), 310),
        _row(date(2026, 11, 4), 320),
        _row(date(2026, 11, 9), 330),
        _row(date(2026, 11, 10), 340),
    ]
    out = history.resample(rows, today)
    assert [r.date.day for r in out["month"]] == [31, 3, 4, 9, 10]
    assert [str(r.total) for r in out["year"]] == ["200", "300", "320", "340"]
    assert [(r.date.year, r.date.month, str(r.total)) for r in out["all"]] == [
        (2024, 12, "100"), (2025, 11, "200"), (2026, 10, "300"), (2026, 11, "340")]


def test_backfill_recomputes_statement_rows_but_not_live_rows():
    from foliotracker.history import Point, backfill
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(_row(date(2026, 9, 30), 999))
        s.commit()
        p1 = Point(date(2026, 8, 31), {(1, 1): (Decimal(1), Decimal(100), Decimal(100))}, {}, {})
        p2 = Point(date(2026, 9, 30), {(1, 1): (Decimal(1), Decimal(110), Decimal(110))}, {}, {})
        assert backfill(s, [p1, p2]) == 1
        p1.balances = {7: Decimal(50)}
        backfill(s, [p1, p2])
        rows = {r.date: r for r in history.series(s)}
        assert str(rows[date(2026, 8, 31)].total) == "150" and rows[date(2026, 8, 31)].source == "statement"
        assert str(rows[date(2026, 9, 30)].total) == "999" and rows[date(2026, 9, 30)].source == "live"
