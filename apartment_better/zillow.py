"""Zillow rentals over plain HTTP. The search and detail pages embed their data in __NEXT_DATA__."""
import json
import re
import time
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx

from . import config
from .util import floors_from_text, money, polite, tier_for

BASE = "https://www.zillow.com"
HEADERS = {
    "User-Agent": config.UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
PAGE_SIZE = 41
MAX_PAGES = 20  # Zillow serves no more than this many pages for one search


def client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=40, follow_redirects=True, http2=False)


def _get(c: httpx.Client, url: str, tries: int = 7) -> str:
    wait = 30.0  # when Zillow rate-limits, back off for minutes: 30s, 60s ... capped at 10 min
    for n in range(tries):
        try:
            r = c.get(url)
            if r.status_code == 200 and "__NEXT_DATA__" in r.text:
                return r.text
            if r.status_code in (403, 429) or "px-captcha" in r.text[:5000].lower():
                time.sleep(wait); wait = min(wait * 2, 600)
                continue
            if r.status_code == 404:
                return ""
        except httpx.HTTPError:
            time.sleep(wait); wait = min(wait * 2, 600)
    raise RuntimeError("zillow blocked or unreachable: " + url[:120])


def _next_data(html: str) -> Dict[str, Any]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else {}


def search_url(cfg: dict, area: dict, page: int) -> str:
    p = cfg.get("search_price", cfg["price"])
    state = {
        "pagination": {"currentPage": page} if page > 1 else {},
        "isMapVisible": True,
        "mapBounds": {k: area[k] for k in ("west", "east", "south", "north")},
        "filterState": {
            "price": {"min": p["min"], "max": p["max"]}, "mp": {"min": p["min"], "max": p["max"]},
            "fr": {"value": True}, "fsba": {"value": False}, "fsbo": {"value": False},
            "nc": {"value": False}, "cmsn": {"value": False}, "auc": {"value": False}, "fore": {"value": False},
        },
        "isListVisible": True,
    }
    return "%s/%s/rentals/?searchQueryState=%s" % (
        BASE, cfg["zillow_city_slug"], urllib.parse.quote(json.dumps(state, separators=(",", ":"))))


def _card_id(item: Dict[str, Any]) -> Optional[str]:
    url = item.get("detailUrl") or ""
    parts = [x for x in url.split("?")[0].split("/") if x]
    if "apartments" in parts and len(parts) >= 3:
        return parts[-1]
    m = re.search(r"/(\d+)_zpid", url)
    if m:
        return m.group(1)
    return item.get("zpid") or None


def parse_card(item: Dict[str, Any], cfg: dict) -> Optional[Dict[str, Any]]:
    lid = _card_id(item)
    if not lid:
        return None
    url = item.get("detailUrl") or ""
    if url.startswith("/"):
        url = BASE + url
    prices: List[int] = []
    beds: List[str] = []
    for u in item.get("units") or []:
        prices += money(u.get("price"))
        if u.get("beds") not in (None, ""):
            beds.append(str(u["beds"]))
    prices += money(item.get("price") if isinstance(item.get("price"), str) else "")
    if item.get("beds") not in (None, ""):
        beds.append(str(item["beds"]))
    ll = item.get("latLong") or {}
    lat, lng = ll.get("latitude"), ll.get("longitude")
    pmin, pmax = (min(prices), max(prices)) if prices else (None, None)
    beds_text = ",".join(sorted(set(beds), key=lambda b: (len(b), b))) if beds else None
    return {
        "id": "z:" + str(lid), "source": "zillow", "url": url,
        "name": item.get("buildingName") or item.get("addressStreet") or item.get("address"),
        "address": item.get("address"), "lat": lat, "lng": lng,
        "price_min": pmin, "price_max": pmax, "beds_text": beds_text,
        "tier": tier_for(lat, lng, pmin, pmax, cfg), "seed_rank": 999,
    }


