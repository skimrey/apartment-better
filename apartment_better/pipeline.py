import hashlib
import json
import os
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import httpx

from . import config, vision, zillow

try:  # optional site module: delete apartments.py and browser.py and the tool still runs Zillow-only
    from . import apartments
except ImportError:
    apartments = None
from .db import DB
from .util import floors_from_text, polite, tier_for

STOP_FILE = os.path.join(config.DATA, "STOP")


def stopped() -> bool:
    return os.path.exists(STOP_FILE)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


# ---- 1. discover listings ---------------------------------------------------

def available_sources() -> List[str]:
    return ["zillow"] + (["apartments"] if apartments else [])


def discover(cfg: dict, db: DB, sources: List[str], limit: Optional[int] = None, max_pages: Optional[int] = None,
             expand: bool = False) -> None:
    """expand=True searches the whole city at any price (config "expand"). New listings are tier 1, so they
    queue behind everything inside the home area and price range."""
    if expand:
        cfg = dict(cfg)
        cfg["search_area"] = cfg["expand"]["area"]
        cfg["search_price"] = cfg["expand"]["price"]
    if "apartments" in sources and not apartments:
        log("apartments module not installed; skipping")
        sources = [x for x in sources if x != "apartments"]
    n_new = 0
    if "zillow" in sources:
        c = zillow.client()
        log("zillow: searching %s" % cfg["city"])
        try:
            for card in zillow.search_pages(cfg, c, cfg.get("search_area", cfg["area"]), max_pages or zillow.MAX_PAGES):
                n_new += db.upsert_listing(card)
                if stopped() or (limit and n_new >= limit):
                    break
        except Exception as e:  # keep going with what was found; rerun discover later for the rest
            log("zillow: search stopped early: %s" % e)
        log("zillow: done, %d new" % n_new)
    if "apartments" in sources and not stopped():
        from .browser import Browser
        br = Browser(cfg)
        try:
            total = 0
            # Expand: the whole city in price bands (one search stops after a few dozen pages); otherwise the home neighborhoods.
            jobs = ([("", {"min": lo, "max": hi}) for lo, hi in cfg["expand"].get("apartments_price_bands", [])]
                    if expand else [(n, cfg["price"]) for n in cfg["apartments_neighborhoods"]])
            for rank, (neigh, price) in enumerate(jobs):
                if stopped() or (limit and total >= limit):
                    break
                got = 0
                try:
                    c2 = dict(cfg, search_price=price)
                    for card in apartments.search_neighborhood(c2, br, neigh, rank):
                        got += 1
                        total += db.upsert_listing(card)
                        if max_pages and got >= max_pages * 40:
                            break
                except Exception as e:  # one search failing must not stop the rest
                    log("apartments: %s %s failed: %s" % (neigh or "city", price, e))
                log("apartments: %s %s-%s -> %d cards (%d new so far)" % (neigh or "city", price["min"], price["max"], got, total))
        finally:
            br.close()
    log("discover finished: %s" % json.dumps(db.status()["listings"]))


# ---- 2. listing details ----------------------------------------------------

def details(cfg: dict, db: DB, limit: Optional[int] = None, source: Optional[str] = None) -> None:
    rows = db.q("SELECT id, source, url FROM listings WHERE detail_at IS NULL %s "
                "ORDER BY tier, seed_rank, first_seen" % ("AND source='%s'" % source if source else ""))
    if limit:
        rows = rows[:limit]
    log("details: %d listings to fetch" % len(rows))
    zc = zillow.client()
    br = None
    try:
        for i, r in enumerate(rows):
            if stopped():
                break
            try:
                if r["source"] == "zillow":
                    d = zillow.fetch_detail(zc, r["url"])
                else:
                    if br is None:
                        from .browser import Browser
                        br = Browser(cfg)
                    d = apartments.fetch_detail(br, r["url"], cfg)
                    d.pop("price_min", None)  # the page shows the whole building's range; the card had the unit price
                    d.pop("price_max", None)
                if not d:
                    db.x("UPDATE listings SET detail_at=? WHERE id=?", (time.time(), r["id"]))
                    continue
                photos = d.pop("photos")
                amen = d.pop("amenities")
                fl = set(d.pop("floors", []) or [])
                named = db.one("SELECT name, address FROM listings WHERE id=?", (r["id"],))
                fl.update(floors_from_text(" ".join(x for x in (named["name"], named["address"], d.get("name"), d.get("address")) if x)))
                d["floors"] = json.dumps(sorted(fl)) if fl else None
                row = {k: v for k, v in d.items() if v is not None}
                row["id"] = r["id"]
                row["amenities"] = json.dumps(amen)
                db.upsert_listing(row)
                cur = db.one("SELECT lat, lng, price_min, price_max FROM listings WHERE id=?", (r["id"],))
                db.x("UPDATE listings SET tier=? WHERE id=?",
                     (tier_for(cur["lat"], cur["lng"], cur["price_min"], cur["price_max"], cfg), r["id"]))
                db.add_images(r["id"], photos)
                db.index_listing(r["id"])
                log("details %d/%d %s: %d photos" % (i + 1, len(rows), r["id"], len(photos)))
            except Exception as e:
                log("details %s failed: %s" % (r["id"], e))
            polite(cfg)
    finally:
        if br:
            br.close()


