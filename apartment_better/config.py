import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
IMAGES = os.path.join(DATA, "images")
DB_PATH = os.path.join(DATA, "apartment_better.db")
CONFIG_PATH = os.environ.get("AB_CONFIG") or next(
    (p for p in (os.path.join(ROOT, "config.json"), os.path.join(ROOT, "config.example.json")) if os.path.exists(p)))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def load() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    os.makedirs(IMAGES, exist_ok=True)
    return cfg
