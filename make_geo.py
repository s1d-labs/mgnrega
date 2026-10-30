"""Write geo.json: the states, districts and blocks the queues are built from (family.py). Needs an Indian IP; about 800 requests.

    python make_geo.py > geo.json                          # everything from the site
    python make_geo.py --districts raw.jsonl > geo.json    # reuse a district/block list (JSON lines as printed by --raw)
    python make_geo.py --raw > raw.jsonl                   # only the district/block list

States and their MIS start pages come from the national "State Reports" page; districts and blocks (with codes) from the public
Social Audit Report form; `district_mis` is the district's name as linked on the MIS state page, where it differs.
"""
import difflib
import json
import re
import sys
from datetime import date

from nrega import LocalHub, Site, links, norm, options

STATES = "https://nrega.dord.gov.in/Nregahome/stHome.aspx"
SA_FORM = "https://mnregaweb4.nic.in/NregaArch/SocialAudit/SA_LoginReport.aspx?id="
P_SA = "ctl00$ContentPlaceHolder1$"


def states(site):
    out = {}
    for href, text in links(site.get(STATES)):
        m = re.search(r"homestciti\.aspx\?state_code=(\d+)", href, re.I)
        if m and m.group(1) not in out:
            out[m.group(1)] = {"state_code": m.group(1), "state": text, "state_url": re.sub(r"^http:", "https:", href)}
    return list(out.values())


def districts(site, state_codes):
    for sc in state_codes:
        try:
            page = site.get(SA_FORM + sc)
        except Exception as e:
            print(f"state {sc}: {e}", file=sys.stderr)
            continue
        for dc, dn in options(page, P_SA + "ddldist"):
            try:
                p2 = site.postback(page, P_SA + "ddldist", **{P_SA + "ddlFin": options(page, P_SA + "ddlFin")[0][0], P_SA + "ddldist": dc})
                yield {"state_code": sc, "district_code": dc, "district": dn, "blocks": options(p2, P_SA + "ddlblock")}
            except Exception as e:
                print(f"district {sc}/{dc}: {e}", file=sys.stderr)


def match_mis_names(site, sts, dists):
    for s in sts:
        try:
            names = [t for h, t in links(site.get(s["state_url"])) if "Homedist.aspx" in h]
        except Exception as e:
            print(f"state page {s['state_code']}: {e}", file=sys.stderr)
            continue
        by_norm = {norm(n): n for n in names}
        for d in (d for d in dists if d["state_code"] == s["state_code"]):
            if norm(d["district"]) in by_norm:
                continue
            close = difflib.get_close_matches(norm(d["district"]), list(by_norm), n=1, cutoff=0.8)
            d["district_mis"] = by_norm[close[0]] if close else None
            print(f"{s['state']}: {d['district']!r} -> {d['district_mis']!r}", file=sys.stderr)


def main():
    site = Site(LocalHub())
    if "--raw" in sys.argv:
        for d in districts(site, [s["state_code"] for s in states(site)]):
            print(json.dumps(d, ensure_ascii=False), flush=True)
        return
    sts = states(site)
    if "--districts" in sys.argv:
        dists = [json.loads(l) for l in open(sys.argv[sys.argv.index("--districts") + 1]) if '"blocks"' in l]
        dists = [{k: d[k] for k in ("state_code", "district_code", "district", "blocks")} for d in dists]
    else:
        dists = list(districts(site, [s["state_code"] for s in sts]))
    match_mis_names(site, sts, dists)
    json.dump({"generated": date.today().isoformat(), "states": sts, "districts": dists}, sys.stdout, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
