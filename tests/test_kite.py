from datetime import date
from decimal import Decimal

from foliotracker.db import Account, ImportRun, Instrument, Position
from foliotracker.holdings import resolve_positions
from foliotracker.kite import map_holdings


def pos(run, inst, qty, value, as_of):
    return Position(import_run=run, instrument=inst, quantity=Decimal(qty), value=Decimal(value), as_of=as_of)


def test_kite_overlay_replaces_equity_and_funds_but_keeps_bonds():
    cas = ImportRun(id=1, sha256="a", filename="cas", file_type="CDSL", period_from=date(2026, 9, 1),
                    period_to=date(2026, 9, 30), parsed_total=Decimal(0))
    kite = ImportRun(id=2, sha256="b", filename="kite", file_type="KITE", period_from=date(2026, 11, 9),
                     period_to=date(2026, 11, 9), parsed_total=Decimal(0), covers="equity,mf")
    stock = Instrument(key="INE1", kind="equity", name="MOTHERSON")
    fund = Instrument(key="INF1", kind="mf", name="Fund")
    new_fund = Instrument(key="INF2", kind="mf", name="New fund bought in October")
    bond = Instrument(key="INE2", kind="bond", name="Example Bond 9.00")
    sep, nov = date(2026, 9, 30), date(2026, 11, 9)
    items = [
        (pos(cas, stock, 30, 4900, sep), cas), (pos(cas, fund, 100, 10000, sep), cas), (pos(cas, bond, 1, 100000, sep), cas),
        (pos(kite, stock, 35, 5700, nov), kite), (pos(kite, new_fund, 10, 5000, nov), kite),
    ]
    positions, base, sources = resolve_positions(items)
    assert base is cas and sources == [cas, kite]
    got = {(p.instrument.key, str(p.quantity), p.as_of) for p in positions}
    assert got == {("INE1", "35", nov), ("INF2", "10", nov), ("INE2", "1", sep)}


def test_newer_statement_supersedes_older_kite_overlay():
    kite = ImportRun(id=1, sha256="b", filename="kite", file_type="KITE", period_from=date(2026, 10, 5),
                     period_to=date(2026, 10, 5), parsed_total=Decimal(0), covers="equity,mf")
    cas = ImportRun(id=2, sha256="a", filename="cas", file_type="CDSL", period_from=date(2026, 10, 1),
                    period_to=date(2026, 10, 31), parsed_total=Decimal(0))
    stock = Instrument(key="INE1", kind="equity", name="X")
    items = [(pos(kite, stock, 50, 1, date(2026, 10, 5)), kite), (pos(cas, stock, 60, 1, date(2026, 10, 31)), cas)]
    positions, base, sources = resolve_positions(items)
    assert base is cas and sources == [cas] and str(positions[0].quantity) == "60"


def test_map_holdings_from_kite_payloads():
    equity = [
        {"tradingsymbol": "MOTHERSON", "isin": "INE775A01035", "quantity": 20, "t1_quantity": 5,
         "collateral_quantity": 0, "average_price": 150.0, "last_price": 163.3},
        {"tradingsymbol": "GOLDBEES", "isin": "INF204KB17I5", "quantity": 100, "t1_quantity": 0,
         "collateral_quantity": 0, "average_price": 60.0, "last_price": 70.5},
        {"tradingsymbol": "SOLD", "isin": "INE000000000", "quantity": 0, "t1_quantity": 0, "average_price": 1, "last_price": 1},
    ]
    funds = [
        {"folio": "123", "fund": "Nippon India Multi Cap Fund", "tradingsymbol": "INF204K01XF9",
         "quantity": 1000.5, "average_price": 250.0, "last_price": 329.54},
        {"folio": "9", "fund": "Weird", "tradingsymbol": "", "quantity": 3, "average_price": 1, "last_price": 1},
    ]
    rows, warnings = map_holdings(equity, funds)
    assert [r["isin"] for r in rows] == ["INE775A01035", "INF204KB17I5", "INF204K01XF9"]
    assert rows[0]["kind"] == "equity" and str(rows[0]["quantity"]) == "25" and str(rows[0]["value"]) == "4082.50"
    assert rows[1]["kind"] == "mf"
    assert str(rows[2]["cost"]) == "250125.00"
    assert warnings == ["fund holding without an ISIN skipped: Weird"]
