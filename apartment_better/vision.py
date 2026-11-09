"""Describe one listing photo with a local Ollama vision model."""
import base64
import io
import json
import re
from typing import Any, Dict

import httpx
from PIL import Image

PROMPT = """You are cataloguing an apartment listing photo so renters can search for things landlords leave out.
Describe only what you can actually see. Reply with JSON only, with exactly these keys:
{"room": one of "kitchen","bathroom","bedroom","living room","dining area","hallway","closet","laundry","office","exterior","outdoor space","view","amenity","floor plan","lobby","other",
 "features": [short lowercase phrases for things a renter would care about, e.g. "patio","balcony","rooftop deck","large windows","floor-to-ceiling windows","hardwood floors","in-unit washer","exposed brick","high ceilings","stainless steel appliances","dishwasher","gas range","walk-in closet","fireplace","bay window","skylight","carpet","tile floors","bathtub","double vanity","central air window unit","ceiling fan","built-in shelves","sunroom"],
 "outdoor_space": "none" or what it is and anything visible about it (e.g. "private balcony", "ground-level patio", "second floor deck", "shared rooftop"),
 "windows": "none" | "few" | "some" | "many" | "wall of windows",
 "light": "dark" | "moderate" | "bright",
 "view": "none visible" or what is outside the windows (e.g. "trees", "brick wall of next building", "street", "skyline", "lake"),
 "elevation": "ground floor" | "upper floor" | "unknown" (judge from what is outside the windows),
 "flooring": "hardwood" | "tile or stone" | "vinyl or laminate" | "carpet" | "concrete" | "not visible",
 "walls": "warm color" | "neutral" | "white" | "cool color" | "exposed brick" | "not visible",
 "style": "modern" | "mixed" | "dated" (cabinets, counters, fixtures, lighting: new and stylish, or old/builder-grade),
 "condition": "new or renovated" | "good" | "worn" | "poor" (scuffs, stains, peeling paint, damage, dinginess),
 "notes": one short factual sentence including condition (new, dated, worn) and anything unusual}"""


DETAIL_PROMPT = """You are cataloguing a rental listing photo for renters. List everything a renter could search for that you can actually see.
Be specific (counts, levels, materials, colors). Do not repeat yourself across keys. Reply with JSON only, with exactly these keys:
{"building": [short phrases: type, era, stories, cladding, roof, notable details],
 "outdoor_spaces": [one phrase per space: what it is, which level, material, what is on it],
 "furniture_and_objects": [short phrases],
 "surroundings": [short phrases: neighbors, alley or street, parking, trees, utilities],
 "renter_takeaways": [3-5 short phrases: what living here would be like],
 "search_tags": [8-15 lowercase tags this photo should match]}"""


def describe_detailed(node: Dict[str, Any], jpeg: bytes, timeout: int = 600) -> Dict[str, Any]:
    body = {"model": node["model"], "stream": False, "think": False, "format": "json", "keep_alive": "60m",
            "options": {"temperature": 0, "num_predict": 900},
            "messages": [{"role": "user", "content": DETAIL_PROMPT, "images": [base64.b64encode(jpeg).decode()]}]}
    r = httpx.post(node["url"].rstrip("/") + "/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    return _parse(r.json()["message"]["content"])


def prep(data: bytes, max_px: int) -> bytes:
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    im.thumbnail((max_px, max_px))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def _parse(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v).strip()


def describe(node: Dict[str, Any], jpeg: bytes, timeout: int = 240) -> Dict[str, Any]:
    body = {
        "model": node["model"], "stream": False, "think": False, "format": "json", "keep_alive": "60m",
        "options": {"temperature": 0, "num_predict": 500},
        "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(jpeg).decode()]}],
    }
    r = httpx.post(node["url"].rstrip("/") + "/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    d = _parse(r.json()["message"]["content"])
    feats = d.get("features") or []
    if not isinstance(feats, list):
        feats = [feats]
    out = {
        "room": _s(d.get("room")), "features": [_s(f).lower() for f in feats if _s(f)],
        "outdoor_space": _s(d.get("outdoor_space")), "windows": _s(d.get("windows")),
        "light": _s(d.get("light")), "view": _s(d.get("view")),
        "elevation": _s(d.get("elevation")), "notes": _s(d.get("notes")),
        "flooring": _s(d.get("flooring")).lower(), "walls": _s(d.get("walls")).lower(),
        "style": _s(d.get("style")).lower(), "condition": _s(d.get("condition")).lower(),
    }
    out["nice"] = niceness(out)
    return out


INTERIOR = ("kitchen", "bathroom", "bedroom", "living room", "dining area", "hallway", "closet", "laundry", "office")


def niceness(d: Dict[str, Any]) -> Any:
    """1-10 from observable facts, for interior photos only. The rule is deliberately simple and easy to retune."""
    if d.get("room") not in INTERIOR:
        return None
    pts = 0.0
    fl, st, cond, walls, light = d["flooring"], d["style"], d["condition"], d["walls"], d["light"]
    pts += 2 if "hardwood" in fl else 1 if "tile" in fl else -2 if "carpet" in fl else 0
    pts += 2 if "new" in cond or "renovated" in cond else 1 if "good" in cond else -2 if "worn" in cond else -3 if "poor" in cond else 0
    pts += 1 if st == "modern" else -1 if st == "dated" else 0
    pts += 1 if "warm" in walls or "brick" in walls else 0
    pts += 1 if light == "bright" else -1 if light == "dark" else 0
    return max(1, min(10, round(5 + pts * 0.75)))
