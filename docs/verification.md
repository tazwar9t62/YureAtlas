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
