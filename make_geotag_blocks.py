"""Build geotag_blocks.json: the states, districts and blocks of Bhuvan Bhugram (GeoMGNREGA), from the map's own dropdowns.

The geotag queue is built from this file (the setup workflow runs on GitHub and does not call the site). About one request per
state and per district, one second apart. Run it again when Bhugram adds districts:
    python make_geotag_blocks.py > geotag_blocks.json                 # all of India
    python make_geotag_blocks.py --district 3318 > geotag_blocks.json # only these districts (pilot)
"""
import argparse
import json
import sys
import time

import requests

from geotag import APP, UA, parse_options

PANEL = "https://bhuvan-app2.nrsc.gov.in/bhugram/usrtasks/nrega_phase2/nrega2.php"


def get(s, url):
    for i in range(3):
        try:
            r = s.get(url, timeout=60)
            if r.status_code == 200:
                return r.text
        except requests.RequestException:
            pass
        time.sleep(5 * (i + 1))
    raise SystemExit(f"no answer: {url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", action="append", default=[], help="state code(s) to include (default: all)")
    ap.add_argument("--district", action="append", default=[], help="district code(s) to include (default: all)")
    a = ap.parse_args()
    s = requests.Session()
    s.headers["User-Agent"] = UA
    panel = get(s, PANEL)
    import re
    sel = re.search(r"<select[^>]*id=['\"]?statecode.*?</select>", panel, re.S | re.I).group(0)
    states = [(c, n) for c, n in parse_options(sel) if not n.lower().startswith("demo")]
    out = {"source": PANEL, "states": [], "districts": []}
    for sc, sn in states:
        if a.state and sc not in a.state:
            continue
        if a.district and not any(d.startswith(sc) for d in a.district):
            continue
        out["states"].append({"state_code": sc, "state": sn})
        for dc, dn in parse_options(get(s, APP + f"get_district.php?val={sc}&fyear=all")):
            if a.district and dc not in a.district:
                continue
            time.sleep(1)
            blocks = parse_options(get(s, APP + f"get_block.php?val={dc}&fyear=all"))
            out["districts"].append({"state_code": sc, "district_code": dc, "district": dn, "blocks": blocks})
            print(f"{sn} / {dn}: {len(blocks)} blocks", file=sys.stderr)
        time.sleep(1)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
