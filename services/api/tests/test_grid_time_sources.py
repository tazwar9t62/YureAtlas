from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from yureatlas.grid import INVERSE, StudyGrid
from yureatlas.providers import USGS, fixed_decimal, parse_csv, parse_jma
from yureatlas.timeutil import jst_day, utc


def test_half_open_jst_midnight():
    start, end = jst_day(date(2024, 1, 2))
    assert start == utc("2024-01-01T15:00:00Z")
    event_at_midnight = end
    assert not start <= event_at_midnight < end
    assert end == jst_day(date(2024, 1, 3))[0]
    with pytest.raises(ValueError):
        utc("2024-01-01T00:00:00")


def test_grid_boundaries_and_offshore():
    grid = StudyGrid()
    x, y = 275 * 50000, 83 * 50000
    lon, lat = INVERSE.transform(x, y)
    assert grid.assign(lon, lat) == "275:83"
    assert grid.assign_projected(x - 0.1, y) == "274:83"
    assert grid.assign(151, 35) is not None
    assert grid.assign(156, 35) is None
    assert len(grid.ids) == len(set(grid.ids))
    assert max(grid.areas) < 2501
    assert min(grid.areas) > 0


def test_jma_official_record_and_depth_slice():
    record = Path(__file__).with_name("fixtures").joinpath("jma_2023_first.txt").read_bytes()
    rows, report = parse_jma(record, "h2023")
    assert report["accepted"] == 1
    assert rows[0]["origin_time"] == utc("2022-12-31T15:08:01.500000Z")
    assert rows[0]["depth_km"] == 50
    assert rows[0]["magnitude"] == 0.3
    assert rows[0]["magnitude_type"] == "v"
    assert rows[0]["latitude"] == pytest.approx(35 + 40.59 / 60)
    assert fixed_decimal("12345", 2) == 123.45


def test_csv_validation_retains_null_depth():
    raw = b"provider_id,origin_time,longitude,latitude,depth_km,magnitude,magnitude_type\na,2024-01-01T00:00:00Z,140,35,,4,J\nb,2024-01-01,200,35,1,4,J\n"
    records, report = parse_csv(raw)
    assert records[0]["depth_km"] is None
    assert report["rejected_count"] == 1


def test_usgs_rejects_missing_magnitude_but_retains_reclassification_evidence():
    good = {
        "id": "a",
        "geometry": {"type": "Point", "coordinates": [140, 35, None]},
        "properties": {
            "type": "earthquake",
            "time": 1700000000000,
            "updated": 1700000000000,
            "status": "reviewed",
            "mag": 4.2,
            "magType": "mb",
        },
    }
    missing = dict(good, id="b", properties=dict(good["properties"], mag=None))
    explosion = dict(good, id="c", properties=dict(good["properties"], type="explosion"))
    with closing(USGS()) as provider:
        records, rejected = provider.parse({"features": [good, missing, explosion]})
    assert len(records) == 2 and len(rejected) == 1
    assert records[0]["magnitude_type"] == "mb"
    assert records[1]["event_type"] == "explosion"
