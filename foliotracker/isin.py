import re

_INE = re.compile(r"^IN[E9]([0-9A-Z]{4})(\d{2})")

EQUITY_TYPES = {"01"}
BOND_TYPES = {"07", "08", "09"}


def security_kind(isin: str) -> str:
    if isin.startswith("INF"):
        return "mf"
    m = _INE.match(isin)
    if not m:
        return "equity"
    code = m.group(2)
    if code in EQUITY_TYPES:
        return "equity"
    if code in BOND_TYPES:
        return "bond"
    return "equity"
