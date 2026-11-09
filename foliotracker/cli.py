from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .db import session_factory
from .holdings import latest_holdings
from .importer import AlreadyImported, import_cas


def cmd_import(args):
    load_dotenv(Path.cwd() / ".env")
    password = os.environ.get(args.password_env)
    if not password:
        sys.exit(f"set {args.password_env} in the environment or .env")
    Session = session_factory(args.db)
    with Session() as session:
        try:
            result = import_cas(session, args.pdf, password)
        except AlreadyImported as e:
            sys.exit(f"already imported as run {e.run.id} on {e.run.imported_at:%Y-%m-%d}")
    run = result.run
    print(f"run {run.id}: {run.file_type} {run.cas_type or ''} {run.period_from} to {run.period_to}")
    print(f"accounts {result.accounts}, positions {result.positions}, transactions {result.transactions}")
    print(f"statement total {run.statement_total}  parsed total {run.parsed_total}")
    for w in result.warnings:
        print("warning:", w)


def cmd_holdings(args):
    Session = session_factory(args.db)
    with Session() as session:
        grand = 0
        for h in latest_holdings(session):
            a = h.account
            print(f"\n[{a.wrapper}] {a.display_name}  holders={a.holders}  as of {h.as_of}  statement {h.total:,.2f}  current {h.current_total:,.2f}")
            for p in sorted(h.positions, key=lambda p: -p.current_value):
                i = p.instrument
                print(f"  {i.kind:10} {i.name[:44]:44} {p.quantity:>14} {p.value:>16,.2f} {p.current_value:>16,.2f}  {p.current_date}")
            grand += h.current_total
        print(f"\nnet worth: {grand:,.2f}")


def cmd_refresh(args):
    from .prices import refresh_navs

    Session = session_factory(args.db)
    with Session() as session:
        r = refresh_navs(session)
    print(f"NAVs: {r.updated} new, {r.unchanged} already current, latest {r.latest}")
    for name in r.missing:
        print("not in AMFI file:", name)


def cmd_serve(args):
    import uvicorn

    load_dotenv(Path.cwd() / ".env")
    os.environ["FOLIOTRACKER_DB"] = str(args.db)
    uvicorn.run("foliotracker.app:app", host=args.host, port=args.port, reload=args.reload)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="foliotracker")
    parser.add_argument("--db", default=os.environ.get("FOLIOTRACKER_DB", "data/foliotracker.db"))
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("import", help="import a CAS PDF")
    p.add_argument("pdf")
    p.add_argument("--password-env", default="CAS_PASS")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("holdings", help="print latest holdings per account")
    p.set_defaults(func=cmd_holdings)

    p = sub.add_parser("refresh-navs", help="pull today's NAVs from AMFI for every fund held")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8321)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
