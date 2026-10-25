"""Retrieval is distinct from parsing and immutable catalogue persistence."""

import asyncio
import csv
import io
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from .artifacts import canonical, put
from .eligibility import JMA_MAGNITUDE_TYPES
from .timeutil import JST, utc


class Provider(Protocol):
    def retrieve(self, start: datetime, end: datetime, bounds: dict, minimum: float): ...
    def parse(self, payload: dict) -> tuple[list[dict], list[dict]]: ...


class RetrievalBudgetExceeded(ValueError):
    """Known-event reconciliation must be resumed in a narrower request."""


def validate_record(record):
    for key in ("longitude", "latitude", "magnitude"):
        if not math.isfinite(record[key]):
            raise ValueError(f"Unusable {key}")
    if not -180 <= record["longitude"] <= 180 or not -90 <= record["latitude"] <= 90:
        raise ValueError("Invalid coordinates")
    if not -3 <= record["magnitude"] <= 10:
        raise ValueError("Unusable magnitude")
    if record["depth_km"] is not None and (
        not math.isfinite(record["depth_km"]) or not -100 <= record["depth_km"] <= 1000
    ):
        raise ValueError("Unusable depth")
    record["origin_time"] = utc(record["origin_time"])
    if record["provider_updated"] is not None:
        record["provider_updated"] = utc(record["provider_updated"])
    if not record["provider_id"] or not record["magnitude_type"]:
        raise ValueError("Missing event ID or magnitude type")
    if not isinstance(record.get("event_type", "earthquake"), str) or not record.get(
        "event_type", "earthquake"
    ):
        raise ValueError("Missing event type")
    return record


