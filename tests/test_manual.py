from datetime import date
from decimal import Decimal

from foliotracker.db import Deposit
from foliotracker.manual import deposit_value, last_payout_before


def dep(**kw):
    base = dict(principal=Decimal("100000"), rate=Decimal("8"), start_date=date(2026, 1, 1),
                maturity_date=date(2028, 1, 1), compounding="quarterly")
    base.update(kw)
    return Deposit(**base)


def test_value_is_principal_on_start_date():
    assert deposit_value(dep(), date(2026, 1, 1)) == Decimal("100000")


def test_non_cumulative_carries_interest_accrued_since_last_payout():
    annual = dep(compounding="payout_annual")
    assert deposit_value(annual, date(2027, 1, 1)) == Decimal("100000.00")
    half_year = deposit_value(annual, date(2026, 7, 2))
    assert Decimal("103980") < half_year < Decimal("104000")
    monthly = dep(compounding="payout_monthly")
    assert deposit_value(monthly, date(2026, 2, 1)) == Decimal("100000.00")
    assert deposit_value(monthly, date(2026, 1, 16)) == (Decimal("100000") * Decimal("0.08") * 15 / 365 + 100000).quantize(Decimal("0.01"))


def test_last_payout_date_handles_month_ends():
    assert last_payout_before(date(2026, 1, 31), date(2026, 3, 15), 12) == date(2026, 2, 28)
    assert last_payout_before(date(2026, 1, 1), date(2026, 12, 31), 1) == date(2026, 1, 1)
    assert last_payout_before(date(2026, 1, 1), date(2027, 1, 1), 1) == date(2027, 1, 1)


def test_simple_and_compound_accrual():
    one_year = date(2027, 1, 1)
    assert deposit_value(dep(compounding="simple"), one_year) == Decimal("108000.00")
    assert deposit_value(dep(compounding="annual"), one_year) == Decimal("108000.00")
    quarterly = deposit_value(dep(compounding="quarterly"), one_year)
    assert Decimal("108240") < quarterly < Decimal("108245")


def test_value_freezes_at_maturity():
    at = deposit_value(dep(), date(2028, 1, 1))
    assert deposit_value(dep(), date(2030, 1, 1)) == at
    assert Decimal("117160") < at < Decimal("117170")


def test_legacy_payout_rows_value_at_principal():
    assert deposit_value(dep(compounding="payout"), date(2027, 6, 1)) == Decimal("100000")
