"""The `geotag` task: geotagged MGNREGA assets from Bhuvan "Bhugram" (formerly GeoMGNREGA, NRSC/ISRO), public view, no login.

Each asset is a point or line put on the map by a field app, with a work code, category, financial years and 2 (Phase I) or 6
(Phase II: before / during / after) photos. Three public interfaces are used, the same ones the Bhugram map calls:
    1. WMS GetFeatureInfo on bhuvan-vec3.nrsc.gov.in, layer nrega:nrega_phase2 (WFS is switched off there), with a CQL filter
       block_code + category_id: every asset of the block as GeoJSON (sid, asset_id, codes, years, status, geometry).
       The server stops a request after 60 s; then the block is asked panchayat by panchayat (get_gp.php lists them).
    2. get_details.php?sno=<sid> on bhuvan-app2.nrsc.gov.in: asset name, work code and name, location names, dates, person-days,
       expenditure, who accepted it, and the photo links.
    3. The photos (bhuvan-app2 /nrega_fdch, bhuvan-fdc1 /object/nrega_fdc): the full-size file the page opens on "Click to Zoom-in"
       (it keeps the phone's EXIF and GPS); the smaller copy shown in the page only when the full-size one does not load.

One queue item = one block and one work category (payload: state_code, district_code, block_code, category). One record per
asset (id = Bhugram's serial number `sid`; asset_id repeats across Phase I and II). Raw answers go to the bucket:
    wms/<state>/<district>/<block>/<category>[/<panchayat>].json.gz   the GetFeatureInfo answer
    details/<state>/<district>/<block>/<sid>.html.gz                  the asset page
    photos/<host>/<path on the host>                                  each photo, as fetched

Try one block without the hub (prints records, saves files under SAVE_DIR if set):
    python geotag.py --local '{"state_code": "33", "district_code": "3318", "block_code": "3311003", "category": "05"}'
MAX_ASSETS=3 limits the assets per item (for checks).
--wms-only (task geotag_wms): only the WMS answers go to the bucket, one summary record per block; category "other" = every
category except 05. Asset pages and photos of those categories are not fetched (about 140 TB of photos in all).
"""
import gzip
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

import requests

from scrape_hub_client import stop_flag, timed_get

UA = "s1d-labs scraper (https://github.com/s1d-labs)"
APP = "https://bhuvan-app2.nrsc.gov.in/bhugram/usrtasks/nrega_phase2/get/"
WMS = "https://bhuvan-vec3.nrsc.gov.in/bhuvan/wms"
LAYER = "nrega:nrega_phase2"
FEATURE_COUNT = 100000
BUDGET = float(os.environ.get("SCRAPE_MAX_HOURS", "1")) * 3600
DEADLINE = time.time() + BUDGET - 120
MIN_LEFT = min(15 * 60, BUDGET / 2)   # don't start an item with less time left than this (a cut-off item starts over later)
MAX_ASSETS = int(os.environ.get("MAX_ASSETS") or 0) or None
WMS_ONLY = "--wms-only" in sys.argv or os.environ.get("WMS_ONLY") == "1"   # only the WMS answers: no asset pages, no photos
SAFE = re.compile(r"[^A-Za-z0-9._-]")


class PageError(Exception):
    """An answer did not load or did not look as expected: the item fails and the hub retries it later."""


class TooSlow(PageError):
    """The WMS server stopped the request at its 60 s limit: ask for smaller parts."""


class Busy(PageError):
    """The WMS server had no free database connection ("Timeout waiting for idle object"): wait and ask again."""


# ---------------------------------------------------------------- parsing (pure, tested on saved pages)

def wms_url(cql):
    """GetFeatureInfo for every feature that matches `cql`: one 1x1 pixel over all of India, so the filter alone decides."""
    return WMS + "?" + urlencode({
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetFeatureInfo", "LAYERS": LAYER, "QUERY_LAYERS": LAYER, "STYLES": "",
        "SRS": "EPSG:4326", "BBOX": "60,0,100,40", "WIDTH": 1, "HEIGHT": 1, "X": 0, "Y": 0,
        "INFO_FORMAT": "application/json", "FEATURE_COUNT": FEATURE_COUNT, "CQL_FILTER": cql})


