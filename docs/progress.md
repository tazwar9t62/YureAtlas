# Build record

Specification: the supplied YureAtlas full-stack local build brief. This project is an experimental occurrence-forecast workbench, not a warning system.

## Milestones

1. Infrastructure: local PostGIS, FastAPI, Next.js, schema, generated contract.
2. Data: deterministic projected grid, append-only revisions, USGS, JMA import, synthetic data.
3. Science loop: historical-rate model, immutable forecasts, versioned scores, event overlay.
4. Model lab: temporal ML, hybrid, fitted ETAS research pathway, walk-forward backtests.
5. Operations: durable worker, scheduling/deadlines, exports, consistent backups.
6. Acceptance: invariant tests, browser flow/layout, source smoke tests, clean setup documentation.

Each milestone is committed independently after relevant verification. Final execution evidence belongs in `docs/verification.md`.

## Initial inspection

- Empty project directory and empty GitHub repository; initialized local `main` and configured origin.
- macOS arm64, Node 24.14.1, pnpm 10.33.0, Python 3.12 available via Homebrew, running Docker Desktop.
- Use a multiarch PostgreSQL base with PostGIS installed in the image, avoiding an amd64-only PostGIS image on Apple Silicon.
- Keep prospective receipt-time semantics distinct from retrospective revised catalogues.
- Six bounded increments; no cloud services or deployment.
