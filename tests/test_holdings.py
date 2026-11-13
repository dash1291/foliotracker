from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from foliotracker.db import Account, Base, ImportRun, Instrument, Position
from foliotracker.holdings import latest_holdings

SEP, OCT, NOV = date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 13)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def run(session, sha, file_type, period_to, covers=None):
    r = ImportRun(sha256=sha, filename=sha, file_type=file_type, period_from=period_to, period_to=period_to,
                  parsed_total=Decimal(0), covers=covers)
    session.add(r)
    return r


def hold(session, run, account, inst, qty, as_of):
    session.add(Position(import_run=run, account=account, instrument=inst, as_of=as_of,
                         quantity=Decimal(qty), value=Decimal(qty) * 10))


def by_account(session: Session) -> dict[str, list[str]]:
    return {h.account.identifier: sorted(f"{p.instrument.key}:{p.quantity}" for p in h.positions) for h in latest_holdings(session)}


def test_redeemed_date_hides_statement_positions_until_a_later_statement(session):
    folio = Account(wrapper="mf_folio", identifier="F1", closed_as_of=date(2026, 11, 5))
    fund = Instrument(key="INF1", kind="mf", name="Fund")
    sep = run(session, "sep", "CDSL", SEP)
    hold(session, sep, folio, fund, 100, SEP)
    session.commit()
    assert by_account(session) == {}

    nov = run(session, "nov", "CDSL", date(2026, 11, 30))
    hold(session, nov, folio, fund, 40, date(2026, 11, 30))
    session.commit()
    assert by_account(session) == {"F1": ["INF1:40"]}


def test_account_missing_from_a_newer_full_statement_is_closed(session):
    folio, demat = Account(wrapper="mf_folio", identifier="F1"), Account(wrapper="demat", identifier="D1")
    fund, stock = Instrument(key="INF1", kind="mf", name="Fund"), Instrument(key="INE1", kind="equity", name="X")
    sep, oct = run(session, "sep", "CDSL", SEP), run(session, "oct", "CDSL", OCT)
    hold(session, sep, folio, fund, 100, SEP)
    hold(session, sep, demat, stock, 10, SEP)
    hold(session, oct, demat, stock, 12, OCT)
    session.commit()
    assert by_account(session) == {"D1": ["INE1:12"]}


def test_kite_only_account_and_other_statement_families_are_not_closed_by_a_depository_statement(session):
    kite_acct = Account(wrapper="demat", identifier="kite:AB", institution="Zerodha")
    rta_folio = Account(wrapper="mf_folio", identifier="R1")
    demat = Account(wrapper="demat", identifier="D1")
    fund, stock = Instrument(key="INF1", kind="mf", name="Fund"), Instrument(key="INE1", kind="equity", name="X")
    cams = run(session, "cams", "CAMS", SEP)
    hold(session, cams, rta_folio, fund, 50, SEP)
    kite = run(session, "kite", "KITE", NOV, covers="equity,mf")
    hold(session, kite, kite_acct, stock, 5, NOV)
    oct = run(session, "oct", "CDSL", OCT)
    hold(session, oct, demat, stock, 12, OCT)
    session.commit()
    assert by_account(session) == {"D1": ["INE1:12"], "R1": ["INF1:50"], "kite:AB": ["INE1:5"]}


def test_zero_unit_positions_are_dropped(session):
    folio = Account(wrapper="mf_folio", identifier="F1")
    fund = Instrument(key="INF1", kind="mf", name="Fund")
    oct = run(session, "oct", "CDSL", OCT)
    hold(session, oct, folio, fund, 0, OCT)
    session.commit()
    assert by_account(session) == {}
