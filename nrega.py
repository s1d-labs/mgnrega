"""Shared plumbing for the mgnrega tasks: fetching with the hub's counters, ASP.NET forms, links and tables.

The NREGA MIS (NREGASoft, *.dord.gov.in / mnregaweb4.nic.in) answers only from Indian IPs, so its projects run on the hub's India
pool servers (geo="IN"). Report links carry an encrypted `payload=` that is bound to the session that produced it, so a task always
walks from a public start page and follows links in one requests.Session; URLs are never built by hand.
"""
import html
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import unquote_plus, urljoin

import requests
from bs4 import BeautifulSoup

UA = "s1d-labs scraper (https://github.com/s1d-labs)"
DEADLINE = time.time() + float(os.environ.get("SCRAPE_MAX_HOURS", "1")) * 3600 - 120


def cap(name):
    """MAX_BLOCKS / MAX_PANCHAYATS from the environment: limits for checks (scrape-check --env MAX_PANCHAYATS=1); unset = no limit."""
    return int(os.environ.get(name) or 0) or None


class OutOfTime(Exception):
    """The time budget ran out in the middle of an item."""


class PageError(Exception):
    """A page did not load, or did not look the way the parser expects."""


class Missing(PageError):
    """The site doesn't list what the item asks for (a district, block or year): retrying won't help."""


class Page(str):
    """Page text that remembers its URL, so its links and form resolve against the right base."""

    def __new__(cls, text, url):
        p = super().__new__(cls, text)
        p.url = url
        return p


class Site:
    """One browsing session: cookies, the last page (for Referer) and the hub's counters for every request."""

    def __init__(self, hub, timeout=90):
        self.hub, self.timeout = hub, timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.url = None

    def pause(self):
        time.sleep(max(0.5, self.hub.settings.get("delay_ms", 0) / 1000))

    def get(self, url, data=None, referer=None):
        """GET (or POST when `data` is given) an absolute URL; returns a Page, or raises PageError. Counts n_http, t_http_s,
        http_<code> for the hub. The Referer is the page the link was on (given) or the last page loaded."""
        while self.hub.settings.get("paused"):              # the hub holds the site (owner's pause or back-off): wait mid-item too
            if time.time() > DEADLINE:
                raise OutOfTime()
            time.sleep(30)
        if time.time() > DEADLINE:
            raise OutOfTime()
        headers = {"Referer": referer or self.url} if (referer or self.url) else {}
        t = time.time()
        try:
            r = self.s.request("POST" if data is not None else "GET", url, data=data, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            self.hub.metric("http_error")
            self.hub.error()
            raise PageError(f"{type(e).__name__} on {url[:120]}")
        finally:
            self.pause()
        self.hub.metric("n_http")
        self.hub.metric("t_http_s", time.time() - t)
        if r.status_code != 200:
            self.hub.metric(f"http_{r.status_code}")
            if r.status_code in (403, 429, 503):
                self.hub.block()
            raise PageError(f"HTTP {r.status_code} on {url[:120]}")
        if "/error.html" in r.url or "aspxerrorpath" in r.url:
            self.hub.metric("site_error_page")
            raise PageError(f"site error page for {url[:120]}")
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        self.url = r.url
        return Page(r.text, r.url)

    def postback(self, page, target, **fields):
        """Submit the ASP.NET form on `page` the way a browser does when a dropdown changes (target = the control's name) or a
        button is pressed (target=None, the button given in fields). Keeps the form's hidden fields and current selections."""
        form = form_fields(page)
        form.update(fields)
        form["__EVENTTARGET"] = target or ""
        form["__EVENTARGUMENT"] = ""
        m = re.search(r'<form[^>]*action="([^"]*)"', page)
        return self.get(urljoin(page.url, html.unescape(m.group(1))) if m else page.url, data=form, referer=page.url)


def form_fields(page):
    """Hidden inputs plus the selected option of every <select>, as a browser would post them."""
    out = {html.unescape(n): html.unescape(v) for n, v in re.findall(r'<input type="hidden" name="([^"]+)"[^>]*?value="([^"]*)"', page)}
    for name, body in re.findall(r'<select name="([^"]+)"(.*?)</select>', page, re.S):
        m = re.search(r'<option selected="selected" value="([^"]*)"', body)
        if m:
            out[html.unescape(name)] = html.unescape(m.group(1))
    for tag in re.findall(r'<input[^>]*type="radio"[^>]*checked="checked"[^>]*>', page):
        n, v = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if n and v:
            out[html.unescape(n.group(1))] = html.unescape(v.group(1))
    return out


def options(page, name):
    """[(value, text)] of a <select>, without the "[Select ...]" placeholder."""
    m = re.search(r'<select name="' + re.escape(name) + r'"(.*?)</select>', page, re.S)
    if not m:
        return []
    return [(html.unescape(v), clean(t)) for v, t in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)', m.group(1))
            if v not in ("", "0", "-1")]


