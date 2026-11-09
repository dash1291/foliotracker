from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import casparser
from casparser.types import CASData, NSDLCASData
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import amfi, pdftext
from .db import Account, ImportRun, Instrument, Position, Transaction
from .isin import security_kind

_CTRL = re.compile(r"[\x00-\x1f]+")


class AlreadyImported(Exception):
    def __init__(self, run: ImportRun):
        self.run = run


@dataclass
class ImportResult:
    run: ImportRun
    accounts: int
    positions: int
    transactions: int
    warnings: list[str] = field(default_factory=list)


def _clean(name: str | None) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub(" ", name or "")).strip().rstrip(" -")


def _equity_name(name: str | None) -> str:
    return _clean((name or "").split("#")[0])


def _folio_id(folio: str) -> str:
    f = re.sub(r"\s+", "", folio)
    return f[:-2] if f.endswith("/0") else f


def _parse_date(text: str | date) -> date:
    if isinstance(text, date):
        return text
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {text!r}")


class _Upserter:
    def __init__(self, session: Session, index: amfi.AmfiIndex):
        self.session = session
        self.index = index

    def account(self, wrapper: str, identifier: str, **fields) -> Account:
        acct = self.session.scalar(
            select(Account).where(Account.wrapper == wrapper, Account.identifier == identifier)
        )
        if acct is None:
            acct = Account(wrapper=wrapper, identifier=identifier)
            self.session.add(acct)
        for k, v in fields.items():
            if v is not None:
                setattr(acct, k, v)
        self.session.flush()
        return acct

    def instrument(self, key: str, kind: str, **fields) -> Instrument:
        inst = self.session.scalar(select(Instrument).where(Instrument.key == key))
        if inst is None:
            inst = Instrument(key=key, kind=kind, name=fields.get("name") or key)
            self.session.add(inst)
        for k, v in fields.items():
            if v is not None:
                setattr(inst, k, v)
        self.session.flush()
        return inst

    def mf_instrument(self, isin: str | None, fallback_name: str, amfi_code: str | None = None) -> Instrument:
        scheme = self.index.get(isin)
        key = isin or f"mf:{_clean(fallback_name)}"
        return self.instrument(
            key,
            "mf",
            isin=isin,
            amfi_code=scheme.code if scheme else amfi_code,
            name=scheme.name if scheme else _clean(fallback_name),
            amc=scheme.amc if scheme else None,
            category=scheme.category if scheme else None,
        )


def import_cas(session: Session, pdf: str | Path, password: str, index: amfi.AmfiIndex | None = None) -> ImportResult:
    pdf = Path(pdf)
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    existing = session.scalar(select(ImportRun).where(ImportRun.sha256 == sha))
    if existing:
        raise AlreadyImported(existing)

    data = casparser.read_cas_pdf(str(pdf), password)
    index = index or amfi.load()
    up = _Upserter(session, index)

    period_from = _parse_date(data.statement_period.from_)
    period_to = _parse_date(data.statement_period.to)
    run = ImportRun(
        sha256=sha,
        filename=pdf.name,
        file_type=str(data.file_type),
        cas_type=str(getattr(data, "cas_type", "")) or None,
        period_from=period_from,
        period_to=period_to,
        parsed_total=Decimal(0),
    )
    session.add(run)
    session.flush()

    warnings = list(data.parse_warnings)
    if isinstance(data, NSDLCASData):
        texts = pdftext.page_texts(pdf, password)
        n_accounts, n_positions = _import_depository(up, run, data, period_to, warnings, texts)
        n_tx = 0
        run.statement_total = pdftext.grand_total(texts) or sum(a.balance for a in data.accounts)
    elif isinstance(data, CASData):
        n_accounts, n_positions, n_tx = _import_rta(up, run, data, warnings)
    else:
        raise TypeError(f"unsupported casparser result {type(data).__name__}")

    run.parsed_total = sum((p.value for p in run.positions), Decimal(0))
    if run.statement_total is not None and run.statement_total != run.parsed_total:
        warnings.append(
            f"statement grand total {run.statement_total} differs from parsed total {run.parsed_total}"
        )
    run.warnings = json.dumps(warnings)
    session.commit()
    return ImportResult(run, n_accounts, n_positions, n_tx, warnings)


