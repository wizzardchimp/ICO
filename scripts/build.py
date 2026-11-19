#!/usr/bin/env python3
"""Build docs/index.html: a single self-contained dashboard (no CDN, no server).

Reads data/raw/*.csv (see fetch_data.py), compacts it to incident-level integer
rows and inlines it into scripts/template.html.
"""
import csv
import glob
import io
import json
import os
import re
from datetime import date

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "docs", "index.html")

SIZES = ["Unknown", "1 to 9", "10 to 99", "100 to 1k", "1k to 10k", "10k to 100k", "100k and above"]
REPORT = ["Less than 24 hours", "24 hours to 72 hours", "72 hours to 1 week", "More than 1 week"]
DECISIONS = ["No Further Action", "Informal Action Taken", "Investigation Pursued",
             "Regulatory action taken", "Not Yet Assigned"]
TYPES = ["Phishing", "Ransomware", "Unauthorised access", "Malware", "Other cyber incident",
         "Brute Force", "Denial of service", "Cryptographic flaw"]  # cyber first; rest are appended


def decode(b):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace")


def interner(initial=()):
    vals = list(initial)

    def idx(v):
        if v not in vals:
            vals.append(v)
        return vals.index(v)
    idx.vals = vals
    return idx


def build_trends():
    files = glob.glob(os.path.join(RAW, "trends-*.csv"))
    if not files:
        raise SystemExit("Run scripts/fetch_data.py first.")
    rows = list(csv.DictReader(io.StringIO(decode(open(sorted(files)[-1], "rb").read()))))
    t_i, s_i = interner(TYPES), interner()
    subj_i, dt_i = interner(), interner()
    inc = {}
    for r in rows:
        ref = r["BI Reference"]
        if ref not in inc:
            q = (int(r["Year"]) - 2019) * 4 + int(r["Quarter"].split()[-1]) - 1
            inc[ref] = {
                "q": q, "cyber": r["Incident Category"] == "Cyber",
                "t": t_i(r["Incident Type"]), "s": s_i(r["Sector"]),
                "n": SIZES.index(r["No. Data Subjects Affected"]),
                "r": REPORT.index(r["Time Taken to Report"]),
                "d": DECISIONS.index(r["Decision Taken"]),
                "subj": 0, "dt": 0,
            }
        inc[ref]["subj"] |= 1 << subj_i(r["Data Subject Type"])
        inc[ref]["dt"] |= 1 << dt_i(r["Data Type"])
    out = [[v["q"], v["t"], v["s"], v["n"], v["r"], v["d"], int(v["cyber"]), v["subj"], v["dt"]]
           for v in inc.values()]
    out.sort()
    qmax = max(r[0] for r in out)
    return {"rows": out, "types": t_i.vals, "sectors": s_i.vals, "subjects": subj_i.vals,
            "dataTypes": dt_i.vals, "sizes": SIZES, "report": REPORT, "decisions": DECISIONS,
            "qmax": qmax, "q0year": 2019}


def parse_date(v):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", (v or "").strip())
    return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}" if m else (v or "")[:10]


def build_named():
    seen, out = set(), []
    for f in sorted(glob.glob(os.path.join(RAW, "pdb-*.csv"))):
        for r in csv.DictReader(io.StringIO(decode(open(f, "rb").read()))):
            if (r.get("ISCyberIncident") or "").strip().lower() != "yes":
                continue
            ref = (r.get("ICOCaseRef") or "").strip()
            if ref in seen:
                continue
            seen.add(ref)
            out.append([(r.get("Submitted About Account") or "").strip(),
                        (r.get("Sector") or "").strip(), (r.get("SubSector") or "").strip(),
                        parse_date(r.get("Received Date")), (r.get("Decision") or "").strip(),
                        (r.get("DecisionDetail1") or "").strip(), ref])
    out.sort(key=lambda x: x[3], reverse=True)
    return out


def main():
    data = build_trends()
    data["named"] = build_named()
    data["built"] = date.today().isoformat()
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html"), encoding="utf-8").read()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(tpl.replace("/*__DATA__*/", payload))
    print(f"{len(data['rows'])} incidents, {len(data['named'])} named cyber cases -> docs/index.html "
          f"({os.path.getsize(OUT)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
