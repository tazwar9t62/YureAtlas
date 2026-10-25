# YureAtlas

**Forecast. Observe. Improve.** A local earthquake occurrence forecasting research workbench for the Japan region.

Experimental forecast — not an official warning.

**Status: completion work in progress; the application is still an unfinished prototype.** Core data/model workflows and the dashboard are implemented. Follow the [remaining-task tracker](docs/remaining-tasks.md) for the ordered issue/branch/PR plan, acceptance criteria, and current status. The [checkpoint handoff](docs/handoff.md), [verification evidence](docs/verification.md), and [build record](docs/progress.md) preserve the earlier results and known limitations.

Stack: Next.js, FastAPI, PostgreSQL/PostGIS, NumPy/scikit-learn, separate durable worker. No cloud credentials, GPU, Redis, or paid maps.

Development prerequisites: Node 24 LTS, pnpm 10.33, Python 3.12 with uv, and Docker Compose. Full operating instructions and verification evidence are added as the milestones land.

## Development checks

Use Python 3.12.12 (`.python-version`), Node 24.21.0 (`.nvmrc`) and pnpm 10.33.0 (`apps/web/package.json`). Run `make native-setup` to install the locked dependencies. Activate Node 24 before invoking Make; it does not load shell version-manager hooks. `PYTHON` and `PNPM` may override command locations, but version checks still apply. A local pinned pnpm under `.tools/pnpm/node_modules/pnpm/bin/pnpm.cjs` is detected automatically.

| Command | Scope |
| --- | --- |
| `make lint` | Python lint, without a database |
| `make test-unit` | Unit/scientific fixtures and test-harness failure checks, without a database |
| `make test-integration` | Creates, migrates and removes an owned temporary PostGIS database and artifact directory |
| `make test-migrations` | Seeds a separate owned schema002 archive and verifies forward upgrades preserve its rows, hashes, artifacts and constraints |
| `make test` | Both backend suites; does not target a running browser/research instance |
| `make typecheck` | TypeScript, without writing incremental build state |
| `make contract-check` | Checks OpenAPI and generated TypeScript drift without rewriting either file |
| `make build` | Production Next.js build |
| `make verify` | All checks above |
| `make contract` | Intentionally regenerates both contracts; review and commit the diff |

Integration checks use `DATABASE_URL` (or the repository `.env`) only to locate PostgreSQL and obtain credentials. The role must create databases and install PostGIS. The harness connects administratively to `postgres`, creates a unique test database, verifies its ownership before migration and cleanup, and never runs tests against the configured research database. Do not set `YUREATLAS_INTEGRATION=1` manually. Cleanup failures return failure and report resource names; machine loss or SIGKILL can leave resources for inspection. Browser acceptance remains tracked separately under T07 until its isolated stack is implemented.

Some restricted macOS execution environments deny Turbopack's local PostCSS worker port. `make build NEXT_BUILD_ARGS=--webpack` uses Next.js's supported alternative bundler for diagnosis; record which build ran. This does not establish that the default build passed.

## Research protocol and evidence

The [experiment protocol](docs/experiment-protocol.md) defines receipt cutoffs, publication deadlines, chronological partitions and report semantics. [Model notes](docs/model-notes.md) describe actual fitting/selection/calibration roles and saved metadata. [ETAS numerical verification](docs/etas-validation.md) gives independent fixtures, reproduction commands, tolerances and the external implementation assessment. The [dated verification record](docs/verification.md) distinguishes completed software checks from remaining operational acceptance and external scientific validation. Synthetic or numerical test results do not establish predictive skill.

The [scheduling policy](docs/scheduling.md) documents JST run times, bounded recovery after sleep or restart, missed forecast decisions, and disabled scheduling.
