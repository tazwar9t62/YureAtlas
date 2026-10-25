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
    def collection(payload):
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "FeatureCollection"
            or not isinstance(payload.get("features"), list)
            or not isinstance(payload.get("metadata"), dict)
        ):
            raise ValueError("Expected a USGS FeatureCollection with explicit metadata")
        count = payload["metadata"].get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(payload["features"]):
            raise ValueError("USGS response count does not match its feature array")
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
            else self.collection(response.json())
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


def parse_jma(data: bytes, namespace: str):
    """Official 96-byte hypocentre format. Namespace identifies a stable source file.

    JMA has no event ID in this format; stable namespace+line is explicit. A revised
    file with reordered rows must be imported as a separate catalogue namespace.
    """
    accepted, rejected = [], []
    for number, raw in enumerate(data.splitlines(), 1):
        try:
            if len(raw) != 96:
                raise ValueError("Expected a 96-byte JMA hypocentre record")
            line = raw.decode("ascii")
            selection_reason = (
                "Not a JMA natural earthquake" if line[0] != "J" or line[60] not in {"1", "2", "5"} else None
            )
            if line[59] == "8":
                selection_reason = selection_reason or "Unaccepted hypocentre"
            chosen = next(
                (
                    (line[a : a + 2], line[a + 2])
                    for a in (52, 55)
                    if line[a + 2] in JMA_MAGNITUDE_TYPES and line[a : a + 2].strip()
                ),
                None,
            )
            if not chosen:
                selection_reason = selection_reason or "No usable JMA magnitude family; Mw/mb/Ms excluded"
                chosen = next(
                    (
                        (line[a : a + 2], line[a + 2])
                        for a in (52, 55)
                        if line[a : a + 2].strip() and line[a + 2].strip()
                    ),
                    None,
                )
                if not chosen:
                    raise ValueError("No usable magnitude")
            origin = datetime.strptime(line[1:13], "%Y%m%d%H%M").replace(tzinfo=JST) + timedelta(
                seconds=fixed_decimal(line[13:17], 2)
            )
            record = {
                "provider_id": f"{namespace}:{number}",
                "origin_time": origin,
                "provider_updated": None,
                "latitude": int(line[21:24]) + fixed_decimal(line[24:28], 2) / 60,
                "longitude": int(line[32:36]) + fixed_decimal(line[36:40], 2) / 60,
                "depth_km": fixed_decimal(line[44:49], 2) if line[44:49].strip() else None,
                "magnitude": jma_magnitude(chosen[0]),
                "magnitude_type": chosen[1],
                "status": "reviewed",
                "event_type": "earthquake" if line[60] in {"1", "2", "5"} else "non_earthquake",
                "selection_reason": selection_reason,
                "raw": {"line": line, "namespace": namespace, "line_number": number},
            }
            accepted.append(validate_record(record))
            if selection_reason:
                # Retain valid correction evidence without relaxing the existing
                # conservative coverage policy; exclusion reporting is separate work.
                rejected.append({"line": number, "reason": selection_reason})
        except (ValueError, UnicodeDecodeError, IndexError) as exc:
            rejected.append({"line": number, "reason": str(exc)})
    return accepted, {
        "accepted": sum(r.get("selection_reason") is None for r in accepted),
        "retained_corrections": sum(r.get("selection_reason") is not None for r in accepted),
        "rejected_count": len(rejected),
        "rejected": rejected[:100],
        "identity_policy": "stable file namespace + line number; reordered files require new namespace",
    }


def parse_csv(data: bytes, mapping: dict | None = None):
    """Canonical CSV uses UTC-offset ISO timestamps, decimal degrees and km."""
    mapping = mapping or {}
    accepted, rejected = [], []
    for number, raw in enumerate(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))), 2):
        try:
            row = {mapping.get(k, k): v for k, v in raw.items()}
            record = {
                "provider_id": row["provider_id"],
                "origin_time": utc(row["origin_time"]),
                "provider_updated": utc(row["provider_updated"]) if row.get("provider_updated") else None,
                "longitude": float(row["longitude"]),
                "latitude": float(row["latitude"]),
                "depth_km": float(row["depth_km"]) if row.get("depth_km") else None,
                "magnitude": float(row["magnitude"]),
                "magnitude_type": row["magnitude_type"],
                "status": row.get("status") or "reviewed",
                "event_type": row.get("event_type") or "earthquake",
                "raw": raw,
            }
            accepted.append(validate_record(record))
        except (KeyError, ValueError, TypeError) as exc:
            rejected.append({"line": number, "reason": str(exc)})
    return accepted, {
        "accepted": len(accepted),
        "rejected_count": len(rejected),
        "rejected": rejected[:100],
        "mapping": mapping,
        "units": "decimal degrees, depth km, timezone-aware ISO8601",
    }
