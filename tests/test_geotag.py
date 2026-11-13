"""Parsing tests for the geotag task on saved Bhuvan Bhugram answers (fetched 2026-11-13): fast, no network."""
import gzip
import json
from pathlib import Path

import pytest

import geotag

PAGES = Path(__file__).parent / "pages"


def page(name):
    return gzip.open(PAGES / name, "rt", encoding="utf-8").read()


def test_phase1_asset_page():
    d = geotag.parse_details(page("bhugram-asset-phase1.html.gz"))
    f = d["fields"]
    assert (f["sl_no"], f["asset_id"], f["work_code"]) == ("46336682", "36002809411", "3619051015/RC/7034080001")
    assert (f["category"], f["sub_category"], f["stage"]) == ("Rural Connectivity", "WBM", "0")
    assert f["accepted_by"] == "KSREDDY142" and f["date_of_validation"] == "2018-02-08 17:41:26"
    assert d["location"] == {"panchayat": "CHAKEPALLE", "block": "Chennur", "district": "ADILABAD", "state": "TELANGANA"}
    assert [(p["stage"], p["n"]) for p in d["photos"]] == [("0", 1), ("0", 2)]
    # the page's zoom rule: photos_comp2 -> photos, nrega_fdc -> nrega_fdch (the full-size file with EXIF)
    assert d["photos"][0]["url"].startswith("https://bhuvan-app2.nrsc.gov.in/nrega_fdch/upload/photos/0/2009-2010/36/3619/")
    assert "/photos_comp2/" in d["photos"][0]["shown_url"]
    assert d["photo_check"].startswith("Geo-tagged point =79.77/18.74")


def test_phase2_asset_page_has_before_during_after_photos():
    d = geotag.parse_details(page("bhugram-asset-phase2.html.gz"))
    f = d["fields"]
    assert (f["sl_no"], f["work_code"], f["work_person_days"]) == ("220452727", "3311003009/RC/GIS/616097", "228")
    assert (f["expenditure_unskilled"], f["expenditure_material_skilled"]) == ("50388", "254410")
    assert f["work_name"].startswith("RCC Puliya Nirman Kary")
    assert [(p["stage"], p["n"]) for p in d["photos"]] == [("1", 1), ("1", 2), ("2", 1), ("2", 2), ("3", 1), ("3", 2)]
    assert len({p["url"] for p in d["photos"]}) == 6
    k = geotag.photo_key(d["photos"][0]["url"])
    assert k.startswith("photos/bhuvan-fdc1.nrsc.gov.in/object/nrega_fdc/upload/photos/1/2023-06/") and k.endswith(".jpg")


def test_wms_answer_and_time_limit():
    feats = geotag.parse_wms((PAGES / "bhugram-wms.json").read_text())
    assert len(feats) == 3 and {"sid", "asset_id", "block_code", "category_id", "stage"} <= set(feats[0]["properties"])
    with pytest.raises(geotag.TooSlow):
        geotag.parse_wms((PAGES / "bhugram-wms-timeout.xml").read_text())
    with pytest.raises(geotag.PageError):
        geotag.parse_wms("<html>error</html>")


def test_panchayat_dropdown():
    gps = geotag.parse_options((PAGES / "bhugram-gp.html").read_text())
    assert gps[0] == ("3311003001", "Aadnar") and all(c.startswith("3311003") for c, _ in gps)


def test_wms_url_filters_by_block_and_category():
    u = geotag.wms_url("block_code='3311003' and category_id='05'")
    assert "QUERY_LAYERS=nrega%3Anrega_phase2" in u and "INFO_FORMAT=application%2Fjson" in u and "block_code" in u


def test_busy_server_is_told_apart_from_time_limit():
    busy = ("<ServiceExceptionReport><ServiceException code=\"internalError\">Rendering process failed. Layers: nrega:nrega_phase2\n"
            "Unable to obtain connection: Cannot get a connection, pool error Timeout waiting for idle object</ServiceException>")
    with pytest.raises(geotag.Busy):
        geotag.parse_wms(busy)
    assert geotag.category_cql("other") == "(category_id <> '05' or category_id is null)"
    assert geotag.category_cql("05") == "category_id='05'"
