"""Retrieval is distinct from parsing and immutable catalogue persistence."""
import csv
import io
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .artifacts import put
from .timeutil import JST, utc


class Provider(Protocol):
    def retrieve(self, start: datetime, end: datetime, bounds: dict, minimum: float): ...
    def parse(self, payload: dict) -> tuple[list[dict], list[dict]]: ...


def validate_record(record):
    for key in ("longitude", "latitude", "magnitude"):
        if not math.isfinite(record[key]):
            raise ValueError(f"Unusable {key}")
    if not -180 <= record["longitude"] <= 180 or not -90 <= record["latitude"] <= 90:
        raise ValueError("Invalid coordinates")
    if not -3 <= record["magnitude"] <= 10:
        raise ValueError("Unusable magnitude")
    if record["depth_km"] is not None and (not math.isfinite(record["depth_km"]) or not -100 <= record["depth_km"] <= 1000):
        raise ValueError("Unusable depth")
    record["origin_time"] = utc(record["origin_time"])
    if record["provider_updated"] is not None:
        record["provider_updated"] = utc(record["provider_updated"])
    if not record["provider_id"] or not record["magnitude_type"]:
        raise ValueError("Missing event ID or magnitude type")
    return record


class USGS:
    url = "https://earthquake.usgs.gov/fdsnws/event/1/"

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=45, headers={"User-Agent": "YureAtlas/0.1 (local research)"})

    def request(self, url, params=None):
        for attempt in range(4):
            try:
                time.sleep(.25)
                response = self.client.get(url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
                if attempt == 3:
                    raise
                time.sleep(2**attempt)

    def retrieve(self, start, end, bounds, minimum):
        params = {"starttime": start.isoformat(), "endtime": end.isoformat(), "eventtype": "earthquake",
                  "minlatitude": bounds["south"], "maxlatitude": bounds["north"],
                  "minlongitude": bounds["west"], "maxlongitude": bounds["east"],
                  "minmagnitude": minimum, "includedeleted": "true"}
        count = int(self.request(self.url + "count", params=dict(params, format="geojson")).json()["count"])
        if count >= 19000:
            if end-start < timedelta(seconds=1):
                raise ValueError("Catalogue interval cannot be safely subdivided")
            middle = start+(end-start)/2
            yield from self.retrieve(start, middle, bounds, minimum)
            yield from self.retrieve(middle, end, bounds, minimum)
            return
        response = self.request(self.url + "query", params=dict(params, format="geojson", limit=20000, orderby="time-asc"))
        payload = {"features": [], "metadata": {"count": 0}} if response.status_code == 204 else response.json()
        raw_ref = put(response.content or b"{}", "json")
        actual = len(payload.get("features", []))
        # A changing catalogue between count/query is retried by the bounded job.
        if actual != count or actual != payload.get("metadata", {}).get("count", actual):
            raise ValueError(f"Catalogue changed or truncated: count={count}, received={actual}; retry interval")
        records, rejected = self.parse(payload)
        records = [r for r in records if start <= r["origin_time"] < end]
        yield start, end, records, {"query": params, "raw_ref": raw_ref, "reported_count": count,
                                    "rejected": rejected[:100], "rejected_count": len(rejected),
                                    "coverage_policy": "complete query, not a claim of scientific completeness"}

    def feed(self):
        response = self.request("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson")
        return self.parse(response.json()), put(response.content, "json")

    def parse(self, payload):
        accepted, rejected = [], []
        for feature in payload.get("features", []):
            try:
                properties = feature["properties"]
                if properties.get("type") != "earthquake":
                    raise ValueError("Not an earthquake")
                coordinates = feature["geometry"]["coordinates"]
                record = {"provider_id": feature["id"], "origin_time": datetime.fromtimestamp(properties["time"]/1000, timezone.utc),
                          "provider_updated": datetime.fromtimestamp(properties["updated"]/1000, timezone.utc) if properties.get("updated") else None,
                          "longitude": float(coordinates[0]), "latitude": float(coordinates[1]),
                          "depth_km": float(coordinates[2]) if len(coordinates)>2 and coordinates[2] is not None else None,
                          "magnitude": float(properties["mag"]), "magnitude_type": properties.get("magType") or "unknown_preferred",
                          "status": properties.get("status", "reviewed"), "raw": feature}
                accepted.append(validate_record(record))
            except (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError) as exc:
                rejected.append({"id": feature.get("id"), "reason": str(exc)})
        return accepted, rejected


def fixed_decimal(value: str, places: int) -> float:
    # FORTRAN fixed-point fields imply decimal places; blank fractional columns
    # denote an integer depth/second in the official depth-slice/fixed formats.
    if not value.strip():
        raise ValueError("Missing fixed-width value")
    if "." in value:
        return float(value)
    return float(value[:-places]) if value[-places:].isspace() else int(value)/(10**places)


def jma_magnitude(value: str) -> float:
    if value[0] in "ABC":
        return -(ord(value[0])-ord("A")+1) - int(value[1])/10
    return int(value)/10


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
            if line[0] != "J" or line[60] not in {"1", "2", "5"}:
                raise ValueError("Not a JMA natural earthquake")
            if line[59] == "8":
                raise ValueError("Unaccepted hypocentre")
            chosen = next(((line[a:a+2], line[a+2]) for a in (52, 55) if line[a+2] in "JDdVv" and line[a:a+2].strip()), None)
            if not chosen:
                raise ValueError("No usable JMA magnitude family; Mw/mb/Ms excluded")
            origin = datetime.strptime(line[1:13], "%Y%m%d%H%M").replace(tzinfo=JST) + timedelta(seconds=fixed_decimal(line[13:17], 2))
            record = {"provider_id": f"{namespace}:{number}", "origin_time": origin,
                      "provider_updated": None, "latitude": int(line[21:24])+fixed_decimal(line[24:28], 2)/60,
                      "longitude": int(line[32:36])+fixed_decimal(line[36:40], 2)/60,
                      "depth_km": fixed_decimal(line[44:49], 2) if line[44:49].strip() else None,
                      "magnitude": jma_magnitude(chosen[0]), "magnitude_type": chosen[1],
                      "status": "reviewed", "raw": {"line": line, "namespace": namespace, "line_number": number}}
            accepted.append(validate_record(record))
        except (ValueError, UnicodeDecodeError, IndexError) as exc:
            rejected.append({"line": number, "reason": str(exc)})
    return accepted, {"accepted": len(accepted), "rejected_count": len(rejected), "rejected": rejected[:100],
                       "identity_policy": "stable file namespace + line number; reordered files require new namespace"}


def parse_csv(data: bytes, mapping: dict | None = None):
    """Canonical CSV uses UTC-offset ISO timestamps, decimal degrees and km."""
    mapping = mapping or {}
    accepted, rejected = [], []
    for number, raw in enumerate(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))), 2):
        try:
            row = {mapping.get(k, k): v for k, v in raw.items()}
            record = {"provider_id": row["provider_id"], "origin_time": utc(row["origin_time"]),
                      "provider_updated": utc(row["provider_updated"]) if row.get("provider_updated") else None,
                      "longitude": float(row["longitude"]), "latitude": float(row["latitude"]),
                      "depth_km": float(row["depth_km"]) if row.get("depth_km") else None,
                      "magnitude": float(row["magnitude"]), "magnitude_type": row["magnitude_type"],
                      "status": row.get("status") or "reviewed", "raw": raw}
            accepted.append(validate_record(record))
        except (KeyError, ValueError, TypeError) as exc:
            rejected.append({"line": number, "reason": str(exc)})
    return accepted, {"accepted": len(accepted), "rejected_count": len(rejected), "rejected": rejected[:100], "mapping": mapping,
                       "units": "decimal degrees, depth km, timezone-aware ISO8601"}
