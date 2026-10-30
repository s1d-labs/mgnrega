"""The `socialaudit` task (A07): the MIS records published for each panchayat's social audit (NREGA "Social Audit Report",
mnregaweb4.nic.in/NregaArch/SocialAudit, a public form with no login).

One queue item = one block and financial year. In one session: pick year, district and block in the form, then for each panchayat
and each social audit period it offers, "Get Reports" and fetch
    Format 1  Panchayat Report Card          -> one `panchayat` record (registration, job cards, works, wage/material totals)
    Format 2A Works Executed (General)        -> one `work` record per work (sanctioned, spent so far, spent in the audit period)
    Format 2B Works Expenditure (wage, material) -> one `material` record per bill line (vendor, bill, material, price, quantity)
Formats 3 (payments to named workers) and 4 (SC/ST households) list people and are never fetched; wage lines of 2B (muster roll
numbers and person-days) stay in the saved page only. The three pages are kept in the bucket.

Try one block without the hub (needs an Indian IP):
    python socialaudit.py --local '{"state_code": "34", "district_code": "3401", "block_code": "3401001", "fin_year": "2025-2026", "max_panchayats": 1}'
"""
import gzip
import re

from nrega import Missing, PageError, cap, day, link, num, options, run, split_code_name, table

BASE = "https://mnregaweb4.nic.in/NregaArch/SocialAudit/"
P = "ctl00$ContentPlaceHolder1$"


def heading(page):
    """State/district/block/panchayat names and the audit period printed at the top of every format."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
    out = {}
    for k in ("State", "District", "Block", "Panchayat"):
        m = re.search(k + r":\s*(.+?)\s+(?:District:|Block:|Panchayat:|Social Audit Period:)", text)
        out[k.lower()] = m.group(1).strip() if m else None
    m = re.search(r"Social Audit Period:\s*From\s*(\d\d/\d\d/\d{4})\s*To\s*(\d\d/\d\d/\d{4})", text)
    out["sa_period_from"], out["sa_period_to"] = (day(m.group(1)), day(m.group(2))) if m else (None, None)
    return out


def total_row(rows):
    return next((r for r in rows or [] if r and r[0].startswith("Total")), None)


def parse_report_card(page):
    """Format 1 -> the panchayat's totals (registration, job cards, works with expenditure)."""
    out = {}
    for tid, prefix in (("dtRegistration", "registered"), ("dtJobCard", "jobcard")):
        t = total_row(table(page, tid))
        if t and len(t) >= 11:
            for i, k in enumerate(("households", "persons", "sc_households", "sc_persons", "st_households", "st_persons",
                                   "other_households", "other_persons", "male_persons", "female_persons"), start=1):
                out[f"{prefix}_{k}"] = num(t[i])
    t = total_row(table(page, "dtExpenseWork"))
    if t and len(t) >= 4:
        out["works_with_expenditure"], out["works_completed_with_expenditure"], out["works_completed_no_expenditure"] = map(num, t[1:4])
    return out


WORK_COLS = ("sanctioned_unskilled", "sanctioned_skilled", "sanctioned_material", "sanctioned_total",
             "spent_unskilled", "spent_skilled", "spent_material", "spent_total",
             "period_unskilled", "period_skilled", "period_material", "period_total")


def parse_works(page):
    """Format 2A -> [work dict] (one per work)."""
    out = []
    for r in table(page, "dtWorkDetails") or []:
        if len(r) < 19 or not r[0].isdigit() or not r[1] or r[1] == "2":
            continue
        out.append({"work_code": r[1], "work_name": r[2], "agency": r[3] or None, "work_start_date": day(r[4]),
                    "last_work_date": day(r[5]), "work_status": r[6] or None,
                    **{k: num(v) for k, v in zip(WORK_COLS, r[7:19])}})
    return out


def parse_material(page):
    """Format 2B -> ([material line dict], {wage and material totals}). Work name carries down from the first line of a work."""
    out, work = [], ("", "")
    for r in table(page, "dtMaterial") or []:
        if len(r) < 9 or not r[0].isdigit() or r[2] == "3":
            continue
        if r[1]:
            work = split_code_name(r[1])
        out.append({"work_code": work[0], "work_name": work[1], "vendor_name": r[2] or None, "bill_no": r[3] or None,
                    "bill_date": day(r[4]), "material": r[5], "unit_price": num(r[6]), "quantity": num(r[7]),
                    "amount": num(r[8])})
    totals = {}
    for tid, k in (("dtunskilled", "unskilled"), ("dtSkilled", "skilled")):
        t = total_row(table(page, tid))
        if t and len(t) >= 3:
            totals[f"{k}_person_days"], totals[f"{k}_wages"] = num(t[1]), num(t[2])
    t = total_row(table(page, "dtMaterial"))
    if t and len(t) >= 4:
        totals["material_amount"] = num(t[3])
    return out, totals


