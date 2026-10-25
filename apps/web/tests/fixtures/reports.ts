import type { components } from "../../lib/schema";

type Report = components["schemas"]["MetricReport"];
type Score = components["schemas"]["PooledScore"];
type Decision = components["schemas"]["PairDecision"];
type Comparison = components["schemas"]["ModelComparison"];
type Forecast = components["schemas"]["ForecastView"];
type Model = components["schemas"]["ModelView"];

export const START = "2024-12-31T15:00:00Z";
export const END = "2025-01-01T15:00:00Z";
export const experiment = {
  id: "report-experiment",
  name: "jma_comparison_fixture",
  synthetic: false,
  source_id: "source",
  grid_id: "grid",
  config_hash: "config",
  config: {
    provider: "jma",
    catalogue_namespace: "fixture",
    red: 0.5,
    yellow: 0.1,
  },
} satisfies components["schemas"]["ExperimentView"];

export function pooled(brier = 0.265): Score {
  return {
    n: 2,
    missing: 0,
    positives: 1,
    tp: 0,
    fp: 0,
    tn: 1,
    fn: 1,
    brier,
    brier_sum: brier * 2,
    log_loss: 0.7135581778,
    log_loss_sum: 1.4271163556,
    recall: 0,
    precision: null,
    false_positive_rate: 0,
    red_coverage: 0,
    red_area: 0,
    eligible_area: 2,
  };
}

export function emptyReport(
  options: { mode?: Report["mode"]; days?: number; experimentId?: string } = {},
): Report {
  const mode = options.mode ?? "retrospective_revised";
  const days = options.days ?? 30;
  return {
    report_id: "a".repeat(64),
    generated_at: "2025-01-04T12:00:00Z",
    mode,
    days,
    scope: {
      kind: days === 0 ? "whole_experiment" : "relative",
      requested_days: days,
      requested_start: null,
      requested_end: null,
      resolved_start: null,
      resolved_end: null,
      anchor: null,
      empty_reason:
        days === 0 ? "no_forecasts" : "no_completed_evaluated_window",
      display_timezone: "Asia/Tokyo",
      containment_policy: "Full windows in [start,end).",
      limits: {
        max_span_days: 3650,
        max_forecasts: 2000,
        max_cell_rows: 1000000,
        max_grid_cells: 10000,
      },
    },
    experiment: {
      id: options.experimentId ?? experiment.id,
      name: experiment.name,
      config_hash: experiment.config_hash,
      config: experiment.config,
      grid_id: experiment.grid_id,
      grid_hash: "grid-hash",
      source_id: experiment.source_id,
      source_name: "Fixture catalogue",
      source_provider: "jma",
      synthetic: false,
    },
    policies: {
      generation: "One immutable database read snapshot.",
      evaluation: "Latest archived evaluation.",
      inclusion: "All complete windows in scope.",
    },
    counts: {
      forecast_count: 0,
      evaluation_count: 0,
      forecast_cell_rows: 0,
      evaluation_cell_rows: 0,
    },
    selected_forecast_ids: [],
    selected_evaluation_ids: [],
    policy_version: "earliest-compatible-historical-rate-v1",
    score_policy_version: "pooled-eligible-cell-windows-v1",
    matching_policy:
      "Earliest archived compatible historical_rate reference; same exact window, grid and observation cohort.",
    aggregation_policy: "Pooled valid loss sums and eligible cell counts.",
    series_policy:
      "Trusted evaluated scores only; invalid evaluations remain in decisions.",
    series: [],
    comparisons: [],
    decisions: [],
  };
}

export const forecasts: Forecast[] = [
  ["candidate-one", "candidate-model"],
  ["candidate-two", "candidate-model"],
  ["wrong-reference", "wrong-baseline-model"],
  ["chosen-reference", "chosen-baseline-model"],
].map(([id, model_id]) => ({
  id,
  experiment_id: experiment.id,
  model_id,
  snapshot_id: "input-snapshot",
  issued_at: "2024-12-30T12:00:00Z",
  start: START,
  end: END,
  input_cutoff: "2024-12-30T11:30:00Z",
  mode: "retrospective_revised",
  status: "published",
  provenance: { grid_hash: "grid-hash" },
}));

