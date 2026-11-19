#!/usr/bin/env python3
"""Geocode the organisations in named ICO cyber cases (keyless, free data).

Stage 1: match names against the Companies House monthly bulk dump -> registered
         office postcode -> postcodes.io -> lat/lng + region/country.
Stage 2: GeoNames GB gazetteer place-name match (councils, NHS trusts, schools...).
Stage 3: region / police-force name hints.

Stage 4 (opt-in, --google): Google Places API (New) text search for organisations
         still unlocated. Key is read from GOOGLE_MAPS_API_KEY if set; in the cloud
         environment the proxy injects the X-Goog-Api-Key header instead.

Results are cached in data/geocache.json (committed to the repo), so only new
organisations are ever looked up. The 400MB+ Companies House file is downloaded
only when there are uncached organisations.
"""
import csv
import glob
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
CACHE_PATH = os.path.join(DATA_DIR, "geocache.json")
CHBULK_DIR = os.path.join(DATA_DIR, "chbulk")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CH_INDEX = "https://download.companieshouse.gov.uk/en_output.html"
GEONAMES_URL = "https://download.geonames.org/export/dump/GB.zip"
GEONAMES_ZIP = os.path.join(DATA_DIR, "geonames", "GB.zip")
PFA_PATH = os.path.join(DATA_DIR, "pfa-compact.json")

SUFFIXES = {
    "limited", "ltd", "plc", "llp", "llc", "cic", "inc", "company", "co",
    "uk", "the", "group", "holding", "holdings",
}
# Words too generic to indicate a place on their own
GENERIC = {
    "nhs", "trust", "foundation", "hospital", "health", "care", "social",
    "council", "county", "borough", "district", "city", "school", "academy",
    "college", "university", "institute", "services", "service", "solutions",
    "group", "holdings", "partners", "partnership", "practice", "surgery",
    "centre", "center", "clinic", "medical", "dental", "pharmacy", "limited",
    "ltd", "plc", "llp", "uk", "england", "scotland", "wales", "britain",
    "british", "national", "royal", "community", "housing", "association",
    "society", "charity", "company", "management", "consulting", "consultancy",
    "systems", "technology", "technologies", "solicitors", "law", "legal",
    "financial", "finance", "insurance", "bank", "capital", "investments",
    "properties", "property", "estates", "construction", "engineering",
    "energy", "water", "gas", "electricity", "transport", "travel", "hotel",
    "hotels", "retail", "stores", "foods", "food", "motors", "motor",
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def norm_name(name):
    s = unicodedata.normalize("NFKD", (name or "")).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\b(?:ltd|limited|plc|llp|llc|cic|inc)\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t)


def norm_postcode(pc):
    pc = re.sub(r"\s+", "", (pc or "")).upper()
    if len(pc) >= 5:
        pc = pc[:-3] + " " + pc[-3:]
    return pc


