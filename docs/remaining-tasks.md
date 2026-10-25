# Remaining work and delivery tracker

Last updated: 2026-10-25 (Asia/Tokyo). Development has resumed at the owner's request.

This is the active plan for completing the unfinished prototype. It combines the original local-build acceptance requirements, the [checkpoint handoff](handoff.md), and the 2026-10-25 code audit. Historical evidence remains in [verification.md](verification.md); it must not be mistaken for a current passing run.

## Delivery rules

1. Work on one main task at a time. Fetch `origin/main`, fast-forward local `main`, and create the task branch from that commit. Preserve unrelated work and existing research data.
2. Before implementing a task, create its GitHub parent issue and smaller issues with acceptance criteria. Planned identifiers below become links when the task starts. Implement smaller issues sequentially, with a focused commit for each. Include the issue number and verification evidence in the commit/issue record.
3. Run checks appropriate to the change. Use isolated databases, artifact directories, and acceptance instances. Never use the research archive as disposable test data. Do not delete immutable rows, reset research volumes, or use global Docker pruning.
4. Update this tracker with implementation status, issue links, evidence, and limitations. Open a PR against `main` when all smaller issues are implemented and applicable checks pass. The PR describes resulting behavior, validation, risks, and linked issues.
5. Comment `@codex review` on the PR and request an overall review, including the repository's research invariants. Inspect review submissions, inline threads, general comments, and checks. An acknowledgement, an empty review list, or elapsed time is not a clear completed review.
6. Address every actionable finding in focused commits and request another review of the changed head. Do not dismiss findings merely to unblock merging. A local second review supplements the requested GitHub bot review; it does not replace it.
7. Merge only when the latest PR head has a completed clear Codex review, no outstanding actionable findings, all applicable checks pass, and repository merge requirements are satisfied. Use an expected head SHA to avoid merging changes that were not reviewed. Preserve the smaller commits with a merge commit where supported.
8. Record the merge SHA and close the completed issues. Start the next branch from updated `main`. The following task's first tracker update records the previous merge; the final PR's review/merge outcome is recorded in its GitHub issue and final report, avoiding an endless documentation-only commit/review cycle.
9. If an external dependency blocks progress, record the exact blocker and the evidence. Do not mark it complete, bypass the review gate, or silently replace the requested workflow. Continue only independent preparation that respects the task sequence.

