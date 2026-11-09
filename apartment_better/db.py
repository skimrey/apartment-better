import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id TEXT PRIMARY KEY,            -- 'z:<id>' (Zillow) or 'a:<id>' (Apartments.com)
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  name TEXT, address TEXT, neighborhood TEXT,
  lat REAL, lng REAL,
  price_min INTEGER, price_max INTEGER,
  beds_text TEXT,
  description TEXT,
  amenities TEXT,                 -- JSON list
  tier INTEGER DEFAULT 1,         -- 0 = inside the home area and price range
  seed_rank INTEGER DEFAULT 999,  -- position of the neighborhood in the config list
  photo_count INTEGER,            -- unique photos found on the detail page
  detail_at REAL,
  first_seen REAL,
  floors TEXT
);
CREATE TABLE IF NOT EXISTS images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id TEXT NOT NULL REFERENCES listings(id),
  url TEXT NOT NULL,
  key TEXT NOT NULL,              -- identity of the photo across sizes
  caption TEXT,
  idx INTEGER,
  sha1 TEXT, path TEXT,
  status TEXT DEFAULT 'new',      -- new | fetched | working | done | skipped | error
  attempts INTEGER DEFAULT 0,
  error TEXT,
  room TEXT, features TEXT, outdoor_space TEXT, windows TEXT, light TEXT, view TEXT,
  elevation TEXT, notes TEXT,
  flooring TEXT, walls TEXT, style TEXT, condition TEXT, nice INTEGER,
  model TEXT, done_at REAL,
  UNIQUE(listing_id, key)
);
CREATE INDEX IF NOT EXISTS images_status ON images(status);
CREATE INDEX IF NOT EXISTS images_listing ON images(listing_id);
CREATE INDEX IF NOT EXISTS images_sha ON images(sha1);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(kind UNINDEXED, listing_id UNINDEXED, image_id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: str = config.DB_PATH):
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=60, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.executescript(SCHEMA)
        if "floors" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(listings)")}:
            self.conn.execute("ALTER TABLE listings ADD COLUMN floors TEXT")
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(images)")}
        for col, typ in (("flooring", "TEXT"), ("walls", "TEXT"), ("style", "TEXT"), ("condition", "TEXT"), ("nice", "INTEGER")):
            if col not in have:
                self.conn.execute("ALTER TABLE images ADD COLUMN %s %s" % (col, typ))

    def q(self, sql: str, args: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, tuple(args)).rowcount

    # ---- listings -------------------------------------------------------
    def upsert_listing(self, row: Dict[str, Any]) -> bool:
        """Insert a listing or fill in what the new row knows. True when it is new."""
        now = time.time()
        existing = self.one("SELECT id FROM listings WHERE id=?", (row["id"],))
        if existing is None:
            row = dict(row, first_seen=now)
            cols = ",".join(row)
            self.x("INSERT INTO listings (%s) VALUES (%s)" % (cols, ",".join("?" * len(row))), row.values())
            return True
        sets, vals = [], []
        for k, v in row.items():
            if k in ("id", "first_seen") or v is None:
                continue
            if k in ("tier", "seed_rank"):
                sets.append("%s=MIN(%s, ?)" % (k, k)); vals.append(v)
            else:
                sets.append("%s=?" % k); vals.append(v)
        if sets:
            self.x("UPDATE listings SET %s WHERE id=?" % ",".join(sets), vals + [row["id"]])
        return False

    def add_images(self, listing_id: str, photos: List[Dict[str, Any]]) -> int:
        added = 0
        for i, p in enumerate(photos):
            n = self.x("INSERT OR IGNORE INTO images (listing_id, url, key, caption, idx) VALUES (?,?,?,?,?)",
                       (listing_id, p["url"], p["key"], p.get("caption"), i))
            added += max(n, 0)
        self.x("UPDATE listings SET photo_count=(SELECT COUNT(*) FROM images WHERE listing_id=?), detail_at=? WHERE id=?",
               (listing_id, time.time(), listing_id))
        return added

    # ---- search index ---------------------------------------------------
    def index_listing(self, listing_id: str) -> None:
        row = self.one("SELECT name, address, neighborhood, description, amenities FROM listings WHERE id=?", (listing_id,))
        if not row:
            return
        try:
            amen = " ; ".join(json.loads(row["amenities"] or "[]"))
        except ValueError:
            amen = ""
        text = " . ".join(x for x in (row["name"], row["neighborhood"], row["description"], amen) if x)
        self.x("DELETE FROM fts WHERE kind='listing' AND listing_id=?", (listing_id,))
        self.x("INSERT INTO fts (kind, listing_id, image_id, text) VALUES ('listing', ?, 0, ?)", (listing_id, text))

    def index_image(self, image_id: int) -> None:
        r = self.one("SELECT * FROM images WHERE id=?", (image_id,))
        if not r or r["status"] != "done":
            return
        try:
            feats = " ; ".join(json.loads(r["features"] or "[]"))
        except ValueError:
            feats = ""
        parts = [r["room"], feats, r["outdoor_space"], "windows " + (r["windows"] or ""),
                 "light " + (r["light"] or ""), "view " + (r["view"] or ""), r["elevation"], r["notes"], r["caption"]]
        text = " . ".join(p for p in parts if p)
        self.x("DELETE FROM fts WHERE kind='image' AND image_id=?", (image_id,))
        self.x("INSERT INTO fts (kind, listing_id, image_id, text) VALUES ('image', ?, ?, ?)",
               (r["listing_id"], image_id, text))

    def summary(self, listing_id: str) -> dict:
        """Listing-level finish summary. One photo can be wrong (a 'carpeted kitchen'); a majority across photos is far safer."""
        rows = self.q("SELECT room, flooring, walls, style, condition, light FROM images WHERE listing_id=? AND status='done' "
                      "AND room IN ('living room','bedroom','dining area','hallway','office','kitchen','bathroom')", (listing_id,))
        if len(rows) < 3:
            return {}
        n = len(rows)
        living = [r for r in rows if r["room"] in ("living room", "bedroom", "dining area", "hallway", "office")]
        floors = [r["flooring"] for r in living if r["flooring"] and r["flooring"] != "not visible"]
        out = {"photos": n}
        if len(floors) >= 2:
            top = max(set(floors), key=floors.count)
            if floors.count(top) / len(floors) >= 0.6:
                out["flooring"] = top
        share = lambda col, vals: sum(1 for r in rows if (r[col] or "") in vals) / n
        out["worn_share"] = round(share("condition", ("worn", "poor")), 2)
        out["dated_share"] = round(share("style", ("dated",)), 2)
        out["warm_share"] = round(share("walls", ("warm color", "exposed brick")), 2)
        out["bright_share"] = round(share("light", ("bright",)), 2)
        pts = {"hardwood": 2, "tile or stone": 1, "carpet": -2}.get(out.get("flooring", ""), 0)
        pts += -3 * out["worn_share"] - 2 * out["dated_share"] + 1.5 * out["warm_share"] + 1.5 * out["bright_share"]
        out["nice"] = max(1, min(10, round(5 + pts * 0.9, 1)))
        return out

    def index_summary(self, listing_id: str) -> None:
        sm = self.summary(listing_id)
        self.x("DELETE FROM fts WHERE kind='summary' AND listing_id=?", (listing_id,))
        if not sm:
            return
        bits = []
        if sm.get("flooring"):
            bits.append(sm["flooring"] + " floors throughout")
        if sm["worn_share"] >= 0.3:
            bits.append("worn condition")
        if sm["dated_share"] >= 0.4:
            bits.append("dated finishes")
        if sm["warm_share"] >= 0.3:
            bits.append("warm colors")
        if sm["bright_share"] >= 0.6:
            bits.append("bright")
        if bits:
            self.x("INSERT INTO fts (kind, listing_id, image_id, text) VALUES ('summary', ?, 0, ?)", (listing_id, " ; ".join(bits)))

    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        out["listings"] = {r["source"]: r["n"] for r in self.q("SELECT source, COUNT(*) n FROM listings GROUP BY source")}
        out["detailed"] = self.one("SELECT COUNT(*) n FROM listings WHERE detail_at IS NOT NULL")["n"]
        out["tier0"] = self.one("SELECT COUNT(*) n FROM listings WHERE tier=0")["n"]
        out["images"] = {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM images GROUP BY status")}
        return out