def bulk_postcodes(postcodes):
    """POST up to 100 postcodes at a time to postcodes.io (free, keyless)."""
    out = {}
    pcs = sorted(p for p in set(postcodes) if p)
    for i in range(0, len(pcs), 100):
        batch = pcs[i:i + 100]
        req = urllib.request.Request(
            "https://api.postcodes.io/postcodes",
            data=json.dumps({"postcodes": batch}).encode(),
            headers={"Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    d = json.load(resp)
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  postcodes.io batch failed: {e}", file=sys.stderr)
                    d = {"result": []}
                time.sleep(2 ** attempt)
        for item in d.get("result") or []:
            q, r = item.get("query"), item.get("result")
            if r and r.get("latitude") is not None and r.get("longitude") is not None:
                out[norm_postcode(q)] = {
                    "lat": round(r["latitude"], 5),
                    "lng": round(r["longitude"], 5),
                    "region": r.get("region") or r.get("admin_county") or r.get("admin_district") or "",
                    "country": r.get("country") or "",
                }
        print(f"  postcodes {min(i + 100, len(pcs))}/{len(pcs)}", flush=True)
        time.sleep(0.15)

    # Terminated postcodes are absent from the bulk endpoint but the single
    # lookup returns their last known coordinates.
    missing = [p for p in pcs if p not in out]
    if missing:
        print(f"  resolving {len(missing)} terminated/unknown postcodes individually…")
        for pc in missing:
            url = "https://api.postcodes.io/postcodes/" + urllib.parse.quote(pc)
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    d = json.load(resp)
            except urllib.error.HTTPError as e:
                try:
                    d = json.load(e)
                except Exception:
                    continue
            except Exception:
                continue
            t = d.get("terminated") if isinstance(d, dict) else None
            if t and t.get("latitude") is not None and t.get("longitude") is not None:
                out[pc] = {
                    "lat": round(t["latitude"], 5),
                    "lng": round(t["longitude"], 5),
                    "region": "",
                    "country": "",
                    "terminated": True,
                }
            time.sleep(0.05)
    return out


def download_inputs():
    os.makedirs(CHBULK_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(GEONAMES_ZIP), exist_ok=True)
    if not glob.glob(os.path.join(CHBULK_DIR, "*.zip")):
        html = urllib.request.urlopen(CH_INDEX, timeout=60).read().decode("utf-8", "replace")
        files = sorted(set(re.findall(r"BasicCompanyDataAsOneFile-[0-9-]+\.zip", html)))
        if files:
            print("Downloading", files[-1], "(large)...")
            urllib.request.urlretrieve("https://download.companieshouse.gov.uk/" + files[-1],
                                       os.path.join(CHBULK_DIR, files[-1]))
    if not os.path.exists(GEONAMES_ZIP):
        urllib.request.urlretrieve(GEONAMES_URL, GEONAMES_ZIP)


def find_bulk_csv():
    zips = sorted(glob.glob(os.path.join(CHBULK_DIR, "*.zip")))
    if not zips:
        download_inputs()
        zips = sorted(glob.glob(os.path.join(CHBULK_DIR, "*.zip")))
    if not zips:
        print("No Companies House bulk zip found in data/chbulk/", file=sys.stderr)
        print("Download BasicCompanyDataAsOneFile from "
              "http://download.companieshouse.gov.uk/en_output.html", file=sys.stderr)
        sys.exit(1)
    with zipfile.ZipFile(zips[-1]) as z:
        candidates = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not candidates:
            print("No CSV inside bulk zip", file=sys.stderr)
            sys.exit(1)
        return zips[-1], candidates[0]


def match_companies(orgs):
    """Stream the bulk CSV and return {org: {pc, addr}} for name matches."""
    zpath, member = find_bulk_csv()
    targets = {}
    for o in orgs:
        targets.setdefault(norm_name(o), []).append(o)
    print(f"Scanning {os.path.basename(zpath)} for {len(targets)} normalised names…")
    found = {}
    with zipfile.ZipFile(zpath) as z, z.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text)
        cols = {(c or "").strip(): c for c in (reader.fieldnames or [])}
        name_col = cols.get("CompanyName")
        pc_col = cols.get("RegAddress.PostCode")
        a1_col = cols.get("RegAddress.AddressLine1")
        town_col = cols.get("RegAddress.PostTown")
        status_col = cols.get("CompanyStatus")
        if not name_col or not pc_col:
            print("Unexpected bulk CSV header", file=sys.stderr)
            sys.exit(1)
        n = 0
        for row in reader:
            n += 1
            norm = norm_name(row.get(name_col, ""))
            if norm not in targets:
                continue
            pc = norm_postcode(row.get(pc_col, ""))
            if not pc:
                continue
            status = (row.get(status_col) or "").strip().lower()
            score = 2 if status == "active" else (1 if status else 0)
            rec = {
                "pc": pc,
                "addr": ", ".join(x for x in [row.get(a1_col), row.get(town_col), row.get(pc_col)] if x),
                "score": score,
            }
            for org in targets[norm]:
                cur = found.get(org)
                if cur is None or rec["score"] > cur["score"]:
                    found[org] = rec
            if n % 1000000 == 0:
                print(f"  {n/1e6:.0f}M rows scanned, {len(found)} matched", flush=True)
    print(f"  scanned {n:,} companies, matched {len(found)} organisations")
    return found


def load_gazetteer():
    if not os.path.exists(GEONAMES_ZIP):
        print("No GeoNames GB.zip found; skipping name fallback", file=sys.stderr)
        return []
    best = {}

    def add(nm, lat, lng, pop):
        nm = nm.lower().strip()
        if len(nm) < 4:
            return
        if nm not in best or pop > best[nm][2]:
            best[nm] = (lat, lng, pop)

    with zipfile.ZipFile(GEONAMES_ZIP) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".txt")
                 and "readme" not in n.lower()]
        name = names[0] if names else None
        if not name:
            return []
        with z.open(name) as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
                f = line.rstrip("\n").split("\t")
                if len(f) < 15:
                    continue
                fclass, fcode = f[6], f[7]
                try:
                    pop = int(f[14] or 0)
                except ValueError:
                    pop = 0
                is_place = fclass == "P" and pop >= 500
                is_admin = fclass == "A" and fcode in {
                    "ADM1", "ADM2", "ADM3", "ADM4", "ADMD", "ADM1H", "ADM2H"}
                if not (is_place or is_admin):
                    continue
                lat, lng = float(f[4]), float(f[5])
                add(f[2], lat, lng, pop)          # ascii name
                add(f[1], lat, lng, pop)          # primary name
                for alt in (f[3] or "").split(","):  # alternate names
                    if alt and len(alt) <= 30:
                        add(alt, lat, lng, pop)
    out = [(nm, v[0], v[1], v[2]) for nm, v in best.items()]
    out.sort(key=lambda x: -x[3])
    print(f"GeoNames: {len(out)} place/administrative names loaded")
    return out


