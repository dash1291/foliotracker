from decimal import Decimal

from foliotracker.allocation import CLASSES, resolve_instrument, split_category
from foliotracker.db import Instrument


def mf(name, category):
    return Instrument(key=name, kind="mf", name=name, category=category)


def test_category_header_variants_normalise():
    assert split_category("Equity Scheme - Large Cap Fund") == ("equity scheme", "large cap")
    assert split_category("Equity Schemes - Large Cap Fund") == ("equity schemes", "large cap")
    assert split_category("Income/Debt Oriented Schemes - Short Term Fund") == ("income/debt oriented schemes", "short term")
    assert split_category("Income") == ("income", "income")


def test_equity_and_debt_categories_are_exact():
    r = resolve_instrument(mf("ABSL Flexi Cap", "Equity Scheme - Flexi Cap Fund"))
    assert r.vector == {"equity_in": Decimal(1)} and r.confidence == "exact"
    r = resolve_instrument(mf("HDFC Short Term", "Income/Debt Oriented Schemes - Short Term Fund"))
    assert r.vector == {"debt": Decimal(1)} and r.confidence == "exact"
    r = resolve_instrument(mf("ICICI Liquid", "Debt Scheme - Liquid Fund"))
    assert r.vector == {"debt": Decimal(1)}


def test_arbitrage_is_debt_not_equity():
    r = resolve_instrument(mf("Kotak Arbitrage Fund", "Hybrid Scheme - Arbitrage Fund"))
    assert r.vector == {"debt": Decimal(1)} and r.confidence == "exact"


def test_hybrids_are_estimated_or_need_override():
    r = resolve_instrument(mf("X Aggressive Hybrid", "Hybrid Schemes - Aggressive Hybrid Fund"))
    assert r.confidence == "estimated" and r.vector["equity_in"] == Decimal("0.725")
    r = resolve_instrument(mf("X BAF", "Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation"))
    assert r.confidence == "override_required"
    r = resolve_instrument(mf("X BAF", "Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage"))
    assert r.confidence == "override_required"


def test_other_groups_fall_back_to_name():
    assert resolve_instrument(mf("Nippon India Gold Savings Fund", "Other Scheme - FoF Domestic")).vector == {"gold": Decimal(1)}
    assert resolve_instrument(mf("HDFC Gold ETF", "Other Scheme - Gold ETF")).vector == {"gold": Decimal(1)}
    assert resolve_instrument(mf("Nippon India ETF Nifty 1D Rate Liquid BeES", "Other Scheme - Other  ETFs")).vector == {"debt": Decimal(1)}
    assert resolve_instrument(mf("Nippon India ETF Nifty Midcap 150", "Other Scheme - Other  ETFs")).vector == {"equity_in": Decimal(1)}
    assert resolve_instrument(mf("Motilal Oswal Nasdaq 100 FoF", "Other Scheme - FoF Overseas")).vector == {"equity_intl": Decimal(1)}
    assert resolve_instrument(mf("Zerodha Nifty LargeMidcap 250 Index Fund", "Index Funds - Equity Funds")).vector == {"equity_in": Decimal(1)}
    unknown = resolve_instrument(mf("Some Opaque FoF", "Other Scheme - FoF Domestic"))
    assert unknown.confidence == "override_required"


def test_non_fund_kinds():
    assert resolve_instrument(Instrument(key="INE1", kind="equity", name="MOTHERSON")).vector == {"equity_in": Decimal(1)}
    assert resolve_instrument(Instrument(key="INE2", kind="bond", name="Example Bond 9.00")).vector == {"debt": Decimal(1)}
    nps = Instrument(key="nps:x", kind="nps_scheme", name="NPS TRUST SCHEME E TIER I", nps_asset_class="E")
    assert resolve_instrument(nps).vector == {"equity_in": Decimal(1)}


def test_every_vector_sums_to_one():
    import yaml
    from pathlib import Path
    rules = yaml.safe_load((Path("foliotracker/categories.yaml")).read_text())
    for rule in rules["categories"] + rules["names"] + list(rules["wrappers"].values()) + list(rules["nps"].values()) + list(rules["groups"].values()):
        vec = rule["vector"] if "vector" in rule else rule
        assert abs(sum(Decimal(str(v)) for v in vec.values()) - 1) < Decimal("0.0001"), rule
        assert set(vec) <= set(CLASSES), rule


def test_refresh_navs_writes_one_valuation_per_fund_and_date():
    from datetime import date
    from foliotracker import amfi, prices
    from foliotracker.db import Base, Instrument, Valuation
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from tests.test_parsing import NAV_SAMPLE

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            Instrument(key="INF209K01AJ8", kind="mf", isin="INF209K01AJ8", name="Flexi"),
            Instrument(key="INF000000000", kind="mf", isin="INF000000000", name="Unknown"),
            Instrument(key="INE775A01035", kind="equity", isin="INE775A01035", name="MOTHERSON"),
        ])
        s.commit()
        first = prices.refresh_navs(s, amfi.parse(NAV_SAMPLE))
        assert (first.updated, first.unchanged, first.missing) == (1, 0, ["Unknown"])
        assert first.latest == date(2026, 9, 30)
        again = prices.refresh_navs(s, amfi.parse(NAV_SAMPLE))
        assert (again.updated, again.unchanged) == (0, 1)
        rows = s.scalars(select(Valuation)).all()
        assert len(rows) == 1 and str(rows[0].price) == "1879.19"
