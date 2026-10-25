"""Import policy distinguishes known exclusions from ambiguous catalogue gaps."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from yureatlas import tasks
from yureatlas.grid import StudyGrid
from yureatlas.providers import parse_csv, parse_jma, validate_csv_mapping
from yureatlas.timeutil import utc

HEADER = "provider_id,origin_time,longitude,latitude,magnitude,magnitude_type"
ROW = "a,2020-01-01T00:00:00Z,140.5,35.5,4.2,J"
START, END = utc("2020-01-01T00:00:00Z"), utc("2020-01-02T00:00:00Z")


def jma_line(**changes):
    raw = list(Path(__file__).with_name("fixtures").joinpath("jma_2023_first.txt").read_text().strip())
    slices = {
        "agency": (0, 1),
        "seconds": (13, 17),
        "latitude_minutes": (24, 28),
        "longitude_minutes": (36, 40),
        "depth": (44, 49),
        "magnitude": (52, 54),
        "family": (54, 55),
        "quality": (59, 60),
        "event": (60, 61),
    }
    for name, value in changes.items():
        start, end = slices[name]
        assert len(value) == end - start
        raw[start:end] = value
    return "".join(raw).encode()


@pytest.mark.parametrize(
    "change",
    [
        {"agency": "U"},
        {"agency": "I"},
        {"event": "3"},
        {"event": "4"},
        {"family": "W"},
        {"family": "B"},
        {"family": "S"},
    ],
)
def test_recognized_structurally_valid_jma_exclusions_do_not_block_coverage(change):
    rows, report = parse_jma(jma_line(**change), "h2023")
    assert len(rows) == report["retained_corrections"] == 1
    assert rows[0]["selection_reason"]
    assert report["selected_count"] == report["invalid_count"] == report["rejected_count"] == 0
    assert report["policy_excluded_count"] == 1
    assert report["policy_excluded"][0]["line"] == 1
    assert sum(report["reason_counts"].values()) == 1


@pytest.mark.parametrize(
    "change, code",
    [
        ({"agency": "X"}, "unknown_agency"),
        ({"event": "9"}, "unknown_event_code"),
        ({"family": "M"}, "unknown_magnitude_code"),
        ({"quality": "6"}, "unknown_quality_code"),
        ({"quality": "8"}, "unaccepted_hypocentre"),
        ({"seconds": "6000"}, "invalid_origin_time"),
        ({"seconds": "-100"}, "invalid_origin_time"),
        ({"latitude_minutes": "6000"}, "invalid_coordinates"),
        ({"longitude_minutes": "6000"}, "invalid_coordinates"),
        ({"magnitude": "XX"}, "invalid_magnitude"),
        ({"agency": "U", "magnitude": "XX"}, "invalid_magnitude"),
    ],
)
def test_unknown_or_malformed_jma_rows_remain_coverage_blockers(change, code):
    _, report = parse_jma(jma_line(**change), "h2023")
    assert report["invalid_count"] == report["rejected_count"] == 1
    assert report["policy_excluded_count"] == report["selected_count"] == 0
    assert report["invalid"][0]["reason_code"] == code


def test_jma_missing_depth_and_fixed_integer_seconds_are_valid():
    rows, report = parse_jma(jma_line(depth="     ", seconds="01  "), "h2023")
    assert report["selected_count"] == 1
    assert rows[0]["depth_km"] is None
    assert rows[0]["origin_time"].second == 1


def test_jma_invalid_lines_keep_original_physical_identity_and_bounded_examples():
    rows, report = parse_jma(b"bad\n" * 101 + jma_line() + b"\n", "h2023")
    assert report["line_count"] == 102
    assert rows[0]["provider_id"] == "h2023:102"
    assert report["invalid_count"] == 101
    assert len(report["invalid"]) == 100
    assert report["reason_counts"]["invalid_record_width"] == 101
    with pytest.raises(ValueError, match="empty"):
        parse_jma(b"", "h2023")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\n",
        ROW.encode(),
        (HEADER + ",provider_id\n").encode(),
        (HEADER + ",\n").encode(),
        HEADER.replace("magnitude_type", "other").encode(),
    ],
)
def test_missing_duplicate_blank_or_headerless_csv_is_structurally_rejected(data):
    with pytest.raises(ValueError):
        parse_csv(data)


@pytest.mark.parametrize(
    "mapping",
    [
        [],
        {"": "provider_id"},
        {"id": 1},
        {"id": "unsupported"},
        {"a": "provider_id", "b": "provider_id"},
        {"id": "provider_id", " id ": "magnitude"},
    ],
)
def test_mapping_validation_can_run_before_queueing(mapping):
    with pytest.raises(ValueError):
        validate_csv_mapping(mapping)


def test_csv_mapping_rejects_unknown_sources_and_collisions_with_unmapped_headers():
    with pytest.raises(ValueError, match="unknown source"):
        parse_csv((HEADER + "\n" + ROW).encode(), {"id": "provider_id"})
    with pytest.raises(ValueError, match="collision"):
        parse_csv((HEADER + ",id\n" + ROW + ",alias").encode(), {"id": "provider_id"})


def test_csv_valid_mapping_and_header_only_report():
    header = HEADER.replace("provider_id", "event_id")
    rows, report = parse_csv((header + "\n" + ROW).encode(), {"event_id": "provider_id"})
    assert rows[0]["provider_id"] == "a"
    assert report["mapping"] == {"event_id": "provider_id"}
    assert report["selected_count"] == 1
    rows, report = parse_csv((HEADER + "\n").encode())
    assert rows == []
    assert report["data_row_count"] == report["rejected_count"] == 0
    assert report["line_count"] == 1


@pytest.mark.parametrize(
    "row, code",
    [
        (ROW + ",extra", "invalid_row_width"),
        (ROW.rsplit(",", 1)[0], "invalid_row_width"),
        (ROW.replace("a,", ",", 1), "invalid_identity"),
        (ROW.replace("a,", " a,", 1), "invalid_identity"),
        (ROW.replace("00:00:00Z", "00:00:00"), "invalid_origin_time"),
        (ROW.replace("4.2", "NaN"), "invalid_numeric_fields"),
        ('"unclosed,' + ROW, "invalid_csv_quoting"),
        (ROW.replace("a,", 'a"b,', 1), "invalid_csv_quoting"),
    ],
)
def test_csv_invalid_rows_are_reported_without_becoming_empty_complete_data(row, code):
    rows, report = parse_csv((HEADER + "\n" + row).encode())
    assert rows == []
    assert report["rejected_count"] == 1
    assert report["invalid"][0]["reason_code"] == code
    assert report["invalid"][0]["line"] == 2


def test_csv_multiline_fields_report_physical_start_and_end_lines():
    rows, report = parse_csv((HEADER + ",notes\n" + ROW + ',"first\nsecond"\nbad\n').encode())
    assert len(rows) == 1
    assert report["selected"][0]["line"] == 2
    assert report["selected"][0]["end_line"] == 3
    assert report["invalid"][0]["line"] == 4
    assert report["line_count"] == 4


@pytest.fixture
def import_task(monkeypatch):
    from yureatlas import artifacts

    state = SimpleNamespace(raw=(HEADER + "\n" + ROW).encode(), captures=[], discovery=[])
    config = {"provider": "jma", "catalogue_namespace": "official-test", "input_magnitude": 3}
    grid = StudyGrid(west=140, east=142, south=35, north=37)
    monkeypatch.setattr(
        tasks, "load_experiment", lambda _: (SimpleNamespace(source_id="source", config=config), grid)
    )
    monkeypatch.setattr(artifacts, "read", lambda _: state.raw)

    def persist(source, rows, start, end, report, **kwargs):
        state.discovery.append((rows, kwargs["discovery_context"]["discovery_interval"]))
        state.captures.append(dict(source=source, rows=rows, start=start, end=end, report=report, **kwargs))
        return dict(report, status=kwargs["status"])

    monkeypatch.setattr(tasks, "ingest_records", persist)
    state.payload = {
        "experiment_id": "experiment",
        "format": "csv",
        "artifact": "artifact",
        "namespace": "upload",
        "start": START.isoformat(),
        "end": END.isoformat(),
    }
    return state


@pytest.mark.parametrize("declared", [False, True])
def test_import_requires_explicit_coverage_declaration_and_passes_atomic_identity(import_task, declared):
    state = import_task
    result = tasks.execute("import", dict(state.payload, coverage_declared=declared), lambda _: None)
    assert result["status"] == ("complete" if declared else "partial")
    saved = state.captures[0]
    assert saved["import_identity"] == {
        "namespace": "upload",
        "format": "csv",
        "artifact": "artifact",
        "line_count": 2,
        "mapping": {},
        "parser_version": "csv-v2",
        "coverage_declared": declared,
        "catalogue_namespace": "official-test",
    }
    assert state.discovery[0][1] == (START, END)


def test_import_reports_interval_domain_and_magnitude_exclusions_separately(import_task):
    state = import_task
    state.raw = (
        HEADER
        + "\n"
        + "\n".join(
            [
                ROW,
                ROW.replace("a,", "below,", 1).replace("4.2", "2.8"),
                ROW.replace("a,", "outside,", 1).replace("140.5", "150"),
                ROW.replace("a,", "end,", 1).replace("2020-01-01", "2020-01-02"),
            ]
        )
    ).encode()
    result = tasks.execute("import", dict(state.payload, coverage_declared=True), lambda _: None)
    assert result["status"] == "complete"
    assert result["selected_count"] == 1
    assert result["policy_excluded_count"] == 3
    assert result["selection_exclusions"] == {
        "below_input_magnitude": 1,
        "outside_domain": 1,
        "outside_declared_interval": 1,
    }
    assert result["rejected_count"] == 0


def test_partial_csv_can_retain_usable_rows_but_never_claim_complete_coverage(import_task):
    state = import_task
    state.raw += b"\nmalformed\n"
    result = tasks.execute("import", dict(state.payload, coverage_declared=True), lambda _: None)
    assert result["status"] == "partial"
    assert result["invalid_count"] == result["selected_count"] == 1
    assert len(state.captures[0]["rows"]) == 1


def test_header_only_import_requires_declaration_even_with_no_invalid_rows(import_task):
    state = import_task
    state.raw = (HEADER + "\n").encode()
    assert tasks.execute("import", state.payload, lambda _: None)["status"] == "partial"
    assert (
        tasks.execute("import", dict(state.payload, coverage_declared=True), lambda _: None)["status"]
        == "complete"
    )


def test_queued_parser_version_mismatch_rejected_before_reading_or_persisting(import_task):
    with pytest.raises(ValueError, match="parser version"):
        tasks.execute("import", dict(import_task.payload, parser_version="csv-v1"), lambda _: None)
    assert import_task.captures == []


def test_unknown_csv_magnitude_family_blocks_declared_coverage(import_task):
    import_task.raw = (HEADER + "\n" + ROW.replace(",J", ",typo")).encode()
    result = tasks.execute("import", dict(import_task.payload, coverage_declared=True), lambda _: None)
    assert result["status"] == "partial"
    assert result["invalid_count"] == 1
    assert result["reason_counts"]["unknown_magnitude_code"] == 1


@pytest.mark.parametrize("change, expected", [({"agency": "U"}, "complete"), ({"quality": "8"}, "partial")])
def test_jma_import_coverage_uses_ambiguity_not_recognized_exclusion(import_task, change, expected):
    state = import_task
    state.raw = jma_line(**change)
    result = tasks.execute(
        "import", dict(state.payload, format="jma", coverage_declared=True), lambda _: None
    )
    assert result["status"] == expected
    assert result["rejected_count"] == int(expected == "partial")
    assert state.captures[0]["import_identity"]["parser_version"] == "jma-v2"
    assert len(state.captures[0]["rows"]) == 1  # Valid known-correction candidates survive classification.


@pytest.mark.parametrize("event_type", ["earthquke", "unknown", "Earthquake", " "])
def test_unknown_csv_event_types_remain_correction_evidence_and_block_coverage(import_task, event_type):
    state = import_task
    state.raw = (HEADER + ",event_type\n" + ROW + "," + event_type).encode()
    rows, report = parse_csv(state.raw)
    assert report["invalid_count"] == report["rejected_count"] == 1
    assert report["reason_counts"]["unknown_event_type"] == 1
    assert report["retained_corrections"] == 1
    assert rows[0]["provider_id"] == "a"
    assert rows[0]["event_type"] == event_type
    assert rows[0]["selection_reason"]
    result = tasks.execute("import", dict(state.payload, coverage_declared=True), lambda _: None)
    assert result["status"] == "partial"
    assert result["invalid_count"] == 1
    assert result["policy_excluded_count"] == 0
    assert state.captures[0]["rows"][0]["event_type"] == event_type


@pytest.mark.parametrize("event_type", ["explosion", "non_earthquake"])
def test_recognized_csv_non_earthquakes_are_safe_policy_exclusions(import_task, event_type):
    state = import_task
    state.raw = (HEADER + ",event_type\n" + ROW + "," + event_type).encode()
    result = tasks.execute("import", dict(state.payload, coverage_declared=True), lambda _: None)
    assert result["status"] == "complete"
    assert result["policy_excluded_count"] == 1
    assert result["invalid_count"] == 0
    assert result["event_types"] == ["earthquake", "explosion", "non_earthquake"]
