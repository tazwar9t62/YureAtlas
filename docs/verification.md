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
