from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

NAV_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
CACHE_DIR = Path(os.environ.get("FOLIOTRACKER_DATA", "data")) / "amfi"

_HEADER = re.compile(r"^(Open|Close|Interval) Ended Schemes\((.*)\)\s*$")


@dataclass(frozen=True)
class Scheme:
    code: str
    name: str
    amc: str
    category: str
    nav: str
    nav_date: str


class AmfiIndex:
    def __init__(self, by_isin: dict[str, Scheme]):
        self.by_isin = by_isin

    def get(self, isin: str | None) -> Scheme | None:
        return self.by_isin.get(isin) if isin else None


def parse(text: str) -> AmfiIndex:
    by_isin: dict[str, Scheme] = {}
    category = None
    amc = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADER.match(line)
        if m:
            category = m.group(2).strip()
            continue
        parts = line.split(";")
        if len(parts) != 8:
            if ";" not in line and not line.startswith("Scheme Code"):
                amc = line
            continue
        code, isin1, isin2, name, _plan, _option, nav, nav_date = (p.strip() for p in parts)
        if code == "Scheme Code" or category is None:
            continue
        scheme = Scheme(code, name, amc or "", category, nav, nav_date)
        for isin in (isin1, isin2):
            if isin and isin != "-":
                by_isin.setdefault(isin, scheme)
    return AmfiIndex(by_isin)


def load(today: date | None = None, cache_dir: Path = CACHE_DIR) -> AmfiIndex:
    today = today or date.today()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"NAVAll-{today.isoformat()}.txt"
    if not path.exists():
        resp = httpx.get(NAV_URL, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        path.write_text(resp.text, encoding="utf-8")
    return parse(path.read_text(encoding="utf-8-sig"))