class USGS:
    url = "https://earthquake.usgs.gov/fdsnws/event/1/"

    def __init__(self, client=None, *, progress=lambda: None, max_id_requests=100, id_seconds=120):
        # The provider owns injected clients too, so its event loop and transport
        # always share one explicit lifetime. Public retrieval remains synchronous.
        self.runner = asyncio.Runner()
        self.closed = False
        self.client = client or httpx.AsyncClient(
            timeout=45, headers={"User-Agent": "YureAtlas/0.1 (local research)"}
        )
        self.progress = progress
        self.requests = []
        self.max_id_requests = max_id_requests
        self.id_seconds = id_seconds
        self.id_requests = 0
        self.id_elapsed = 0.0

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.runner.run(self.client.aclose())
            finally:
                self.runner.close()

    async def receive(self, url, params, timeout, evidence):
        """Capture partial wire bytes as well as completed responses on interruption."""
        chunks = []
        try:
            async with self.client.stream("GET", url, params=params, timeout=timeout) as response:
                evidence.update(status_code=response.status_code, response_headers=dict(response.headers))
                if response.is_stream_consumed:
                    # In-memory test transports may return an already decoded body.
                    chunks.append(response.content)
                    result = response
                else:
                    async for chunk in response.aiter_raw():
                        chunks.append(chunk)
                        self.progress()
                    result = httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=b"".join(chunks),
                        request=response.request,
                    )
                evidence["response_complete"] = True
                return result
        finally:
            if "status_code" in evidence:
                primary_error = sys.exception()
                evidence.update(
                    received_at=datetime.now(UTC).isoformat(),
                    response_complete=evidence.get("response_complete", False),
                )
                try:
                    evidence["raw_ref"] = put(b"".join(chunks), "json")
                except Exception as artifact_error:
                    evidence["artifact_error"] = type(artifact_error).__name__
                    if primary_error is None:
                        raise
                    primary_error.add_note(
                        f"Could not archive partial USGS response: {type(artifact_error).__name__}"
                    )

    async def bounded_receive(self, url, params, timeout, remaining, evidence):
        # This encloses header reception and body reads. HTTPX inactivity
        # timeouts alone do not bound a server that keeps trickling bytes.
        return await asyncio.wait_for(self.receive(url, params, timeout, evidence), timeout=remaining)

    def request(self, url, params=None, *, detail=False):
        started = time.monotonic()
        deadline = started + self.id_seconds - self.id_elapsed if detail else None
        try:
            for attempt in range(4):
                self.progress()  # Cancellation is checked between requests and bounded retries.
                delay = 0.25 + (2 ** (attempt - 1) if attempt else 0)
                if detail and (
                    self.id_requests >= self.max_id_requests or time.monotonic() + delay >= deadline
                ):
                    raise RetrievalBudgetExceeded(
                        "Known-ID request/time budget exhausted; narrow the interval"
                    )
                time.sleep(delay)
                self.progress()
                evidence = {
                    "url": url,
                    "params": params or {},
                    "request_ref": put(canonical({"url": url, "params": params or {}}), "json"),
                    "requested_at": datetime.now(UTC).isoformat(),
                    "kind": "known_id" if detail else "discovery",
                    "attempt": attempt + 1,
                }
                try:
                    remaining = deadline - time.monotonic() if detail else 45
                    if remaining <= 0:
                        raise RetrievalBudgetExceeded("Known-ID time budget exhausted; narrow the interval")
                    if detail:
                        self.id_requests += 1
                    try:
                        response = self.runner.run(
                            self.bounded_receive(
                                url, params, min(15, remaining) if detail else 45, remaining, evidence
                            )
                        )
                    except TimeoutError as exc:
                        if detail:
                            raise RetrievalBudgetExceeded(
                                "Known-ID response exceeded the time budget"
                            ) from exc
                        raise httpx.ReadTimeout("Discovery response exceeded its total 45s deadline") from exc
                    self.progress()
                    response.raise_for_status()
                    if detail and time.monotonic() >= deadline:
                        raise RetrievalBudgetExceeded("Known-ID response exceeded the time budget")
                    return response
                except httpx.HTTPError as exc:
                    evidence["error"] = type(exc).__name__
                    retryable = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or (
                        isinstance(exc, httpx.HTTPStatusError)
                        and (exc.response.status_code >= 500 or exc.response.status_code == 429)
                    )
                    if not retryable or attempt == 3:
                        raise
                except Exception as exc:
                    evidence["error"] = type(exc).__name__
                    raise
                finally:
                    self.requests.append(evidence)

        finally:
            if detail:
                self.id_elapsed += time.monotonic() - started

    def budget_report(self):
        return {
            "max_id_requests": self.max_id_requests,
            "id_requests": self.id_requests,
            "id_time_budget_seconds": self.id_seconds,
            "id_elapsed_seconds": round(self.id_elapsed, 3),
        }

    @staticmethod
    def collection(payload, *, expected_count=None):
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "FeatureCollection"
            or not isinstance(payload.get("features"), list)
            or not isinstance(payload.get("metadata"), dict)
        ):
            raise ValueError("Expected a USGS FeatureCollection with explicit metadata")
        # FDSN query responses can omit metadata.count (observed API 2.8.2).
        # They are accepted only against the independently verified preflight
        # count. Summary feeds have no such preflight and must provide a count.
        count = payload["metadata"].get("count", expected_count)
        if isinstance(count, bool) or not isinstance(count, int) or count != len(payload["features"]):
            raise ValueError("USGS response count does not match its feature array")
        if expected_count is not None and count != expected_count:
            raise ValueError("USGS response differs from its preflight count")
        return payload

    def retrieve(self, start, end, bounds, minimum):
        first_request = len(self.requests)
        params = {
            "starttime": start.isoformat(),
            "endtime": end.isoformat(),
            "eventtype": "earthquake",
            "minlatitude": bounds["south"],
            "maxlatitude": bounds["north"],
            "minlongitude": bounds["west"],
            "maxlongitude": bounds["east"],
            "minmagnitude": minimum,
            "includedeleted": "true",
        }
        count_payload = self.request(self.url + "count", params=dict(params, format="geojson")).json()
        count = count_payload.get("count") if isinstance(count_payload, dict) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("USGS count response lacks a valid nonnegative count")
        if count >= 19000:
            if end - start < timedelta(seconds=1):
                raise ValueError("Catalogue interval cannot be safely subdivided")
            middle = start + (end - start) / 2
            yield from self.retrieve(start, middle, bounds, minimum)
            yield from self.retrieve(middle, end, bounds, minimum)
            return
        response = self.request(
            self.url + "query", params=dict(params, format="geojson", limit=20000, orderby="time-asc")
        )
        payload = (
            {"type": "FeatureCollection", "features": [], "metadata": {"count": 0}}
            if response.status_code == 204
            else self.collection(response.json(), expected_count=count)
        )
        response_evidence = self.requests[-1]
        actual = len(payload["features"])
        # A changing catalogue between count/query is retried by the bounded job.
        if actual != count or actual != payload.get("metadata", {}).get("count", actual):
            raise ValueError(
                f"Catalogue changed or truncated: count={count}, received={actual}; retry interval"
            )
        records, rejected = self.parse(payload)
        records = [r for r in records if start <= r["origin_time"] < end]
        yield (
            start,
            end,
            records,
            {
                "query": params,
                "raw_ref": response_evidence["raw_ref"],
                "received_at": response_evidence["received_at"],
                "requests": self.requests[first_request:],
                "reported_count": count,
                "rejected": rejected[:100],
                "rejected_count": len(rejected),
                "coverage_policy": "complete query, not a claim of scientific completeness",
            },
        )

    def feed(self):
        first_request = len(self.requests)
        response = self.request("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson")
        records, rejected = self.parse(self.collection(response.json()))
        return records, {
            "feed": True,
            "raw_ref": self.requests[-1]["raw_ref"],
            "received_at": self.requests[-1]["received_at"],
            "rejected": rejected[:100],
            "rejected_count": len(rejected),
            "requests": self.requests[first_request:],
        }

    def retrieve_id(self, provider_id):
        first_request = len(self.requests)
        params = {"eventid": provider_id, "format": "geojson", "includedeleted": "true"}
        response = self.request(self.url + "query", params=params, detail=True)
        if response.status_code == 204:
            raise ValueError("Known event missing from ID response; absence is not deletion")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "Feature"
            or payload.get("id") != provider_id
        ):
            raise ValueError("Known-ID response must be a single Feature with the exact requested identity")
        records, rejected = self.parse(payload)
        if rejected or len(records) != 1:
            raise ValueError("Known-ID response has unusable event fields; correction remains unresolved")
        return records[0], {
            "query": params,
            "raw_ref": self.requests[-1]["raw_ref"],
            "received_at": self.requests[-1]["received_at"],
            "requests": self.requests[first_request:],
        }

    def parse(self, payload):
        accepted, rejected = [], []
        if not isinstance(payload, dict):
            raise TypeError("USGS payload must be an object")
        features = [payload] if payload.get("type") == "Feature" else payload.get("features")
        if not isinstance(features, list):
            raise TypeError("USGS payload lacks a feature array or single Feature")
        for feature in features:
            try:
                if (
                    not isinstance(feature, dict)
                    or not isinstance(feature.get("id"), str)
                    or not feature["id"].strip()
                ):
                    raise ValueError("Missing event identity")
                properties = feature["properties"]
                geometry = feature["geometry"]
                if (
                    not isinstance(properties, dict)
                    or not isinstance(geometry, dict)
                    or geometry.get("type") != "Point"
                ):
                    raise ValueError("USGS event requires properties and Point geometry")
                coordinates = geometry.get("coordinates")
                if not isinstance(coordinates, list) or not 2 <= len(coordinates) <= 3:
                    raise ValueError("USGS Point requires two or three coordinates")
                if properties.get("status") not in {"automatic", "reviewed", "deleted"}:
                    raise ValueError("USGS event lacks a recognized explicit status")
                for key in ("time", "updated"):
                    if (
                        isinstance(properties.get(key), bool)
                        or not isinstance(properties.get(key), (int, float))
                        or not math.isfinite(properties[key])
                    ):
                        raise ValueError(f"USGS event lacks a usable {key} timestamp")
                numeric_fields = [("mag", properties.get("mag")), *enumerate(coordinates[:2])]
                if len(coordinates) > 2 and coordinates[2] is not None:
                    numeric_fields.append(("depth", coordinates[2]))
                for name, value in numeric_fields:
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                    ):
                        raise ValueError(f"USGS event lacks a usable numeric {name}")
                record = {
                    "provider_id": feature["id"],
                    "origin_time": datetime.fromtimestamp(properties["time"] / 1000, UTC),
                    "provider_updated": datetime.fromtimestamp(properties["updated"] / 1000, UTC),
                    "longitude": float(coordinates[0]),
                    "latitude": float(coordinates[1]),
                    "depth_km": float(coordinates[2])
                    if len(coordinates) > 2 and coordinates[2] is not None
                    else None,
                    "magnitude": float(properties["mag"]),
                    "magnitude_type": properties.get("magType") or "unknown_preferred",
                    "status": properties["status"],
                    "event_type": properties.get("type"),
                    "raw": feature,
                }
                accepted.append(validate_record(record))
            except (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError) as exc:
                rejected.append(
                    {"id": feature.get("id") if isinstance(feature, dict) else None, "reason": str(exc)}
                )
        return accepted, rejected