def load_authority_hints():
    """Force/region names -> representative coordinates.

    Lets us place bodies named after a police force or broad region (e.g.
    "Chief Constable North Wales Police", "East of England Ambulance") even
    though those names are not in the gazetteer.
    """
    hints = {}
    for f in load_json(PFA_PATH, []):
        bbox = f.get("bbox")
        if not bbox:
            continue
        lat = (bbox[1] + bbox[3]) / 2
        lng = (bbox[0] + bbox[2]) / 2
        nm = f["name"].lower()
        hints[nm] = (lat, lng, "")
        if nm == "metropolitan police":
            hints["met police"] = (lat, lng, "")
            hints["metropolitan police service"] = (lat, lng, "")
        elif nm == "london, city of":
            hints["city of london"] = (lat, lng, "")
    hints.update({
        "east of england": (52.35, 0.75, "England"),
        "east midlands": (52.9, -0.9, "England"),
        "west midlands": (52.48, -1.9, "England"),
        "north east": (54.9, -1.7, "England"),
        "north west": (53.5, -2.3, "England"),
        "south east": (51.3, -0.7, "England"),
        "south west": (51.0, -3.6, "England"),
        "yorkshire and the humber": (53.8, -1.3, "England"),
        "london": (51.507, -0.128, "England"),
        "scotland": (56.5, -4.0, "Scotland"),
        "scottish": (56.5, -4.0, "Scotland"),
        "wales": (52.4, -3.5, "Wales"),
        "welsh": (52.4, -3.5, "Wales"),
        "northern ireland": (54.65, -6.6, "Northern Ireland"),
        "northern irish": (54.65, -6.6, "Northern Ireland"),
    })
    return hints


def authority_match(org, hints):
    norm = " " + norm_name(org) + " "
    best = None
    for phrase, (lat, lng, country) in hints.items():
        if (" " + phrase + " ") in norm:
            score = len(phrase.split())
            if best is None or score > best[0]:
                best = (score, lat, lng, country, phrase)
    return best


def name_match(org, gazetteer):
    tokens = [t for t in norm_name(org).split() if t not in GENERIC and len(t) > 2]
    if not tokens:
        return None
    joined = " " + " ".join(tokens) + " "
    best = None
    for nm, lat, lng, pop in gazetteer:
        if nm in GENERIC:
            continue
        if (" " + nm + " ") in joined:
            score = (len(nm.split()), pop)
            if best is None or score > best[0]:
                best = (score, lat, lng, nm)
    return best


GOOGLE_URL = "https://places.googleapis.com/v1/places:searchText"
UK_BOX = (49.8, 60.95, -8.7, 1.85)  # lat_min, lat_max, lng_min, lng_max


