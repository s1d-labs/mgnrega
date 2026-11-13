"""What this family scrapes: the MGNREGA MIS (NREGASoft) of the Ministry of Rural Development.

The NREGA MIS sites answer only from Indian IPs, so both projects are geo="IN" (the hub's India pool servers) and proxy="IN" (GitHub runners,
each through one of the team's Indian proxies; requests picks up HTTPS_PROXY by itself). The queue items come from geo.json, the
list of states, districts and blocks taken from the site's own dropdowns (the setup workflow runs on GitHub, which can't reach the
site, so the list is kept in the repo; see make_geo.py). The geotag project (Bhuvan Bhugram) needs no proxy; its queue comes from geotag_blocks.json (make_geotag_blocks.py).
"""
import json
from pathlib import Path

FAMILY = json.loads((Path(__file__).parent / "scrape.json").read_text())["family"]
GEO = json.loads((Path(__file__).parent / "geo.json").read_text())
GEOTAG = json.loads((Path(__file__).parent / "geotag_blocks.json").read_text())   # Bhugram's own district/block list
GEOTAG_CATEGORIES = ["05"]
# each block once (a block can be listed under an old and a new district; the WMS filter uses the block code only)
GEOTAG_BLOCKS = list({b[0]: {"state_code": d["state_code"], "district_code": d["district_code"], "district": d["district"],
                             "block_code": b[0], "block": b[1]}
                      for d in GEOTAG["districts"] for b in d["blocks"]}.values())                     # Bhugram work categories to fetch: 05 = Rural Connectivity (roads, culverts, drains)
FIN_YEARS = ["2026-2027", "2025-2026", "2024-2025", "2023-2024", "2022-2023"]     # the years the MIS offers
STATE_URL = {s["state_code"]: s["state_url"] for s in GEO["states"]}
# In the pool or not: False takes every project out; a project's own "active" does the same for it alone (no new work is handed
# out; queues and records are kept). Run setup after changing. vendors and socialaudit are out (paused 2026-11); geotag is in.
ACTIVE = True

PROJECTS = [
    {
        "name": f"{FAMILY}-vendors",
        "title": "G06 MGNREGA material vendors (MIS vendor-wise expenditure)",
        "task": "vendors",
        "target": "mnregaweb2.dord.gov.in",
        "env": {},
        "geo": "IN",                   # the hub's India servers ...
        "proxy": "IN",                 # ... and GitHub runners through the team's Indian proxies (one per worker)
        "active": False,
        "max_workers": 8,
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
        "geo": "IN",                   # the hub's India servers ...
        "proxy": "IN",                 # ... and GitHub runners through the team's Indian proxies (one per worker)
        "active": False,
        "max_workers": 4,
        "unit_label": "block-year", "record_label": "record",
        # one item per block and financial year (the audit pages are slow: about 7 requests per panchayat)
        "items": [{"key": f"{fy}/{b[0]}",
                   "payload": {"state_code": d["state_code"], "district_code": d["district_code"], "block_code": b[0],
                               "block": b[1], "fin_year": fy}}
                  for fy in FIN_YEARS for d in GEO["districts"] for b in d["blocks"]],
    },
    {
        "name": f"{FAMILY}-geotag",
        "title": "Bhuvan Bhugram (GeoMGNREGA) road assets: WMS record, asset page, photos",
        "task": "geotag",
        "target": "bhuvan-app2.nrsc.gov.in",
        "env": {},
        "probe": f"{FAMILY}:bhuvan-wms",   # work holds while the WMS server answers "busy"
        "active": True,
        "max_workers": 4,              # Bhuvan is one shared government server: 4, then 8 only while the error rate stays low
        "threads": 2,
        "delay_ms": 300,
        "unit_label": "block-category", "record_label": "asset",
        # one item per block and work category: the WMS lists the block's assets, then each asset page and its photos
        "items": [{"key": f"{c}/{b['state_code']}/{b['district_code']}/{b['block_code']}", "payload": {**b, "category": c}}
                  for c in GEOTAG_CATEGORIES for b in GEOTAG_BLOCKS],
    },
    {
        "name": f"{FAMILY}-geotag-wms",
        "title": "Bhuvan Bhugram (GeoMGNREGA) all other categories: WMS records only (location, codes, years, status)",
        "task": "geotag_wms",
        "target": "bhuvan-vec3.nrsc.gov.in",
        "env": {},
        "probe": f"{FAMILY}:bhuvan-wms",
        "active": True,
        "max_workers": 1,              # low rate, beside the road project
        "threads": 1,
        "delay_ms": 2000,
        "unit_label": "block", "record_label": "block summary",
        # one item per block: every category except 05; the WMS answers go to the bucket, one summary record per block
        "items": [{"key": f"other/{b['state_code']}/{b['district_code']}/{b['block_code']}", "payload": {**b, "category": "other"}}
                  for b in GEOTAG_BLOCKS],
    },
]

# No health checks yet: the hub checks sites from its own server (outside India), where these sites never answer, so a probe
# would hold the work forever. Add them once the hub can run checks from the India servers.
# The WMS answer for one known asset (a 1-feature query): "busy" answers (no free database connection) hold the geotag work.
PROBES = [{"name": f"{FAMILY}:bhuvan-wms", "expect": "FeatureCollection",
           "url": "https://bhuvan-vec3.nrsc.gov.in/bhuvan/wms?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetFeatureInfo&LAYERS=nrega%3Anrega_phase2"
                  "&QUERY_LAYERS=nrega%3Anrega_phase2&STYLES=&SRS=EPSG%3A4326&BBOX=60%2C0%2C100%2C40&WIDTH=1&HEIGHT=1&X=0&Y=0"
                  "&INFO_FORMAT=application%2Fjson&FEATURE_COUNT=1&CQL_FILTER=sid%3D46336682"}]
