from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from . import allocation, history, kite, manual, prices
from .db import Account, Allocation, Deposit, ImportRun, Instrument, session_factory
from .holdings import latest_holdings
from .importer import AlreadyImported, import_cas

app = FastAPI(title="foliotracker")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
Session = session_factory(os.environ.get("FOLIOTRACKER_DB", "data/foliotracker.db"))


def inr(value: Decimal | None) -> str:
    if value is None:
        return ""
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def qty(value: Decimal | None) -> str:
    return "" if value is None else f"{value.normalize():f}"


def pct(part: Decimal, whole: Decimal) -> str:
    return "0.0%" if not whole else f"{part / whole * 100:.1f}%"


def cat(category: str | None) -> str:
    if not category:
        return ""
    return category.split(" - ", 1)[-1].replace("  ", " ").strip()


_NOISE = ("LIMITED", "LTD", "LTD.", "PRIVATE", "PVT", "PVT.", "BROKING", "SECURITIES", "MUTUAL", "FUND", "ASSET", "MANAGEMENT")


def account_title(account) -> str:
    if account.label:
        return account.label
    words = [w for w in (account.institution or "").split() if w.upper().strip(".") not in _NOISE]
    name = " ".join(w if w.isupper() and len(w) <= 4 else w.title() for w in words) or account.wrapper
    suffix = {"demat": "demat", "mf_folio": "folio", "nps": "NPS"}.get(account.wrapper, "")
    return f"{name} {suffix}".strip()


templates.env.filters["inr"] = inr
templates.env.filters["qty"] = qty
templates.env.filters["pct"] = pct
templates.env.filters["cat"] = cat
templates.env.filters["account_title"] = account_title
templates.env.globals["CLASSES"] = allocation.CLASSES
templates.env.globals["MANUAL_WRAPPERS"] = manual.MANUAL_WRAPPERS
templates.env.globals["COMPOUNDING"] = manual.COMPOUNDING


def portfolio(session) -> dict:
    kite_warning = kite.auto_sync(session)
    groups = latest_holdings(session)
    manuals = manual.manual_holdings(session)
    b = allocation.breakdown(session, groups, manuals)
    manual_total = sum((m.value for m in manuals), Decimal(0))
    cas_total = sum((g.total for g in groups), Decimal(0))
    cas_current = sum((g.current_total for g in groups), Decimal(0))
    nav_date = prices.latest_nav_date(session)
    statement_dates = sorted({g.import_run.period_to for g in groups})
    balance_dates = [m.as_of for m in manuals if not m.account.deposit]
    freshness = []
    if statement_dates:
        freshness.append("statements " + " and ".join(d.strftime("%d %b %Y") for d in statement_dates))
    if nav_date:
        freshness.append(f"fund NAVs {nav_date:%d %b %Y}")
    kite_run = kite.last_sync(session)
    if kite_run:
        freshness.append(f"Zerodha synced {kite_run.imported_at:%d %b %H:%M}")
    if balance_dates:
        freshness.append(f"oldest manual balance {min(balance_dates):%d %b %Y}")
    fresh_text = " · ".join(freshness)
    if b.rows:
        history.record(session, b.total, cas_total, b.by_class, b.by_wrapper)
    cards = [("group", g.current_total, g) for g in groups]
    if manuals:
        cards.append(("manual", manual_total, None))
    cards.sort(key=lambda c: -c[1])
    return {
        "groups": groups, "manuals": manuals, "b": b, "manual_total": manual_total, "cas_total": cas_total,
        "cas_current": cas_current, "nav_date": nav_date, "freshness": fresh_text[:1].upper() + fresh_text[1:], "cards": cards,
        "today": date.today(), "kite_warning": kite_warning,
    }