def google_lookup(org):
    """Return {lat,lng,addr} for the best Places text-search hit inside the UK, else None."""
    body = {"textQuery": f"{org}, UK", "regionCode": "GB", "maxResultCount": 1,
            "locationBias": {"rectangle": {"low": {"latitude": UK_BOX[0], "longitude": UK_BOX[2]},
                                           "high": {"latitude": UK_BOX[1], "longitude": UK_BOX[3]}}}}
    headers = {"Content-Type": "application/json",
               "X-Goog-Api-Key": os.environ.get("GOOGLE_MAPS_API_KEY", "proxy-injected"),
               "X-Goog-FieldMask": "places.location,places.formattedAddress,places.displayName"}
    req = urllib.request.Request(GOOGLE_URL, data=json.dumps(body).encode(), headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit(f"Google Places refused the request ({e.code}): check the key, "
                                 "that Places API (New) is enabled, and billing.")
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            time.sleep(1)
    else:
        return None
    for pl in d.get("places") or []:
        loc = pl.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is not None and UK_BOX[0] <= lat <= UK_BOX[1] and UK_BOX[2] <= lng <= UK_BOX[3]:
            return {"lat": round(lat, 5), "lng": round(lng, 5), "region": "", "country": "",
                    "addr": pl.get("formattedAddress", ""), "src": "google"}
    return None


def run_google(orgs, cache, limit=None):
    todo = [o for o in orgs if (cache.get(o) or {}).get("lat") is None and not (cache.get(o) or {}).get("g")]
    if limit:
        todo = todo[:limit]
    print(f"Google Places: {len(todo)} organisations to try")
    hit = 0
    for n, org in enumerate(todo, 1):
        g = google_lookup(org)
        cache[org] = g if g else {"lat": None, "g": True}
        hit += bool(g)
        if n % 50 == 0:
            save_json(CACHE_PATH, cache)
            print(f"  {n}/{len(todo)} tried, {hit} located", flush=True)
        time.sleep(0.1)
    save_json(CACHE_PATH, cache)
    print(f"Google Places: located {hit}/{len(todo)}")


def named_orgs():
    import build  # scripts/build.py
    return sorted({r[0] for r in build.build_named() if r[0]})


def main():
    args = sys.argv[1:]
    skip_names = "--no-names" in args
    cache = load_json(CACHE_PATH, {})
    orgs = named_orgs()
    todo = [o for o in orgs if o not in cache]
    print(f"{len(orgs)} unique orgs, {len(orgs)-len(todo)} cached, {len(todo)} to go")
    limit = None
    if "--google-limit" in args:
        limit = int(args[args.index("--google-limit") + 1])
    if not todo:
        if "--google" in args:
            run_google(orgs, cache, limit)
        return

    matches = match_companies(todo)
    postcodes = {rec["pc"] for rec in matches.values()}
    print(f"Looking up {len(postcodes)} postcodes via postcodes.io...")
    pc_geo = bulk_postcodes(postcodes)
    hit = 0
    for org, rec in matches.items():
        g = pc_geo.get(rec["pc"])
        if g:
            cache[org] = {**g, "addr": rec["addr"], "src": "ch"}
            hit += 1
    print(f"Companies House: located {hit}/{len(todo)}")

    if not skip_names:
        remaining = [o for o in todo if o not in cache]
        gaz = load_gazetteer()
        named = 0
        for org in remaining:
            m = name_match(org, gaz) if gaz else None
            if m:
                _, lat, lng, place = m
                cache[org] = {"lat": round(lat, 4), "lng": round(lng, 4), "region": "", "country": "",
                              "addr": f"~{place.title()} (name match)", "src": "name"}
                named += 1
        hints = load_authority_hints()
        hinted = 0
        for org in [o for o in todo if o not in cache]:
            m = authority_match(org, hints)
            if m:
                _, lat, lng, country, phrase = m
                cache[org] = {"lat": round(lat, 4), "lng": round(lng, 4), "region": "", "country": country,
                              "addr": f"~{phrase.title()} (name match)", "src": "name"}
                hinted += 1
        print(f"Place names: {named}; region hints: {hinted}")

    for o in todo:  # remember misses so they are not retried every run
        cache.setdefault(o, {"lat": None})
    save_json(CACHE_PATH, cache)
    if "--google" in args:
        run_google(orgs, cache, limit)
    located = sum(1 for o in orgs if (cache.get(o) or {}).get("lat") is not None)
    print(f"Coverage: {located}/{len(orgs)} orgs ({100*located/len(orgs):.1f}%)")


if __name__ == "__main__":
    main()
