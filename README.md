# YureAtlas

**Forecast. Observe. Improve.** A local earthquake occurrence forecasting research workbench for the Japan region.

Experimental forecast — not an official warning.

**Status: development paused at an unfinished prototype checkpoint.** Core data/model workflows and the dashboard are implemented; browser acceptance, current database test failures, clean installation and backup/restore verification remain. See the [resume handoff](docs/handoff.md), [verification evidence](docs/verification.md), and [build record](docs/progress.md).

Stack: Next.js, FastAPI, PostgreSQL/PostGIS, NumPy/scikit-learn, separate durable worker. No cloud credentials, GPU, Redis, or paid maps.

Development prerequisites: Node 24 LTS, pnpm 10.33, Python 3.12 with uv, and Docker Compose. Full operating instructions and verification evidence are added as the milestones land.
