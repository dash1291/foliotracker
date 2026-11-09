from decimal import Decimal

from foliotracker import amfi, pdftext
from foliotracker.importer import _clean, _equity_name, _folio_id
from foliotracker.isin import security_kind

NAV_SAMPLE = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

Aditya Birla Sun Life Mutual Fund

103166;INF209K01AJ8;-;Aditya Birla Sun Life Flexi Cap Fund;Regular Plan;Growth;1879.19;30-Sep-2026
103167;INF209K01AK6;INF209K01AL4;Aditya Birla Sun Life Flexi Cap Fund;Regular Plan;IDCW;200.10;30-Sep-2026

Open Ended Schemes(Other Scheme - Gold ETF)

HDFC Mutual Fund

113049;INF179KC1981;-;HDFC Gold ETF;Direct;Growth;125.73;30-Sep-2026
"""


def test_amfi_parse_groups_category_and_amc():
    idx = amfi.parse(NAV_SAMPLE)
    flexi = idx.get("INF209K01AJ8")
    assert flexi.code == "103166"
    assert flexi.category == "Equity Scheme - Flexi Cap Fund"
    assert flexi.amc == "Aditya Birla Sun Life Mutual Fund"
    assert idx.get("INF209K01AL4").code == "103167"
    gold = idx.get("INF179KC1981")
    assert gold.category == "Other Scheme - Gold ETF"
    assert gold.amc == "HDFC Mutual Fund"
    assert idx.get("INF000000000") is None


def test_security_kind_from_isin_type_code():
    assert security_kind("INE775A01035") == "equity"
    assert security_kind("INE000A08017") == "bond"
    assert security_kind("INE000C07019") == "bond"
    assert security_kind("INE000D09016") == "bond"
    assert security_kind("INF179KC1981") == "mf"


def test_folio_id_normalisation():
    assert _folio_id("123456789012/0") == "123456789012"
    assert _folio_id("1234567890 / 0") == "1234567890"
    assert _folio_id("1234567890") == "1234567890"
    assert _folio_id("91012112582/1") == "91012112582/1"


def test_name_cleaning():
    assert _clean("HDFC AMC LTD#HDFC MF\x02 HDFC GOLD ETF") == "HDFC AMC LTD#HDFC MF HDFC GOLD ETF"
    assert _clean("ICICI Prudential Liquid Fund -") == "ICICI Prudential Liquid Fund"
    assert _equity_name("VEDANTA POWER LIMITED # EQUITY SHARES") == "VEDANTA POWER LIMITED"


BOND_PAGE = """HOLDING STATEMENT OF BONDS AS ON 30-09-2026
ISIN ISIN Name Coupon / Rate /
INE000A08017 ABCL 9.50 15062030 9.50 15062030 5.00 1,00,000.00 1,00,000.00 5,00,000.00
INE000B08025 XYZB 8.75 31012029 8.75 31012029 2.00 10,00,000.00 10,24,500.00 20,49,000.00
Portfolio Value for Bond ` 25,49,000.00 as on 30-09-2026
"""


def test_bond_rows():
    rows = pdftext.bond_rows([BOND_PAGE])
    b = rows["INE000A08017"]
    assert b.name == "ABCL 9.50 15062030"
    assert b.coupon == Decimal("9.50")
    assert b.maturity == "2030-06-15"
    assert b.quantity == Decimal("5.00")
    assert b.face_value == Decimal("100000.00")
    assert b.market_price == Decimal("100000.00")
    assert rows["INE000B08025"].quantity == Decimal("2.00")


SUMMARY_PAGE = "Mutual Fund Folios 1 Folios 1 12,34,567.89\nTotal 12,34,567.89\nGrand Total 1,23,45,678.90\n"

HOLDER_PAGE = """Account Details Account Details
PRIMARY HOLDER PAN: ABCDE1234F
MF Folios
AMC Name : Example Mutual Fund
Folio No : 1234567890 Mode of Holding : Single Email id : x@y.com
PRIMARY HOLDER
Joint A/C Holder: SECOND HOLDER
PAN: ABCDE1234F
MF Folios
Folio No : 123456789012/0 Mode of Holding : Anyone or
"""


def test_grand_total_and_folio_holders():
    assert pdftext.grand_total([SUMMARY_PAGE]) == Decimal("12345678.90")
    assert pdftext.grand_total(["no totals here"]) is None
    holders = pdftext.folio_holders([HOLDER_PAGE])
    assert holders == {
        "1234567890": "PRIMARY HOLDER",
        "123456789012/0": "PRIMARY HOLDER, SECOND HOLDER",
    }
