"""The `vendors` task (G06): material vendors, bills and amounts per work, from the NREGA MIS report "Vendor Wise Expenditure".

One queue item = one district and financial year. The walk, all in one session (report links only work in the session that
produced them):
    state citizen page -> district (-> post the financial year) -> each block -> "Vendor Wise Expenditure Report"
    -> each panchayat that has vendors -> vendorwise_Exp_report1.aspx: one row per material line of a bill
One record per material line (flat fields: vendor, work, bill, material, amount, payment); the raw panchayat page is kept in the
bucket. Muster rolls and worker lists are never fetched.

Try one district without the hub (needs an Indian IP):
    python vendors.py --local '{"state_url": "https://mnregaweb2.dord.gov.in/netnrega/homestciti.aspx?state_code=34&state_name=JHARKHAND&lflag=eng&labels=labels", "district": "RANCHI", "fin_year": "2025-2026", "max_blocks": 1, "max_panchayats": 2}'
"""
import gzip
import html
import re
from urllib.parse import urljoin

from nrega import Missing, PageError, cap, clean, day, form_action_params, form_fields, link, links, norm, num, run, split_code_name

CELL = re.compile(r'<td id="ctl00_ContentPlaceHolder1_GridView1_(ctl\d+)_(\w+)"[^>]*>(.*?)</td>', re.S)


def grid_rows(page):
    """The vendor report's GridView as [{cell suffix: text}] in page order (cells are named td1..td11, tdvn, tdan)."""
    rows = {}
    for ctl, suffix, body in CELL.findall(page):
        text = re.sub(r"<(?!br)[^>]+>", " ", body)
        rows.setdefault(ctl, {})[suffix] = clean(re.sub(r"<br\s*/?>", " | ", text))
    return list(rows.values())


def parse(page):
    """[(record_id, data)] for one panchayat's vendor-wise expenditure page. Pure: tested on saved pages.

    The report leaves vendor and bill number blank on the rows after the first of a group; they carry down."""
    meta = form_action_params(page)
    fy, gp = meta.get("fin_year", ""), meta.get("panchayat_code", "")
    base = {"fin_year": fy, "state_code": meta.get("state_code"), "state": meta.get("state_name"),
            "district_code": meta.get("district_code"), "district": meta.get("district_name"),
            "block_code": meta.get("block_code"), "block": meta.get("block_name"),
            "panchayat_code": gp, "panchayat": meta.get("panchayat_name")}
    out, seen, vendor, bill = [], {}, "", ""
    for r in grid_rows(page):
        if re.match(r"(grant ?total|sub ?total)", r.get("td5", ""), re.I):
            continue
        if not r.get("td1") and not r.get("td6"):
            continue
        vendor = r.get("td2") or vendor
        bill = r.get("td3") or bill
        regd, vname = split_code_name(vendor)
        work_code, work_name = split_code_name(r.get("td1", ""))
        mcode, _, mname = r.get("td5", "").partition("-")
        if not mname:
            mcode, mname = "", mcode
        rid = f"{fy}/{gp}/{r.get('tdvn', '')}/{bill}/{work_code}/{mcode.strip() or norm(mname)[:20]}"
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:
            rid += f"#{seen[rid]}"
        out.append((rid, {**base,
                          "vendor_regd_no": regd, "vendor_name": vname,
                          "vendor_tin": regd.rsplit("_", 1)[0] if "_" in regd else regd,
                          "work_code": work_code, "work_name": work_name, "work_category": r.get("td10") or None,
                          "voucher_no": r.get("tdvn") or None, "agency": r.get("tdan") or None,
                          "bill_no": bill or None, "bill_date": day(r.get("td4")),
                          "material_code": mcode.strip() or None, "material": mname.strip(),
                          "amount": num(r.get("td6")), "expenditure_type": r.get("td11") or None,
                          "payment_date": day(r.get("td8")), "amount_paid": num(r.get("td9"))}))
    return out