// Misleading list order is intentional: the server's chosen reference is second.
export const models: Model[] = [
  "wrong-baseline-model",
  "chosen-baseline-model",
  "candidate-model",
].map((id) => ({
  id,
  experiment_id: experiment.id,
  algorithm:
    id === "candidate-model" ? "hist_gradient_boosting" : "historical_rate",
  status: "available",
  training_cutoff: "2024-12-01T15:00:00Z",
  available_at: "2024-12-02T15:00:00Z",
  artifact: null,
  metadata: {},
}));

function decision(id: string, reference = false): Decision {
  return {
    forecast_id: id,
    model_id: reference ? "chosen-baseline-model" : "candidate-model",
    evaluation_id: "evaluation-" + id,
    evaluation_version: 1,
    experiment_id: experiment.id,
    start: START,
    end: END,
    mode: "retrospective_revised",
    grid_hash: "grid-hash",
    revision_signature: "revision-cohort",
    cell_signature: "cell-cohort",
    observation_signature: "observation-cohort",
    status: "paired",
    reason: null,
    reasons: [],
    reference_forecast_id: "chosen-reference",
    reference_model_id: "chosen-baseline-model",
    reference_evaluation_id: "evaluation-chosen-reference",
    reference_evaluation_version: 1,
    reference_created_at: "2024-12-30T12:00:00Z",
    self_reference: reference,
    reference_rejection_count: 1,
    reference_rejections_omitted: 0,
    reference_rejection_reasons: { revision_mismatch: 1 },
    reference_rejection_limit: 20,
    reference_rejections: [
      { forecast_id: "wrong-reference", reasons: ["revision_mismatch"] },
    ],
  };
}

function comparison(
  decisions: Decision[],
  brier: number,
  modelId: string,
): Comparison {
  const base = pooled(brier);
  const total = { ...base };
  for (const key of [
    "n",
    "missing",
    "positives",
    "tp",
    "fp",
    "tn",
    "fn",
    "brier_sum",
    "log_loss_sum",
    "red_area",
    "eligible_area",
  ] as const) {
    total[key] *= decisions.length;
  }
  const reference = { ...total, brier: 0.25, brier_sum: 0.25 * total.n };
  return {
    model_id: modelId,
    algorithm:
      modelId === "candidate-model"
        ? "hist_gradient_boosting"
        : "historical_rate",
    metrics: total,
    paired_metrics: total,
    reference_metrics: reference,
    brier_skill: 1 - brier / 0.25,
    brier_skill_reason: null,
    paired_days: decisions.length,
    excluded_days: 0,
    paired_forecasts: decisions.length,
    excluded_forecasts: 0,
    total_forecasts: decisions.length,
    evaluated_forecasts: decisions.length,
    scoreable_forecasts: decisions.length,
    unevaluated_forecasts: 0,
    unavailable_evaluation_forecasts: 0,
    invalid_evidence_forecasts: 0,
    unavailable_forecasts: 0,
    total_cell_windows: total.n,
    observed_cell_windows: total.n,
    unscored_cell_windows: 0,
    missing_cell_windows: 0,
    paired_cell_windows: total.n,
    paired_missing_cell_windows: 0,
    excluded_observed_cell_windows: 0,
    excluded_missing_cell_windows: 0,
    unknown_cell_window_forecasts: 0,
    uncertainty: {
      status: "insufficient_data",
      reason: "insufficient_days",
      days: decisions.length,
      minimum_days: 28,
      block_days: 7,
      simulations: 1000,
      seed: 2026,
      paired_cell_windows: total.n,
      mean_difference: null,
      interval95: null,
      method_version: "paired-moving-block-ratio-v1",
      estimand: "Pooled paired Brier difference",
      policy: "Experimental seven-day block interval.",
    },
    reference_model_ids: ["chosen-baseline-model"],
    reference_model_count: 1,
    reference_forecast_ids: ["chosen-reference"],
    reference_forecast_count: 1,
    reference_evaluation_ids: ["evaluation-chosen-reference"],
    reference_evaluation_count: 1,
    mixed_reference_models: false,
    exclusion_reasons: {},
    primary_exclusion_reasons: {},
    decisions,
  };
}

