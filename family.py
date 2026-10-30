"""What this family scrapes: the MGNREGA MIS (NREGASoft) of the Ministry of Rural Development.

Both sites answer only from Indian IPs, so both projects are geo="IN" (the hub's India pool servers run them; GitHub runners never
do). The queue items come from geo.json, the list of states, districts and blocks taken from the site's own dropdowns
(the setup workflow runs on GitHub, which can't reach the site, so the list is kept in the repo; see make_geo.py).
"""
import json
from pathlib import Path

FAMILY = json.loads((Path(__file__).parent / "scrape.json").read_text())["family"]
GEO = json.loads((Path(__file__).parent / "geo.json").read_text())
FIN_YEARS = ["2026-2027", "2025-2026", "2024-2025", "2023-2024", "2022-2023"]     # the years the MIS offers
STATE_URL = {s["state_code"]: s["state_url"] for s in GEO["states"]}

PROJECTS = [
    {
        "name": f"{FAMILY}-vendors",
        "title": "G06 MGNREGA material vendors (MIS vendor-wise expenditure)",
        "task": "vendors",
        "target": "mnregaweb2.dord.gov.in",
        "env": {},
        "geo": "IN",
        "max_workers": 4,
        "unit_label": "district-year", "record_label": "material line",
        # one item per district and financial year: the task walks its blocks and panchayats in one session
        "items": [{"key": f"{fy}/{d['state_code']}/{d['district_code']}",
                   "payload": {"state_url": STATE_URL[d["state_code"]], "state_code": d["state_code"], "district": d["district"],
                               "district_mis": d.get("district_mis"), "district_code": d["district_code"], "fin_year": fy}}
                  for fy in FIN_YEARS for d in GEO["districts"] if d["state_code"] in STATE_URL],
    },
    {
        "name": f"{FAMILY}-socialaudit",
        "title": "A07 MGNREGA social audit (MIS records per panchayat audit)",
        "task": "socialaudit",
        "target": "mnregaweb4.nic.in",
        "env": {},
        "geo": "IN",
        "max_workers": 4,
        "unit_label": "block-year", "record_label": "record",
        # one item per block and financial year (the audit pages are slow: about 7 requests per panchayat)
        "items": [{"key": f"{fy}/{b[0]}",
                   "payload": {"state_code": d["state_code"], "district_code": d["district_code"], "block_code": b[0],
                               "block": b[1], "fin_year": fy}}
                  for fy in FIN_YEARS for d in GEO["districts"] for b in d["blocks"]],
    },
]

# No health checks yet: the hub checks sites from its own server (outside India), where these sites never answer, so a probe
# would hold the work forever. Add them once the hub can run checks from the India servers.
PROBES = []