def refloors(cfg: dict, db: DB, fetch: bool = False) -> None:
    """Fill in `floors` for listings already stored. Offline from names first; with fetch, re-read Zillow pages (no photos)."""
    n = 0
    for r in db.q("SELECT id, name, address, floors FROM listings"):
        fl = set(json.loads(r["floors"] or "[]")) | set(floors_from_text(" ".join(x for x in (r["name"], r["address"]) if x)))
        if fl and json.dumps(sorted(fl)) != (r["floors"] or ""):
            db.x("UPDATE listings SET floors=? WHERE id=?", (json.dumps(sorted(fl)), r["id"]))
            n += 1
    log("floors: %d listings updated from names" % n)
    if not fetch:
        return
    zc = zillow.client()
    rows = db.q("SELECT id, url FROM listings WHERE source='zillow' AND detail_at IS NOT NULL ORDER BY tier, seed_rank")
    for i, r in enumerate(rows):
        if stopped():
            break
        try:
            d = zillow.fetch_detail(zc, r["url"])
            cur = db.one("SELECT floors FROM listings WHERE id=?", (r["id"],))
            fl = set(json.loads(cur["floors"] or "[]")) | set(d.get("floors") or [])
            if fl:
                db.x("UPDATE listings SET floors=? WHERE id=?", (json.dumps(sorted(fl)), r["id"]))
        except Exception as e:
            log("floors %s failed: %s" % (r["id"], e))
        if i % 50 == 0:
            log("floors: %d/%d zillow pages re-read" % (i, len(rows)))
        polite(cfg)


# ---- 3. photo pass -----------------------------------------------------------

def _fetch_image(cfg, zc, br, source: str, url: str) -> Optional[bytes]:
    if source == "zillow":
        r = zc.get(url)
        return r.content if r.status_code == 200 else None
    return br.fetch(url)


def _copy_from_twin(db: DB, img_id: int, sha1: str) -> bool:
    t = db.one("SELECT * FROM images WHERE sha1=? AND status='done' AND id<>? LIMIT 1", (sha1, img_id))
    if not t:
        return False
    db.x("UPDATE images SET status='done', room=?, features=?, outdoor_space=?, windows=?, light=?, view=?, "
         "elevation=?, notes=?, flooring=?, walls=?, style=?, condition=?, nice=?, model=?, done_at=?, path=? WHERE id=?",
         (t["room"], t["features"], t["outdoor_space"], t["windows"], t["light"], t["view"], t["elevation"],
          t["notes"], t["flooring"], t["walls"], t["style"], t["condition"], t["nice"], t["model"], time.time(), t["path"], img_id))
    db.index_image(img_id)
    return True


class NodeBackoff:
    """Per-node cool-off so a struggling box is not hammered: 1 min after the first failure, doubling to 15 min."""

    def __init__(self):
        self.streak = 0

    def wait(self) -> int:
        secs = min(60 * 2 ** (self.streak - 1), 900)
        log("node failing (%d in a row): backing off %ds" % (self.streak, secs))
        end = time.time() + secs
        while time.time() < end and not stopped():
            time.sleep(2)
        return secs


def _work_image(cfg: dict, db: DB, node: dict, img: Dict[str, Any], bo: "NodeBackoff") -> None:
    try:
        jpeg = open(img["path"], "rb").read()
        d = vision.describe(node, jpeg, timeout=node.get("timeout", 240))
        db.x("UPDATE images SET status='done', room=?, features=?, outdoor_space=?, windows=?, light=?, view=?, "
             "elevation=?, notes=?, flooring=?, walls=?, style=?, condition=?, nice=?, model=?, done_at=?, error=NULL WHERE id=?",
             (d["room"], json.dumps(d["features"]), d["outdoor_space"], d["windows"], d["light"], d["view"],
              d["elevation"], d["notes"], d["flooring"], d["walls"], d["style"], d["condition"], d["nice"],
              node["model"], time.time(), img["id"]))
        db.index_image(img["id"])
        bo.streak = 0
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
        # The node is struggling, not the photo: put it back without using up an attempt, and cool off.
        db.x("UPDATE images SET status='fetched', error=? WHERE id=?", ("node: " + str(e)[:200], img["id"]))
        bo.streak += 1
        bo.wait()
    except Exception as e:
        db.x("UPDATE images SET status='error', attempts=attempts+1, error=? WHERE id=?", (str(e)[:300], img["id"]))


