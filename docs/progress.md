# Build record

Specification: the supplied YureAtlas full-stack local build brief. This project is an experimental occurrence-forecast workbench, not a warning system.

## Milestones

1. Infrastructure: local PostGIS, FastAPI, Next.js, schema, generated contract.
2. Data: deterministic projected grid, append-only revisions, USGS, JMA import, synthetic data.
3. Science loop: historical-rate model, immutable forecasts, versioned scores, event overlay.
4. Model lab: temporal ML, hybrid, fitted ETAS research pathway, walk-forward backtests.
5. Operations: durable worker, scheduling/deadlines, exports, consistent backups.
6. Acceptance: invariant tests, browser flow/layout, source smoke tests, clean setup documentation.

The first three increments were committed after relevant verification. Model/dashboard/operations work was saved as an unfinished checkpoint at the user's request. Completion work resumed on 2026-10-25 under the [remaining-task tracker](remaining-tasks.md). Historical execution evidence and known failures are recorded in `docs/verification.md`.

## Initial inspection

- Empty project directory and empty GitHub repository; initialized local `main` and configured origin.
- macOS arm64, Node 24.14.1, pnpm 10.33.0, Python 3.12 available via Homebrew, running Docker Desktop.
- Use a multiarch PostgreSQL base with PostGIS installed in the image, avoiding an amd64-only PostGIS image on Apple Silicon.
- Keep prospective receipt-time semantics distinct from retrospective revised catalogues.
- Six bounded increments; no cloud services or deployment.

## Delivered increments

- `f2893b4`: local full-stack foundation, frozen initial relational migration, generated OpenAPI contract, loopback request controls. Frontend production build and API boundary check passed.
- `728298d`: projected catalogue/grid layer, deterministic synthetic generator, USGS adapter, official JMA/CSV parser, offline basemap. Five grid/time/parser tests passed. Official h2023 file verified: 246,944 accepted records, 10,076 policy/validation rejections, 1,176 selected M4+ records.
- `43011cd`: immutable complete-grid publication, historical rate engine, proper probability scores, versioned evaluations and API reads. Database integration verified SQL mutation rejection, publication retry idempotence, revision preservation and lease recovery.
- In progress: trained ML/hybrid/ETAS, dashboard, operations, backup/restore, clean-install and browser acceptance. A full computed synthetic seven-day run has executed; four models each saved seven forecasts, with one missing-coverage day withheld. A real USGS one-day smoke import succeeded (3 records, no rejections).

## Paused checkpoint

On 2026-09-29 the user requested pushing the current changes and continuing later. Model lab, dashboard and operations implementation is saved together with known failures and unverified acceptance work. The project is not complete. See [the handoff](handoff.md) for the ordered resume plan and [verification evidence](verification.md) for the latest test results.

## Resumed completion work

On 2026-10-25 the user authorized completing the remaining work through sequential task branches from `main`, smaller GitHub issues with separate commits, and PRs reviewed by the Codex bot before merging. [The active tracker](remaining-tasks.md) records acceptance criteria, status, issue/PR links, and evidence. The historical checkpoint above remains unchanged as a record of what was actually verified then.