def fixed_decimal(value: str, places: int) -> float:
    # FORTRAN fixed-point fields imply decimal places; blank fractional columns
    # denote an integer depth/second in the official depth-slice/fixed formats.
    if not value.strip():
        raise ValueError("Missing fixed-width value")
    if "." in value:
        return float(value)
    return float(value[:-places]) if value[-places:].isspace() else int(value) / (10**places)


def jma_magnitude(value: str) -> float:
    if value[0] in "ABC":
        return -(ord(value[0]) - ord("A") + 1) - int(value[1]) / 10
    return int(value) / 10


IMPORT_PARSER_VERSION = 2
CSV_REQUIRED_FIELDS = frozenset(
    {"provider_id", "origin_time", "longitude", "latitude", "magnitude", "magnitude_type"}
)
CSV_OPTIONAL_FIELDS = frozenset({"provider_updated", "depth_km", "status", "event_type"})
JMA_OTHER_MAGNITUDE_TYPES = frozenset({"W", "B", "S"})
# CSV is a canonical import format: other recognized non-earthquakes must
# explicitly map to non_earthquake instead of being inferred from free text.
CSV_EVENT_TYPES = frozenset({"earthquake", "explosion", "non_earthquake"})


def import_report():
    return {
        "selected_count": 0,
        "policy_excluded_count": 0,
        "invalid_count": 0,
        "selected": [],
        "policy_excluded": [],
        "invalid": [],
        "reason_counts": {},
    }


