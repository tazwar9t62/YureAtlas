# Paused development checkpoint

Development paused at the user's request on 2026-09-29. This is an unfinished prototype checkpoint, not a completed or release-ready build. Resume the remaining work only when requested.

## Saved implementation

- Local Next.js/FastAPI/PostGIS application with append-only catalogue revisions, deterministic projected cells, immutable forecasts and versioned evaluations.
- USGS ingestion, JMA/CSV imports, a reproducible synthetic catalogue, and proper probability scores.
- Historical-rate, calibrated gradient-boosting, hybrid and fitted discrete-space ETAS research models, temporal features and walk-forward orchestration.
- Dashboard, probability maps, cell evidence, model comparison, model registry, source operations and durable jobs.
- Worker scheduling, command-line operations, artifact integrity checks and backup/restore tooling.

The original brief remains at `/Users/mdtazwarislam/Downloads/YureAtlas_Fullstack_Local_Build_Prompt.md` on the development machine. Architecture, experiment definitions and model limitations are in the neighboring documentation files. See [verification.md](verification.md) for evidence and failures.

## Resume in this order

1. Check Docker/PostgreSQL health and resource usage. The latest backend run ended with database connection failures: 15 passed, 4 setup errors; cleanup also lost its database connection. Determine the cause before retrying. The disposable database `yureatlas_test_3750f48415c9` may remain after the failed cleanup. Preserve the main database and its immutable archive.
2. Re-run `OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python scripts/test_backend.py` once the database is healthy. Confirm isolation and cleanup. An earlier run passed 18 tests, but that does not establish that this checkpoint passes.
3. Start the API and worker, then the frontend. Run browser acceptance again and fix the failures: the synthetic job exceeded the original two-minute test timeout; the mobile map region was hidden; the offline flow did not load model options. The timeout was subsequently increased to ten minutes, but that version has not passed. Inspect local `apps/web/test-results/` screenshots, traces and error contexts; these generated files are intentionally ignored by Git.
4. Visually inspect desktop/mobile layouts, map interaction, cell details, forecasts, evaluations and error/empty/loading states. Check the history chart color cycle when more than four model versions exist.
5. Rebuild the current Python and web images and verify a clean four-service Compose installation. The local Python image predates some source changes; the web image build was interrupted when development paused. Do not treat either as the final checkpoint image.
6. Verify worker schedules, startup recovery, lease recovery, missed forecast deadlines, cancellation and prospective availability semantics end to end.
7. Exercise backup and restore into a separate Compose project, compare database and artifact integrity, and test subsequent operation. Check compatibility of Docker Compose JSON output parsing across versions.
8. Refresh/check the generated API contract, complete remaining brief acceptance requirements, and write final setup/operation/recovery instructions. Review readiness checks and outstanding data diagnostics/filter coverage against the brief. ETAS external scientific validation remains pending.
9. Run final backend, frontend, browser, clean-install and backup/restore checks, record concrete results, and commit the finished increments. Do not claim predictive superiority from the synthetic demonstration.

## Local environment and preserved state

- macOS arm64; Python 3.12; Node 24.14.1; pnpm 10.33.0. A shell can select an older broken Node installation: prepend `/Users/mdtazwarislam/.nvm/versions/node/v24.14.1/bin` to PATH when resuming frontend commands.
- Native dependencies are in `.venv` and `apps/web/node_modules`; the local uv binary is `.tools/bin/uv`. These are ignored by Git.
- The development database uses port 55432 and the Compose database volume. Native model/raw-data artifacts are in `data/artifacts`. Preserve both; native artifacts have not been synchronized to the Compose artifact volume.
- Some `test_*` experiments from early integration testing are present in the main development database. Tests now use a disposable database. Do not delete immutable archive rows to clean the display; use a separate clean acceptance instance.
- The native API/frontend and active image build were stopped for this checkpoint. Database volumes and artifacts were not removed. Confirm database status on resume rather than assuming it is running.
- Initial native database setup used the example development configuration. `make setup` generates configuration for a fresh installation; do not replace existing database credentials without checking compatibility with the preserved volume.
- Docker Desktop on this machine is old (4.16.2 / Engine 20.10.22 / Compose 2.15.1). Public image builds previously stalled in a credential helper. A temporary empty Docker config under `.tools/docker-config`, the existing Docker socket and `DOCKER_BUILDKIT=0` enabled classic builds without changing global Docker configuration.

For native development, inspect `.env.example` and use `make native-setup`, `make migrate`, `make api`, `make worker`, and `make web` as appropriate. The API, worker and web commands run in separate terminals. Use `SCHEDULE_ENABLED=false` while running controlled acceptance tests. `make test` includes browser tests and therefore requires healthy running services. The full Compose path (`make setup`, `make up`) still needs acceptance verification.
