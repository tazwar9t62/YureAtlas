# Repository guidance

YureAtlas is a local earthquake occurrence research workbench. Read `docs/remaining-tasks.md` before continuing its completion work and follow the task's linked acceptance criteria. Keep one main task in implementation at a time, commit smaller issues separately, and branch each new task from updated `main` after the previous PR has cleared the requested Codex review and merged. Update the tracker with actual evidence; do not equate implemented code with verified behavior.

Use isolated databases, artifact directories, ports, and Compose projects for tests. Preserve the existing research database and native artifacts. Read `apps/web/AGENTS.md` and the applicable installed Next.js documentation before editing frontend code.

## Code Review Rules

- Preserve source/experiment separation and append-only catalogue revisions, forecasts, and evaluations. Eligibility filtering must not discard a known event's correction or resurrect an older revision; an absent query result alone is not deletion evidence.
- Prospective features and coverage must respect the declared receipt-time cutoff, fitted artifacts must be available before issuance, and publication must finish before the target window starts. Training, calibration, tuning, and comparisons must use their declared chronological partitions and compatible observation cohorts.
- Backups and recovery must preserve every referenced artifact and leave the original installation untouched. Report consequential correctness issues, including actionable P2 findings, with concrete triggers; do not treat synthetic results, skipped checks, or incomplete external validation as successful scientific evidence.
