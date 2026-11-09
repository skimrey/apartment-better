import random
import re
import time
from typing import List, Optional


def polite(cfg: dict) -> None:
    d = cfg.get("delay_seconds", {"min": 1.5, "max": 3.5})
    time.sleep(random.uniform(d["min"], d["max"]))


def money(text: Optional[str]) -> List[int]:
    """Every dollar amount in a string like '$1,688 - $4,794' or '$2,563+'."""
    return [int(x.replace(",", "")) for x in re.findall(r"\$\s*([\d,]{3,7})", text or "")]


def in_area(lat: Optional[float], lng: Optional[float], area: dict) -> bool:
    if lat is None or lng is None:
        return False
    return area["south"] <= lat <= area["north"] and area["west"] <= lng <= area["east"]


def price_overlaps(pmin: Optional[int], pmax: Optional[int], want: dict) -> bool:
    if pmin is None and pmax is None:
        return False
    lo = pmin if pmin is not None else pmax
    hi = pmax if pmax is not None else pmin
    return lo <= want["max"] and hi >= want["min"]


def tier_for(lat, lng, pmin, pmax, cfg) -> int:
    """0 = inside the home area and the price range, 1 = everything else."""
    return 0 if in_area(lat, lng, cfg["area"]) and price_overlaps(pmin, pmax, cfg["price"]) else 1


_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}
_FLOOR_RE = [
    re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)\s*(?:floor|flr|fl)\b", re.I),
    re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth)\s*(?:floor|flr)\b", re.I),
    re.compile(r"\bfloor\s*(\d{1,2})\b", re.I),
]


_UNIT_RE = [
    re.compile(r"(?:\bunit|\bapt\.?|\bapartment|\bsuite|\bste\.?|#)\s*0*(\d{1,2})\s*-?\s*[A-Za-z]{1,2}\b", re.I),
    re.compile(r"(?:\bunit|\bapt\.?|\bapartment|#)\s*(\d)\d{2}\b(?!\d)", re.I),
    re.compile(r"(?:\bunit|\bapt\.?|\bapartment|#)\s*(\d{2})\d{2}\b", re.I),
    re.compile(r"\b(?:ave|avenue|st|street|rd|road|blvd|dr|drive|pl|place|ct|court|ln|way|pkwy)\.?,?\s+#?0*(\d{1,2})[A-Za-z]{1,2}\b", re.I),
]


def floors_from_text(text: Optional[str]) -> List[int]:
    """Floor numbers named in a title/address ('2ND FLR', 'Second Floor', 'Floor 3'). Not guessed from unit numbers."""
    out: List[int] = []
    t = text or ""
    for rx in _FLOOR_RE:
        for m in rx.finditer(t):
            g = m.group(1).lower()
            out.append(_ORD.get(g) or int(g))
    # Unit labels carry the floor: "2F", "Unit 3A", "#2R", "Apt 304" (3 digits: floor 3), "1203" (4 digits: floor 12).
    for rx in _UNIT_RE:
        for m in rx.finditer(t):
            out.append(int(m.group(1)))
    if re.search(r"\b(ground floor|garden level|garden unit|basement)\b", t, re.I):
        out.append(1 if "ground" in t.lower() or "garden" in t.lower() else 0)
    return sorted(set(n for n in out if 0 <= n <= 99))


def floor_from_query(query: str):
    """'second floor porch' -> (2, 'porch'). Returns (None, query) when no floor is named."""
    for rx in _FLOOR_RE[:2]:
        m = rx.search(query)
        if m:
            g = m.group(1).lower()
            rest = (query[:m.start()] + " " + query[m.end():]).strip(" ,")
            return (_ORD.get(g) or int(g)), (rest or query)
    return None, query