def parse(card, works, expense, codes):
    """[(record_id, data)] for one panchayat and audit period from Formats 1, 2A and 2B. Pure: tested on saved pages.
    `codes`: fin_year, state/district/block/panchayat codes (the pages print names only)."""
    h = heading(card)
    period = f"{h['sa_period_from']}_{h['sa_period_to']}"
    gp = codes["panchayat_code"]
    base = {**codes, "state": h["state"], "district": h["district"], "block": h["block"], "panchayat": h["panchayat"],
            "sa_period_from": h["sa_period_from"], "sa_period_to": h["sa_period_to"]}
    w = parse_works(works)
    m, totals = parse_material(expense)
    out = [(f"gp/{period}/{gp}", {**base, "type": "panchayat", **parse_report_card(card), **totals,
                                   "works_listed": len(w), "material_lines": len(m)})]
    out += [(f"work/{period}/{x['work_code']}", {**base, "type": "work", **x}) for x in w]
    seen = {}
    for x in m:
        rid = f"mat/{period}/{gp}/{x['work_code']}/{x['bill_no']}/{x['material'][:40]}"
        seen[rid] = seen.get(rid, 0) + 1
        out.append((rid + (f"#{seen[rid]}" if seen[rid] > 1 else ""), {**base, "type": "material", **x}))
    return out


def open_block(site, p):
    """The report form with year, district and block chosen; its panchayat list is filled in."""
    page = site.get(BASE + f"SA_LoginReport.aspx?id={p['state_code']}")
    for field, value in (("ddlFin", p["fin_year"]), ("ddldist", p["district_code"]), ("ddlblock", p["block_code"])):
        if value not in dict(options(page, P + field)):
            raise Missing(f"{field} {value} not offered (state {p['state_code']})")
        page = site.postback(page, P + field, **{P + field: value})
    return page


def task(hub, site, p):
    """One block and year: returns the number of records stored."""
    form = open_block(site, p)
    gps = options(form, P + "ddlpanchayat")
    if not gps:
        raise Missing(f"no panchayats listed for block {p['block_code']}")
    n = 0
    for gp, gname in gps[:p.get("max_panchayats") or cap("MAX_PANCHAYATS")]:
        g = site.postback(form, P + "ddlpanchayat", **{P + "ddlpanchayat": gp})
        g = site.postback(g, P + "rbLoginLevel$0", **{P + "rbLoginLevel": "0"})      # "With SA Calendar"
        periods = options(g, P + "ddlSAPeriod")
        if not periods:
            hub.metric("panchayat_no_audit_period")
            continue
        for period, _ in periods:
            q = site.postback(g, P + "ddlSAPeriod", **{P + "ddlSAPeriod": period})
            index = site.postback(q, None, **{P + "login": "Get Reports"})
            pages = {}
            for name, href in (("format1", "panchayat_reportcard"), ("format2a", "work_info"), ("format2b", "exp_info")):
                url = link(index, href=href)
                if not url:
                    raise PageError(f"no {name} link for panchayat {gp} period {period}")
                pages[name] = site.get(url, referer=index.url)
            codes = {"fin_year": p["fin_year"], "state_code": p["state_code"], "district_code": p["district_code"],
                     "block_code": p["block_code"], "panchayat_code": gp}
            rows = parse(pages["format1"], pages["format2a"], pages["format2b"], codes)
            folder = f"socialaudit/{p['fin_year']}/{p['state_code']}/{p['district_code']}/{p['block_code']}/{gp}/{period.replace('/', '_')}"
            hub.upload_many([(f"{folder}/{name}.html.gz", gzip.compress(page.encode("utf-8")), "application/gzip")
                             for name, page in pages.items()])
            for rid, data in rows:
                hub.record(rid, {**data, "pages": folder})
            n += len(rows)
            hub.metric("audits_done")
        hub.metric("panchayats_done")
    return n


def item(p):
    return p


if __name__ == "__main__":
    run(task, item)
