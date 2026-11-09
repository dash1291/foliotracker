from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pypdfium2 as pdfium

_GRAND_TOTAL = re.compile(r"Grand\s+Total\s*[^\d]*([\d,]+\.\d{2})", re.I)
_BOND_ROW = re.compile(
    r"^(IN[E9][0-9A-Z]{9})\s+(.+?)\s+(\d+\.\d+)\s+(\d{8})\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s*$"
)
_SINGLE_HEADER = re.compile(r"^(.+?)\s+PAN:\s*[A-Z0-9]+\s*$")
_JOINT_HEADER = re.compile(r"^Joint A/C Holder:\s*(.+?)\s*$")
_FOLIO = re.compile(r"Folio No\s*:\s*(\S+)")


@dataclass(frozen=True)
class BondRow:
    isin: str
    name: str
    coupon: Decimal
    maturity: str
    quantity: Decimal
    face_value: Decimal
    market_price: Decimal
    value: Decimal


def _num(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def page_texts(pdf: str | Path, password: str) -> list[str]:
    doc = pdfium.PdfDocument(str(pdf), password=password)
    return [doc[i].get_textpage().get_text_bounded() for i in range(len(doc))]


def grand_total(texts: list[str]) -> Decimal | None:
    for text in texts[:3]:
        m = _GRAND_TOTAL.search(text)
        if m:
            return _num(m.group(1))
    return None


def bond_rows(texts: list[str]) -> dict[str, BondRow]:
    rows: dict[str, BondRow] = {}
    for text in texts:
        if "HOLDING STATEMENT OF BONDS" not in text:
            continue
        for line in text.splitlines():
            m = _BOND_ROW.match(line.strip())
            if m:
                isin, name, coupon, maturity, qty, face, market, value = m.groups()
                rows[isin] = BondRow(
                    isin, name, Decimal(coupon), f"{maturity[4:]}-{maturity[2:4]}-{maturity[:2]}",
                    _num(qty), _num(face), _num(market), _num(value),
                )
    return rows


def folio_holders(texts: list[str]) -> dict[str, str]:
    holders: dict[str, str] = {}
    for text in texts:
        if "Folio No" not in text:
            continue
        current: list[str] = []
        prev = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if m := _JOINT_HEADER.match(line):
                current = [prev, m.group(1)]
            elif m := _SINGLE_HEADER.match(line):
                current = [m.group(1)]
            elif (m := _FOLIO.search(line)) and current:
                holders[m.group(1)] = ", ".join(current)
            prev = line
    return holders