def parse_wms(text):
    """GetFeatureInfo answer -> list of GeoJSON features. Raises TooSlow on the server's time limit, PageError on anything else."""
    if "more time than allowed" in text:
        raise TooSlow("WMS stopped the request at its time limit")
    if "waiting for idle object" in text or "Unable to obtain connection" in text:
        raise Busy("WMS server busy (no free database connection)")
    try:
        data = json.loads(text)
    except ValueError:
        raise PageError("WMS answer is not JSON: " + re.sub(r"\s+", " ", text)[:200])
    feats = data.get("features")
    if not isinstance(feats, list):
        raise PageError("WMS answer has no features list")
    return feats


def parse_options(text):
    """<option value=..>name</option> list of a Bhugram dropdown -> [(code, name)], without 'all'."""
    return [(v, html.unescape(n).strip()) for v, n in re.findall(r"<option[^>]*value=['\"]?([^'\" >]*)[^>]*>([^<]*)", text, re.I)
            if v and v != "all"]


def _text(cell):
    cell = re.sub(r"<br\s*/?>", "\n", cell, flags=re.I)
    cell = html.unescape(re.sub(r"<[^>]+>", " ", cell)).replace("\xa0", " ")
    return "\n".join(re.sub(r"[ \t]+", " ", x).strip() for x in cell.split("\n")).strip()