@app.get("/", response_class=HTMLResponse)
def holdings(request: Request, ok: str | None = None, error: str | None = None):
    with Session() as session:
        ctx = portfolio(session)
        overrides = allocation.latest_overrides(session)
        ctx["classes"] = {
            p.instrument_id: allocation.resolve_instrument(p.instrument, overrides.get(p.instrument_id))
            for g in ctx["groups"] for p in g.positions
        }
        ctx["top_classes"] = [
            (k, v) for k, v in sorted(ctx["b"].by_class.items(), key=lambda kv: -kv[1]) if v > 0
        ]
        shown = {g.account.id for g in ctx["groups"]}
        ctx["closed"] = [
            a for a in session.scalars(select(Account).where(Account.closed_as_of.is_not(None)).order_by(Account.closed_as_of))
            if a.id not in shown
        ]
        ctx.update(ok=ok, error=error or ctx["kite_warning"], kite_connected=kite.current_session(session) is not None,
                   kite_configured=bool(os.environ.get("KITE_API_KEY")))
        return templates.TemplateResponse(request, "holdings.html", ctx)


@app.get("/zerodha/connect")
def zerodha_connect():
    try:
        return RedirectResponse(kite.login_url(), status_code=303)
    except kite.KiteNotConfigured as e:
        return RedirectResponse(f"/?error={e}", status_code=303)


@app.get("/zerodha/callback")
def zerodha_callback(request_token: str | None = None, status: str | None = None):
    if not request_token:
        return RedirectResponse(f"/?error=Zerodha login did not return a token ({status or 'cancelled'})", status_code=303)
    try:
        with Session() as session:
            kite.exchange_request_token(session, request_token)
    except kite.KiteNotConfigured as e:
        return RedirectResponse(f"/?error={e}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/?error=Zerodha login failed: {type(e).__name__}: {e}", status_code=303)
    return _run_kite_sync()


def _run_kite_sync():
    try:
        with Session() as session:
            r = kite.sync(session)
    except kite.KiteLoginRequired:
        return RedirectResponse("/zerodha/connect", status_code=303)
    except kite.KiteNotConfigured as e:
        return RedirectResponse(f"/?error={e}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/?error=Zerodha sync failed: {type(e).__name__}: {e}", status_code=303)
    if r.unchanged:
        msg = f"Zerodha holdings unchanged since the last sync today ({r.positions} holdings)"
    else:
        msg = f"Zerodha synced: {r.positions} holdings into {r.account.display_name}"
    if r.warnings:
        msg += f", {len(r.warnings)} warning{'s' if len(r.warnings) != 1 else ''}"
    return RedirectResponse(f"/?ok={msg}", status_code=303)


@app.get("/accounts/new", response_class=HTMLResponse)
def new_account(request: Request, wrapper: str = "fd", error: str | None = None):
    return templates.TemplateResponse(
        request, "account_form.html",
        {"wrapper": wrapper if wrapper in manual.MANUAL_WRAPPERS else "fd", "today": date.today(), "error": error,
         "default_class": manual.DEFAULT_CLASS},
    )