Review setup follows the [official Codex GitHub integration documentation](https://learn.chatgpt.com/docs/third-party/github). Repository-specific review guidance supplements tests; it cannot certify scientific predictive performance.

## Status definitions

| Status | Meaning |
| --- | --- |
| Planned | Scope and acceptance defined; implementation has not started. |
| In progress | Current task is being implemented on its branch. |
| Implemented | Smaller issue committed; verification evidence recorded; parent PR not yet merged. |
| In review | PR opened; review and checks are pending or findings are being addressed. |
| Merged | Required review and checks cleared; PR merged into `main`. |
| Blocked | A named dependency prevents completion; no claim of success. |

## Ordered task board

Dependencies below are in addition to the sequential merge rule. Branch names are planned until started.

| ID | Main task | Depends on | Status | Branch | GitHub / PR |
| --- | --- | --- | --- | --- | --- |
| T00 | Backlog, traceability, and review guidance | — | Merged | `chore/completion-plan` | [#1](https://github.com/tazwar9t62/YureAtlas/issues/1) / [PR #4](https://github.com/tazwar9t62/YureAtlas/pull/4) |
| T01 | Reliable isolated backend checks and CI foundation | T00 | Merged | `test/isolated-verification` | [#5](https://github.com/tazwar9t62/YureAtlas/issues/5) / [PR #10](https://github.com/tazwar9t62/YureAtlas/pull/10) |
| T02 | Catalogue revisions and import coverage correctness | T01 | Merged | `fix/catalogue-reconciliation` | [#11](https://github.com/tazwar9t62/YureAtlas/issues/11) / [PR #16](https://github.com/tazwar9t62/YureAtlas/pull/16) |
| T03 | Deterministic baseline comparisons and report scope | T02 | Merged | `fix/comparison-provenance` | [#17](https://github.com/tazwar9t62/YureAtlas/issues/17) / [PR #23](https://github.com/tazwar9t62/YureAtlas/pull/23) |
| T04 | Prospective timing, model partitions, and scientific checks | T02, T03 | In progress | `test/scientific-invariants` | [#24](https://github.com/tazwar9t62/YureAtlas/issues/24); PR pending |
| T05 | Worker scheduling, recovery, and operational readiness | T01, T04 | Planned | `fix/worker-recovery` | Not created |
| T06 | Complete native/Compose backups and persistent restores | T01, T05 | Planned | `fix/backup-restore` | Not created |
| T07 | Dashboard correctness, map rendering, and browser harness | T03, T05 | Planned | `fix/dashboard-acceptance` | Not created |
| T08 | Historical inspection, evaluation revisions, and cell evidence | T02, T03, T07 | Planned | `feat/research-inspection` | Not created |
| T09 | Catalogue diagnostics, event inspection, and import feedback | T02, T07 | Planned | `feat/catalogue-diagnostics` | Not created |
| T10 | Future experiment settings, scheduling UI, and first-run states | T05, T07, T09 | Planned | `feat/workbench-operations` | Not created |
| T11 | Clean installation, full acceptance, and operating report | T01–T10 | Planned | `docs/verified-local-release` | Not created |

## T00 — Backlog and delivery traceability

Problem: the repository documents a paused checkpoint, but has no active issue/PR completion ledger.

| Smaller issue | Acceptance | Status / commit |
| --- | --- | --- |
| T00.1 [#2](https://github.com/tazwar9t62/YureAtlas/issues/2): detailed backlog | Ordered tasks cover audit findings and unfinished brief requirements; each has issue-sized work, dependencies, and concrete checks; current and historical evidence are distinct. | Implemented in `e866d54` |
| T00.2 [#3](https://github.com/tazwar9t62/YureAtlas/issues/3): documentation navigation and review guidance | README, handoff, and build record link to this tracker; dated historical evidence is preserved; concise review rules protect immutable history, temporal validity, source separation, and honest status. | Implemented in `cea17c6` |

Completion gate: documentation links resolve, `git diff --check` passes, and the documentation PR receives the requested clear bot review before merging.

Review outcome: after the owner authorized the connector for only YureAtlas and completed GitHub identity confirmation, Codex [completed its review without findings](https://github.com/tazwar9t62/YureAtlas/pull/4#issuecomment-6393834647) of `4685422a927b2e5f3bd8e2c9cf47e5eefbf49188`. Documentation checks and the GitGuardian check passed; there were no outstanding review threads. PR #4 merged as `473515251b40559d4b88463370ab895ec1c1e7e3`.

## T01 — Reliable isolated backend checks and CI foundation

Problem: the latest checkpoint database suite failed during setup/cleanup; test commands mix different runtimes, browser tests target the main app by default, and CI is absent.

| Smaller issue | Status / evidence |
| --- | --- |
| T01.1 [#6](https://github.com/tazwar9t62/YureAtlas/issues/6): environment diagnosis | Implemented in `43ca83a`: fresh isolated baseline 19 passed; [diagnosis](verification.md#2026-10-25-database-diagnosis-t011) |
| T01.2 [#7](https://github.com/tazwar9t62/YureAtlas/issues/7): owned isolation and cleanup | Implemented in `62b815b`: 47 unit checks and all 4 PostGIS checks passed; ownership, failure and real descendant-process regressions included |
| T01.3 [#8](https://github.com/tazwar9t62/YureAtlas/issues/8): independent verification commands | Implemented in `e9a4139`: lint, 47 unit + 4 PostGIS checks, TypeScript and read-only contracts passed; Webpack production build passed; local Turbopack worker-port restriction remains |
| T01.4 [#9](https://github.com/tazwar9t62/YureAtlas/issues/9): isolated CI | Merged in PR #10: [CI passed](https://github.com/tazwar9t62/YureAtlas/actions/runs/40166584195), including the default production build |

- **T01.1 — Diagnose the database environment.** Check the installed Docker daemon and PostGIS health, resource limits, and preserved volumes. Start the existing Docker application if needed. Use a uniquely named disposable database to reproduce setup failures; inspect server logs before changing limits. Record the cause or precise environment limitation. Preserve the primary database and artifacts.
- **T01.2 — Harden test isolation and cleanup.** Use the running supported Python interpreter, explicit database/artifact ownership, and cleanup that preserves the original failure. Cover migration/test/cleanup failure paths. Remove only resources created by the current test run; report any remnants.
- **T01.3 — Split verification commands and check the contract.** Provide independent lint, unit, integration, typecheck, build, and contract-drift commands. Use pinned Node/pnpm/Python toolchains. Fix the known migration import-order lint error. Contract checks must not silently rewrite tracked output.
- **T01.4 — Add CI for these checks.** Run the same supported commands with isolated PostGIS on PRs. Pin dependencies/actions appropriately, bound runtimes, minimize permissions, and retain useful failure logs. Browser CI is completed with T07 once its isolated harness exists.

Acceptance: all existing backend tests pass against disposable PostGIS; setup failures leave research state untouched; unit checks run without a database; TypeScript/build/contract results are explicit; CI passes for the PR. A skipped integration test is not a passing integration test.

## T02 — Catalogue revisions and import coverage

Problem: eligibility filters can discard disqualifying updates before persistence, leaving older earthquake positives authoritative. JMA policy exclusions also make otherwise useful official files entirely partial.

| Smaller issue | Status / evidence |
| --- | --- |
| T02.1 [#12](https://github.com/tazwar9t62/YureAtlas/issues/12): corrections before eligibility filtering | Implemented in `027a433`: 67 unit and all 11 PostGIS checks passed; lint and read-only contracts passed |
| T02.2 [#13](https://github.com/tazwar9t62/YureAtlas/issues/13): known-ID USGS reconciliation | Implemented in `e6676d0`: 136 unit and all 14 PostGIS checks passed; lint and read-only contracts passed |
| T02.3 [#14](https://github.com/tazwar9t62/YureAtlas/issues/14): JMA exclusions and import identity | Implemented in `49805fa`: 222 unit and all 31 PostGIS checks, 3 isolated mocked form tests, lint/types/contracts and Webpack build passed |
| T02.4 [#15](https://github.com/tazwar9t62/YureAtlas/issues/15): append-only correction acceptance | Implemented in `eb6924a`: 239 unit + all 49 PostGIS checks, populated002→head migration checks, lint/types/contracts/Actionlint and bounded USGS/JMA smoke passed; [scope/evidence](verification.md#2026-10-25-catalogue-correction-acceptance-t024) |

- **T02.1 — Preserve known-event corrections.** Separate discovery eligibility from revision persistence for local imports and feeds. Save authoritative updates to known IDs even when magnitude/location/type changes disqualify them; choose the latest revision before applying experiment filters. Do not infer deletion from absence.
- **T02.2 — Reconcile known USGS IDs.** Confirm current official event-service semantics and use bounded requests that retrieve changes beyond discovery magnitude/domain/time filters. Handle explicit deletions and malformed/failed responses conservatively. Record raw evidence, receipt times, request limits, and failures.
- **T02.3 — Classify JMA exclusions and validate import identity.** Separate demonstrably non-target policy exclusions from malformed or ambiguous target records. Preserve counts/reasons; retain partial coverage where usable negatives cannot be established. Document stable namespaces, reordered-file limitations, and canonical CSV mapping.
- **T02.4 — Verify append-only correction behavior.** Test downgrade, relocation, moved origin time, deletion, out-of-order update, duplicate import, failed query, and an omitted record. Re-evaluation must append a changed observation version while earlier snapshots and issued probabilities remain unchanged.

Acceptance: M4.2 → M2.8 and in-domain → out-of-domain corrections no longer leave stale positives; repeated delivery remains idempotent; no absent record is fabricated as deleted; known-safe JMA exclusions do not hide valid coverage; ambiguous data never creates negative labels. Run mocked-provider and PostGIS checks plus a separately recorded bounded real-source smoke test.

## T03 — Baseline comparisons and report scope

Problem: same-date historical-rate versions overwrite each other in arbitrary row order; exports ignore the selected period.

| Smaller issue | Status / evidence |
| --- | --- |
| T03.1 [#18](https://github.com/tazwar9t62/YureAtlas/issues/18): deterministic compatible references | Implemented in `c5a5c4d`: 262 unit + all 51 PostGIS checks, lint/types/contracts passed |
| T03.2 [#19](https://github.com/tazwar9t62/YureAtlas/issues/19): exact cohort scores and uncertainty | Implemented in `3dc8dea`: 299 unit + all 51 PostGIS checks, lint/types/contracts passed |
| T03.3 [#20](https://github.com/tazwar9t62/YureAtlas/issues/20): report filters and exports | Implemented in `d7828c7`: 329 unit + all 63 PostGIS checks, lint/types/regenerated contracts passed |
| T03.4 [#21](https://github.com/tazwar9t62/YureAtlas/issues/21): visible provenance and aligned scope | Implemented in `ef455d6`: 6 reporting and 3 import mocked browser cases, lint/types/contracts and Webpack build passed; existing map worker failure remains T07.3 |
| T03.5 [#22](https://github.com/tazwar9t62/YureAtlas/issues/22): bounded rejected-reference diagnostics | Implemented in `345f72e`: 335 unit + all 63 PostGIS tests, 10 isolated production browser mocks, lint/types/contracts and Webpack build passed |

- **T03.1 — Choose compatible references deterministically.** Define a stable reference-selection policy for identical experiment/window/mode/grid/observation eligibility. Return reference model/forecast IDs and explicit exclusion reasons. Avoid silently switching reference models with query ordering.
- **T03.2 — Preserve exact aggregate semantics.** Test row-order permutations, multiple compatible/incompatible baseline versions, unavailable observations, pooled denominators, and uncertainty prerequisites. No match yields N/A with a reason.
- **T03.3 — Align filters and exports.** Add bounded date-range filtering and consistent period/mode export parameters. Use half-open UTC intervals derived from JST dates. Keep candidate and reference cohorts aligned and include provenance in exported reports.
- **T03.4 — Expose reference provenance.** Show selected baseline identity, matched/excluded windows and reasons in the comparison UI. Distinguish selected-window calibration from period-level metrics and preserve the same scope in downloads.
- **T03.5 — Bound derived diagnostic output.** Limit rejected-reference identity examples while preserving total/omitted counts and complete reason summaries. Continue deterministic selection past the example cap and verify worst-case output size.

Acceptance: swapping database row order does not change pairs or scores; hand-calculated aggregates match; displayed and exported period/mode/counts agree; incompatible observations are never paired. Refresh and verify generated API types.

## T04 — Temporal and scientific invariants

Problem: the test named for late prospective publication currently fails earlier on synthetic/real mode mismatch. Actual training partitions, availability, and ETAS likelihood need stronger independent checks.

| Smaller issue | Status / evidence |
| --- | --- |
| T04.1 [#25](https://github.com/tazwar9t62/YureAtlas/issues/25): prospective receipt and publication boundaries | Implemented in `80a7a98`: 335 unit + all 95 PostGIS tests, populated migrations, lint/types/contracts passed |
| T04.2 [#26](https://github.com/tazwar9t62/YureAtlas/issues/26): fitted windows and saved metadata | Implemented in `3c777d7`: 335 unit + all 102 PostGIS tests, lint/types/contracts passed |
| T04.3 [#27](https://github.com/tazwar9t62/YureAtlas/issues/27): chronological training and artifact round trips | Implemented; 402 unit + all 105 PostGIS tests, lint/types/contracts passed; commit recorded in the issue |
| T04.4 [#28](https://github.com/tazwar9t62/YureAtlas/issues/28): ETAS mathematics, simulation and failure paths | Planned |
| T04.5 [#29](https://github.com/tazwar9t62/YureAtlas/issues/29): scientific scope and evidence | Planned |

- **T04.1 — Test actual prospective boundaries.** Require eligible receipt-time snapshots and coverage as of the declared input cutoff; reject unavailable artifacts, invalid future cutoffs, late starts, and computations crossing the publication deadline. A successful retry must not duplicate publication.
- **T04.2 — Align fitted windows and saved metadata.** Check rate warmup/fitting boundaries, ETAS history truncation versus configured fit duration, ML tuning, separate calibration, and hybrid weights. Metadata must describe the data actually used.
- **T04.3 — Exercise chronological training/backtests.** Use bounded deterministic fixtures; modify untouched test-period outcomes and verify fitted parameters/predictions do not change. Ensure each full target window belongs to one partition and folds fit before later testing blocks. Verify artifact round trips and explicit insufficient-data outcomes.
- **T04.4 — Validate the declared ETAS approximation.** Use primary scientific references and independently calculated likelihood/integral fixtures, nonzero-triggering simulations, observed-parent/unknown-gap boundaries, fixed-seed reproducibility, and numerical tolerances. Check stability/resource/nonconvergence failures publish no partial forecast. Compare with a suitable trusted benchmark if its representation and license permit meaningful comparison.
- **T04.5 — Record scientific scope honestly.** Update model notes with actual evidence, differences from reference methods, Monte Carlo resolution, and any remaining external validation. Local mathematical tests or synthetic scores do not establish real-world predictive superiority.

Acceptance: prospective receipt/coverage/deadline checks exercise the intended code paths; held-out outcomes cannot affect fitting or selection; archived windows match execution; ETAS fixture results and tolerances are reproducible. Independent scientific validation remains explicitly outstanding unless it is actually performed; it cannot be closed by renaming software tests.

## T05 — Worker recovery and readiness

Problem: restarting after 23:40 but before midnight skips a still-eligible daily forecast; missed triggers, cancellation, and readiness lack full acceptance coverage.

- **T05.1 — Recover eligible daily triggers.** Test restart at 23:39, 23:40, 23:45, 23:59, and midnight JST. Queue a still-valid upcoming window once; mark expired windows missed without backdating.
- **T05.2 — Reconcile sleep and scheduler misfires.** Reconcile clock jumps/overdue decisions during operation as well as startup, with unique logical keys. Keep ingestion/evaluation catch-up separate from prospective issuance and respect disabled scheduling.
- **T05.3 — Verify leases, ownership, retries, and cancellation.** Exercise queued/running cancellation, process death, lease expiry, retry exhaustion, and competing workers in isolated PostgreSQL. Preserve publication idempotence and useful progress/error states.
- **T05.4 — Make effective scheduling inspectable.** Add validated persisted local schedule settings where needed by the brief, effective enablement, next run times, and promotion audit/current selection. Define configuration precedence so restore-disabled scheduling remains disabled.
- **T05.5 — Report actionable readiness.** Check the expected schema revision, database availability, artifact read/write access, worker heartbeat, and appropriate source readiness. Return structured reasons/status codes; liveness remains lightweight.

Acceptance: no recovered or duplicated job publishes a late forecast; missed deadlines remain visible; cancellation/recovery tests pass; effective settings survive restart; stale workers are reported; readiness cannot claim healthy operation with an incompatible schema or unusable artifact directory.

## T06 — Backup and restore completeness

Problem: native artifacts are separate from the Compose volume, restore settings only live in a subprocess environment, and end-to-end recoverability has not been verified.

- **T06.1 — Make backup storage explicit.** Support the actual native and Compose layouts without destructive synchronization. Define consistent snapshot ordering/quiescence and inventory all referenced raw/model/feature/Parquet/dependency/import artifacts, including nested manifests. Exclude temporary files; verify immutable hashes.
- **T06.2 — Harden orchestration and failures.** Support documented Compose JSON formats, preserve writer state, restart only previously running writers, and report failure without producing a misleading successful manifest. Cover corrupt/missing artifacts, path traversal, command failures, and interrupted snapshots.
- **T06.3 — Persist restored-instance configuration.** Write a target-specific environment/override with separate ports/project/volumes and schedules disabled; provide its exact restart command. Validate manifest/schema/tool compatibility, database readiness and artifact references before declaring success. Never overwrite an existing installation.
- **T06.4 — Exercise complete restoration.** Restore into a uniquely named disposable installation, compare database/provenance and artifact hashes, verify archive immutability, load a saved model, execute a worker job, and restart using persisted settings.

Acceptance: every database artifact reference resolves after restore; primary research state is unchanged; restored ports/schedule settings persist; corrupt backups fail clearly; a restored installation performs a meaningful operation. Record database-plus-artifact evidence, not only tar round-trip tests.

## T07 — Dashboard correctness and browser acceptance

Problem: selection races can show stale evidence, dates mix UTC/JST, chart colors run out, and historical demo/mobile/offline tests failed.

- **T07.1 — Correct asynchronous selection state.** Clear all forecast-dependent evidence when no matching forecast exists. Guard cell/map/report/baseline responses against stale selections; handle all terminal job states including missed forecasts. Use focused delayed-response browser fixtures.
- **T07.2 — Make dates and model display consistent.** Format history in Asia/Tokyo, use stable colors for arbitrary model-version counts, and preserve clear missing/unknown values. Verify midnight and month/year boundaries.
- **T07.3 — Reproduce and fix map failures.** Inspect actual production browser worker/CSS/WebGL errors, dimensions, polygons, events, comparison mode, resize, and tab changes. Ensure locally served basemap/workers render with external requests blocked and a useful numeric fallback remains if WebGL fails.
- **T07.4 — Own an isolated browser stack.** Provide a harness with its own database, artifacts, API, worker, frontend/ports, process ownership and interruption cleanup. Browser tests cannot silently target the research app. Each test prepares/selects its own required demo state and works independently.
- **T07.5 — Complete browser CI and visual checks.** Run production-build desktop/mobile/offline flows, cell/actual-event inspection, computed scores, and job completion/failure. Capture useful traces/screenshots only on failure and inspect layouts at desktop and 390px widths.

Acceptance: stale requests never replace current evidence; JST labels are consistent; six model versions have defined colors; real map polygons/events are visible and interactive; essential tests pass individually and together without external runtime requests or research-data mutations.

## T08 — Historical inspection and forecast evidence

Problem: older forecasts are capped, evaluation revisions are only raw JSON, and cell details omit forecast-time features and prior observed outcomes.

- **T08.1 — Add bounded history navigation.** Implement stable pagination and explicit date/model/mode filters that reach history beyond 1,000 runs. Preserve baseline availability and stable selection when filters change.
- **T08.2 — Select observation revisions consistently.** Validate an explicit evaluation ID belongs to the selected forecast; apply it to map, cell outcomes, metrics, calibration and exports. For multi-window aggregates define latest/as-of semantics rather than equating unrelated revision numbers.
- **T08.3 — Expose archived cell evidence.** Derive relevant feature values and missing indicators from the saved forecast input snapshot/schema, with units, expected count where applicable, and prior outcomes linked to their evaluation versions. Never reconstruct original evidence from today's latest catalogue.
- **T08.4 — Make candidate/baseline inspection unambiguous.** Clicking each map must load that map's model/forecast evidence. Provide inspectable missed/false-positive cells and keyboard-accessible revision/details controls.

Acceptance: later catalogue changes cannot alter original forecast inputs/probabilities; selecting old evaluations reproduces their overlays and scores; foreign evaluation IDs fail; deep history is reachable; comparison clicks identify the correct model. Contract and browser regressions pass.

## T09 — Catalogue diagnostics and imports

Problem: the brief's magnitude-frequency, gap/delay/source-change diagnostics and event/revision browser are missing; imports expose limited mapping/progress feedback.

- **T09.1 — Add bounded source diagnostics.** Provide date/source-scoped magnitude bins, covered/unknown intervals, receipt/update lag summaries, rejection counts and source/magnitude-type changes. Distinguish quiet covered days from gaps and historical backfill receipt lag from provider reporting latency.
- **T09.2 — Add event/revision inspection and export.** Implement bounded time/magnitude/bbox filters, stable pagination, provider/experiment separation, revision chronology, and provenance-bearing filtered exports. Push bounded queries into appropriate database paths rather than reading entire catalogues unnecessarily.
- **T09.3 — Expose CSV mapping and import reports.** Provide validated canonical/custom column mapping, bounded file/date validation, clear accepted/excluded/rejected reasons, progress, resulting experiment/job navigation, and retry/idempotency behavior.
- **T09.4 — Present diagnostics and recovery actions.** Show actual counts/units/periods in Data & operations with empty, partial, stale, failed, and loading states. Offer relevant import/refresh actions and retain source attribution.

Acceptance: fixture bins/delays/gaps are exact; null metadata remains unknown; corrected authoritative revisions drive diagnostics; filters/export rows agree; failed imports cannot establish complete coverage; desktop/mobile controls are usable. No retrieval diagnostic claims physical catalogue completeness.

## T10 — Future settings and first-run operations

Problem: supported experiment settings lack an interface; labels/bounds are hardcoded; scheduling and real first-run readiness are not actionable enough.

- **T10.1 — Create immutable experiment versions.** Add create/version-from-existing controls for supported provider, envelope/grid, magnitude and color thresholds. Show fixed v1 timing policies as fixed. Changing settings creates a new experiment and cannot mutate prior forecasts or reuse incompatible models.
- **T10.2 — Derive presentation from experiment settings.** Use actual map bounds, grid sizes, target labels and precise threshold legends. Test a 100 km grid and fractional percentage thresholds, invalid envelopes and threshold ordering.
- **T10.3 — Connect operational controls.** Show effective persisted schedule state, next JST runs, missed history, current promotion and rationale. Poll/timestamp worker/source readiness so stopped services cannot appear indefinitely healthy. Use the T05 API rather than hardcoded schedule text.
- **T10.4 — Finish first-run and mobile states.** Guide real setup through coverage, model availability and the next eligible future window. Explain no-data/unknown-observation states without invented zero probabilities. Provide collapsible mobile filters and keyboard-accessible definitions/details.

Acceptance: old experiment hashes and forecasts are unchanged after new settings; schedule UI agrees with effective worker behavior across restart; fresh/empty/failed/partial/completed states are actionable; essential mobile controls remain available without horizontal overflow. Demo and real scopes remain clearly separate.

## T11 — Clean installation, acceptance, and handoff

- **T11.1 — Finish setup and packaging.** Validate supported tools/configuration safely, use pinned dependencies, rebuild current Python/web images, and exercise fresh four-service Compose setup/migrations on a separate project. Preserve existing credentials and research volumes. Verify appropriate macOS/Linux instructions and report untested architectures honestly.
- **T11.2 — Run full acceptance.** Complete fresh first-run/demo, offline rendering, real USGS bounded import, JMA import/reporting, actual baseline/ML/hybrid execution, ETAS available/unavailable paths, future publication/evaluation, scheduling/recovery, restart persistence, exports and backup/restore. Use deliberate bounded fixtures for expensive checks.
- **T11.3 — Complete operating documentation.** Expand README with exact setup/demo/real/daily/shutdown/restart/troubleshooting commands. Document native versus Compose artifact storage, schedule precedence, promotion, source limitations, import formats, backup target selection, persistent restore commands and recovery. Update architecture/protocol/model/source docs to match the implementation.
- **T11.4 — Publish the final evidence report.** Record commands, environment, results, issue/commit/PR/review/merge links, screenshots where useful, remaining external limitations, and reproduction instructions in `docs/completion-report.md`. Refresh contract checks and ensure a clean tracked tree.

Pre-merge acceptance: T01–T10 are verified and merged through the requested review loop; T11 implementation and required checks are complete and green; no historical failure is silently relabeled as passed; unresolved environment or scientific dependencies are explicitly named. T11's own review and merge are recorded afterward as described in delivery rule 8. Do not state that all work is complete while required tasks are blocked.

## Verification baseline

| Date / scope | Evidence | Interpretation |
| --- | --- | --- |
| 2026-09-29 checkpoint | Backend 15 passed / 4 setup errors; all three browser tests failed; complete installation and restore unverified. | Historical failures, retained in verification.md. |
| 2026-10-25 audit | Backend unit run: 15 passed / 4 integration tests deliberately skipped. | Unit evidence only; database invariants not re-established. |
| 2026-10-25 audit | TypeScript passed; generated OpenAPI matched all 26 current paths. | Current static/contract evidence. |
| 2026-10-25 audit | Ruff reported one import-order issue in migration env.py. | Known deterministic cleanup for T01. |
| 2026-10-25 audit | Production build failed because Turbopack CSS processing could not bind a local port, including an escalated attempt. Docker daemon was unreachable on the permitted status check. | Environment limitations to diagnose; not a demonstrated application compile error or resolved DB failure. |
| 2026-10-25 audit | Mocked reproduction confirmed discarded below-threshold revisions, baseline row-order dependence, and missed restart catch-up. | Confirmed regression scenarios to convert into committed checks. |

## Execution ledger

| Date | Task / event | Evidence / next action |
| --- | --- | --- |
| 2026-10-25 | Owner authorized the sequential issue/branch/commit/PR/review/merge loop. | No repeated merge approval needed once the requested review/check conditions hold. |
| 2026-10-25 | T00 started from clean main at `8ed2323c318be9f20986358acc93076139bd8539`. | Remote main matched; branch `chore/completion-plan`; parent issue #1. |
| 2026-10-25 | GitHub connector issue creation returned integration HTTP 403. | Existing authenticated GitHub CLI provides the authorized write fallback; connector reads remain usable. |
| 2026-10-25 | T00.1 implemented: detailed backlog, status definitions, review gates, and baseline evidence. | Relative links and all 12 main-task sections checked; whitespace check passed. |
| 2026-10-25 | T00.2 implemented: README/handoff/build record navigation and focused repository review guidance. | Documentation-only changes; current application verification remains at the baseline above. PR/review pending. |
| 2026-10-25 | T00 opened as PR #4 with separate issue commits. | Supplementary local review found no blocking issue; clarified T11's pre-merge versus post-merge reporting. GitHub Codex review is still required. |
| 2026-10-25 | Requested overall Codex review on PR #4 at `1f880c3`; no acknowledgement/review observed. | YureAtlas was absent from the account's Code Review repository search. GitHub app authorization inspection reached owner authentication. User setup requested; task remains unmerged and T01 has not started. |
| 2026-10-25 | Owner confirmed YureAtlas review access is enabled. | T00 returned to In review; request and verify a fresh review of the updated head before merge. |
| 2026-10-25 | Completed the owner-authorized YureAtlas-only connector installation and verified the repository appears in Code Review settings. | GitHub identity confirmation was performed by the owner. Request a fresh overall review after this tracker update; no automatic-review or credit-spending preferences were changed. |

Each subsequent task updates this ledger and the task board before its PR. Exact current-head review and merge evidence also lives in the linked GitHub PR/issue to avoid self-referential commit hashes in this file.

T01 started from merged main `473515251b40559d4b88463370ab895ec1c1e7e3` on `test/isolated-verification`; parent #5 and smaller issues #6–#9 were created before implementation. Starting the installed Docker Desktop restored daemon access without resetting volumes. The existing disposable backend harness passed all 19 tests and cleaned its database; the historical failed-run database was preserved. Historical failure cause remains unproven.

T01.2 verifies the effective database and ownership before migrations, rejects unsafe direct integration invocation, uses the current interpreter and owned temporary artifacts, revalidates resource identity before deletion, and terminates/reaps child processes before cleanup. Primary errors survive independent cleanup failures. Verification: 47 unit checks passed (including 32 new harness regressions), all 4 PostGIS checks passed, and targeted Ruff/whitespace checks passed. SIGKILL or machine loss cannot guarantee cleanup; printed resource names support later inspection. T01.3 will wire separate unit and integration commands into the default test target.

T01.3 adds independent documented Make targets, supported toolchain checks, read-only OpenAPI/TypeScript drift detection, and the migration lint fix. `YUREATLAS_INTEGRATION=1 make test` correctly forced unit isolation, passed 47 unit checks and then all 4 owned PostGIS checks. Lint, TypeScript and both contract checks passed; deliberately stale temporary contracts were rejected without rewriting them, and tracked contract hashes were unchanged. The default Turbopack build remains blocked locally by an operating-system port-bind denial even with escalation; the documented Webpack production build passed compilation, types, prerendering and standalone output. T01.4 will verify the default build on GitHub's runner. Browser behavior is not claimed by these build checks.

T01.4 adds PR/main CI using the same commands, a digest-pinned official PostGIS service, exact tool versions, full-commit action pins, read-only repository permissions, a 20-minute job bound, and seven-day failure logs. Unit checks use an unreachable database URL. Actionlint 1.7.12 passed. The PR's actual CI result and completed Codex review remain required before merge; this workflow does not yet run the T07 browser checks.

T01 opened as [PR #10](https://github.com/tazwar9t62/YureAtlas/pull/10). Supplementary review confirmed the workflow and read-only contract behavior; a clean-checkout TypeScript simulation also passed without generated `.next` files. At opening, overall Codex review and CI were pending and T02 had not started.

T01 subsequently cleared [Codex review without findings](https://github.com/tazwar9t62/YureAtlas/pull/10#issuecomment-6394059080) at `b81844ab533361733f3d14d952f5caf7dac1c36b` and [CI](https://github.com/tazwar9t62/YureAtlas/actions/runs/40166584195): 47 unit checks, all 4 PostGIS checks, lint, TypeScript, contracts and the default production build passed. GitGuardian's neutral alert concerned only the disposable CI database password; [its test-only scope is documented](https://github.com/tazwar9t62/YureAtlas/pull/10#issuecomment-6394055058). With no outstanding review threads, PR #10 merged as `38ae8d76d3a5d91d9acaec70eeb99fcaecc289bb`.

T02 started from that updated main on `fix/catalogue-reconciliation`. Parent #11 and smaller issues #12–#15 were created before implementation. Work starts with preserving known-event corrections and applying eligibility only after authoritative revision selection.

T02.1 adds immutable event-type/exclusion evidence with legacy-compatible hashes, preserves valid known-ID corrections before discovery filtering, and applies one eligibility policy after authoritative selection for model inputs, evaluation, maps and event lists. Known-ID lookups are chunked. Sources without provider update times use receipt ordering: A → B → A appends a new revision, and repeating the latest content deduplicates; a later delivery of old content cannot be distinguished from an intentional reversal. Conflicting same-event/same-version rows in one delivery are rejected before writes. The additive migration preserves earlier revisions and snapshots. Verification: 67 unit checks and all 11 owned PostGIS checks passed; lint, whitespace and read-only API contracts passed. JMA exclusion coverage remains conservative until T02.3, and filtered USGS queries still need the T02.2 known-ID refresh.

T02.2 reconciles known USGS IDs independently of discovery filters, including earlier event positions and newly persisted feed events. Per-interval ID reads and per-job physical request/time limits are explicit; missing, mismatched, malformed, failed or pending responses cannot fabricate deletions or complete coverage. Raw requests, complete/partial response bytes, headers and receipt times remain auditable. Async deadlines cover headers and body reception; tests exercise actual stalled-header/body cancellation, retries, strict numeric fields and primary-error preservation. Coverage now overlays authoritative attempts as of the receipt cutoff, so a newer incomplete attempt invalidates its overlap until successful retrieval; legacy feed/demo correction receipts remain supplemental. Verification: 136 unit checks and all 14 owned PostGIS checks passed; lint, whitespace and read-only contracts passed. Independent review findings were fixed; a separate 10,000-case interval oracle and compressed-response check also passed. No live-source completeness or scientific-performance claim is made; bounded real-source smoke and populated-schema upgrade evidence remain T02.4.

T02.3 separates explicit JMA catalogue collections from immutable fixed-width file namespaces and stable CSV event IDs. Strict parsing distinguishes usable rows, recognized exclusions and ambiguous/invalid records, with bounded physical-line reports. Complete coverage requires an explicit declaration and no blockers. Local imports commit verified raw identity, parser/mapping/interval/declaration, revisions and an immutable receipt/manifest together under the source lock; changed fixed-width files require a new collection, and legacy archives remain readable without being adopted. The multipart API includes effective parameters in job identity and supports explicit retries; the form isolates collections and resets its opt-in coverage declaration when scope changes. Independent review caught and resolved an unlocked known-ID filtering race: local and USGS selection now runs inside the persistence transaction, and a real database barrier test proves a waiting correction survives a concurrent first insertion. Current verification: 222 unit checks, all 31 owned PostGIS checks, lint, TypeScript, regenerated/read-only API contracts and the Webpack production build passed. Three import-form browser tests passed against a separate temporary frontend at port 3197 with every API request mocked and its backend URL deliberately unreachable; this is form behavior evidence, not full browser acceptance. The temporary server stopped afterward. The known local Turbopack worker-port restriction remains; default-build CI is required on the PR. T02.4 also must close the separately identified race between simultaneous USGS attempts, where a stale captured known-ID set or interleaved supplemental write can outlive another attempt's complete receipt; the parent PR cannot pass until this is resolved.

T02.4 closes that USGS attempt race with per-source, nonblocking session locks and write-time ownership checks. Network work holds no open database transaction, and a disconnected old owner cannot publish after a successor. Eight real concurrency cases cover isolation, cancellation/error cleanup and lost-session fencing. Ten end-to-end correction/reconciliation cases preserve earlier snapshot membership, probabilities, evaluations and real model/feature bytes while observations change or remain unknown as appropriate. A separate owned migration scenario verifies populated002→003→004→head preservation, legacy hashes/deduplication, appended corrections, constraints and repeat-upgrade idempotence. Verification: 239 unit checks and all 49 PostGIS checks, migrations, lint, TypeScript, read-only contracts and Actionlint passed. Live USGS exposed optional query metadata count; independent preflight matching now handles it while feeds remain strict. Live official JMA exposed 87 safe agency-exclusion format cases, now covered by actual-file fixtures. Repeat live smoke passed: five USGS records and one exact-ID response; all 257,020 JMA rows parsed into 246,944 selected, 103 safe exclusions and 9,973 missing-magnitude blockers. The JMA file therefore remains partial. Details, hashes, reproducible commands and limitations are in [verification](verification.md#2026-10-25-catalogue-correction-acceptance-t024). All smaller issues are implemented; parent PR review and CI are next.

T02 opened as [PR #16](https://github.com/tazwar9t62/YureAtlas/pull/16) with separate implementation commits for #12–#15. Supplementary independent reviews are clear; the requested overall Codex review and exact-head CI remain pending. T03 implementation has not started.

T02 subsequently cleared [Codex review without findings](https://github.com/tazwar9t62/YureAtlas/pull/16#issuecomment-6394696919) at `b6693d44193bc09e5ae6bbd31fe7dca0f42fd932`. [CI passed](https://github.com/tazwar9t62/YureAtlas/actions/runs/40171345947), including 239 unit checks, all 49 PostGIS checks, populated migrations, lint, TypeScript, contracts and the default production build; GitGuardian also passed. No actionable review threads remained. PR #16 merged as `40497f22cf6b5e781567343514ac40dc229f14ff`, and issues #11–#15 closed. T03 starts from that updated main on `fix/comparison-provenance`; parent #17 and smaller issues #18–#21 were created before implementation.

T03 opened as [PR #23](https://github.com/tazwar9t62/YureAtlas/pull/23) with separate commits for #18–#21 and the final-review follow-up #22, which was created before its implementation. The report now uses deterministic compatible references, matched pooled scores/uncertainty, bounded coherent scopes and exact displayed-report downloads. Verification: 335 unit tests, all 63 PostGIS tests, 10 isolated production browser mocks, lint, TypeScript, read-only contracts and Webpack build passed. Supplementary reviews are clear. The local Turbopack worker-port restriction and existing development map worker failure remain explicitly documented; full-stack/map acceptance stays in T07/T11. Requested overall Codex review and exact-head CI are pending; T04 implementation has not started.

T03 subsequently cleared [Codex review without findings](https://github.com/tazwar9t62/YureAtlas/pull/23#issuecomment-6395351875) at `50e0605bcf712803750a05dfda23d8d14f172a75`. [CI passed](https://github.com/tazwar9t62/YureAtlas/actions/runs/40176178580), including the default production build and populated migrations; GitGuardian also passed. No actionable review threads remained. PR #23 merged as `39062a3bc654a9512051d4ca5b02254e0728d654`, and issues #17–#22 closed. T04 starts from that updated main on `test/scientific-invariants`; parent #24 and smaller issues #25–#29 were created before implementation. Initial read-only preparation confirmed coverage uses issuance time instead of input cutoff, and the final application deadline check precedes lock waits and publication work. These are the first issue's targets.
