from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import amfi
from .db import Account, ImportRun, KiteSession, Position
from .importer import _Upserter, _clean
from .isin import security_kind

COVERS = "equity,mf"


class KiteNotConfigured(Exception):
    pass


class KiteLoginRequired(Exception):
    pass


def _client():
    try:
        from kiteconnect import KiteConnect
    except ImportError as e:
        raise KiteNotConfigured("kiteconnect is not installed") from e
    key = os.environ.get("KITE_API_KEY")
    if not key:
        raise KiteNotConfigured("KITE_API_KEY is not set in .env")
    return KiteConnect(api_key=key)


def login_url() -> str:
    return _client().login_url()


def exchange_request_token(session: Session, request_token: str) -> KiteSession:
    secret = os.environ.get("KITE_API_SECRET")
    if not secret:
        raise KiteNotConfigured("KITE_API_SECRET is not set in .env")
    data = _client().generate_session(request_token, api_secret=secret)
    for old in session.scalars(select(KiteSession)):
        session.delete(old)
    ks = KiteSession(user_id=data["user_id"], user_name=data.get("user_name"), access_token=data["access_token"])
    session.add(ks)
    session.commit()
    return ks


def current_session(session: Session) -> KiteSession | None:
    ks = session.scalar(select(KiteSession).order_by(KiteSession.created_at.desc()).limit(1))
    if ks is None:
        return None
    expiry = datetime.combine(ks.created_at.date(), time(6, 0))
    if ks.created_at.time() >= time(6, 0):
        expiry = datetime.combine(date.fromordinal(ks.created_at.date().toordinal() + 1), time(6, 0))
    return ks if datetime.now() < expiry else None


@dataclass
class SyncResult:
    run: ImportRun | None
    account: Account
    positions: int = 0
    unchanged: bool = False
    warnings: list[str] = field(default_factory=list)


def fetch_holdings(session: Session) -> tuple[dict, list[dict], list[dict]]:
    ks = current_session(session)
    if ks is None:
        raise KiteLoginRequired("no Zerodha session for today")
    kite = _client()
    kite.set_access_token(ks.access_token)
    try:
        from kiteconnect.exceptions import TokenException
    except ImportError:
        TokenException = Exception
    try:
        return kite.profile(), kite.holdings(), kite.mf_holdings()
    except TokenException as e:
        raise KiteLoginRequired(str(e)) from e


def _dec(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def map_holdings(equity: list[dict], funds: list[dict]) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    warnings: list[str] = []
    for h in equity:
        isin = h.get("isin") or ""
        qty = _dec(h.get("quantity")) + _dec(h.get("t1_quantity")) + _dec(h.get("collateral_quantity"))
        if not isin or qty == 0:
            continue
        price = _dec(h.get("last_price"))
        rows.append({
            "isin": isin, "kind": security_kind(isin), "symbol": h.get("tradingsymbol"),
            "name": h.get("tradingsymbol") or isin, "quantity": qty, "price": price,
            "value": (qty * price).quantize(Decimal("0.01")),
            "cost": (qty * _dec(h.get("average_price"))).quantize(Decimal("0.01")),
        })
    for h in funds:
        isin = h.get("tradingsymbol") or ""
        qty = _dec(h.get("quantity"))
        if not isin.startswith("INF") or qty == 0:
            if qty:
                warnings.append(f"fund holding without an ISIN skipped: {h.get('fund')}")
            continue
        price = _dec(h.get("last_price"))
        rows.append({
            "isin": isin, "kind": "mf", "symbol": None, "name": h.get("fund") or isin,
            "quantity": qty, "price": price, "value": (qty * price).quantize(Decimal("0.01")),
            "cost": (qty * _dec(h.get("average_price"))).quantize(Decimal("0.01")), "folio": h.get("folio"),
        })
    return rows, warnings


def _target_account(session: Session, profile: dict) -> Account:
    acct = session.scalar(
        select(Account).where(Account.wrapper == "demat", Account.institution.ilike("%zerodha%")).order_by(Account.id)
    )
    if acct is None:
        acct = Account(wrapper="demat", identifier=f"kite:{profile.get('user_id')}", institution="Zerodha",
                       holders=profile.get("user_name"), label="Zerodha demat")
        session.add(acct)
        session.flush()
    return acct


def sync(session: Session, index: amfi.AmfiIndex | None = None) -> SyncResult:
    profile, equity, funds = fetch_holdings(session)
    rows, warnings = map_holdings(equity, funds)
    today = date.today()
    payload = json.dumps({"date": today.isoformat(), "rows": rows}, default=str, sort_keys=True)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    acct = _target_account(session, profile)
    existing = session.scalar(select(ImportRun).where(ImportRun.sha256 == sha))
    if existing:
        return SyncResult(existing, acct, len(rows), unchanged=True, warnings=warnings)

    index = index or amfi.load()
    up = _Upserter(session, index)
    run = ImportRun(
        sha256=sha, filename=f"kite {profile.get('user_id', '')} {today:%d %b %Y}".strip(), file_type="KITE",
        period_from=today, period_to=today, parsed_total=Decimal(0), covers=COVERS,
    )
    session.add(run)
    session.flush()
    total = Decimal(0)
    for r in rows:
        if r["kind"] == "mf":
            inst = up.mf_instrument(r["isin"], r["name"])
        else:
            inst = up.instrument(r["isin"], r["kind"], isin=r["isin"], symbol=r["symbol"], name=_clean(r["name"]))
        session.add(Position(
            import_run=run, account=acct, instrument=inst, as_of=today, quantity=r["quantity"],
            price=r["price"], value=r["value"], cost=r["cost"],
        ))
        total += r["value"]
    run.parsed_total = total
    run.statement_total = total
    run.warnings = json.dumps(warnings)
    session.commit()
    return SyncResult(run, acct, len(rows), warnings=warnings)


_last_attempt: datetime | None = None
MAX_AGE = timedelta(minutes=15)


def last_sync(session: Session) -> ImportRun | None:
    return session.scalar(
        select(ImportRun).where(ImportRun.file_type == "KITE").order_by(ImportRun.imported_at.desc()).limit(1)
    )


def auto_sync(session: Session) -> str | None:
    """Sync on page load when a session exists and the last sync is stale. Returns a warning
    to show, or None. Failures never block the page; one attempt per MAX_AGE window."""
    global _last_attempt
    if not os.environ.get("KITE_API_KEY") or current_session(session) is None:
        return None
    now = datetime.now()
    last = last_sync(session)
    if last and now - last.imported_at < MAX_AGE:
        return None
    if _last_attempt and now - _last_attempt < MAX_AGE:
        return None
    _last_attempt = now
    try:
        sync(session)
        return None
    except KiteLoginRequired:
        return "Zerodha session expired; connect again for today's holdings"
    except Exception as e:
        return f"Zerodha sync failed: {type(e).__name__}: {e}"