@app.post("/accounts")
async def create_account(request: Request):
    form = await request.form()
    wrapper = form.get("wrapper", "")
    label = (form.get("label") or "").strip()
    if wrapper not in manual.MANUAL_WRAPPERS or not label:
        return RedirectResponse(f"/accounts/new?wrapper={wrapper}&error=pick a type and give it a name", status_code=303)
    try:
        with Session() as session:
            acct = manual.create_account(
                session, wrapper, label, form.get("institution"), form.get("holders"), form.get("asset_class"),
            )
            if wrapper == "fd":
                session.add(Deposit(
                    account_id=acct.id, principal=Decimal(form["principal"]), rate=Decimal(form["rate"]),
                    start_date=date.fromisoformat(form["start_date"]),
                    maturity_date=date.fromisoformat(form["maturity_date"]),
                    compounding=form.get("compounding", "quarterly"),
                ))
            elif form.get("value"):
                manual.add_snapshot(
                    session, acct, date.fromisoformat(form.get("as_of") or date.today().isoformat()),
                    Decimal(form["value"]), form.get("note"),
                )
            session.commit()
    except (KeyError, ValueError, ArithmeticError):
        return RedirectResponse(f"/accounts/new?wrapper={wrapper}&error=every amount must be a number and every date filled in", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(request: Request, account_id: int, error: str | None = None):
    with Session() as session:
        acct = session.get(Account, account_id)
        if acct is None or not acct.manual:
            return RedirectResponse("/", status_code=303)
        holding = next(h for h in manual.manual_holdings(session) if h.account.id == account_id)
        snapshots = sorted(acct.snapshots, key=lambda s: s.date, reverse=True)
        return templates.TemplateResponse(
            request, "account_detail.html",
            {"acct": acct, "holding": holding, "snapshots": snapshots, "today": date.today(), "error": error},
        )


@app.post("/accounts/{account_id}/snapshots")
async def add_snapshot(request: Request, account_id: int):
    form = await request.form()
    try:
        with Session() as session:
            acct = session.get(Account, account_id)
            manual.add_snapshot(
                session, acct, date.fromisoformat(form["as_of"]), Decimal(form["value"]), form.get("note"),
            )
            session.commit()
    except (KeyError, ValueError, ArithmeticError):
        return RedirectResponse(f"/accounts/{account_id}?error=the balance must be a number and the date filled in", status_code=303)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.post("/accounts/{account_id}/deposit")
async def edit_deposit(request: Request, account_id: int):
    form = await request.form()
    try:
        with Session() as session:
            acct = session.get(Account, account_id)
            dep = acct.deposit
            dep.principal = Decimal(form["principal"])
            dep.rate = Decimal(form["rate"])
            dep.start_date = date.fromisoformat(form["start_date"])
            dep.maturity_date = date.fromisoformat(form["maturity_date"])
            if form.get("compounding") in manual.COMPOUNDING:
                dep.compounding = form["compounding"]
            session.commit()
    except (KeyError, ValueError, ArithmeticError, AttributeError):
        return RedirectResponse(f"/accounts/{account_id}?error=every amount must be a number and every date filled in", status_code=303)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.post("/accounts/{account_id}/edit")
async def edit_account(request: Request, account_id: int):
    form = await request.form()
    with Session() as session:
        acct = session.get(Account, account_id)
        acct.label = (form.get("label") or acct.label).strip()
        acct.institution = form.get("institution") or None
        acct.holders = form.get("holders") or None
        if acct.wrapper == "other" and form.get("asset_class") in allocation.CLASSES:
            acct.asset_class = form["asset_class"]
        session.commit()
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.post("/accounts/{account_id}/delete")
async def delete_account(request: Request, account_id: int):
    form = await request.form()
    if form.get("confirm") != "on":
        return RedirectResponse(f"/accounts/{account_id}?error=tick the box to confirm deletion", status_code=303)
    with Session() as session:
        acct = session.get(Account, account_id)
        if acct and acct.manual:
            session.delete(acct)
            session.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/closed")
async def close_account(request: Request, account_id: int):
    form = await request.form()
    with Session() as session:
        acct = session.get(Account, account_id)
        if acct is None:
            return RedirectResponse("/", status_code=303)
        try:
            acct.closed_as_of = date.fromisoformat(form["closed_as_of"]) if form.get("closed_as_of") else None
        except ValueError:
            return RedirectResponse("/?error=the redemption date must be a valid date", status_code=303)
        session.commit()
        name = account_title(acct)
        if acct.closed_as_of:
            msg = f"{name} marked redeemed on {acct.closed_as_of:%d %b %Y}; a statement dated after that takes over"
        else:
            msg = f"{name} is no longer marked redeemed"
    return RedirectResponse(f"/?ok={msg}", status_code=303)


@app.post("/prices/refresh")
def refresh_prices():
    try:
        with Session() as session:
            r = prices.refresh_navs(session)
        msg = f"NAVs refreshed: {r.updated} new, {r.unchanged} already current"
        if r.missing:
            msg += f", {len(r.missing)} not in the AMFI file ({', '.join(r.missing[:3])})"
        return RedirectResponse(f"/?ok={msg}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/?error={type(e).__name__}: {e}", status_code=303)


@app.get("/allocation", response_class=HTMLResponse)
def allocation_page(request: Request, by: str = "class"):
    with Session() as session:
        ctx = portfolio(session)
        b = ctx["b"]
        if by == "wrapper":
            bars = b.by_wrapper
        elif by == "holder":
            bars = b.by_holder
        else:
            by, bars = "class", {allocation.CLASSES[c]: v for c, v in b.by_class.items()}
        bars = dict(sorted(bars.items(), key=lambda kv: -kv[1]))
        return templates.TemplateResponse(
            request, "allocation.html", {"b": b, "bars": bars, "by": by, "freshness": ctx["freshness"]},
        )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    with Session() as session:
        ctx = portfolio(session)
        points = history.statement_points(session)
        history.backfill(session, points)
        if not points or points[-1].date < date.today():
            points.append(history.current_point(session, ctx["groups"], ctx["manuals"]))
        years = list(reversed(history.growth_by_year(points)))
        last_year = history.trailing(points)
        rows = history.series(session)
        chart = {
            key: [
                {"date": r.date.isoformat(), "total": float(r.total), "source": r.source,
                 "classes": {allocation.CLASSES[k]: float(v) for k, v in r.classes.items() if v}}
                for r in subset
            ]
            for key, subset in history.resample(rows).items()
        }
        first, last = (rows[0], rows[-1]) if rows else (None, None)
        return templates.TemplateResponse(
            request, "history.html",
            {"rows": list(reversed(rows)), "points": json.dumps(chart), "first": first, "last": last,
             "years": years, "last_year": last_year, "today": date.today()},
        )


@app.get("/instruments/{instrument_id}/allocation", response_class=HTMLResponse)
def override_form(request: Request, instrument_id: int, error: str | None = None):
    with Session() as session:
        inst = session.get(Instrument, instrument_id)
        current = allocation.latest_overrides(session).get(instrument_id)
        resolved = allocation.resolve_instrument(inst, current)
        rule = allocation.resolve_instrument(inst)
        percents = {c: int(round(resolved.vector.get(c, Decimal(0)) * 100)) for c in allocation.CLASSES}
        return templates.TemplateResponse(
            request, "override.html",
            {"inst": inst, "resolved": resolved, "rule": rule, "percents": percents, "current": current,
             "today": date.today(), "error": error},
        )


@app.post("/instruments/{instrument_id}/allocation")
async def save_override(request: Request, instrument_id: int):
    form = await request.form()
    if form.get("action") == "clear":
        with Session() as session:
            for a in session.scalars(select(Allocation).where(Allocation.instrument_id == instrument_id)):
                session.delete(a)
            session.commit()
        return RedirectResponse("/allocation", status_code=303)
    vector = {}
    for c in allocation.CLASSES:
        raw = (form.get(c) or "0").strip()
        try:
            value = Decimal(raw)
        except Exception:
            return RedirectResponse(f"/instruments/{instrument_id}/allocation?error=not a number: {raw}", status_code=303)
        if value:
            vector[c] = value
    total = sum(vector.values(), Decimal(0))
    if abs(total - 100) > Decimal("0.5"):
        return RedirectResponse(f"/instruments/{instrument_id}/allocation?error=percentages add up to {total}, not 100", status_code=303)
    normalised = {c: str((v / total).quantize(Decimal("0.0001"))) for c, v in vector.items()}
    with Session() as session:
        session.add(Allocation(
            instrument_id=instrument_id, vector=json.dumps(normalised), source="manual",
            as_of=date.fromisoformat(form.get("as_of") or date.today().isoformat()), note=form.get("note") or None,
        ))
        session.commit()
    return RedirectResponse("/allocation", status_code=303)


@app.get("/imports", response_class=HTMLResponse)
def imports(request: Request, error: str | None = None, ok: str | None = None):
    with Session() as session:
        runs = session.scalars(select(ImportRun).order_by(ImportRun.imported_at.desc())).all()
        for r in runs:
            r.warning_list = json.loads(r.warnings or "[]")
        return templates.TemplateResponse(request, "imports.html", {"runs": runs, "error": error, "ok": ok})


@app.post("/imports")
async def upload(file: UploadFile = File(...), password: str = Form(...)):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        with Session() as session:
            result = import_cas(session, tmp_path, password)
        msg = (
            f"imported {file.filename}: {result.positions} positions across {result.accounts} accounts"
            + (f", {len(result.warnings)} warnings" if result.warnings else "")
        )
        return RedirectResponse(f"/imports?ok={msg}", status_code=303)
    except AlreadyImported as e:
        return RedirectResponse(f"/imports?error=already imported as run {e.run.id}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/imports?error={type(e).__name__}: {e}", status_code=303)
    finally:
        tmp_path.unlink(missing_ok=True)
