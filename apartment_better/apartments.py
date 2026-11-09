"""Apartments.com through a real Chrome (see browser.py)."""
import json
import re
from typing import Any, Dict, Iterator, List

from bs4 import BeautifulSoup

from .browser import Browser
from .util import floors_from_text, money, polite, price_overlaps, tier_for

BASE = "https://www.apartments.com"
SIZE_PREF = ["111", "116", "115", "110", "105", "102"]  # size codes the site uses, best first


def search_url(neigh: str, cfg: dict, page: int) -> str:
    p = cfg.get("search_price", cfg["price"])
    place = "%s-%s" % (neigh, cfg["apartments_city_slug"]) if neigh else cfg["apartments_city_slug"]
    path = "%s/%s/%d-to-%d/" % (BASE, place, p["min"], p["max"])
    return path + ("%d/" % page if page > 1 else "")


def parse_search(html: str, cfg: dict, seed_rank: int) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", html)
    pages = int(m.group(2)) if m else 1
    cards: List[Dict[str, Any]] = []
    for a in soup.select("article.placard"):
        lid = a.get("data-listingid")
        link = a.select_one("a.property-link")
        if not lid or not link:
            continue
        text = a.get_text(" | ", strip=True)
        prices = money(text)
        title = a.select_one(".property-title, .js-placardTitle")
        addr = a.select_one(".property-address")
        beds = re.findall(r"Studio|\d+\s*Beds?|\d+\s*bd", text)
        pmin, pmax = (min(prices), max(prices)) if prices else (None, None)
        cards.append({
            "id": "a:" + lid, "source": "apartments", "url": link["href"],
            "name": title.get_text(strip=True) if title else None,
            "address": addr.get_text(strip=True) if addr else None,
            "price_min": pmin, "price_max": pmax,
            "beds_text": ",".join(dict.fromkeys(beds)) or None,
            "tier": 0 if price_overlaps(pmin, pmax, cfg["price"]) else 1,
            "seed_rank": seed_rank,
        })
    return {"pages": pages, "cards": cards}


def search_neighborhood(cfg: dict, br: Browser, neigh: str, seed_rank: int) -> Iterator[Dict[str, Any]]:
    page, pages = 1, 1
    while page <= min(pages, cfg.get("max_pages_per_search", 25)):
        html = br.html(search_url(neigh, cfg, page))
        res = parse_search(html, cfg, seed_rank)
        pages = res["pages"]
        if not res["cards"]:
            break
        for c in res["cards"]:
            yield c
        page += 1
        polite(cfg)


def _slug(url: str) -> str:
    return [x for x in url.split("?")[0].split("/") if x][-2] if url.count("/") >= 4 else ""


def parse_detail(html: str, url: str, cfg: dict) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    slug = _slug(url)
    first = re.search(r'https://images\d?\.apartments\.com/i2/[^/"\'\s)]+/\d+/([^"\'\s)\\]+?)-primary-photo\.', html)
    if first:
        slug = first.group(1)  # file names start with the page's own property name, which can differ from the URL slug
    # Photos: only files named after this property. The page also carries other listings' pictures.
    best: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    for h, size, name in re.findall(r'https://images\d?\.apartments\.com/i2/([^/"\'\s)]+)/(\d+)/([^"\'\s)\\]+)', html):
        if not name.startswith(slug):
            continue
        name = name.split("?")[0]
        rank = SIZE_PREF.index(size) if size in SIZE_PREF else 99
        if h not in best:
            order.append(h)
        if h not in best or rank < best[h]["rank"]:
            best[h] = {"rank": rank, "url": "https://images1.apartments.com/i2/%s/%s/%s" % (h, size, name), "name": name}
    photos = []
    for h in order:
        label = re.sub(r"^" + re.escape(slug) + r"-?", "", best[h]["name"]).rsplit(".", 1)[0].replace("-", " ").strip()
        photos.append({"key": h, "url": best[h]["url"], "caption": label or None})

    def text(sel: str) -> str:
        e = soup.select_one(sel)
        return e.get_text(" ", strip=True) if e else ""

    amen = []
    for sec in soup.select("#amenitiesSection, .amenitiesSection, .specList, .spec-list"):
        amen += [li.get_text(" ", strip=True) for li in sec.select("li")]
    for li in soup.select(".amenityLabel, .combinedAmenitiesList li"):
        amen.append(li.get_text(" ", strip=True))
    lat = re.search(r'"latitude"\s*:\s*"?(-?[\d.]+)', html)
    lng = re.search(r'"longitude"\s*:\s*"?(-?[\d.]+)', html)
    lat_v = float(lat.group(1)) if lat else None
    lng_v = float(lng.group(1)) if lng else None
    desc = text("#descriptionSection") or text(".descriptionSection")
    price_info = text(".priceBedRangeInfo")
    prices = money(price_info)
    nb = text(".neighborhoodAddress a, .neighborhood")
    return {
        "name": text("h1") or None, "address": text("#propertyAddressRow") or None,
        "lat": lat_v, "lng": lng_v, "neighborhood": nb or None,
        "description": desc or None, "amenities": sorted(set(a for a in amen if a)),
        "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
        "photos": photos,
        "floors": sorted({int(m) for m in re.findall(r'class="floor-text">\s*Floor\s*(\d+)', html)}),
    }


def fetch_detail(br: Browser, url: str, cfg: dict) -> Dict[str, Any]:
    return parse_detail(br.html(url, wait_ms=3000), url, cfg)