def grand_total(page):
    for r in grid_rows(page):
        if r.get("td5", "").lower().startswith("grant total"):
            return num(r.get("td6"))
    return None


def panchayats(page):
    """[(absolute href, panchayat name, vendor count)] from a block's "Vendor Wise Expenditure" page."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        a = re.search(r'<a[^>]*href="([^"]*vendorwise_Exp_report1[^"]*)"[^>]*>(.*?)</a>', row, re.S)
        if a:
            cells = [clean(re.sub(r"<[^>]+>", " ", c)) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            out.append((urljoin(getattr(page, "url", ""), html.unescape(a.group(1))), cells[1] if len(cells) > 1 else "",
                        int(num(re.sub(r"<[^>]+>", "", a.group(2))) or 0)))
    return out


def open_district(site, p):
    """The district's MIS page for the item's financial year, reached from the state page in this session."""
    state = site.get(p["state_url"])
    href = link(state, text=p.get("district_mis") or p["district"])
    if not href:
        raise Missing(f"district {p['district']!r} is not on the state page")
    page = site.get(href)
    if form_fields(page).get("fin_year") != p["fin_year"]:
        page = site.postback(page, "fin_year", fin_year=p["fin_year"])
        if form_fields(page).get("fin_year") != p["fin_year"]:
            raise Missing(f"financial year {p['fin_year']} not offered for {p['district']}")
    return page


def blocks(district_page):
    return [(h, t) for h, t in links(district_page) if "PoIndexFrame.aspx" in h and t]


def open_block(hub, site, p, district, name):
    """(district page, the block's "Vendor Wise Expenditure" page or None). Payload links expire, so on a refused link the
    district is opened again from the state page and the block tried once more."""
    for attempt in (1, 2):
        try:
            href = dict((t, h) for h, t in blocks(district)).get(name)
            if not href:
                raise PageError(f"block {name!r} is not on the district page")
            block = site.get(href, referer=district.url)
            vhref = link(block, text="Vendor Wise Expenditure Report")
            if not vhref:
                hub.metric("block_without_vendor_report")
                return district, None
            return district, site.get(vhref, referer=block.url)
        except PageError:
            if attempt == 2:
                raise
            hub.metric("renavigate")
            district = open_district(site, p)


def task(hub, site, p):
    """One district and year: returns the number of records stored."""
    district = open_district(site, p)
    names = [t for _, t in blocks(district)]
    if not names:
        raise Missing(f"no blocks listed for {p['district']}")
    n = 0
    for name in names[:p.get("max_blocks") or cap("MAX_BLOCKS")]:
        district, summary = open_block(hub, site, p, district, name)
        if summary is None:
            continue
        for ghref, gname, count in panchayats(summary)[:p.get("max_panchayats") or cap("MAX_PANCHAYATS")]:
            if count <= 0:
                continue
            try:
                page = site.get(ghref, referer=summary.url)
            except PageError as e:
                hub.failure("panchayat", f"{p['fin_year']}/{p['state_code']}/{p['district']}/{name}/{gname}", str(e))
                continue
            rows = parse(page)
            if not rows:
                hub.metric("panchayat_empty")
                continue
            total = grand_total(page)
            if total is not None and abs(sum(d["amount"] or 0 for _, d in rows) - total) > 1:
                hub.metric("total_mismatch")
            meta = form_action_params(page)
            path = (f"vendors/{p['fin_year']}/{meta.get('state_code') or p['state_code']}/{meta.get('district_code') or 'd'}/"
                    f"{meta.get('block_code') or 'b'}/{meta.get('panchayat_code') or norm(gname)}.html.gz")
            hub.upload_many([(path, gzip.compress(page.encode("utf-8")), "application/gzip")])
            for rid, data in rows:
                hub.record(rid, {**data, "page": path})
            n += len(rows)
            hub.metric("panchayats_done")
        hub.metric("blocks_done")
    return n


def item(p):
    return {"state_code": "", **p}


if __name__ == "__main__":
    run(task, item)
