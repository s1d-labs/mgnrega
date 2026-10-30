"""Parsing tests on saved NREGA MIS pages (Ranchi district, Angara block, Jharkhand; fetched 2026-10-30): fast, no network."""
import gzip
from collections import Counter
from pathlib import Path

import nrega
import socialaudit
import vendors

PAGES = Path(__file__).parent / "pages"


def page(name, url=""):
    return nrega.Page(gzip.open(PAGES / name, "rt", encoding="utf-8").read(), url)


def test_vendor_lines_add_up_to_the_reports_grand_total():
    p = page("vendors-panchayat-2025-26.html.gz")
    rows = vendors.parse(p)
    assert len(rows) == 33 and len({r for r, _ in rows}) == 33                 # ids unique, stable across runs
    assert round(sum(d["amount"] for _, d in rows), 2) == vendors.grand_total(p) == 103472.43
    assert round(sum(d["amount_paid"] for _, d in rows), 2) == 83178.91
    rid, d = rows[0]
    assert rid == "2025-2026/3401001002/3401001-ML-1836/1552/3401001002/IF/7080902627682/7"
    assert d["vendor_name"] == "M/S KALYAN ENTERPRISES" and d["vendor_tin"] == "20840306441"
    assert d["work_code"] == "3401001002/IF/7080902627682" and d["work_category"] == "Works on Individuals Land (Category IV)"
    assert (d["bill_no"], d["bill_date"], d["amount"], d["payment_date"]) == ("1552", "2025-04-05", 500.0, "2025-12-10")
    assert (d["state_code"], d["district_code"], d["block_code"], d["panchayat_code"]) == ("34", "3401", "3401001", "3401001002")
    assert all(d["work_code"] and d["vendor_name"] and d["bill_no"] for _, d in rows)    # blanks carried down from the group


def test_block_summary_lists_panchayats_with_vendor_counts():
    gps = vendors.panchayats(page("vendors-block-summary-2025-26.html.gz", "https://mnregaweb2.dord.gov.in/netnrega/materialwise_exp.aspx?x"))
    assert len(gps) == 21 and gps[0][1:] == ("ANGARA", 4)
    assert gps[0][0].startswith("https://mnregaweb2.dord.gov.in/netnrega/vendorwise_Exp_report1.aspx?payload=")


def test_district_page_lists_blocks_and_year():
    d = page("mis-district.html.gz", "https://mnregaweb2.dord.gov.in/netnrega/Homedist.aspx?x")
    blocks = vendors.blocks(d)
    assert len(blocks) == 18 and blocks[0][1] == "ANGARA"
    assert blocks[0][0].startswith("https://mnregaweb2.dord.gov.in/netnrega/Progofficer/PoIndexFrame.aspx?payload=")
    assert nrega.form_fields(d)["fin_year"] == "2026-2027"


def test_social_audit_form_dropdowns():
    f = page("sa-form-block.html.gz")
    assert nrega.options(f, socialaudit.P + "ddlpanchayat")[0] == ("3401001002", "ANGARA")
    f = page("sa-form-periods.html.gz")
    assert nrega.options(f, socialaudit.P + "ddlSAPeriod") == [("01-04-2024/31-03-2025", "01 Apr 2024 - 31 Mar 2025")]
    assert nrega.form_fields(f)[socialaudit.P + "rbLoginLevel"] == "0"


def test_social_audit_formats():
    codes = {"fin_year": "2025-2026", "state_code": "34", "district_code": "3401", "block_code": "3401001", "panchayat_code": "3401001002"}
    rows = socialaudit.parse(page("sa-format1.html.gz"), page("sa-format2a.html.gz"), page("sa-format2b.html.gz"), codes)
    assert len({r for r, _ in rows}) == len(rows)
    assert Counter(d["type"] for _, d in rows) == {"work": 82, "material": 55, "panchayat": 1}
    rid, gp = rows[0]
    assert rid == "gp/2024-04-01_2025-03-31/3401001002"
    assert (gp["sa_period_from"], gp["sa_period_to"], gp["block"], gp["panchayat"]) == ("2024-04-01", "2025-03-31", "ANGARA", "ANGARA")
    assert (gp["registered_households"], gp["jobcard_persons"], gp["works_with_expenditure"]) == (1277, 3266, 85)
    assert (gp["unskilled_wages"], gp["skilled_wages"], gp["material_amount"]) == (1621664, 15751, 29601)
    works = [d for _, d in rows if d["type"] == "work"]
    assert sum(w["period_total"] for w in works) == 1735395                     # the report's own total row
    assert works[0]["work_code"] == "3401001002/AV/7080901185010" and works[0]["work_status"] == "Completed"
    mats = [d for _, d in rows if d["type"] == "material"]
    assert mats[0]["vendor_name"] == "M/S KALYAN ENTERPRISES" and mats[0]["work_code"] == "3401001002/IF/7080903024454"
    assert all(m["work_code"] for m in mats)


def test_helpers():
    assert nrega.num("₹16,21,664") == 1621664 and nrega.num("") is None
    assert nrega.day("29/06/2026") == "2026-06-29" and nrega.day("") is None
    assert nrega.split_code_name("20CWOPM0523Q1ZB_142894 | (TIRATH ENTERPRISES)") == ("20CWOPM0523Q1ZB_142894", "TIRATH ENTERPRISES")
    assert nrega.split_code_name("Mango orchard(3401001002/IF/7080903024454)") == ("3401001002/IF/7080903024454", "Mango orchard")
