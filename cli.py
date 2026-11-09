#!/usr/bin/env python3
"""Apartment Better: discover -> details -> classify -> search.   `python cli.py run` does the first three."""
import argparse
import json
import os
import sys

from apartment_better import config, pipeline, search
from apartment_better.db import DB


def main() -> None:
    ap = argparse.ArgumentParser(prog="apartment-better")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "details", "classify", "run"):
        p = sub.add_parser(name)
        p.add_argument("--sources", default=",".join(pipeline.available_sources()))
        p.add_argument("--limit", type=int, help="discover: max new listings; details/classify: max listings")
        p.add_argument("--max-pages", type=int)
        p.add_argument("--expand", action="store_true", help="discover/run: search the whole city at any price (queued after the home area)")
        p.add_argument("--only", help="details/classify: just this source (zillow or apartments)")
        p.add_argument("--images", type=int, help="classify: stop after this many photos")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--max-price", type=int)
    s.add_argument("--min-price", type=int)
    s.add_argument("--home-only", action="store_true", help="only listings inside the home area and price range")
    s.add_argument("--no-llm", action="store_true", help="skip query expansion")
    s.add_argument("--min-nice", type=float, help="average 1-10 interior niceness score")
    s.add_argument("--sort", choices=["relevance", "nice"], default="relevance")
    s.add_argument("--floor", type=int, help="only listings with a unit on this floor (also parsed from the query: 'second floor porch')")
    s.add_argument("--json", action="store_true")
    fp = sub.add_parser("floors", help="fill in floor numbers for stored listings")
    fp.add_argument("--fetch", action="store_true", help="also re-read Zillow pages for their floor field")
    cp = sub.add_parser("character", help="judge each building (type, era, porches) from its exterior photos")
    cp.add_argument("--only", help="just this source (zillow or apartments)")
    cp.add_argument("--redo-unknown", action="store_true", help="retry buildings judged 'not visible' (better photos may be known now)")
    cp.add_argument("--limit", type=int)
    dp = sub.add_parser("describe", help="describe one photo file with the vision model")
    dp.add_argument("path")
    sub.add_parser("picks", help="write data/picks.html and data/picks.md: vintage, private porch, not ground floor")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("web")
    a = ap.parse_args()

    cfg = config.load()
    db = DB()
    if a.cmd == "stop":
        open(pipeline.STOP_FILE, "w").close()
        print("stop requested; the running job finishes its current listing and exits")
        return
    if os.path.exists(pipeline.STOP_FILE) and a.cmd in ("discover", "details", "classify", "run", "character"):
        os.remove(pipeline.STOP_FILE)
    if a.cmd == "describe":
        from apartment_better import vision
        node = next(n for n in cfg["vision_nodes"] if n.get("enabled"))
        from apartment_better import character
        jpeg = vision.prep(open(a.path, "rb").read(), cfg.get("image_max_px", 768))
        d = vision.describe(node, jpeg, node.get("timeout", 600))
        for k in ("room", "features", "outdoor_space", "light", "view", "elevation", "notes"):
            print("%-15s%s" % (k + ":", ", ".join(d[k]) if isinstance(d[k], list) else d[k]))
        b = character.describe(node, [jpeg], node.get("timeout", 600))
        print()
        for k in ("building_type", "era", "porch", "porch_level", "character", "notes"):
            v = b.get(k)
            print("%-15s%s" % (k + ":", ", ".join(map(str, v)) if isinstance(v, list) else v))
    elif a.cmd == "character":
        from apartment_better import character
        character.run(cfg, db, a.only, a.redo_unknown, a.limit)
    elif a.cmd == "picks":
        from apartment_better import picks
        print(picks.write(db, cfg))
    elif a.cmd == "floors":
        pipeline.refloors(cfg, db, a.fetch)
    elif a.cmd == "status":
        print(json.dumps(db.status(), indent=2))
    elif a.cmd == "discover":
        pipeline.discover(cfg, db, a.sources.split(","), a.limit, a.max_pages, a.expand)
    elif a.cmd == "details":
        pipeline.details(cfg, db, a.limit, a.only)
    elif a.cmd == "classify":
        pipeline.classify(cfg, db, a.limit, a.images, a.only)
    elif a.cmd == "run":
        pipeline.discover(cfg, db, a.sources.split(","), a.limit, a.max_pages, a.expand)
        pipeline.details(cfg, db)
        pipeline.classify(cfg, db, None, a.images)
    elif a.cmd == "search":
        res = search.search(cfg, db, a.query, a.limit, a.max_price, a.min_price, a.home_only, not a.no_llm, a.min_nice, a.sort, a.floor)
        if a.json:
            print(json.dumps(res, indent=2))
            return
        for i, r in enumerate(res, 1):
            pr = "-".join(str(x) for x in dict.fromkeys(r["price"]) if x) or "?"
            print("%2d. %s | %s | $%s | %s | floors %s | nice %s | %d/%s photos described%s" % (
                i, r["name"], r["address"], pr, r["beds"], ",".join(map(str, r["floors"])) or "?", r["nice"], r["photos_described"], r["photos_total"],
                "" if r["tier"] == 0 else " | outside home area/price"))
            print("    " + r["url"])
            for e in r["photo_evidence"][:3]:
                print("    - [%s] %s" % (e["room"], e["match"]))
            if r["listing_text_match"]:
                print("    - listing text: " + r["listing_text_match"])
        if not res:
            print("no matches (photos described so far: %s)" % db.status()["images"].get("done", 0))
    elif a.cmd == "web":
        from apartment_better import web
        web.serve(cfg, db)


if __name__ == "__main__":
    main()