def report_import_row(report, outcome, number, code, reason, *, end_line=None):
    report[outcome + "_count"] += 1
    report["reason_counts"][code] = report["reason_counts"].get(code, 0) + 1
    if len(report[outcome]) < 100:
        report[outcome].append(
            {"line": number, "end_line": end_line or number, "reason_code": code, "reason": reason}
        )


def finish_import_report(report, records):
    report.update(
        accepted=report["selected_count"],
        retained_corrections=sum(r["_import_outcome"] != "selected" for r in records),
        rejected_count=report["invalid_count"],
        rejected=report["invalid"],
    )
    return records, report


def parse_jma(data: bytes, namespace: str):
    """Parse documented 96-byte records; unknown data never proves zero events.

    Physical line identity is immutable within a fixed-file namespace. Recognized,
    structurally valid policy exclusions still provide known-event corrections.
    """
    records, report = [], import_report()
    lines = data.splitlines()
    if not lines:
        raise ValueError("JMA input is empty")
    for number, raw in enumerate(lines, 1):
        code = "invalid_record_width"
        try:
            if len(raw) != 96:
                raise ValueError("Expected a 96-byte JMA hypocentre record")
            code = "invalid_encoding"
            line = raw.decode("ascii")
            issues = []
            if line[0] not in {"J", "U", "I"}:
                issues.append(("unknown_agency", "Unknown JMA agency code"))
            # Official U/I rows may leave JMA precision/subsidiary fields blank.
            # Their explicit foreign agency already proves policy exclusion;
            # core time, coordinates and magnitude still must parse correctly.
            foreign_agency = line[0] in {"U", "I"}
            if line[60] not in {"1", "2", "3", "4", "5"} and not (foreign_agency and line[60] == " "):
                issues.append(("unknown_event_code", "Unknown JMA subsidiary event code"))
            if line[59] not in {"1", "2", "3", "4", "5", "7", "8", "9", "M"} and not (
                foreign_agency and line[59] == " "
            ):
                issues.append(("unknown_quality_code", "Unknown JMA hypocentre precision code"))
            elif line[59] == "8":
                issues.append(("unaccepted_hypocentre", "Unaccepted hypocentre"))
            magnitudes = []
            for offset in (52, 55):
                value, family = line[offset : offset + 2], line[offset + 2]
                if not value.strip() and not family.strip():
                    continue
                if family not in JMA_MAGNITUDE_TYPES | JMA_OTHER_MAGNITUDE_TYPES:
                    issues.append(("unknown_magnitude_code", "Unknown or missing magnitude family"))
                code = "invalid_magnitude"
                magnitudes.append((jma_magnitude(value), family))
            chosen = next((m for m in magnitudes if m[1] in JMA_MAGNITUDE_TYPES), None)
            chosen = chosen or next(iter(magnitudes), None)
            if chosen is None:
                code = "missing_magnitude"
                raise ValueError("No usable magnitude; undetermined magnitude cannot establish absence")
            code = "invalid_origin_time"
            seconds = fixed_decimal(line[13:17], 2)
            if not 0 <= seconds < 60:
                raise ValueError("Origin seconds must be in [0,60); overflow is not normalized")
            origin = datetime.strptime(line[1:13], "%Y%m%d%H%M").replace(tzinfo=JST) + timedelta(
                seconds=seconds
            )
            code = "invalid_coordinates"
            coordinates = []
            for degrees, minutes in ((line[21:24], line[24:28]), (line[32:36], line[36:40])):
                minute = fixed_decimal(minutes, 2)
                if not 0 <= minute < 60:
                    raise ValueError("Coordinate minutes must be in [0,60); overflow is not normalized")
                degree_text = degrees.strip()
                if degree_text[:1] in {"-", "+"}:
                    # The official file uses padded signed fields such as '- 7'
                    # and '- 0'. Preserve the sign even for zero whole degrees.
                    degree_text = degree_text[0] + degree_text[1:].lstrip()
                degree = int(degree_text)
                coordinates.append(
                    (-1 if degrees.strip().startswith("-") else 1) * (abs(degree) + minute / 60)
                )
            code = "invalid_depth"
            depth = fixed_decimal(line[44:49], 2) if line[44:49].strip() else None
            if issues:
                outcome, (reason_code, reason) = "invalid", issues[0]
            elif line[0] != "J":
                outcome, reason_code, reason = (
                    "policy_excluded",
                    "agency_excluded",
                    "Not a JMA natural earthquake",
                )
            elif line[60] not in {"1", "2", "5"}:
                outcome, reason_code, reason = (
                    "policy_excluded",
                    "event_type_excluded",
                    "Not a JMA natural earthquake",
                )
            elif chosen[1] not in JMA_MAGNITUDE_TYPES:
                outcome, reason_code, reason = (
                    "policy_excluded",
                    "magnitude_family_excluded",
                    "No usable JMA magnitude family; Mw/mb/Ms excluded",
                )
            else:
                outcome, reason_code, reason = "selected", "selected", "Usable JMA earthquake"
            code = "invalid_numeric_range"
            record = validate_record(
                {
                    "provider_id": f"{namespace}:{number}",
                    "origin_time": origin,
                    "provider_updated": None,
                    "latitude": coordinates[0],
                    "longitude": coordinates[1],
                    "depth_km": depth,
                    "magnitude": chosen[0],
                    "magnitude_type": chosen[1],
                    "status": "reviewed",
                    "event_type": "earthquake" if line[60] in {"1", "2", "5"} else "non_earthquake",
                    "selection_reason": None if outcome == "selected" else reason,
                    "raw": {"line": line, "namespace": namespace, "line_number": number},
                    "_import_outcome": outcome,
                    "_import_line": number,
                }
            )
            records.append(record)
            report_import_row(report, outcome, number, reason_code, reason)
        except (ValueError, UnicodeDecodeError, IndexError, OverflowError) as exc:
            report_import_row(report, "invalid", number, code, str(exc))
    report.update(
        parser_version=f"jma-v{IMPORT_PARSER_VERSION}",
        line_count=len(lines),
        identity_policy=(
            "immutable file namespace + physical line number; changed or reordered files require a new "
            "catalogue collection; file namespaces within one collection must have disjoint intervals"
        ),
    )
    return finish_import_report(report, records)