def search_pages(cfg: dict, c: httpx.Client, area: dict, max_pages: int = MAX_PAGES,
                 depth: int = 0) -> Iterator[Dict[str, Any]]:
    """Every card for one map area. An area that Zillow caps is split into quarters."""
    seen = set()
    total_pages, total = 1, 0
    page = 1
    while page <= min(total_pages, max_pages):
        html = _get(c, search_url(cfg, area, page))
        if not html:
            break
        state = _next_data(html).get("props", {}).get("pageProps", {}).get("searchPageState", {})
        cat = state.get("cat1", {})
        lst = cat.get("searchList", {})
        total_pages = int(lst.get("totalPages") or 1)
        total = int(lst.get("totalResultCount") or 0)
        for item in cat.get("searchResults", {}).get("listResults", []):
            card = parse_card(item, cfg)
            if card and card["id"] not in seen:
                seen.add(card["id"])
                yield card
        page += 1
        polite(cfg)
    if total_pages >= MAX_PAGES and total > PAGE_SIZE * MAX_PAGES * 0.9 and depth < 3:
        mid_lat = (area["south"] + area["north"]) / 2
        mid_lng = (area["west"] + area["east"]) / 2
        for south, north in ((area["south"], mid_lat), (mid_lat, area["north"])):
            for west, east in ((area["west"], mid_lng), (mid_lng, area["east"])):
                sub = {"south": south, "north": north, "west": west, "east": east}
                for card in search_pages(cfg, c, sub, max_pages, depth + 1):
                    if card["id"] not in seen:
                        seen.add(card["id"])
                        yield card


# ---- detail page ----------------------------------------------------------

def _photo_key(url: str) -> Optional[str]:
    m = re.match(r"https://photos\.zillowstatic\.com/fp/([0-9a-f]+)-", url)
    return m.group(1) if m else None


def _photo_url(key: str) -> str:
    return "https://photos.zillowstatic.com/fp/%s-cc_ft_768.webp" % key


def _collect_photos(node: Any, out: List[str], skip: Tuple[str, ...] = ("comps",)) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in skip:
                continue
            _collect_photos(v, out, skip)
    elif isinstance(node, list):
        for v in node:
            _collect_photos(v, out, skip)
    elif isinstance(node, str):
        key = _photo_key(node)
        if key:
            out.append(key)


def parse_detail(html: str) -> Dict[str, Any]:
    data = _next_data(html)
    try:
        b = data["props"]["pageProps"]["componentProps"]["initialReduxState"]["gdp"]["building"]
    except (KeyError, TypeError):
        return {}
    keys: List[str] = []
    # The building's own gallery first, then each unit's, then amenity photos. Never `comps`: those are other buildings.
    for part in (b.get("galleryPhotos"), b.get("photos"), b.get("signaturePhotos")):
        _collect_photos(part, keys)
    for fp in b.get("floorPlans") or []:
        for unit in fp.get("units") or []:
            _collect_photos(unit.get("galleryPhotos"), keys)
        _collect_photos(fp.get("floorPlanUnitGalleryPhotos"), keys)
    _collect_photos(b.get("galleryAmenityPhotos"), keys)
    photos, seen = [], set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            photos.append({"key": k, "url": _photo_url(k), "caption": None})
    amenities: List[str] = []
    sa = b.get("structuredAmenities") or {}
    for kind in ("buildingAmenities", "unitAmenities"):
        for g in (sa.get(kind) or {}).get("amenityGroups") or []:
            amenities += [str(a) for a in g.get("amenities") or []]
    custom = (b.get("buildingAttributes") or {}).get("customAmenities") or ""
    amenities += [x.strip() for x in re.split(r"[?;|]", custom) if x.strip()]
    floors = set()
    for fp in b.get("floorPlans") or []:
        for unit in fp.get("units") or []:
            f = unit.get("floor")
            if isinstance(f, int) or (isinstance(f, str) and f.isdigit()):
                floors.add(int(f))
            floors.update(floors_from_text(unit.get("unitNumber")))
    desc = " ".join(x for x in (b.get("description"), b.get("neighborhoodDescription")) if x)
    return {
        "name": b.get("buildingName"), "address": b.get("fullAddress"),
        "lat": b.get("latitude"), "lng": b.get("longitude"),
        "neighborhood": (b.get("neighborhood") or {}).get("name") if isinstance(b.get("neighborhood"), dict) else b.get("neighborhood"),
        "description": desc or None, "amenities": sorted(set(amenities)),
        "photos": photos, "floors": sorted(floors),
    }


def fetch_detail(c: httpx.Client, url: str) -> Dict[str, Any]:
    html = _get(c, url)
    return parse_detail(html) if html else {}
