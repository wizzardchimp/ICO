# ICO cyber incident dashboard

An interactive dashboard of personal data breaches reported to the UK Information Commissioner's Office (ICO), focused on **cyber attacks** over the **last five years**. The data runs from Q1 2019, so you can widen the window.

**Live page:** enable GitHub Pages (Settings → Pages → Deploy from branch → `main` / `/docs`).
Or open `docs/index.html` directly: it is one self-contained file with no server and no CDN.

## What it shows
- KPIs: cyber incidents, share of all reports, per-quarter change vs the prior period, ransomware, large (10,000+ people) incidents
- Quarterly stacked trend by attack type (phishing, ransomware, unauthorised access, malware, other), with a table view
- Attack types, sectors, a sector × attack-type heatmap, people affected, time to report (72-hour rule), ICO outcome, data types exposed, who was affected
- Every chart is clickable and filters the rest. Filters for period, scope (cyber / all / non-cyber), sector and size. Dark mode.
- Zoomable map of named organisations in cyber cases handled by the ICO (Apr 2024 onwards), plus a searchable, exportable list

## Rebuild
```bash
python3 scripts/fetch_data.py   # downloads CSVs into data/raw/ (links scraped from ico.org.uk)
python3 scripts/build.py        # writes docs/index.html
```
Python 3 standard library only.

The map needs locations for each organisation. `data/geocache.json` (committed) holds them, so a normal rebuild needs nothing extra. When new organisations appear, run `python3 scripts/geocode.py`: it matches names against the Companies House bulk file (downloaded on demand, ~500 MB) to get registered-office postcodes, looks them up on postcodes.io, and falls back to GeoNames place names for public bodies. Only uncached organisations are processed.

Optional Google fallback: `python3 scripts/geocode.py --google` (add `--google-limit 20` to try a few first) looks up organisations still unlocated with the Google Places API (New). It reads `GOOGLE_MAPS_API_KEY` from the environment, needs Places API (New) enabled and billing on, and shows those points in green on the map. Misses are remembered so they are not re-billed.

`.github/workflows/refresh.yml` runs fetch + build weekly and commits the result. It does not geocode, so organisations new since the last `geocode.py` run appear in the table but not on the map until you run it and commit `data/geocache.json`.

## Data and caveats
Sources: ICO [data security incident trends](https://ico.org.uk/action-weve-taken/complaints-and-concerns-data-sets/data-security-incident-trends/) and [self-reported personal data breach cases](https://ico.org.uk/action-weve-taken/complaints-and-concerns-data-sets/self-reported-personal-data-breach-cases/), under the Open Government Licence v3.0.

Counts are reports received by the ICO, not all attacks. Categories are assigned by the ICO as a best fit. The trends file has one row per incident × data type × data subject type, so the build collapses it to one row per incident (89,591 incidents).
