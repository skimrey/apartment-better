"""Building character: one vision call per listing over its exterior / outdoor photos.
Answers what single interior photos cannot: is this a vintage two-flat with stacked porches or a new-build box?"""
import base64
import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from . import config, vision, zillow
from .db import DB

PROMPT = """This photo is from a Chicago rental listing. Judge the BUILDING and its private outdoor space.
Reply with JSON only, with exactly these keys:
{"building_type": one of "two-flat or three-flat","greystone","brownstone or rowhouse","converted house","vintage courtyard building","vintage walk-up","vintage high-rise","new-build mid-rise","new-build high-rise","modern low-rise or infill","single-family house","not visible",
 "era": "pre-war vintage" | "mid-century" | "1970s-1990s" | "2000s or newer" | "unknown",
 "porch": [private outdoor spaces you can see: "front porch","back porch","stacked back porches","wooden deck","balcony","juliet balcony","stoop","patio","rooftop deck"; [] if none],
 "porch_level": "ground" | "upper" | "both" | "unknown" (which floors those porches or balconies are on),
 "character": 1-10 (classic architectural character: masonry, bay windows, cornices, stained or leaded glass, original woodwork; 1 = plain new-build box, 10 = beautiful vintage),
 "renovated": "yes" | "no" | "unknown" (do the interiors look recently updated?),
 "notes": one short factual sentence}"""

VINTAGE = {"two-flat or three-flat", "greystone", "brownstone or rowhouse", "converted house", "vintage courtyard building",
           "vintage walk-up", "single-family house"}

SCHEMA = """CREATE TABLE IF NOT EXISTS character (
  listing_id TEXT PRIMARY KEY, building_type TEXT, era TEXT, porch TEXT, porch_level TEXT, character INTEGER,
  renovated TEXT, notes TEXT, image_ids TEXT, model TEXT, done_at REAL, error TEXT)"""


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def ensure(db: DB) -> None:
    db.x(SCHEMA)


def pick_images(db: DB, listing_id: str, n: int = 1) -> List[Dict[str, Any]]:
    """The front of the building when the photo pass has seen it; otherwise the cover photo (usually the front).
    One photo per call: multi-photo requests knocked the P40 over."""
    rows = [dict(r) for r in db.q("SELECT id, idx, url, path, room, outdoor_space, status FROM images WHERE listing_id=? ORDER BY idx", (listing_id,))]
    def rank(r):
        return (0 if r["room"] == "exterior" else 1 if r["room"] == "outdoor space" else 2, r["idx"])
    return sorted(rows, key=rank)[:n]


def _jpeg(cfg, zc, br, source: str, im: Dict[str, Any], db: DB) -> Optional[bytes]:
    if im["path"] and os.path.exists(im["path"]):
        return open(im["path"], "rb").read()
    if source == "zillow":
        r = zc.get(im["url"])
        raw = r.content if r.status_code == 200 else None
    else:
        raw = br.fetch(im["url"]) if br else None
    if not raw:
        return None
    import hashlib
    jpeg = vision.prep(raw, cfg.get("image_max_px", 768))
    sha = hashlib.sha1(jpeg).hexdigest()
    path = os.path.join(config.IMAGES, sha + ".jpg")
    if not os.path.exists(path):
        open(path, "wb").write(jpeg)
    db.x("UPDATE images SET sha1=?, path=?, status=CASE WHEN status='new' THEN 'fetched' ELSE status END WHERE id=?", (sha, path, im["id"]))
    return jpeg


def describe(node: dict, jpegs: List[bytes], timeout: int) -> Dict[str, Any]:
    body = {"model": node["model"], "stream": False, "think": False, "format": "json", "keep_alive": "60m",
            "options": {"temperature": 0, "num_predict": 400},
            "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(j).decode() for j in jpegs]}]}
    r = httpx.post(node["url"].rstrip("/") + "/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    return vision._parse(r.json()["message"]["content"])


def run(cfg: dict, db: DB, source: Optional[str] = None, redo_unknown: bool = False, limit: Optional[int] = None) -> None:
    """Tier-0 listings first, cheapest signal first. Node trouble backs off instead of hammering the box."""
    from .pipeline import NodeBackoff, stopped
    ensure(db)
    node = next(n for n in cfg["vision_nodes"] if n.get("enabled"))
    where = "l.photo_count>0 AND l.tier=0"
    if source:
        where += " AND l.source='%s'" % source
    done_clause = ("(c.listing_id IS NULL OR c.error IS NOT NULL OR c.building_type='not visible')" if redo_unknown
                   else "(c.listing_id IS NULL OR c.error IS NOT NULL)")
    rows = db.q("SELECT l.id, l.source, l.name FROM listings l LEFT JOIN character c ON c.listing_id=l.id WHERE %s AND %s "
                "ORDER BY l.photo_count, l.seed_rank" % (where, done_clause))
    if limit:
        rows = rows[:limit]
    log("character: %d listings to judge" % len(rows))
    zc = zillow.client()
    br = None
    bo = NodeBackoff()
    try:
        for i, r in enumerate(rows):
            if stopped():
                break
            ims = pick_images(db, r["id"])
            jpegs, used = [], []
            if r["source"] != "zillow" and br is None and any(not (x["path"] and os.path.exists(x["path"])) for x in ims):
                from .browser import Browser
                br = Browser(cfg)
            for im in ims:
                try:
                    j = _jpeg(cfg, zc, br, r["source"], im, db)
                except Exception:
                    j = None
                if j:
                    jpegs.append(j)
                    used.append(im["id"])
            if not jpegs:
                db.x("INSERT OR REPLACE INTO character (listing_id, error, done_at) VALUES (?,?,?)", (r["id"], "no photos", time.time()))
                continue
            while not stopped():
                try:
                    d = describe(node, jpegs, node.get("timeout", 600))
                    bo.streak = 0
                    break
                except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError):
                    bo.streak += 1
                    bo.wait()
                except Exception as e:
                    d = {"error": str(e)[:200]}
                    break
            else:
                break
            porch = d.get("porch") or []
            if not isinstance(porch, list):
                porch = [porch]
            try:
                ch = int(d.get("character")) if d.get("character") is not None else None
            except (TypeError, ValueError):
                ch = None
            db.x("INSERT OR REPLACE INTO character (listing_id, building_type, era, porch, porch_level, character, renovated, notes, "
                 "image_ids, model, done_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (r["id"], vision._s(d.get("building_type")).lower(), vision._s(d.get("era")).lower(),
                  json.dumps([vision._s(p).lower() for p in porch if vision._s(p)]), vision._s(d.get("porch_level")).lower(), ch,
                  vision._s(d.get("renovated")).lower(), vision._s(d.get("notes")), json.dumps(used), node["model"], time.time(),
                  d.get("error")))
            log("character %d/%d %s (%s): %s, %s, porch %s %s, character %s" % (
                i + 1, len(rows), r["id"], (r["name"] or "")[:28], d.get("building_type"), d.get("era"), porch,
                d.get("porch_level"), d.get("character")))
    finally:
        if br:
            br.close()
