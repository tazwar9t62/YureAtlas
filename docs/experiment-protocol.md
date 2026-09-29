# Experiment protocol v1

## Target and domain

The target is a binary cell-window occurrence: at least one selected catalogue event at or above the experiment's target magnitude, initially 4.0. It does not predict shaking, damage, tsunami, or personal safety. The input threshold is initially 3.0. Provider, magnitude selection, geometry, thresholds, and evaluation policy are immutable experiment settings. Changes require a new experiment version.

The Japan-region envelope is `[122,156)` longitude and `[20,48)` latitude, including offshore and neighbouring regions. EPSG:6933 squares are 50,000 projected metres, clipped at the exact envelope; 3,953 cells are retained. Interior cells are about 2,500 km². Actual WGS84 ellipsoidal polygon areas are stored. Projected dimensions are not exact ground distances. IDs are projected integer column:row. Shared boundaries belong to the north/east cell (projection roundoff within 0.05 mm is snapped to the mathematical edge). The outer east/north edges are excluded. No post-event radius expansion is permitted.

## Timing and evidence

All stored times are aware UTC; display/scheduling uses Asia/Tokyo. A JST midnight starts at 15:00 UTC on the preceding date. Target windows are half-open `[start,end)` and exactly 24 hours. The v1 feature cutoff is exactly 30 minutes before the start. Scheduled next-day generation begins at 23:40 JST using the 23:30 cutoff; publication must finish before 00:00. A manual run starts 30 minutes after its cutoff, with the actual publication time recorded. A late scheduled run is marked missed, never backdated.

Prospective input snapshots select the latest eligible provider revision *received by the cutoff*, then filter origin times. Provider update time orders out-of-order revision deliveries; receipt time controls availability. Model/preprocessing/calibration artifacts must exist before issue time, with training cutoff no later than input cutoff. Training labels never include the target/test period. Inputs require successful recent retrieval covering the preceding day through at least cutoff minus 30 minutes, and a nonfailed source. This is a declared latency allowance, not proof of catalogue completeness. Unknown target coverage never supplies negative labels.

- `prospective`: actual future publication and receipt-time-restricted inputs.
- `retrospective_revised`: historical study using versions downloaded/revised later. Issue time remains the real current publication time.
- `synthetic_demo`: artificial, fixed-seed data with explicitly historical dates.
- `replay_asof`: reserved and rejected in v1; historical fitted artifacts and availability evidence would be necessary.

Modes are kept separate in API filters and comparisons. Forecast runs, individual probabilities, classifications, model metadata, snapshots, and evaluations are database protected append-only archives. Atomic publication verifies the exact retained-cell count and domain, then seals the run against later insertions.

## Chronological model development

Defaults: 180 fitting days, 30 tuning days, 30 calibration days, then 7 testing days; 90 warmup days are required. The CLI accepts larger partitions (e.g. 730/90/90/180) subject to a four-million-cell-day bound. Coarsen a *new* grid or reduce partitions if needed. Every cell on a date stays in the same partition. Full target windows are contained within one partition, and the final target interval ends before the forecast cutoff. No random split, random early stopping, negative sampling, or class weighting is used.

Feature rows only use earlier eligible origin times under the declared revised-catalogue research policy. Generated background features use expanding earlier counts, never a baseline fitted on later dates. Feature Parquet batches and partition manifests are immutable. Model parameter selection uses the tuning block; sigmoid calibration uses the separate later calibration block. Backtest folds refit before each later testing block. Outcomes learned from an earlier test period make further work exploratory; saved forecasts remain fixed and later candidates need new evaluation periods.

## Scores and revision policy

Only complete retrieved target windows supply labels. No covered source interval means status `awaiting_data`, zero eligible samples, all cells missing, and undefined scores. Available evaluations remain provisional: neither USGS review status nor elapsed D+3/D+7/D+30 alone guarantees completeness or finality. Daily reconciliation covers the last 31 days and rescoring appends changed observation versions, retaining old results.

Saved display bands initially use green <1%, yellow ≥1% and <5%, red ≥5%. Classification uses that frozen red threshold. The headline “Affected cells identified” is `TP/(TP+FN)`; precision is `TP/(TP+FP)`. Undefined denominators display N/A. Red coverage is the fraction of eligible area-time marked red. Brier score is mean squared probability error. Log loss uses numeric clipping at 1e-12 while preserving raw probabilities and counts of confident misses. Average precision is reported only with both classes and at least five positive cells.

Aggregate sums/counts are pooled; incompatible daily percentages are never averaged. Brier skill is `1 - candidate Brier / reference Brier` on identical experiment/window/mode/observation revision/eligible-cell pairs. Exclusions are reported. Paired moving seven-day blocks, seed 2026, 1,000 resamples, require at least 28 consecutive matched days. No winner is declared for short or interrupted records. Calibration displays fixed bins and their counts. Synthetic scores are software evidence, not scientific forecasting evidence.
