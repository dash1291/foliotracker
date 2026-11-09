from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Account, Allocation, Instrument
from .holdings import AccountHoldings
from .manual import MANUAL_WRAPPERS, ManualHolding

RULES = yaml.safe_load((Path(__file__).with_name("categories.yaml")).read_text())
CLASSES: dict[str, str] = RULES["classes"]
CONFIDENCE_ORDER = {"manual": 0, "exact": 1, "estimated": 2, "override_required": 3}

Vector = dict[str, Decimal]


@dataclass
class Resolved:
    vector: Vector
    confidence: str
    basis: str

    @property
    def dominant(self) -> str:
        return max(self.vector.items(), key=lambda kv: kv[1])[0]

    @property
    def summary(self) -> str:
        parts = sorted(self.vector.items(), key=lambda kv: -kv[1])
        if len(parts) == 1:
            return CLASSES[parts[0][0]]
        return ", ".join(f"{int(round(w * 100))}% {CLASSES[c]}" for c, w in parts)


def _vec(raw: dict) -> Vector:
    return {k: Decimal(str(v)) for k, v in raw.items()}


def _norm(text: str) -> str:
    text = text.lower().replace("**", "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*fund(s)?$", "", text)
    return text.strip()


def split_category(category: str) -> tuple[str, str]:
    if " - " in category:
        group, sub = category.split(" - ", 1)
    else:
        group, sub = category, category
    return _norm(group), _norm(sub)


def resolve_category(category: str | None, name: str) -> Resolved | None:
    if not category:
        return None
    group, sub = split_category(category)
    group_sub = f"{group} - {sub}"
    for rule in RULES["categories"]:
        for m in rule["match"]:
            if sub == m or group_sub == m or (len(m) > 6 and m in sub):
                return Resolved(_vec(rule["vector"]), rule["confidence"], f"category: {category}")
    for prefix, rule in RULES["groups"].items():
        if group.startswith(prefix):
            return Resolved(_vec(rule["vector"]), rule["confidence"], f"category: {category}")
    return None


def resolve_name(name: str, category: str | None) -> Resolved | None:
    lowered = name.lower()
    for rule in RULES["names"]:
        if re.search(rule["pattern"], lowered):
            where = f" under {category}" if category else ""
            return Resolved(_vec(rule["vector"]), "exact", f"name match{where}")
    return None


def resolve_instrument(inst: Instrument, override: Allocation | None = None) -> Resolved:
    if override:
        return Resolved(_vec(json.loads(override.vector)), "manual", f"manual, {override.as_of:%d %b %Y}")
    if inst.kind == "equity":
        return Resolved({"equity_in": Decimal(1)}, "exact", "listed share")
    if inst.kind == "bond":
        return Resolved({"debt": Decimal(1)}, "exact", "bond")
    if inst.kind == "nps_scheme":
        vec = RULES["nps"].get(inst.nps_asset_class or "")
        if vec:
            return Resolved(_vec(vec), "exact", f"NPS scheme {inst.nps_asset_class}")
        return Resolved({"equity_in": Decimal("0.5"), "debt": Decimal("0.5")}, "override_required", "NPS scheme of unknown class")
    hit = resolve_category(inst.category, inst.name)
    if hit and hit.confidence != "override_required":
        return hit
    by_name = resolve_name(inst.name, inst.category)
    if by_name and (hit is None or hit.confidence == "override_required" and _is_other_group(inst.category)):
        return by_name
    if hit:
        return hit
    return Resolved({"equity_in": Decimal("0.5"), "debt": Decimal("0.5")}, "override_required", "no category match")


def _is_other_group(category: str | None) -> bool:
    if not category:
        return True
    group, _ = split_category(category)
    return group.startswith(("other", "index", "exchange traded", "fund of funds", "overseas"))


def resolve_account(acct: Account) -> Resolved:
    if acct.asset_class in CLASSES:
        return Resolved({acct.asset_class: Decimal(1)}, "manual", f"{MANUAL_WRAPPERS.get(acct.wrapper, acct.wrapper)}, chosen at entry")
    vec = RULES["wrappers"].get(acct.wrapper)
    if vec:
        return Resolved(_vec(vec), "exact", f"{acct.wrapper} account")
    return Resolved({"other": Decimal(1)}, "override_required", "manual account of unknown class")


def latest_overrides(session: Session) -> dict[int, Allocation]:
    rows = session.scalars(
        select(Allocation).where(Allocation.instrument_id.is_not(None)).order_by(Allocation.as_of, Allocation.id)
    ).all()
    return {a.instrument_id: a for a in rows}


@dataclass
class HoldingRow:
    account: Account
    instrument: Instrument | None
    value: Decimal
    resolved: Resolved

    @property
    def name(self) -> str:
        return self.instrument.name if self.instrument else self.account.display_name


@dataclass
class Breakdown:
    total: Decimal
    by_class: dict[str, Decimal]
    by_wrapper: dict[str, Decimal]
    by_holder: dict[str, Decimal]
    rows: list[HoldingRow]
    estimated: int
    override_required: int


def breakdown(session: Session, groups: list[AccountHoldings], manual: list[ManualHolding] = ()) -> Breakdown:
    overrides = latest_overrides(session)
    by_class = {c: Decimal(0) for c in CLASSES}
    by_wrapper: dict[str, Decimal] = {}
    by_holder: dict[str, Decimal] = {}
    rows: list[HoldingRow] = []
    estimated = override_required = 0
    for g in groups:
        for p in g.positions:
            r = resolve_instrument(p.instrument, overrides.get(p.instrument_id))
            value = p.current_value
            for c, w in r.vector.items():
                by_class[c] += value * w
            wkey = {"mf": "Mutual funds and ETFs", "equity": "Direct stocks", "bond": "Bonds", "nps_scheme": "NPS"}.get(
                p.instrument.kind, p.instrument.kind
            )
            by_wrapper[wkey] = by_wrapper.get(wkey, Decimal(0)) + value
            holder = g.account.holders or "Unknown holder"
            by_holder[holder] = by_holder.get(holder, Decimal(0)) + value
            estimated += r.confidence == "estimated"
            override_required += r.confidence == "override_required"
            rows.append(HoldingRow(g.account, p.instrument, value, r))
    for m in manual:
        r = resolve_account(m.account)
        for c, w in r.vector.items():
            by_class[c] += m.value * w
        wkey = MANUAL_WRAPPERS.get(m.account.wrapper, m.account.wrapper)
        by_wrapper[wkey] = by_wrapper.get(wkey, Decimal(0)) + m.value
        holder = m.account.holders or "Unknown holder"
        by_holder[holder] = by_holder.get(holder, Decimal(0)) + m.value
        rows.append(HoldingRow(m.account, None, m.value, r))
    total = sum(by_class.values(), Decimal(0))
    rows.sort(key=lambda r: (CONFIDENCE_ORDER[r.resolved.confidence] * -1, -r.value))
    return Breakdown(total, by_class, by_wrapper, by_holder, rows, estimated, override_required)