def _has_priority(db: DB) -> bool:
    return "priority" in {r["name"] for r in db.q("PRAGMA table_info(listings)")}


def classify(cfg: dict, db: DB, limit_listings: Optional[int] = None, limit_images: Optional[int] = None,
             source: Optional[str] = None) -> None:
    """Listing by listing, fewest photos first (inside the home area / price range before the rest)."""
    nodes = [n for n in cfg["vision_nodes"] if n.get("enabled")]
    if not nodes:
        raise SystemExit("no vision node enabled in config.json")
    slots = [n for n in nodes for _ in range(n.get("workers", 1))]
    db.x("UPDATE images SET status='fetched' WHERE status='working'")
    zc = zillow.client()
    br = None
    done_images = 0
    done_listings = 0
    backoffs: Dict[int, NodeBackoff] = {}
    try:
        while not stopped():
            lst = db.one(
                "SELECT l.id, l.source, l.name, l.photo_count FROM listings l WHERE l.photo_count>0 AND EXISTS "
                "(SELECT 1 FROM images i WHERE i.listing_id=l.id AND (i.status IN ('new','fetched') "
                "OR (i.status='error' AND i.attempts<3))) " + ("AND l.source='%s' " % source if source else "") +
                "ORDER BY l.tier, " + ("COALESCE(l.priority, 0) DESC, " if _has_priority(db) else "") +
                "l.photo_count, l.seed_rank, l.id LIMIT 1")
            if not lst:
                log("classify: nothing left to do")
                break
            imgs = db.q("SELECT * FROM images WHERE listing_id=? AND (status IN ('new','fetched') "
                        "OR (status='error' AND attempts<3)) ORDER BY idx", (lst["id"],))
            ready: List[Dict[str, Any]] = []
            for im in imgs:
                im = dict(im)
                if not (im["path"] and os.path.exists(im["path"])):
                    try:
                        if lst["source"] != "zillow" and br is None:
                            from .browser import Browser
                            br = Browser(cfg)
                        raw = _fetch_image(cfg, zc, br, lst["source"], im["url"])
                        if not raw:
                            raise RuntimeError("download failed")
                        jpeg = vision.prep(raw, cfg.get("image_max_px", 768))
                    except Exception as e:
                        db.x("UPDATE images SET status='error', attempts=attempts+1, error=? WHERE id=?",
                             (str(e)[:300], im["id"]))
                        continue
                    sha = hashlib.sha1(jpeg).hexdigest()
                    path = os.path.join(config.IMAGES, sha + ".jpg")
                    if not os.path.exists(path):
                        open(path, "wb").write(jpeg)
                    im["sha1"], im["path"] = sha, path
                    db.x("UPDATE images SET sha1=?, path=?, status='fetched' WHERE id=?", (sha, path, im["id"]))
                if _copy_from_twin(db, im["id"], im["sha1"]):
                    continue  # this exact photo was already described (other listing or other site)
                ready.append(im)
            if ready:
                ids = [x["id"] for x in ready]
                db.x("UPDATE images SET status='working' WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
                # Each worker slot pulls the next photo as soon as it is free, so a fast node is never held up by a slow one.
                q: "queue.Queue" = queue.Queue()
                for im in ready:
                    q.put(im)

                def worker(node):
                    bo = backoffs.setdefault(id(node), NodeBackoff())
                    while not stopped():
                        try:
                            im = q.get_nowait()
                        except queue.Empty:
                            return
                        _work_image(cfg, db, node, im, bo)

                with ThreadPoolExecutor(max_workers=len(slots)) as ex:
                    for f in [ex.submit(worker, n) for n in slots]:
                        f.result()
                done_images += len(ready)
            db.index_summary(lst["id"])
            done_listings += 1
            left = db.one("SELECT COUNT(*) n FROM images WHERE status IN ('new','fetched','working')")["n"]
            log("classify: %s (%s) %d/%d photos described; %d photos left in queue" % (
                lst["id"], (lst["name"] or "")[:30], len(ready), len(imgs), left))
            if (limit_listings and done_listings >= limit_listings) or (limit_images and done_images >= limit_images):
                break
            polite({"delay_seconds": {"min": 0.2, "max": 0.6}})
    finally:
        if br:
            br.close()