def _import_depository(
    up: _Upserter, run: ImportRun, data: NSDLCASData, as_of: date, warnings: list[str], texts: list[str]
):
    n_accounts = n_positions = 0
    bonds = pdftext.bond_rows(texts)
    holders_by_folio = pdftext.folio_holders(texts)
    for acct_data in data.accounts:
        is_demat = "demat" in acct_data.type.lower()
        holders = ", ".join(o.name for o in acct_data.owners) or None
        if is_demat:
            acct = up.account(
                "demat",
                f"{acct_data.dp_id}{acct_data.client_id}",
                institution=_clean(acct_data.name),
                holders=holders,
                statement_total=acct_data.balance,
                statement_total_as_of=as_of,
            )
            n_accounts += 1
            parsed = Decimal(0)
            for eq in acct_data.equities:
                kind = security_kind(eq.isin)
                if kind == "bond" or eq.isin in bonds:
                    row = bonds.get(eq.isin)
                    inst = up.instrument(
                        eq.isin, "bond", isin=eq.isin, name=_clean(row.name if row else eq.name),
                        coupon=row.coupon if row else None, maturity_date=row.maturity if row else None,
                        face_value=row.face_value if row else None,
                    )
                    quantity = row.quantity if row else (eq.value / eq.price if eq.price else eq.num_shares)
                    price = row.market_price if row else eq.price
                else:
                    inst = up.instrument(
                        eq.isin, kind, isin=eq.isin, symbol=eq.symbol, name=eq.symbol or _equity_name(eq.name)
                    )
                    quantity, price = eq.num_shares, eq.price
                up.session.add(Position(
                    import_run=run, account=acct, instrument=inst, as_of=as_of,
                    quantity=quantity, price=price, value=eq.value,
                ))
                parsed += eq.value
                n_positions += 1
            for bond in acct_data.bonds:
                inst = up.instrument(
                    bond.isin, "bond", isin=bond.isin, name=_clean(bond.name), coupon=bond.coupon_rate,
                    maturity_date=bond.maturity_date, face_value=bond.face_value,
                )
                up.session.add(Position(
                    import_run=run, account=acct, instrument=inst, as_of=as_of,
                    quantity=bond.num_bonds, price=bond.market_price, value=bond.value,
                ))
                parsed += bond.value
                n_positions += 1
            for mf in acct_data.mutual_funds:
                inst = up.mf_instrument(mf.isin, mf.name, mf.amfi)
                up.session.add(Position(
                    import_run=run, account=acct, instrument=inst, as_of=as_of,
                    quantity=mf.balance, price=mf.nav, value=mf.value, cost=mf.total_cost,
                ))
                parsed += mf.value
                n_positions += 1
            if parsed != acct_data.balance:
                warnings.append(
                    f"demat {acct.identifier}: statement total {acct_data.balance} differs from parsed {parsed}"
                )
        else:
            for mf in acct_data.mutual_funds:
                inst = up.mf_instrument(mf.isin, mf.name, mf.amfi)
                acct = up.account(
                    "mf_folio", _folio_id(mf.folio or inst.key), institution=inst.amc,
                    holders=holders_by_folio.get(mf.folio or "", holders),
                )
                n_accounts += 1
                up.session.add(Position(
                    import_run=run, account=acct, instrument=inst, as_of=as_of,
                    quantity=mf.balance, price=mf.nav, value=mf.value, cost=mf.total_cost,
                ))
                n_positions += 1
    if data.nps:
        acct = up.account(
            "nps", data.nps.pran or "nps", institution=data.nps.nps_sp, holders=data.investor_info.name,
            statement_total=data.nps.value, statement_total_as_of=as_of,
        )
        n_accounts += 1
        for s in data.nps.schemes:
            inst = up.instrument(
                f"nps:{_clean(s.scheme)}", "nps_scheme", name=_clean(s.scheme), amc=s.fund_manager,
                nps_tier=s.tier, nps_asset_class=s.asset_class,
            )
            up.session.add(Position(
                import_run=run, account=acct, instrument=inst, as_of=as_of,
                quantity=s.units, price=s.nav, value=s.value,
            ))
            n_positions += 1
    up.session.flush()
    return n_accounts, n_positions


def _import_rta(up: _Upserter, run: ImportRun, data: CASData, warnings: list[str]):
    n_accounts = n_positions = n_tx = 0
    for folio in data.folios:
        acct = up.account(
            "mf_folio", _folio_id(folio.folio), institution=_clean(folio.amc),
            holders=folio.name or data.investor_info.name,
        )
        n_accounts += 1
        for scheme in folio.schemes:
            inst = up.mf_instrument(scheme.isin, scheme.scheme, scheme.amfi)
            if scheme.close != scheme.close_calculated:
                warnings.append(
                    f"{inst.name} in folio {acct.identifier}: printed balance {scheme.close} "
                    f"differs from computed {scheme.close_calculated}"
                )
            up.session.add(Position(
                import_run=run, account=acct, instrument=inst, as_of=_parse_date(scheme.valuation.date),
                quantity=scheme.close, price=scheme.valuation.nav, value=scheme.valuation.value,
                cost=scheme.valuation.cost,
            ))
            n_positions += 1
            for tx in scheme.transactions:
                tx_date = _parse_date(tx.date)
                fp = hashlib.sha256(
                    "|".join(map(str, (acct.id, inst.id, tx_date, tx.type, tx.description, tx.amount, tx.units))).encode()
                ).hexdigest()
                if up.session.scalar(select(Transaction.id).where(Transaction.fingerprint == fp)):
                    continue
                up.session.add(Transaction(
                    fingerprint=fp, account_id=acct.id, instrument_id=inst.id, date=tx_date,
                    type=str(tx.type), description=tx.description, amount=tx.amount, units=tx.units,
                    nav=tx.nav, balance=tx.balance,
                ))
                n_tx += 1
    up.session.flush()
    return n_accounts, n_positions, n_tx