def _key(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def parse_details(page):
    """get_details.php page -> {"fields": {label_key: value}, "location": {...}, "photos": [{stage, n, url, shown_url}],
    "photo_check": text of the page's distance check}. Every label/value row of the page is kept, under its own label."""
    if "Sl.No" not in page:
        raise PageError("asset page without details")
    fields = {}
    for row in re.split(r"<tr[^>]*>", page, flags=re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row.split("</tr>")[0], re.S | re.I)
        if len(cells) != 2 or "<table" in row:
            continue
        a, b = cells
        k, v = _key(_text(a)), _text(b)
        if k and len(k) <= 60 and k not in fields:
            fields[k] = v or None
    lonlat = (fields.get("longitude_latitude") or "").split("Analyse")[0].strip()
    fields["longitude_latitude"] = lonlat or None
    loc = (fields.get("asset_location") or "").split("\n")
    location = dict(zip(("panchayat", "block", "district", "state"), [x.strip() or None for x in loc])) if len(loc) == 4 else {}
    # the page's own rule for the full-size photo: url.replace('A','B') in the image's onclick
    swaps = re.findall(r"url\.replace\('([^']*)','([^']*)'\)", page)
    shown = re.search(r"id ?=\s*'attachphoto' src='([^']+)'", page)
    photos, seen = [], set()
    for stage_block in re.finditer(r"(stage:\d+|Phase I)\s*&nbsp;<br/>(.*?)</td>", page, re.S):
        stage = stage_block.group(1).replace("stage:", "")
        stage = "0" if stage == "Phase I" else stage
        for url, n in re.findall(r'changeImage\("([^"]+)"\)\s*>(\d+)<', stage_block.group(2)):
            full = url
            for a, b in swaps:
                full = full.replace(a, b)
            if full not in seen:
                seen.add(full)
                photos.append({"stage": stage, "n": int(n), "url": full, "shown_url": url})
    if not photos and shown:                      # Phase I pages show the photos without stage links
        for url in re.findall(r"'(https?://[^']+/photo\d_[^']+\.jpe?g)'", page) or [shown.group(1)]:
            full = url
            for a, b in swaps:
                full = full.replace(a, b)
            if full not in seen:
                seen.add(full)
                m = re.search(r"/photo(\d)_", url)
                photos.append({"stage": "0", "n": int(m.group(1)) if m else len(photos) + 1, "url": full, "shown_url": url})
    check = re.search(r"alert\('(Geo-tagged point[^']*)'\)", page)
    return {"fields": fields, "location": location, "photos": photos,
            "photo_check": check.group(1).replace("\\n", "\n").strip() if check else None}


def photo_key(url):
    """Bucket path of a photo: photos/<host>/<path>, every segment reduced to [A-Za-z0-9._-]."""
    u = urlsplit(url)
    segs = [SAFE.sub("_", s) for s in u.path.split("/") if s and s not in (".", "..")]
    return "/".join(["photos", SAFE.sub("_", u.hostname or "host")] + segs)


# ---------------------------------------------------------------- fetching

class Bhuvan:
    def __init__(self, hub):
        self.hub, self.local = hub, threading.local()

    def session(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = self.local.s = requests.Session()
            s.headers["User-Agent"] = UA
        return s

    def get(self, url, timeout=90, binary=False, tries=3):
        last = None
        for i in range(tries):
            r = timed_get(self.hub, self.session(), url, timeout=timeout)
            if r is not None and r.status_code == 200:
                return r.content if binary else r.content.decode("utf-8", "replace")
            last = "no answer" if r is None else f"HTTP {r.status_code}"
            if r is not None and r.status_code in (404, 410):
                return None
            if r is not None and r.status_code in (403, 429, 503):
                time.sleep(30 * (i + 1))             # the site is pushing back: wait, and let the hub's back-off act
            else:
                time.sleep(5 * (i + 1))
        raise PageError(f"{url[:120]}: {last}")

    def pause(self):
        time.sleep(max(0.2, (self.hub.settings.get("delay_ms") or 0) / 1000))


OTHER = "other"     # payload category for "every category except 05": WMS records only (wms-only mode)
CATEGORIES = ["01", "02", "03", "04", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "16", "17"]


def category_cql(category):
    if category == OTHER:
        return "(category_id <> '05' or category_id is null)"
    return f"category_id='{category}'"


def _ask(site, cql):
    """The WMS answer for `cql`, checked; a busy server is asked again after 1, 2, 4, 8 minutes (counted as wms_busy)."""
    for i in range(5):
        text = site.get(wms_url(cql), timeout=120)
        try:
            parse_wms(text)
            return text
        except Busy:
            site.hub.metric("wms_busy")
            site.hub.block()
            if i == 4:
                raise
            time.sleep(60 * 2 ** i)


def features(site, p):
    """Every asset of the block and category, and the raw answers [(bucket path, text)]. When the server's 60 s limit stops a
    request: panchayat by panchayat (get_gp.php lists them), and for category "other" then category by category."""
    base = f"wms/{p['state_code']}/{p['district_code']}/{p['block_code']}/{p['category']}"
    cql = f"block_code='{p['block_code']}' and {category_cql(p['category'])}"
    try:
        text = _ask(site, cql)
        feats = parse_wms(text)
        if len(feats) < FEATURE_COUNT:
            return feats, [(base + ".json.gz", text)]
    except TooSlow:
        pass
    gps = parse_options(site.get(APP + f"get_gp.php?val={p['block_code']}&fyear=all"))
    if not gps:
        raise PageError("block too big for one WMS request and no panchayat list")
    out, raw = [], []
    for code, _ in gps:
        gcql = f"block_code='{p['block_code']}' and panchayat_code='{code}'"
        try:
            text = _ask(site, gcql + " and " + category_cql(p["category"]))
            out += parse_wms(text)
            raw.append((f"{base}/{code}.json.gz", text))
        except TooSlow:
            if p["category"] != OTHER:
                raise PageError(f"panchayat {code} too big for one WMS request")
            for c in CATEGORIES + ["none"]:
                ccql = "(category_id is null or category_id = '')" if c == "none" else f"category_id='{c}'"
                text = _ask(site, gcql + " and " + ccql)
                out += parse_wms(text)
                raw.append((f"{base}/{code}/{c}.json.gz", text))
                site.pause()
        site.pause()
    return out, raw


def do_asset(site, p, feat, uploaded):
    """One asset: its page, its photos (to the bucket), and the record."""
    props = feat.get("properties") or {}
    sid = str(props.get("sid"))
    url = APP + f"get_details.php?sno={sid}&mode=nm&uname=empty&block_code={p['block_code']}&stage={props.get('stage', '')}"
    page = site.get(url)
    if page is None:
        raise PageError(f"asset {sid}: page not found")
    det = parse_details(page)
    dkey = f"details/{p['state_code']}/{p['district_code']}/{p['block_code']}/{sid}.html.gz"
    files = [(dkey, gzip.compress(page.encode("utf-8")), "application/gzip")]
    photos = []
    for ph in det["photos"]:
        got, data = None, None
        err = None
        for u in dict.fromkeys([ph["url"], ph["shown_url"]]):
            try:
                data = site.get(u, timeout=120, binary=True)
            except PageError as e:
                data, err = None, str(e)
            if data:
                got, err = u, None
                break
        site.pause()
        entry = dict(ph, fetched_url=got, status="stored" if data else ("error" if err else "not_found"))
        if err:
            entry["error"] = err[:200]
            site.hub.failure("photo", ph["url"][-200:], err[:300], sid=sid)
        if data:
            k = photo_key(got)
            entry.update(key=k, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            if k not in uploaded:
                files.append((k, data, "image/jpeg"))
        photos.append(entry)
    site.hub.upload_many(files)
    for path, *_ in files:
        uploaded.add(path)
    data = {**props, "geometry": feat.get("geometry"), "feature_id": feat.get("id"), "details": det["fields"],
            "location": det["location"], "photos": photos, "photo_check": det["photo_check"],
            "details_key": dkey, "details_url": url, "source": "Bhuvan Bhugram (GeoMGNREGA), NRSC/ISRO, public view"}
    site.hub.record(sid, data)
    return sum(1 for x in photos if x["status"] == "stored")


def wms_summary(p, feats, raw):
    """WMS-only mode: one record per block (the assets themselves stay in the bucket copies of the WMS answers)."""
    from collections import Counter
    props = [f.get("properties") or {} for f in feats]
    return {**{k: p.get(k) for k in ("state_code", "district_code", "district", "block_code", "block", "category")},
            "assets": len({x.get("sid") for x in props}), "features": len(feats),
            "by_category": dict(Counter(x.get("category_id") or "" for x in props)),
            "by_stage": dict(Counter(x.get("stage") or "" for x in props)),
            "files": [k for k, _ in raw], "bytes": sum(len(t.encode("utf-8")) for _, t in raw),
            "source": "Bhuvan Bhugram (GeoMGNREGA) WMS nrega:nrega_phase2, GetFeatureInfo"}


def do_item(hub, p, stop):
    site = Bhuvan(hub)
    feats, raw = features(site, p)
    hub.upload_many([(k, gzip.compress(t.encode("utf-8")), "application/gzip") for k, t in raw])
    if WMS_ONLY:
        hub.record(f"{p['category']}/{p['block_code']}", wms_summary(p, feats, raw))
        return len(feats)
    if MAX_ASSETS:
        feats = feats[:MAX_ASSETS]
    seen, todo = set(), []
    for f in feats:
        sid = (f.get("properties") or {}).get("sid")
        if sid is not None and sid not in seen:
            seen.add(sid)
            todo.append(f)
    uploaded, done, errors, photos = set(), 0, [], 0
    threads = max(1, min(8, int(hub.settings.get("threads") or 1)))
    with ThreadPoolExecutor(threads) as pool:
        futs = []
        for f in todo:
            futs.append(pool.submit(lambda f=f: None if stop.is_set() or time.time() > DEADLINE else do_asset(site, p, f, uploaded)))
        for f, fut in zip(todo, futs):
            try:
                n = fut.result()
                if n is not None:
                    done += 1
                    photos += n
            except PageError as e:
                sid = str((f.get("properties") or {}).get("sid"))
                hub.failure("asset", sid, str(e)[:300])
                errors.append(sid)
    hub.metric("photos_stored", photos)
    if stop.is_set() or time.time() > DEADLINE:
        raise PageError(f"stopped after {done} of {len(todo)} assets; will be retried")
    if errors and len(errors) > max(3, len(todo) // 20):
        raise PageError(f"{len(errors)} of {len(todo)} assets failed")
    return done


def main():
    stop = stop_flag()
    args = [a for a in sys.argv[1:] if a != "--wms-only"]
    if args[:1] == ["--local"]:
        from scrape_hub_client import LocalHub
        hub = LocalHub(threads=2, delay_ms=200, save_dir=os.environ.get("SAVE_DIR"))
        n = do_item(hub, json.loads(args[1]), stop)
        print(f"{n} assets, counters {hub.counts}", file=sys.stderr)
        return
    from scrape_hub_client import HubClient
    hub = HubClient(os.environ["SCRAPE_POOL_PROJECT"], job_index=int(os.environ.get("SCRAPE_JOB_INDEX", "0")),
                    total_jobs=int(os.environ.get("SCRAPE_TOTAL_JOBS", "1")))
    item = None
    try:
        while not stop.is_set() and DEADLINE - time.time() > MIN_LEFT:
            if hub.settings.get("paused"):
                time.sleep(30)
                continue
            items = hub.claim(1, steal=True, retry_failed=True)
            if not items:
                break
            item = items[0]
            try:
                n = do_item(hub, item["payload"], stop)
                hub.complete([item["key"]], "done", fetched=n)
            except PageError as e:
                hub.complete([item["key"]], "failed", error=str(e)[:200])
            item = None
        hub.close("failed", "stopped by signal") if stop.is_set() else hub.close()
    except BaseException as e:
        if item is not None:
            try:
                hub.complete([item["key"]], "failed", error=f"stopped: {type(e).__name__}"[:200])
            except Exception:
                pass
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise


if __name__ == "__main__":
    main()