export function reportFixture(query = new URLSearchParams()): Report {
  const mode = (query.get("mode") ?? "retrospective_revised") as Report["mode"];
  const days = Number(query.get("days") ?? 30);
  const report = emptyReport({ mode, days });
  report.report_id = (
    days === 7 ? "7" : days === 90 ? "9" : days === 0 ? "0" : "a"
  ).repeat(64);
  report.scope = {
    ...report.scope,
    resolved_start: START,
    resolved_end: END,
    anchor: END,
    empty_reason: null,
  };
  if (query.has("start")) {
    report.days = null;
    report.report_id = "c".repeat(64);
    report.scope = {
      ...report.scope,
      kind: "explicit",
      requested_days: null,
      requested_start: new Date(query.get("start")!).toISOString(),
      requested_end: new Date(query.get("end")!).toISOString(),
      resolved_start: new Date(query.get("start")!).toISOString(),
      resolved_end: new Date(query.get("end")!).toISOString(),
      anchor: null,
    };
  }
  const selected = [
    decision("candidate-one"),
    decision("candidate-two"),
    decision("chosen-reference", true),
  ];
  selected.forEach((row) => {
    row.mode = mode;
  });
  const excluded: Decision = {
    ...decision("wrong-reference"),
    mode,
    model_id: "wrong-baseline-model",
    status: "excluded",
    reason: "candidate_evidence_invalid",
    reasons: ["candidate_evidence_invalid"],
    reference_forecast_id: null,
    reference_model_id: null,
    reference_evaluation_id: null,
    reference_evaluation_version: null,
    reference_created_at: null,
    reference_rejection_count: 0,
    reference_rejections_omitted: 0,
    reference_rejection_reasons: {},
    reference_rejections: [],
  };
  report.decisions = [...selected, excluded];
  report.selected_forecast_ids = report.decisions
    .map((row) => row.forecast_id)
    .sort();
  report.selected_evaluation_ids = report.decisions
    .map((row) => row.evaluation_id!)
    .sort();
  report.counts = {
    forecast_count: 4,
    evaluation_count: 4,
    forecast_cell_rows: 8,
    evaluation_cell_rows: 8,
  };
  const invalid = comparison([], 0, "wrong-baseline-model");
  const emptyScore: Score = {
    n: 0,
    missing: 0,
    positives: 0,
    tp: 0,
    fp: 0,
    tn: 0,
    fn: 0,
    brier_sum: 0,
    log_loss_sum: 0,
    red_area: 0,
    eligible_area: 0,
    brier: null,
    log_loss: null,
    recall: null,
    precision: null,
    false_positive_rate: null,
    red_coverage: null,
  };
  Object.assign(invalid, {
    metrics: emptyScore,
    paired_metrics: emptyScore,
    reference_metrics: emptyScore,
    brier_skill: null,
    brier_skill_reason: "no_pairs",
    excluded_days: 1,
    excluded_forecasts: 1,
    total_forecasts: 1,
    evaluated_forecasts: 1,
    invalid_evidence_forecasts: 1,
    unavailable_forecasts: 1,
    total_cell_windows: 2,
    unscored_cell_windows: 2,
    reference_model_ids: [],
    reference_model_count: 0,
    reference_forecast_ids: [],
    reference_forecast_count: 0,
    reference_evaluation_ids: [],
    reference_evaluation_count: 0,
    exclusion_reasons: { candidate_evidence_invalid: 1 },
    primary_exclusion_reasons: { candidate_evidence_invalid: 1 },
    decisions: [excluded],
  });
  report.comparisons = [
    comparison(selected.slice(0, 2), 0.215, "candidate-model"),
    comparison(selected.slice(2), 0.25, "chosen-baseline-model"),
    invalid,
  ];
  report.series = selected.map((row, index) => ({
    date: START,
    start: START,
    end: END,
    forecast_id: row.forecast_id,
    model_id: row.model_id,
    algorithm: index < 2 ? "hist_gradient_boosting" : "historical_rate",
    evaluation_id: row.evaluation_id!,
    evaluation_version: 1,
    status: "provisional",
    snapshot: "revision-cohort",
    metrics: {
      ...pooled(index < 2 ? (index === 0 ? 0.265 : 0.165) : 0.25),
      calibration: [
        { lower: 0, upper: 0.5, count: 2, predicted: 0.25, observed: 0.5 },
      ],
      clipping_epsilon: 1e-12,
      confident_misses: 0,
      threshold: 0.5,
      average_precision: null,
      source_policy: "Retrieval coverage only.",
    },
  }));
  return report;
}