def links(page):
    """[(href, text)] of every <a>: href made absolute against the page's URL (when it has one), text with tags removed."""
    base = getattr(page, "url", "")
    return [(urljoin(base, html.unescape(h).strip()), clean(re.sub(r"<[^>]+>", " ", t)))
            for h, t in re.findall(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.S | re.I)]


def link(page, text=None, href=None):
    """The first absolute link whose text equals `text` (case and spacing ignored) or whose href matches the regex `href`."""
    for h, t in links(page):
        if (text is not None and norm(t) == norm(text)) or (href is not None and re.search(href, h, re.I)):
            return h
    return None


def form_action_params(page):
    """Query parameters of the page's own form action: the site puts the readable codes there (state_code, block_code, ...)."""
    m = re.search(r'<form[^>]*action="[^"?]*\?([^"]*)"', page)
    out = {}
    for kv in html.unescape(m.group(1)).split("&") if m else []:
        k, _, v = kv.partition("=")
        if k:
            out[k.lower()] = unquote_plus(v)
    return out


def table(page, table_id):
    """Rows of the table with this id, as lists of cell texts (td and th); nested tables are kept inside their cell."""
    t = BeautifulSoup(page, "html.parser").find("table", id=table_id)
    if t is None:
        return None
    rows = []
    for tr in t.find_all("tr"):
        if tr.find_parent("table") is not t:
            continue
        rows.append([clean(c.get_text(" ")) for c in tr.find_all(["td", "th"], recursive=False)])
    return rows


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "").replace("\xa0", " ")).strip()


def norm(s):
    """For matching names across pages: upper case, letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def num(s):
    """'₹16,21,664' -> 1621664.0, '' -> None."""
    s = re.sub(r"[^0-9.\-]", "", s or "")
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def day(s):
    """'29/06/2026' -> '2026-06-29'; anything else -> None."""
    try:
        return datetime.strptime((s or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def split_code_name(s):
    """'3401001002/IF/7080902339902 | (Work name)' or 'name(code)' -> (code, name)."""
    s = clean(s)
    m = re.match(r"^(.*?)\s*\|\s*\((.*)\)$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.*)\(([^()]*/[^()]*)\)\s*$", s)      # 'Work name(3401001002/IF/...)'
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return s, ""


class LocalHub:
    """Stand-in for HubClient for `--local` runs: records to stdout as JSON lines, counters and files to stderr."""
    settings = {"threads": 1, "delay_ms": 1000, "paused": False}

    def __init__(self, save_dir=None):
        self.counts, self.records, self.save_dir = {}, 0, save_dir

    def metric(self, name, value=1):
        self.counts[name] = self.counts.get(name, 0) + value

    def error(self, n=1):
        self.metric("errors", n)

    def block(self, n=1):
        self.metric("blocked", n)

    def record(self, rid, data):
        import json
        self.records += 1
        print(json.dumps({"record_id": rid, "data": data}, ensure_ascii=False))

    def failure(self, kind, key, reason="", **detail):
        print(f"failure {kind} {key}: {reason} {detail}", file=sys.stderr)

    def upload_many(self, files):
        for path, data, _ in files:
            if self.save_dir:
                p = os.path.join(self.save_dir, path)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "wb").write(data)
            print(f"upload {path} ({len(data)} bytes)", file=sys.stderr)
        return []


def run(task, parse_args_item):
    """The claim loop shared by the tasks. `task(hub, site, payload) -> records stored` does one queue item."""
    import json
    if sys.argv[1:2] == ["--local"]:
        hub = LocalHub(save_dir=os.environ.get("SAVE_DIR"))
        payload = parse_args_item(json.loads(sys.argv[2]))
        n = task(hub, Site(hub), payload)
        print(f"{n} records, counters {hub.counts}", file=sys.stderr)
        return
    from scrape_hub_client import HubClient
    hub = HubClient(os.environ["SCRAPE_POOL_PROJECT"], job_index=int(os.environ.get("SCRAPE_JOB_INDEX", "0")),
                    total_jobs=int(os.environ.get("SCRAPE_TOTAL_JOBS", "1")))
    item = None
    try:
        while time.time() < DEADLINE:
            if hub.settings.get("paused"):
                time.sleep(30)
                continue
            items = hub.claim(1, steal=True, retry_failed=True)
            if not items:
                break
            item = items[0]
            try:
                n = task(hub, Site(hub), item["payload"])
            except OutOfTime:
                hub.complete([item["key"]], "failed", error="out of time; will be retried")
                item = None
                break
            except Missing as e:
                hub.failure("not-listed", item["key"], str(e)[:300])           # shown on the dashboard; not retried
                hub.complete([item["key"]], "done", fetched=0)
                item = None
                continue
            except PageError as e:
                hub.complete([item["key"]], "failed", error=str(e)[:200])
                item = None
                continue
            hub.complete([item["key"]], "done", fetched=n)
            item = None
        hub.close()
    except BaseException as e:
        if item is not None:
            try:
                hub.complete([item["key"]], "failed", error=f"stopped: {type(e).__name__}"[:200])
            except Exception:
                pass
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise
