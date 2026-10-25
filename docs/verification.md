# Verification evidence at the paused checkpoint

Status: incomplete, 2026-09-29. Results below distinguish successful earlier checks from the latest failing checks. No claim is made that all tests pass at this checkpoint.

| Check | Observed result |
| --- | --- |
| Initial frontend production build and API boundary test | Passed during the foundation increment. |
| Grid, time and source parser tests | Five tests passed during the catalogue increment. |
| Database invariants | Four integration tests previously passed against PostGIS, covering immutable archive behavior, revisions, publication retry and job recovery. |
| Backend suite before the latest changes | 18 tests passed using a disposable database. |
| Latest backend suite | **15 passed, 4 errors**. Persistence test setup could not connect reliably to PostgreSQL; cleanup reported that the server closed its connection unexpectedly. Root cause remains unresolved. |
| Frontend production build | Passed natively after the MapLibre import and React typing corrections. Later edits received a passing TypeScript check, but not another full production build. |
| Latest frontend TypeScript check | Passed. |
| Browser acceptance | **All three tests failed**, including retries: demo timeout/UI flow, hidden mobile map and offline model loading. A longer demo timeout is saved but not verified by a successful run. |
| Synthetic scientific workflow | Four model families each produced seven immutable daily forecasts; evaluation withheld the missing-coverage day. A later queued demo run also completed. Synthetic evidence is not real predictive validation. |
| Real USGS smoke import | One-day bounded query imported 3 records with no rejections. This is a source connectivity/parser check, not a historical coverage assessment. |
| Official JMA h2023 parser exercise | 246,944 accepted records, 10,076 policy/validation exclusions, and 1,176 selected M4+ records. |
| Container builds | Database and an earlier Python image built. Current Python source needs rebuilding. Web image build interrupted at the user's pause request. |
| Full four-service clean install | Not verified. |
| Scheduled operations and recovery | Implemented; full operational acceptance remains pending. |
| Backup/restore | Artifact unit coverage exists; complete database-plus-artifact restore acceptance remains pending. |
| Scientific validation of ETAS | Pending external validation; explicitly a research pathway. |

Generated browser screenshots and traces remain locally under `apps/web/test-results/` and are excluded from source control. See [handoff.md](handoff.md) for the ordered remaining work and preserved local state.

## 2026-10-25 database diagnosis (T01.1)

Fresh evidence for issue [#6](https://github.com/tazwar9t62/YureAtlas/issues/6), separate from the historical checkpoint above:

- The daemon was unreachable until the installed Docker Desktop was started. Docker Desktop 4.16.2 / Engine 20.10.22 / Compose 2.15.1 then responded; its arm64 VM has 4 CPUs and 4 GiB memory. No Docker settings or volume contents were reset.
- Existing `yureatlas-db-1` became healthy: PostgreSQL 17.9, PostGIS 3.6.4, 64 MiB shared memory, no container memory limit, `OOMKilled=false`, restart count 0. Research volumes `yureatlas_database` and `yureatlas_artifacts` were preserved.
- `OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python scripts/test_backend.py` completed with **19 passed in 7.08 seconds**, including all four persistence tests. One upstream Starlette/httpx deprecation warning remains. The harness created and removed its own UUID-named database and artifact directory.
- The pre-existing failed-run database `yureatlas_test_3750f48415c9` remains untouched. It is not owned by this run and is not automatically removed.
- Retained Docker logs do not establish the September failure's root cause. They show historical health-check timeouts and later PostgreSQL child-process SIGPIPE/recovery events, but no correlated evidence proving a resource limit or OOM caused that test failure. No speculative resource changes were made.
- Local Python 3.12.12 and Node 24.21.0 are supported. Global pnpm 12.9.1 differs from the project's 10.33.0 pin; T01.3 will invoke the pinned tool. The existing harness still needs the isolation/cleanup changes tracked by #7; a successful run does not validate its failure paths.

## 2026-10-25 catalogue correction acceptance (T02.4)

Current evidence for [#15](https://github.com/tazwar9t62/YureAtlas/issues/15), distinct from the paused checkpoint. The research database and artifact directory were preserved.

- Full backend checks passed: **239 unit tests and all 49 PostGIS tests**. Lint, TypeScript, read-only contracts and Actionlint passed. The import form's earlier three isolated mocked tests and Webpack production build remain applicable; default production-build CI is required before merge.
- Eight real CSV import → training → forecast → evaluation → correction cases cover downgrade, relocation, moved origin, event reclassification, deletion, out-of-order provider versions, replay and omission. They compare earlier snapshot membership, archive rows, probabilities, model bytes and real saved feature/Parquet bytes. Two USGS retrieval failure/recovery cases withhold labels until reconciliation succeeds. These are deterministic software acceptance tests, not predictive validation.
- Eight PostgreSQL concurrency cases cover source-attempt serialization, independent sources, no open transaction during network work, failure/cancellation cleanup, failed unlock, direct-write exclusion and lost-session fencing. A terminated old attempt cannot publish a stale response after its successor completes.
- `make test-migrations` uses a separate fresh, owned database. It seeds schema002 with 18 rows and four opaque artifact payloads, upgrades through003/004/head, compares all original values and bytes, checks old/new constraints, and verifies legacy deduplication, appended corrections, receipt-time reversal and repeated-upgrade idempotence. Those marker artifacts prove preservation; the end-to-end cases above cover real model/Parquet artifacts. No downgrade or research-schema migration is performed by these checks.

The opt-in [source smoke script](../scripts/smoke_sources.py) opens no database. It requires a new output directory, retains raw evidence, limits physical USGS requests, and bounds the official ZIP download to 16 MiB/60 seconds and uncompressed text to 32 MiB. Reproduce with `.venv/bin/python scripts/smoke_sources.py --output /tmp/new-yureatlas-source-evidence`; `--source usgs` or `--source jma` selects one source. It is intentionally separate from offline/CI tests.

| Live check | Observed result and scope |
| --- | --- |
| USGS, 2023-05-05 UTC, 136–138°E / 36–38°N, M≥4 | Five records; exact-ID retrieval of `us6000k9mb`; three physical requests, one known-ID attempt, no rejected records. Raw request/body hashes verified. Connectivity and response-shape evidence only. |
| Official JMA `h2023.zip` | 6,977,812 compressed bytes; 24,930,940 uncompressed bytes; 257,020 physical records. Repository first-record fixture matches the download. Full parser: 246,944 selected, 103 safe exclusions (87 agency, 16 event type), 9,973 invalid because magnitude is undetermined. **The file cannot establish complete scoring coverage.** |

The first USGS run failed because its query metadata omitted a count. The parser was corrected to require the independent preflight count in that case, and a repeat live run passed. A committed actual-response fixture preserves this regression; feed count requirements remain strict. The first full JMA run exposed 87 foreign-agency formatting cases, which now have actual-file regressions. The repeat JMA run passed with the conservative counts above. No rejected evidence was silently converted into a quiet event label.

Retained local evidence directories are `/private/tmp/yureatlas-source-smoke-20261025-t02` (initial runs), `/private/tmp/yureatlas-source-smoke-20261025-t02-usgs-fixed` and `/private/tmp/yureatlas-source-smoke-20261025-t02-jma-fixed`. The final JMA report records the provider source hash `b2477bc27504b3c78975aa49df29ec9d287381838b2964d4da9fa00c98fad60c` and script hash; temporary evidence is not a durable backup. JMA ZIP SHA-256 is `e5ced2bf7275825ba75405b071bb54e9d4c2a5eb55aa6bc9b8d670de1f58b98f`; uncompressed SHA-256 is `9f9d0d230e65858388393691fe2f4a445641cab06266f943d03927627fa4d4d4`. Source links and attribution remain in [data sources](data-sources.md). Neither successful retrieval nor these fixtures demonstrate physical catalogue completeness or real-world forecasting skill.

## 2026-10-25 deterministic comparison evidence (T03.1)

Issue [#18](https://github.com/tazwar9t62/YureAtlas/issues/18) replaces row-order-dependent reference selection with the documented earliest compatible archived historical-rate policy. Twenty-three focused pure cases cover permutations, ties, exact windows, revision/cell/label mismatches, unavailable or malformed evidence, self-reference and rolling reference versions. Two isolated PostGIS endpoint cases verify bulk latest-evaluation selection, foreign-cell rejection, unevaluated/awaiting accounting and rejection of a foreign-source snapshot even when both revision sets are empty. The API retains an evaluated-only legacy score series and separately reports decisions for every forecast.

`make lint test-unit typecheck contract-check` passed; after the status-validation regressions, `make lint test-unit` passed with **262 unit tests** and `make test-integration` passed with **all 51 PostGIS tests**. The final snapshot-lineage compatibility fix passed all 23 focused pure cases, including canonical revision identity for awaiting-data evaluations and absence of unevaluated forecasts from the legacy series. The owned database/artifact harness cleaned up successfully. One existing Starlette/httpx deprecation warning remains. Period/export alignment, pooled uncertainty and the provenance UI are the subsequent T03 issues; this evidence does not claim those are complete.

## 2026-10-25 pooled comparison arithmetic (T03.2)

Issue [#19](https://github.com/tazwar9t62/YureAtlas/issues/19) adds 37 independent pure cases. The hand-calculated two-window fixture has four observed cells, Brier sum0.73, pooled Brier0.1825, reference Brier0.25, skill0.27, recall0.5, precision1, false-positive rate0 and red area coverage1/7. Its log loss is checked against `-log(0.9 × 0.2 × 0.8 × 0.8) / 4`. Missing observations increase missing counts without changing known scores. Unpaired extreme scores cannot affect either side of the matched comparison.

The weighted seven-day block fixture alternates one-cell windows with loss difference+0.2 and nine-cell windows with loss difference−0.1 per cell. Over 28 windows the correct pooled difference is **−0.07**, whereas the unweighted daily average is+0.05. The implementation's point matches the pooled report, and its seeded interval matches independently enumerated block-count endpoints `[-12/156, -7.6/124]`. Permutations, 27/28-day boundaries, gaps, duplicate and non-24-hour windows, invalid sums/counts and zero reference denominators are checked explicitly. These arithmetic tests validate the implementation, not predictive performance or a model-ranking claim.

Forecast and cell-window availability partitions are tested, including trusted missing counts versus all unscored cells. Invalid source/observation evidence remains in decisions and exclusions but cannot enter summaries or chart history. Series scores are derived from stored totals, and malformed nonfinite optional values cannot leak into JSON. Source payloads remain unchanged.

Final `make lint test-unit` passed with **299 unit tests**; `make test-integration` passed with **all 51 PostGIS tests** and successful owned-resource cleanup. TypeScript and read-only contracts passed during the same issue; source changes after that run affected only backend score logic and tests. The existing Starlette/httpx warning remains. Report date scopes, exports and frontend provenance remain #20–#21.
