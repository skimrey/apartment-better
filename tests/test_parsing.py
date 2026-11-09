"""Offline tests on tiny synthetic pages (no scraped content is stored in this repo)."""
import json
import unittest

from apartment_better import util, zillow

CFG = {"area": {"west": -87.735, "east": -87.62, "south": 41.91, "north": 42.02},
       "price": {"min": 1700, "max": 2100}, "zillow_city_slug": "chicago-il"}


class T(unittest.TestCase):
    def test_money(self):
        self.assertEqual(util.money("$1,688 - $4,794"), [1688, 4794])
        self.assertEqual(util.money("$2,563+"), [2563])

    def test_tier(self):
        self.assertEqual(util.tier_for(41.95, -87.66, 1900, 1900, CFG), 0)
        self.assertEqual(util.tier_for(41.95, -87.66, 3000, 3500, CFG), 1)
        self.assertEqual(util.tier_for(41.80, -87.66, 1900, 1900, CFG), 1)
        self.assertEqual(util.tier_for(None, None, 1900, 1900, CFG), 1)

    def test_zillow_card(self):
        c = zillow.parse_card({"detailUrl": "https://www.zillow.com/apartments/chicago-il/x/AbC123/", "address": "1 N St",
                               "latLong": {"latitude": 41.95, "longitude": -87.66},
                               "units": [{"price": "$1,900+", "beds": "1"}]}, CFG)
        self.assertEqual((c["id"], c["price_min"], c["tier"]), ("z:AbC123", 1900, 0))

    def test_zillow_detail_skips_comps(self):
        h = "https://photos.zillowstatic.com/fp/%s-cc_ft_960.webp"
        data = {"props": {"pageProps": {"componentProps": {"initialReduxState": {"gdp": {"building": {
            "buildingName": "B", "galleryPhotos": [{"url": h % "aa11"}, {"url": h % "aa11"}, {"url": h % "bb22"}],
            "comps": {"x": [{"photos": [{"url": h % "cc33"}]}]}, "description": "Nice."}}}}}}}
        html = '<script id="__NEXT_DATA__" type="application/json">%s</script>' % json.dumps(data)
        d = zillow.parse_detail(html)
        self.assertEqual([p["key"] for p in d["photos"]], ["aa11", "bb22"])


if __name__ == "__main__":
    unittest.main()


class Floors(unittest.TestCase):
    def test_floor_inference(self):
        f = util.floors_from_text
        self.assertEqual(f("3635 W Dickens Ave 2F"), [2])
        self.assertEqual(f("1733 N Central Park Ave Unit 2nd Floor"), [2])
        self.assertEqual(f("420 W Belmont Ave #6C-R, Chicago"), [6])
        self.assertEqual(f("5650 N Sheridan Rd APT 21G"), [21])
        self.assertEqual(f("Unit 304"), [3])
        self.assertEqual(f("Unit 1203"), [12])
        self.assertEqual(f("4345 N Whipple St"), [])  # a street number is not a floor
        self.assertEqual(f("1 Bed 2 Bath"), [])
        self.assertEqual(util.floor_from_query("2nd floor porch")[0], 2)


class Summary(unittest.TestCase):
    def test_majority_not_single_photo(self):
        import tempfile, os
        from apartment_better.db import DB
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
        db.upsert_listing({"id": "z:t", "source": "zillow", "url": "u"})
        rows = [("kitchen", "carpet"), ("living room", "hardwood"), ("bedroom", "hardwood"), ("bedroom", "hardwood"), ("bathroom", "tile or stone")]
        for i, (room, fl) in enumerate(rows):
            db.x("INSERT INTO images (listing_id,url,key,status,room,flooring,walls,style,condition,light) VALUES ('z:t',?,?,'done',?,?, 'neutral','modern','good','bright')", (str(i), str(i), room, fl))
        sm = db.summary("z:t")
        self.assertEqual(sm["flooring"], "hardwood")  # the carpeted kitchen is outvoted and not counted
        self.assertTrue(sm["nice"] >= 8)
        db.index_summary("z:t")
        self.assertTrue(db.q("SELECT 1 FROM fts WHERE kind='summary' AND text LIKE '%hardwood floors%'"))
