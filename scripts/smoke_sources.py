"""Opt-in bounded live-source check; never opens a database or imports records.

Run with `.venv/bin/python scripts/smoke_sources.py --output /tmp/new-evidence-dir`.
The output directory must not exist. Raw evidence is retained for inspection.
"""

import argparse
import asyncio
import hashlib
import io
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import httpx
from yureatlas import providers
from yureatlas.artifacts import read
from yureatlas.config import settings
from yureatlas.providers import IMPORT_PARSER_VERSION, USGS, parse_jma
from yureatlas.timeutil import utc

JMA_URL = "https://www.data.jma.go.jp/eqev/data/bulletin/data/hypo/h2023.zip"
MAX_ZIP = 16 * 1024 * 1024
MAX_TEXT = 32 * 1024 * 1024


async def download_jma():
    chunks = []
    size = 0
    async with (
        httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "YureAtlas/0.1 (source verification)"}
        ) as client,
        client.stream("GET", JMA_URL) as response,
    ):
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_ZIP:
                raise ValueError("Official ZIP exceeds the smoke check's 16 MiB limit")
            chunks.append(chunk)
        return b"".join(chunks), dict(response.headers)


def check_jma(output):
    async def bounded_download():
        return await asyncio.wait_for(download_jma(), timeout=60)

    zipped, headers = asyncio.run(bounded_download())
    (output / "h2023.zip").write_bytes(zipped)
    with ZipFile(io.BytesIO(zipped)) as archive:
        members = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(members) != 1 or members[0].file_size > MAX_TEXT:
            raise ValueError("Expected one official uncompressed file of at most 32 MiB")
        with archive.open(members[0]) as stream:
            raw = stream.read(MAX_TEXT + 1)
        if len(raw) > MAX_TEXT:
            raise ValueError("Uncompressed file exceeds 32 MiB")
        member = members[0].filename
    (output / "h2023.txt").write_bytes(raw)
    records, report = parse_jma(raw, "h2023")
    fixture = Path(__file__).resolve().parents[1] / "services/api/tests/fixtures/jma_2023_first.txt"
    fixture_matches = raw.splitlines()[0] == fixture.read_bytes().strip()
    if not fixture_matches:
        raise ValueError("Official first record differs from the attributed repository fixture")
    return {
        "status": "passed",
        "url": JMA_URL,
        "response_headers": headers,
        "zip_sha256": hashlib.sha256(zipped).hexdigest(),
        "zip_bytes": len(zipped),
        "member": member,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": len(raw),
        "retained_records": len(records),
        "fixture_matches": fixture_matches,
        "report": report,
        "scope": "Full official-file parser smoke only; invalid rows still prevent complete import coverage",
    }


def check_usgs(output):
    start, end = utc("2023-05-05T00:00:00Z"), utc("2023-05-06T00:00:00Z")
    bounds = {"west": 136, "east": 138, "south": 36, "north": 38}
    rows, reports = [], []

    def request_limit():
        if len(provider.requests) >= 12:
            raise ValueError("Live smoke check reached its 12 physical-request limit")

    with closing(USGS(max_id_requests=4, id_seconds=30, progress=request_limit)) as provider:
        try:
            return usgs_result(provider, start, end, bounds, rows, reports)
        finally:
            (output / "usgs-requests.json").write_text(
                json.dumps(provider.requests, indent=2, sort_keys=True) + "\n"
            )


def usgs_result(provider, start, end, bounds, rows, reports):
    for a, b, records, report in provider.retrieve(start, end, bounds, 4.0):
        if report["rejected_count"]:
            raise ValueError("Live USGS query contained rejected records")
        rows.extend(records)
        reports.append(dict(report, start=a.isoformat(), end=b.isoformat()))
    if not rows:
        raise ValueError("Expected a nonempty historical response for the bounded fixture interval")
    identity = rows[0]["provider_id"]
    detail, detail_report = provider.retrieve_id(identity)
    if detail["provider_id"] != identity:
        raise ValueError("Known-ID response changed identity")
    for attempt in provider.requests:
        read(attempt["request_ref"])
        if "raw_ref" in attempt:
            read(attempt["raw_ref"])
    return {
        "status": "passed",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bounds": bounds,
        "minimum_magnitude": 4.0,
        "records": len(rows),
        "chunks": reports,
        "detail_id": identity,
        "detail_report": detail_report,
        "physical_requests": len(provider.requests),
        "requests": provider.requests,
        "limits": provider.budget_report(),
        "scope": "Connectivity, response shape and raw-evidence verification; no database or coverage claim",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source", choices=("usgs", "jma", "both"), default="both")
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    settings.artifact_dir = output / "artifacts"
    result = {
        "started_at": datetime.now(UTC).isoformat(),
        "parser_version": IMPORT_PARSER_VERSION,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "provider_sha256": hashlib.sha256(Path(providers.__file__).read_bytes()).hexdigest(),
        "sources": {},
    }
    print(f"Evidence directory: {output}", flush=True)
    failed = False
    for name, check in (("usgs", lambda: check_usgs(output)), ("jma", lambda: check_jma(output))):
        if args.source not in {name, "both"}:
            continue
        try:
            result["sources"][name] = check()
        except (ValueError, OSError, httpx.HTTPError, TimeoutError, BadZipFile) as exc:
            failed = True
            result["sources"][name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(f"{name}: {result['sources'][name]['status']}", flush=True)
        result["finished_at"] = datetime.now(UTC).isoformat()
        (output / "report.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    raise SystemExit(int(failed))


if __name__ == "__main__":
    main()
