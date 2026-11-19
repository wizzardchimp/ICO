#!/usr/bin/env python3
"""Download the ICO datasets used by the dashboard into data/raw/.

1. Data security incident trends (aggregate, Q1 2019 -> latest quarter)
2. Self-reported personal data breach cases (named organisations, Apr 2024 ->)

Download links are scraped from the ICO pages, so new quarterly releases are
picked up automatically. Source: ico.org.uk, Open Government Licence v3.0.
"""
import os
import re
import sys
import urllib.request

BASE = "https://ico.org.uk"
TRENDS_PAGE = "/action-weve-taken/complaints-and-concerns-data-sets/data-security-incident-trends/"
PDB_PAGE = "/action-weve-taken/complaints-and-concerns-data-sets/self-reported-personal-data-breach-cases/"
RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "raw")


def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0 (ico-dashboard)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def csv_links(page):
    html = get(page).decode("utf-8", "replace")
    return sorted(set(re.findall(r'href="(/media2/[^"]+\.csv)"', html)))


def save(path, prefix=""):
    dest = os.path.join(RAW, prefix + os.path.basename(path))
    if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
        print("  downloading", path)
        with open(dest, "wb") as f:
            f.write(get(path))
    return dest


def main():
    os.makedirs(RAW, exist_ok=True)
    trends = csv_links(TRENDS_PAGE)
    if not trends:
        sys.exit("No trends CSV found on the ICO page - has the page layout changed?")
    # drop stale trend downloads so only the newest release is used
    for f in os.listdir(RAW):
        if f.startswith("trends-") and os.path.join(RAW, f) not in [os.path.join(RAW, "trends-" + os.path.basename(t)) for t in trends]:
            os.remove(os.path.join(RAW, f))
    for t in trends:
        save(t, "trends-")
    for p in csv_links(PDB_PAGE):
        save(p, "pdb-")
    print("done ->", os.path.normpath(RAW))


if __name__ == "__main__":
    main()
