"""Actual official h2023 lines 5136, 5901 and 10616 (JMA attribution in docs)."""

from pathlib import Path

import pytest
from yureatlas.providers import parse_jma

SAMPLE = Path(__file__).with_name("fixtures").joinpath("jma_2023_non_jma.txt").read_bytes()


def test_official_foreign_agencies_with_blank_jma_codes_are_safe_exclusions():
    records, report = parse_jma(SAMPLE, "foreign_fixture")
    assert report["invalid_count"] == report["selected_count"] == 0
    assert report["policy_excluded_count"] == 3
    assert report["reason_counts"] == {"agency_excluded": 3}
    assert all(record["selection_reason"] for record in records)
    assert records[1]["latitude"] == pytest.approx(-(7 + 3.52 / 60))
    assert records[2]["latitude"] == pytest.approx(-0.7 / 60)  # Signed zero degrees stays south.


@pytest.mark.parametrize("mutation", ["jma_blank_codes", "foreign_unknown_code", "foreign_bad_coordinates"])
def test_foreign_agency_exception_does_not_accept_ambiguous_target_or_malformed_rows(mutation):
    line = bytearray(SAMPLE.splitlines()[1])
    if mutation == "jma_blank_codes":
        line[0] = ord("J")
    elif mutation == "foreign_unknown_code":
        line[60] = ord("?")
    else:
        line[21:24] = b"-??"
    _, report = parse_jma(bytes(line), "fixture")
    assert report["invalid_count"] == 1
    assert report["policy_excluded_count"] == 0
