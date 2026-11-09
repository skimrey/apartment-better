import json
import re
from typing import Any, Dict, List, Optional

import httpx

from .db import DB
from .util import floor_from_query

STOP = {"a", "an", "the", "with", "and", "or", "of", "in", "on", "to", "for", "lots", "lot", "has", "have", "that", "is", "very"}


def expand(cfg: dict, query: str) -> List[str]:
    """Ask the local model for the ways a photo description might word this. Falls back to the raw query."""
    ex = cfg.get("query_expander")
    out = [query]
    if ex:
        prompt = ("A renter searches apartment listings for: \"%s\". Listings are searched through short text descriptions of "
                  "photos and listing text. Give 6 to 10 short phrases (1-3 words each) that such text would likely contain "
                  "if the listing matches. Reply JSON only: {\"phrases\": [...]}" % query)
        try:
            r = httpx.post(ex["url"].rstrip("/") + "/api/chat", timeout=90, json={
                "model": ex["model"], "stream": False, "think": False, "format": "json",
                "options": {"temperature": 0, "num_predict": 200},
                "messages": [{"role": "user", "content": prompt}]})
            ph = json.loads(r.json()["message"]["content"]).get("phrases") or []
            out += [str(p).lower() for p in ph if str(p).strip()]
        except Exception:
            pass
    return list(dict.fromkeys(out))


def _fts_query(phrases: List[str]) -> str:
    parts = []
    for p in phrases:
        words = [w for w in re.findall(r"[a-z0-9]+", p.lower()) if w not in STOP]
        if not words:
            continue
        parts.append('"%s"' % " ".join(words) if len(words) > 1 else words[0])
        if len(words) > 1:
            parts.extend(words if len(phrases) == 1 else [])
    return " OR ".join(dict.fromkeys(parts))


def search(cfg: dict, db: DB, query: str, limit: int = 20, price_max: Optional[int] = None,
           price_min: Optional[int] = None, home_only: bool = False, use_llm: bool = True,
           min_nice: Optional[float] = None, sort: str = "relevance", floor: Optional[int] = None) -> List[Dict[str, Any]]:
    qfloor, query = floor_from_query(query)
    floor = floor or qfloor
    phrases = expand(cfg, query) if use_llm else [query]
    fq = _fts_query(phrases)
    if not fq:
        return []
    rows = db.q("SELECT kind, listing_id, image_id, snippet(fts, 3, '[', ']', ' ... ', 18) AS snip, bm25(fts) AS score "
                "FROM fts WHERE fts MATCH ? ORDER BY score LIMIT 3000", (fq,))
    by: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        e = by.setdefault(r["listing_id"], {"score": 0.0, "images": [], "text": None})
        w = -r["score"]
        if r["kind"] == "image":
            e["score"] += w
            if len(e["images"]) < 6:
                e["images"].append((r["image_id"], r["snip"]))
        else:
            e["score"] += w * 0.5
            e["text"] = r["snip"]
    out = []
    for lid, e in sorted(by.items(), key=lambda kv: -kv[1]["score"]):
        e["nice"] = db.summary(lid).get("nice")
        if min_nice and (e["nice"] is None or e["nice"] < min_nice):
            by[lid] = None
            continue
        l = db.one("SELECT * FROM listings WHERE id=?", (lid,))
        if not l:
            continue
        if home_only and l["tier"] != 0:
            continue
        lf = json.loads(l["floors"] or "[]")
        if floor and floor not in lf:
            continue
        if price_max and (l["price_min"] or 0) > price_max:
            continue
        if price_min and (l["price_max"] or 10 ** 9) < price_min:
            continue
        ev = []
        for iid, snip in e["images"]:
            im = db.one("SELECT url, path, room FROM images WHERE id=?", (iid,))
            ev.append({"image_id": iid, "room": im["room"], "path": im["path"], "url": im["url"], "match": snip})
        out.append({
            "id": lid, "name": l["name"], "address": l["address"], "url": l["url"], "source": l["source"],
            "price": [l["price_min"], l["price_max"]], "floors": lf, "beds": l["beds_text"], "tier": l["tier"],
            "score": round(e["score"], 2), "nice": e["nice"], "listing_text_match": e["text"], "photo_evidence": ev,
            "photos_described": db.one("SELECT COUNT(*) n FROM images WHERE listing_id=? AND status='done'", (lid,))["n"],
            "photos_total": l["photo_count"],
        })
        if len(out) >= limit and sort == "relevance":
            break
    if sort == "nice":
        out.sort(key=lambda r: -(r["nice"] or 0))
    return out[:limit]
