# Local architecture

YureAtlas is a modular monolith with four Compose services:

- **web**: Next.js App Router/React/TypeScript, Tailwind, Lucide, MapLibre and Recharts. A server-only `/api` proxy preserves the browser's same-origin boundary; API configuration is not exposed through public environment variables. The basemap is bundled Natural Earth GeoJSON. No external tile/font/CDN requests are needed at runtime.
- **api**: FastAPI/Pydantic and SQLAlchemy with a bounded pool. Reads return immutable archive data; long mutations enqueue jobs and return HTTP 202. Readiness checks the migrated database and artifact directory. Only explicitly allowed origins/hosts and a custom request header permit mutations. Services bind to loopback on the host.
- **worker**: the same Python image/package, PostgreSQL durable queue, one installation-wide advisory lock, APScheduler triggers. Jobs use unique logical keys, `FOR UPDATE SKIP LOCKED`, two-minute leases, 15-second heartbeats, three recovery attempts, progress, cancellation and errors. Network requests have bounded backoff. A crash cannot duplicate a published forecast because publication has its own unique logical key.
- **db**: PostgreSQL 17.9 with PostGIS 3.6.4 installed in a Debian-based multiarch image, tested on Apple Silicon. The published base tags and lockfiles are pinned; rebuilding the DB image may pick up patched transitive Debian packages. Use tested image digests for stricter bit-level environment retention.

## Storage and transactions

Relational entities include sources, ingestion reports, stable event identities, append-only revisions, snapshots, versioned experiments and grids, feature sets, models, forecasts/cells, evaluations/cells, durable jobs, promotion audit records, and worker heartbeat. JSONB holds structured configuration/diagnostics, not the whole application. Time/source/foreign-key paths have B-tree indexes; PostGIS geometries have GiST indexes. Every timestamp is timestamptz. Forecast-cell uniqueness and finite probability bounds are database constraints.

Immutable tables reject update/delete. Child forecast/evaluation rows are inserted before their run using deferred foreign keys; the publication trigger checks complete grid membership, and the sealing trigger rejects new child rows once the parent exists. An API/UI restriction alone is not the protection. A database owner can still deliberately change schema/triggers; these controls protect normal application transactions, not a hostile local administrator.

Artifacts use immutable SHA-256 filenames under the persistent artifact volume. Raw payloads, feature Parquet batches, model joblib files, dependency manifests and reports are atomically written, fsynced, and verified on reuse. Orphan artifacts after a failed database transaction are harmless and retained; no automatic garbage collector removes research history. Only locally produced, DB-referenced model files are deserialized.

## Data flow

1. A validated request enqueues a unique job. The API never performs training or simulation in an in-process background task.
2. Worker retrieval stores raw source artifacts, normalized append-only revisions, rejection reports, and explicit coverage intervals. Receipt time differs from origin/provider update time.
3. Fitting creates an exact revision snapshot, temporal feature batches and manifest, then model versions with diagnostics and availability.
4. Prediction checks experiment compatibility, cutoffs, model availability, source freshness and complete output. Atomic publication records issue time, 24-hour window, frozen probabilities/colours, code version and dependency manifest.
5. Evaluation requires completed covered target windows, reconstructs a frozen observation snapshot, scores saved probabilities, and appends a new version only when observations/coverage change.
6. The dashboard fetches per-cell GeoJSON and source-compatible event overlays, exposes job progress, and compares matching forecast/observation pairs.

## Scheduling and recovery

JST defaults: recent ingestion every 15 minutes, following-day forecast at 23:40 (23:30 input cutoff), evaluation at 06:00, 31-day reconciliation at 06:30 (including D+3/7/30), retraining Sundays at 03:00. Promotion remains explicit. A 30-second monotonic timer reconciles durable decisions on startup and during operation, independently of worker heartbeat. Jobs and the scheduling cursor commit atomically. Restart from 23:40 until midnight queues the still-eligible upcoming target once; absent expired targets become missed jobs within an explicit 31-target recovery bound. Existing decisions remain unchanged. Safe work coalesces to its latest due occurrence; reconciliation performs USGS refresh before evaluation in one job, and weekly training pins its requested endpoint. Disabled scheduling creates no automatic catch-up jobs. [Scheduling policy](scheduling.md) details canonical keys, clock changes, bounds and omitted-history diagnostics. Schedules run only while the worker and computer run. Cancellation is cooperative at batch/simulation checkpoints; individual model fitting calls finish their bounded operation before cancellation is observed.

## Backup and restore

The backup command stops API and worker writes, captures `pg_dump` plus all verified artifact bytes, writes a checksum manifest, then resumes previously running writers. Restore verifies the manifest and every artifact path/content hash, then restores into a **new Compose project and new volumes**, leaving the original installation untouched. Default restore ports are 3001/8001/55433. Schedules remain disabled in the restored installation until explicitly enabled. No uploaded ZIP/tar member is extracted to an arbitrary filesystem path.

## API contract

`docs/openapi.json` is generated from the application; `apps/web/lib/schema.d.ts` is generated with openapi-typescript and consumed by the frontend. `/health`, `/ready`, `/experiments`, `/events`, revision history, ingestion/import jobs, forecast lists/provenance/maps/cells, evaluations/revisions, metrics, model registry/promotions, training/backtest jobs, cancellation, and JSON exports are available. No endpoint edits issued probabilities. All expensive API actions return a durable job ID. See the generated contract or the local API's `/docs` (interactive docs may need their own CDN assets; the application itself does not).