def validate_csv_mapping(mapping):
    if not isinstance(mapping, dict) or any(
        not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip()
        for source, target in mapping.items()
    ):
        raise ValueError("CSV mapping must map nonblank source header names to canonical fields")
    normalized = {source.strip(): target.strip() for source, target in mapping.items()}
    if len(normalized) != len(mapping):
        raise ValueError("CSV mapping has duplicate normalized source fields")
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("CSV mapping causes destination collisions")
    if set(normalized.values()) - (CSV_REQUIRED_FIELDS | CSV_OPTIONAL_FIELDS):
        raise ValueError("CSV mapping has unknown canonical destination fields")
    return normalized


def csv_headers(reader, mapping):
    try:
        headers = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise ValueError("CSV requires a valid nonempty header row") from exc
    headers = [name.strip() for name in headers]
    if not headers or any(not name for name in headers) or len(headers) != len(set(headers)):
        raise ValueError("CSV headers must be nonblank and unique")
    mapping = validate_csv_mapping(mapping)
    if set(mapping) - set(headers):
        raise ValueError("CSV mapping references unknown source headers")
    destinations = [mapping.get(name, name) for name in headers]
    if len(destinations) != len(set(destinations)):
        raise ValueError("CSV mapping causes destination collisions")
    if CSV_REQUIRED_FIELDS - set(destinations):
        raise ValueError(
            "CSV lacks required canonical fields: "
            + ", ".join(sorted(CSV_REQUIRED_FIELDS - set(destinations)))
        )
    return headers, destinations, mapping


