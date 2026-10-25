# Liveness, infrastructure readiness, and research prerequisites

`GET /health` returns HTTP 200 with `status: "ok"` when the API can handle the request. It performs no database, filesystem, worker, or scheduling checks. It can remain successful while those dependencies are unavailable.

`GET /ready` returns HTTP 200 with `status: "ready"` only when every infrastructure component is `ok`: database, schema, artifact storage, worker, and scheduler. Otherwise it returns HTTP 503 with `status: "not_ready"`. Both responses use the same typed body and `Cache-Control: no-store`. Each component under `checks` has a `status` (`ok`, `failed`, or `skipped`), a machine-readable `code`, and an actionable `message`. A skipped dependency check cannot establish readiness. Schema details include expected and observed revisions; worker and scheduler details identify the evidence used.

The separate `prerequisites` object is advisory. An empty catalogue, partial receipt, or missing model does not by itself fail infrastructure readiness. A first installation can be infrastructure-ready before importing data, provided the worker is running and has acknowledged the applicable scheduling policy. Readiness does not claim that a forecast can be published or that its predictions are scientifically useful.

## Database and schema

The probe uses a dedicated disposable connection and a read-only transaction, without waiting for the application's shared connection pool or its pre-ping. It requires exactly one Alembic revision, `010`, matching this application version. A missing version table, missing revision, different revision, or multiple heads fails the schema component. Worker, scheduler, and research metadata are inspected only after schema compatibility is established.

The PostgreSQL 17 probe applies these limits:

| Operation | Limit |
| --- | --- |
| Driver connection establishment | 3 seconds |
| Each SQL statement | 2 seconds |
| Lock acquisition | 1 second |
| Entire database transaction, including metadata inspection | 5 seconds |

Startup options also cover the driver's initial SQL queries. The connection is closed and the disposable engine is disposed after the probe. Database errors or timeouts produce a sanitized database failure rather than returning connection strings or raw driver exceptions. A schema mismatch directs the operator to apply this installation's migrations.

These are driver and PostgreSQL limits, not a hard total HTTP response deadline. DNS resolution, operating-system networking, connection teardown, and other stalls outside PostgreSQL's control can exceed them. The database and artifact checks run sequentially; their individual budgets should not be interpreted as one combined response-time guarantee.

## Artifact storage

Artifact access is tested in a separate process using the current Python interpreter in isolated, standard-library-only mode. The child opens or creates the configured artifact directory, creates a unique `.yureatlas-readiness-…` probe file, writes a random token, syncs it, reads it back, and cleans it up. It does not deserialize models, read research artifact contents, or remove existing research files.

Cleanup checks the probe's file identity and token before removing it. A replaced file or mismatching contents are preserved and reported as a failed check. Read, write, sync, cleanup, replacement, directory, and process failures have distinct fixed codes and messages. The response does not expose the artifact path, token, or raw filesystem exception.

The parent allows a nominal 3-second probe budget, including a reserved kill-and-reap interval. On timeout it requests process termination and waits only within that budget. An uninterruptible filesystem operation can delay actual process death; a timed-out process may still be terminating, and its owned temporary file may remain. The response states that limitation. The parent does not attempt deletion after a timeout because it cannot safely establish the file's current identity. Process creation and operating-system stalls are not a hard real-time guarantee.

Passing this check establishes current access to a temporary file in the artifact root. It does not verify every archived artifact's checksum, durability across hardware failure, or future filesystem availability. Forecast execution retains its own artifact-integrity checks.

## Worker and scheduler

Worker readiness requires the registered worker epoch to hold the actual PostgreSQL singleton advisory lock, with matching backend identity and a current heartbeat. A heartbeat more than 90 seconds old is stale; one more than 5 seconds ahead of the database check time indicates clock skew. Missing registration, invalid ownership metadata, lost singleton ownership, stale heartbeat, or clock skew fails the worker check. A recent timestamp alone is insufficient.

Scheduler readiness first requires that verified worker and its reported process setting. When the worker reports `SCHEDULE_ENABLED=false`, scheduling is intentionally disabled and the scheduler component can be `ok` without a runtime acknowledgment. The worker component still has to pass. API-process environment values do not establish the worker's setting.

When the worker process enables scheduling, readiness requires a successful runtime application by that same worker owner. Its revision and normalized specification hash must match the current requested configuration; its applied/disabled status and effective enablement must agree with the current scope and kinds. An active scope above 100 real experiments fails. The application acknowledgment must be no more than 90 seconds old and no more than 5 seconds ahead of the check time.

Persisted disablement, empty selected scope, empty kinds, or an inapplicable ingestion-only JMA scope still requires a recent disabled acknowledgment from a process-enabled worker. The timer continues polling those policies. A fresh worker heartbeat therefore cannot conceal a stalled scheduler, and saving a new revision can leave readiness pending until the worker applies it. A failed planning pass preserves its prior acknowledgment instead of claiming success. See [scheduling](scheduling.md) for cadence, bounded catch-up, queued-work pauses, and immutable settings history.

## Advisory source and model evidence

Prerequisite inspection follows the persisted requested real-experiment scope even when scheduling is disabled or no kinds are enabled. It reads at most 21 experiment rows to detect excess, inspects at most the first 20 ordered by creation time and ID, and exposes `inspection_limit`, `inspected_count`, `has_more`, and `scope_complete`. An incomplete sample makes no assertion about omitted experiments. `scope_complete` describes that metadata sample only; it does not establish catalogue coverage.

An installation without experiments reports `first_run`; one with only synthetic experiments reports `real_setup_needed`. A selected scope containing no real experiments reports `empty_scope`. Missing or problematic sampled evidence reports `needs_attention`. `metadata_available` means the inspected experiments have complete latest receipts and selectable model metadata; `sampled` also discloses uninspected experiments. Neither status changes the infrastructure decision. If the database/schema cannot be inspected, the advisory result is `unavailable` with unknown scope fields rather than invented zero-data evidence.

For each inspected source, the probe returns only the latest ingestion receipt recorded by the check time, using creation time and ID to break ties. One batch performs these top-one lookups for the distinct sampled sources, supported by the ingestion `(source_id, created_at, id)` index. It exposes receipt status, interval, and supplemental/feed flags without loading raw request manifests or counting catalogue events. A complete latest receipt does not establish completeness of the training, freshness, or target interval. A supplemental receipt does not declare interval coverage. Every source result explicitly has `coverage_verified: false`.

For each inspected experiment, the probe invokes the actual automatic model-selection policy using the database check time as the input cutoff and a prospective target 30 minutes later. It returns selected model timing and promotion metadata, or the selector's unavailable reason. The historical-rate fallback has an `(experiment_id, algorithm, status, created_at, id)` index; promotion selection uses its experiment/time/ID history index. At most 20 model selections occur, each with bounded lookup results, rather than materializing all model history. These indexes and output limits reduce work; they are not proof of constant query cost, so the statement and whole-transaction limits still apply.

The probe does not reserve that selection, deserialize the artifact, verify its bytes, or reconstruct catalogue coverage. Execution can still reject later model availability, freshness, coverage, or publication-deadline conditions. All database inspection shares the transaction budget above.

## Compatibility fields

`GET /sources` now derives `worker.online` from the same registered singleton and heartbeat check; it also exposes the worker status, reason, and process environment setting. It is not a full readiness check. Its older top-level `schedule_enabled` remains the API process environment value for compatibility. Use `GET /schedules` for requested/effective/applied scheduling truth and `GET /ready` for the structured infrastructure decision.
