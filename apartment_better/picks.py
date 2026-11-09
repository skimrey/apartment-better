"""Rank listings for one renter's taste: vintage, a private porch / deck / balcony, not on the ground floor, nice inside.
Every point comes from something a person can check (a photo description, the building judgement, a phrase in the listing)."""
import html
import json
import os
import re
import time
from typing import Any, Dict, List

from . import config
from .character import VINTAGE, ensure
from .db import DB

PRIVATE_OUT = re.compile(r"\b(porch|deck|balcon(y|ies)|patio|terrace|veranda)\b")
TEXT_OUT = re.compile(r"\b(private|own|your|back|front|rear|huge|large|spacious|sunny|shared)?\s*(porch|deck|balcony|patio|terrace|veranda)\b")
ROOF_SHARED = re.compile(r"\b(shared|common|rooftop|roof deck|roof-top|amenity|sundeck|sun deck|pool)\b")
VINTAGE_TXT = re.compile(r"\b(vintage|greystone|graystone|brownstone|two[- ]flat|2[- ]flat|three[- ]flat|3[- ]flat|historic|"
                         r"original (wood|trim|detail)|crown molding|built-?ins?|bay window|coach house|victorian|courtyard building|"
                         r"classic chicago|tin ceiling|decorative fireplace|clawfoot|pocket doors?)\b")
NEW_TXT = re.compile(r"\b(luxury|fitness center|rooftop pool|sky ?lounge|amenity deck|concierge|co-?working|smart home|"
                     r"package lockers?|resident lounge|brand[- ]new construction)\b")
GROUND_TXT = re.compile(r"\b(garden (unit|apartment|level)|lower level|basement|1st floor|first floor|ground floor|english basement)\b")
UPPER_TXT = re.compile(r"\b(2nd|3rd|4th|second|third|fourth|top|upper)[- ](floor|fl|flr|level|unit)\b")


