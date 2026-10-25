# YureAtlas

**Forecast. Observe. Improve.** A local earthquake occurrence forecasting research workbench for the Japan region.

Experimental forecast — not an official warning.

**Status: completion work in progress; the application is still an unfinished prototype.** Core data/model workflows and the dashboard are implemented. Follow the [remaining-task tracker](docs/remaining-tasks.md) for the ordered issue/branch/PR plan, acceptance criteria, and current status. The [checkpoint handoff](docs/handoff.md), [verification evidence](docs/verification.md), and [build record](docs/progress.md) preserve the earlier results and known limitations.

Stack: Next.js, FastAPI, PostgreSQL/PostGIS, NumPy/scikit-learn, separate durable worker. No cloud credentials, GPU, Redis, or paid maps.

Development prerequisites: Node 24 LTS, pnpm 10.33, Python 3.12 with uv, and Docker Compose. Full operating instructions and verification evidence are added as the milestones land.