def validate_csv_quoting(text):
    # csv.reader(strict=True) still accepts a quote inside an unquoted field.
    # Reject that ambiguous syntax instead of silently changing an identity.
    state = "start"
    for character in text:
        if state == "quoted":
            if character == '"':
                state = "closed"
        elif state == "closed":
            if character == '"':
                state = "quoted"
            elif character == "," or character in "\r\n":
                state = "start"
            else:
                raise ValueError("Unexpected characters after a closing CSV quote")
        elif character == '"':
            if state != "start":
                raise ValueError("Quote inside an unquoted CSV field")
            state = "quoted"
        elif character == "," or character in "\r\n":
            state = "start"
        else:
            state = "unquoted"
    if state == "quoted":
        raise ValueError("Unclosed CSV quote")


def parse_csv(data: bytes, mapping: dict | None = None):
    """Strict canonical CSV, preserving physical line locations and explicit IDs."""
    records, report = [], import_report()
    text = data.decode("utf-8-sig")
    physical_lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    headers, destinations, mapping = csv_headers(reader, {} if mapping is None else mapping)
    validate_csv_quoting("".join(physical_lines[: reader.line_num]))
    while True:
        number, code = reader.line_num + 1, "invalid_csv_quoting"
        try:
            values = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            report_import_row(report, "invalid", number, code, str(exc), end_line=reader.line_num)
            break  # The remaining quoted record has no trustworthy row boundary.
        try:
            validate_csv_quoting("".join(physical_lines[number - 1 : reader.line_num]))
            code = "invalid_row_width"
            if len(values) != len(headers):
                raise ValueError(f"Expected {len(headers)} fields; found {len(values)}")
            raw = dict(zip(headers, values, strict=True))
            row = dict(zip(destinations, values, strict=True))
            code = "invalid_identity"
            identity = row["provider_id"]
            if not identity or identity != identity.strip():
                raise ValueError("provider_id must be an explicit nonblank ID without surrounding whitespace")
            code = "invalid_status"
            status = row.get("status") or "reviewed"
            if status not in {"reviewed", "automatic", "deleted"}:
                raise ValueError("Unknown review status")
            code = "invalid_origin_time"
            origin = utc(row["origin_time"])
            code = "invalid_provider_updated"
            updated = utc(row["provider_updated"]) if row.get("provider_updated") else None
            code = "invalid_numeric_fields"
            record = validate_record(
                {
                    "provider_id": identity,
                    "origin_time": origin,
                    "provider_updated": updated,
                    "longitude": float(row["longitude"]),
                    "latitude": float(row["latitude"]),
                    "depth_km": float(row["depth_km"]) if row.get("depth_km") else None,
                    "magnitude": float(row["magnitude"]),
                    "magnitude_type": row["magnitude_type"],
                    "status": status,
                    "event_type": row.get("event_type") or "earthquake",
                    "raw": raw,
                    "_import_line": number,
                    "_import_end_line": reader.line_num,
                }
            )
            if record["event_type"] not in CSV_EVENT_TYPES:
                outcome, reason_code, reason = (
                    "invalid",
                    "unknown_event_type",
                    "Unrecognized event_type; use earthquake, explosion, or non_earthquake",
                )
                # Retain explicit identity and usable fields for known corrections,
                # while neither selecting this row nor asserting complete coverage.
                record["selection_reason"] = reason
            else:
                outcome, reason_code, reason = "selected", "selected", "Usable canonical CSV row"
            record["_import_outcome"] = outcome
            records.append(record)
            report_import_row(report, outcome, number, reason_code, reason, end_line=reader.line_num)
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            report_import_row(report, "invalid", number, code, str(exc), end_line=reader.line_num)
    report.update(
        parser_version=f"csv-v{IMPORT_PARSER_VERSION}",
        line_count=len(data.splitlines()),
        data_row_count=sum(report[key + "_count"] for key in ("selected", "policy_excluded", "invalid")),
        mapping=mapping,
        units="decimal degrees, depth km, timezone-aware ISO8601",
        event_types=sorted(CSV_EVENT_TYPES),
    )
    return finish_import_report(report, records)