def _j(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def score(db: DB, l) -> Dict[str, Any]:
    lid = l["id"]
    c = db.one("SELECT * FROM character WHERE listing_id=? AND error IS NULL", (lid,))
    text = " ".join(x for x in (l["name"], l["address"], l["description"], " ; ".join(_j(l["amenities"], []))) if x).lower()
    ims = db.q("SELECT id, idx, path, room, outdoor_space, elevation, notes, features FROM images WHERE listing_id=? AND status='done'", (lid,))
    floors = _j(l["floors"], [])
    why, pts = [], 0.0

    # private outdoor space
    out_photos = [r for r in ims if PRIVATE_OUT.search((r["outdoor_space"] or "").lower())
                  and not ROOF_SHARED.search((r["outdoor_space"] or "").lower()) and "ground" not in (r["outdoor_space"] or "").lower()]
    porch = _j(c["porch"], []) if c else []
    porch = [p for p in porch if p not in ("stoop", "rooftop deck", "juliet balcony")]
    txt_out = [m.group(0).strip() for m in TEXT_OUT.finditer(text) if not (m.group(1) or "").startswith("shared")]
    out = 0.0
    if porch:
        out += 2; why.append("building photos: " + ", ".join(porch) + (" (%s)" % c["porch_level"] if c["porch_level"] not in ("", "unknown") else ""))
    if out_photos:
        out += min(2, 1 + len(out_photos) * 0.25); why.append("photo: " + out_photos[0]["outdoor_space"])
    if txt_out:
        out += 1.5; why.append("listing says: " + ", ".join(dict.fromkeys(txt_out[:3])))
    pts += min(out, 4)

    # floor: ground is out, upper is in
    lvl = (c["porch_level"] if c else "") or ""
    up_share = (sum(1 for r in ims if r["elevation"] == "upper floor") /
                max(1, sum(1 for r in ims if r["elevation"] in ("upper floor", "ground floor"))))
    ground = (floors and max(floors) <= 1) or bool(GROUND_TXT.search(text) and not UPPER_TXT.search(text))
    upper = (floors and max(floors) >= 2) or bool(UPPER_TXT.search(text)) or lvl in ("upper", "both")
    if upper:
        pts += 2
        m = UPPER_TXT.search(text)
        why.append("floor " + ",".join(map(str, floors)) if floors and max(floors) >= 2
                   else "listing says: " + m.group(0) if m else "porch or balcony on an upper level")
    elif up_share >= 0.7 and len(ims) >= 4:
        pts += 1; why.append("window views look above street level")

    # vintage character
    vt = [m.group(0) for m in VINTAGE_TXT.finditer(text)]
    new = [m.group(0) for m in NEW_TXT.finditer(text)]
    btype = (c["building_type"] if c else "") or ""
    newbuild = btype.startswith("new-build") or btype == "modern low-rise or infill" or (c and c["era"] == "2000s or newer")
    if c and (btype in VINTAGE or c["era"] == "pre-war vintage"):
        pts += 2 + max(0, ((c["character"] or 5) - 5) * 0.3); why.append("building: %s, %s, character %s/10" % (btype, c["era"], c["character"]))
    if vt:
        pts += 1; why.append("listing says: " + ", ".join(dict.fromkeys(vt[:4])))
    if newbuild:
        pts -= 3; why.append("new build (%s)" % btype)
    if len(new) >= 2:
        pts -= 1

    # inside
    sm = db.summary(lid)
    if sm.get("nice"):
        pts += (sm["nice"] - 5) * 0.4
        why.append("inside: %s/10%s" % (sm["nice"], ", " + sm["flooring"] if sm.get("flooring") else ""))
    if c and c["renovated"] == "yes":
        pts += 0.5

    eligible = out > 0 and not ground and not newbuild
    return {"id": lid, "score": round(pts, 2), "eligible": eligible, "why": why, "ground": bool(ground), "upper": bool(upper),
            "outdoor": out, "vintage": bool(c and (btype in VINTAGE or c["era"] == "pre-war vintage")) or bool(vt),
            "judged": bool(c), "nice": sm.get("nice"),
            "thumbs": [r["path"] for r in sorted(ims, key=lambda r: (0 if r in out_photos else 1 if r["room"] == "exterior" else 2, r["idx"])) if r["path"]][:4]
            or [r["path"] for r in db.q("SELECT path FROM images WHERE listing_id=? AND path IS NOT NULL ORDER BY idx LIMIT 4", (lid,))]}


def rank(db: DB, cfg: dict) -> List[Dict[str, Any]]:
    ensure(db)
    hi = cfg["price"]["max"]
    rows = db.q("SELECT * FROM listings WHERE tier=0 AND detail_at IS NOT NULL AND price_min<=?", (hi,))
    res = []
    for l in rows:
        s = score(db, l)
        s.update(name=l["name"], address=l["address"], url=l["url"], beds=l["beds_text"], source=l["source"],
                 price=[l["price_min"], l["price_max"]], floors=_j(l["floors"], []))
        res.append(s)
    res.sort(key=lambda s: (-s["eligible"], -s["score"]))
    return res


def set_priority(db: DB, ranked: List[Dict[str, Any]]) -> None:
    """The photo pass describes the most promising listings first."""
    have = {r["name"] for r in db.q("PRAGMA table_info(listings)")}
    if "priority" not in have:
        db.x("ALTER TABLE listings ADD COLUMN priority REAL DEFAULT 0")
    for s in ranked:
        db.x("UPDATE listings SET priority=? WHERE id=?", (s["score"] + (3 if s["eligible"] else 0), s["id"]))


def write(db: DB, cfg: dict, top: int = 40) -> str:
    ranked = rank(db, cfg)
    set_priority(db, ranked)
    picks = [s for s in ranked if s["eligible"]][:top]
    st = db.status()
    judged = db.one("SELECT COUNT(*) n FROM character WHERE error IS NULL")["n"]
    rel = lambda p: os.path.relpath(p, config.DATA) if p else ""
    h = ["<!doctype html><meta charset=utf-8><title>Apartment Better picks</title><style>body{font:15px system-ui;margin:24px;max-width:1100px}"
         ".c{border:1px solid #ddd;border-radius:8px;padding:12px;margin:14px 0}.t img{height:150px;margin:4px 4px 0 0;border-radius:4px}"
         "li{margin:2px 0}.s{color:#777}</style>",
         "<h1>Picks: vintage, private porch / deck / balcony, not ground floor, $%d-%d</h1>" % (cfg["price"]["min"], cfg["price"]["max"]),
         "<p class=s>Updated %s. %d listings in area and price, %d buildings judged, %s photos described. Ranked by the evidence listed under each one.</p>"
         % (time.strftime("%a %H:%M"), len(ranked), judged, st["images"].get("done", 0))]
    md = ["# Picks (updated %s)" % time.strftime("%a %H:%M"), ""]
    for i, s in enumerate(picks, 1):
        pr = "-".join(str(x) for x in dict.fromkeys(s["price"]) if x)
        h.append("<div class=c><b>%d. <a href='%s'>%s</a></b> &middot; $%s &middot; %s &middot; floors %s &middot; score %s<div class=s>%s</div>"
                 "<ul>%s</ul><div class=t>%s</div></div>" % (
                     i, html.escape(s["url"]), html.escape(s["name"] or s["address"] or s["id"]), pr, html.escape(s["beds"] or "?"),
                     ",".join(map(str, s["floors"])) or "?", s["score"], html.escape(s["address"] or ""),
                     "".join("<li>%s</li>" % html.escape(w) for w in s["why"]),
                     "".join("<img src='%s'>" % html.escape(rel(p)) for p in s["thumbs"])))
        md.append("%d. **%s** | $%s | %s | floors %s | score %s\n   %s\n%s\n" % (
            i, s["name"] or s["address"], pr, s["beds"] or "?", ",".join(map(str, s["floors"])) or "?", s["score"], s["url"],
            "\n".join("   - " + w for w in s["why"])))
    open(os.path.join(config.DATA, "picks.html"), "w").write("\n".join(h))
    open(os.path.join(config.DATA, "picks.md"), "w").write("\n".join(md))
    return "%d eligible of %d (judged %d)" % (len([s for s in ranked if s["eligible"]]), len(ranked), judged)
