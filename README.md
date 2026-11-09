# Apartment Better

Listing sites let you filter by the checkboxes a landlord filled in. The photos show far more (the porch, the hardwood, the wall of windows) and nobody reads them.

This tool reads them. It collects rentals from Zillow and Apartments.com, describes every listing photo with a free local vision model, judges each building (vintage two-flat or new-build box, porches and on which floor), and lets you search in plain English:

```
python cli.py search "second floor porch" --home-only
python cli.py picks        # vintage, private porch / deck / balcony, not ground floor, in your price
```

Every result shows its evidence: what the photos showed, what the listing text says, and the floor.

**This repo ships a tool, not data.** No listings or photos are included; everything is fetched by you, on your machine, for your own search. Not affiliated with Zillow or Apartments.com.

**Read this before running it.** Zillow and Apartments.com terms of use restrict automated access and scraping. Running it may violate those terms and can get your IP address or accounts blocked. It is offered for personal, non-commercial research and education, with no warranty. Use it at your own risk, keep the request rate low, do not republish scraped content, and do not use it for anything commercial. Nothing here is legal advice.

## Setup
Needs Python 3.10+, Google Chrome (for Apartments.com), and [Ollama](https://ollama.com) with a vision model.
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config.example.json config.json
ollama pull gemma4:e4b          # or any vision model; set it in config.json
```
In `config.json` set your city (`city`, `zillow_city_slug`, `apartments_city_slug`, `apartments_neighborhoods`), price range, home map box (`area`), and `vision_nodes` (one or more Ollama servers; each gets `workers` requests at a time).

## Use
```
.venv/bin/python cli.py run                 # discover -> details -> photo pass
.venv/bin/python cli.py discover|details|classify [--only zillow|apartments] [--limit N] [--images N] [--expand]
.venv/bin/python cli.py character           # judge each building from its front photo (type, era, porches, level)
.venv/bin/python cli.py floors              # infer floors from unit labels ("2F", "Unit 3A", "#304" -> 3)
.venv/bin/python cli.py describe photo.jpg  # what the model sees in one photo
.venv/bin/python cli.py picks               # writes data/picks.html (with photos) and data/picks.md
.venv/bin/python cli.py search "hardwood, lots of windows" [--home-only] [--max-price 2100] [--min-nice 7] [--floor 2]
.venv/bin/python cli.py web                 # http://127.0.0.1:8765
.venv/bin/python cli.py status
.venv/bin/python cli.py stop                # finishes the current listing then exits
```
Everything is resumable; rerun any step. `--expand` on discover searches the whole city at any price; those listings queue behind your home area.

To leave it running unattended: `caffeinate -i ./overnight.sh` (loops details, building judgements, picks and photo batches until done) and `./watchdog.sh` (refreshes picks every 20 minutes, restarts the loop if it dies).

## How it works
- **Zillow**: plain HTTP; page data is embedded in `__NEXT_DATA__`. Searches the map box (split into quarters when Zillow caps the results). Backs off on rate limits.
- **Apartments.com**: read through a normal Chrome window the tool starts with its own profile (`.chrome-profile`) and a debugging port. A Chrome window opens while this runs. Slow on purpose (3-7 s per page), with long cool-offs if the site pushes back.
- **Photo pass**: one vision call per photo returns JSON (room, features, outdoor space, windows, light, view, elevation, flooring, walls, style, condition). Identical photos across listings or sites are described once. Inside your area and price first, most promising listings first, fewest photos first.
- **Building judgement**: one call per listing on its front photo: building type (two-flat, greystone, courtyard building, new-build mid-rise...), era, porches and which level they are on, a 1-10 character score.
- **Niceness**: single photos are noisy (a "carpeted kitchen"), so finish quality is a majority vote across a listing's photos: flooring, worn or dated share, warm colors, light.
- **Floors**: from unit labels and Apartments.com floor fields, plus whether the window views look above street level.
- **Search**: SQLite FTS5 over photo descriptions and listing text. A local model expands the query into likely phrasings.
- **Unstable model servers**: per-node backoff (1 min doubling to 15 min); a node failure puts the photo back without counting it against the photo.

## Zillow only
The Apartments.com half lives in `apartment_better/apartments.py` and `apartment_better/browser.py`. Delete those two files and the tool runs Zillow-only with no other changes.

## Caveats
- Model descriptions can be wrong; every result links to the listing, so look before you go.
- Zillow building pages show the whole building's gallery, so photo counts there run 20-60; Apartments.com pages are usually 8-20.
- Listing photos may be of a similar unit, or virtually staged.
- Tests: `.venv/bin/python -m unittest discover -s tests` (offline parsing, floor inference, niceness summary).
