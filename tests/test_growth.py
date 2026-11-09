from datetime import date
from decimal import Decimal

from foliotracker.history import Change, Point, decompose, fy_label, growth_by_year, trailing


def pt(d, positions, deposits=None, balances=None):
    return Point(d, {k: (Decimal(q), Decimal(p), Decimal(q) * Decimal(p)) for k, (q, p) in positions.items()},
                 {k: Decimal(v) for k, v in (deposits or {}).items()}, {k: Decimal(v) for k, v in (balances or {}).items()})


def test_price_move_is_gain_and_units_added_is_new_money():
    a = pt(date(2026, 3, 31), {(1, 1): (100, 10)})
    b = pt(date(2026, 4, 30), {(1, 1): (120, 12)})
    c = decompose(a, b)
    assert c.change == Decimal(440)
    assert c.gains == Decimal(200)
    assert c.new_money == Decimal(240)
    assert c.unsplit == 0


def test_new_and_sold_holdings_are_money_in_and_out():
    a = pt(date(2026, 3, 31), {(1, 1): (100, 10)})
    b = pt(date(2026, 4, 30), {(1, 2): (50, 20)})
    c = decompose(a, b)
    assert c.new_money == Decimal(0) and c.gains == 0 and c.change == 0


def test_deposits_accrue_as_gain_and_balances_are_unsplit():
    a = pt(date(2026, 3, 31), {}, deposits={7: 100000}, balances={8: 500000})
    b = pt(date(2026, 4, 30), {}, deposits={7: 100700, 9: 200000}, balances={8: 560000})
    c = decompose(a, b)
    assert c.gains == Decimal(700)
    assert c.new_money == Decimal(200000)
    assert c.unsplit == Decimal(60000)


def test_financial_year_bucketing_and_return():
    points = [
        pt(date(2025, 3, 31), {(1, 1): (100, 10)}),
        pt(date(2025, 9, 30), {(1, 1): (100, 11)}),
        pt(date(2026, 3, 31), {(1, 1): (150, 12)}),
        pt(date(2026, 10, 31), {(1, 1): (150, 13)}),
    ]
    years = dict(growth_by_year(points))
    assert set(years) == {"FY 2025-26", "FY 2026-27"}
    fy25 = years["FY 2025-26"]
    assert fy25.start_total == 1000 and fy25.end_total == 1800
    assert fy25.gains == Decimal(100) + Decimal(100) and fy25.new_money == Decimal(600)
    assert fy25.return_pct == Decimal("15.38")
    assert years["FY 2026-27"].gains == Decimal(150)
    assert fy_label(date(2026, 3, 31)) == "FY 2025-26" and fy_label(date(2026, 4, 1)) == "FY 2026-27"
    t = trailing(points)
    assert t.start == date(2025, 9, 30) and t.end == date(2026, 10, 31)
