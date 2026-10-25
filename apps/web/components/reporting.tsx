"use client";

import { algorithmLabel, decimal, percent, type MetricReport } from "@/lib/api";
import { exactJst, reportReason, windowLabel } from "@/lib/reports";

export function ReportSummary({
  report,
  loading,
  error,
}: {
  report: MetricReport | null;
  loading: boolean;
  error: string | null;
}) {
  if (!report)
    return (
      <p className="report-status" role="status">
        {error ??
          (loading
            ? "Loading report for the selected scope…"
            : "Choose an experiment and report scope.")}
      </p>
    );
  const { scope } = report;
  const empty = {
    no_completed_evaluated_window:
      "No completed evaluated window can anchor this period. Choose Whole experiment or a custom range to inspect unevaluated forecasts.",
    no_forecasts:
      "This experiment has no forecasts in the selected evidence mode.",
    no_forecasts_in_range: "No full forecast windows fall within these bounds.",
  };
  return (
    <section className="report-summary" aria-label="Displayed report scope">
      <p>
        <strong>Displayed report</strong> · {scope.kind.replaceAll("_", " ")} ·{" "}
        {report.mode.replaceAll("_", " ")}
      </p>
      <p>
        Report{" "}
        <span className="mono" title={report.report_id}>
          {report.report_id.slice(0, 12)}
        </span>{" "}
        · generated {exactJst(report.generated_at)} JST
      </p>
      <p>
        {scope.resolved_start && scope.resolved_end ? (
          <>
            {windowLabel(scope.resolved_start, scope.resolved_end)} · start
            inclusive, end exclusive
          </>
        ) : (
          "No resolved date bounds"
        )}
      </p>
      {scope.anchor && (
        <p>
          Recent-period anchor: latest ended evaluated window,{" "}
          {exactJst(scope.anchor)} JST. Observations may still be unavailable.
        </p>
      )}
      {scope.empty_reason && <p role="status">{empty[scope.empty_reason]}</p>}
      <p>
        {report.counts.forecast_count} archived forecast windows ·{" "}
        {report.counts.evaluation_count} evaluations selected. Only full windows
        inside the bounds are included.
      </p>
      <details>
        <summary>Report identity and policies</summary>
        <dl className="details report-identities">
          <dt>Report ID</dt>
          <dd className="hash">{report.report_id}</dd>
          <dt>Experiment</dt>
          <dd className="hash">{report.experiment.id}</dd>
          <dt>Configuration</dt>
          <dd className="hash">{report.experiment.config_hash}</dd>
          <dt>Grid</dt>
          <dd className="hash">
            {report.experiment.grid_id} · {report.experiment.grid_hash}
          </dd>
          <dt>Catalogue</dt>
          <dd>
            {report.experiment.source_name} ·{" "}
            {report.experiment.source_provider}
          </dd>
        </dl>
        <p>{report.matching_policy}</p>
        <p>{report.aggregation_policy}</p>
        <p>{report.policies.evaluation}</p>
      </details>
    </section>
  );
}

export function ComparisonTable({ report }: { report: MetricReport | null }) {
  return (
    <section className="panel comparison-panel">
      <div className="panel-heading">
        <h3>Matched-cohort model comparison</h3>
        <span className="muted">
          Same windows, observation revisions and eligible cells
        </span>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th>Brier ↓</th>
              <th>Log loss ↓</th>
              <th>Brier skill</th>
              <th>Recall</th>
              <th>Precision</th>
              <th>Paired / excluded windows</th>
              <th>Paired cell-windows</th>
            </tr>
          </thead>
          <tbody>
            {report?.comparisons.map((row) => (
              <tr key={row.model_id}>
                <td>
                  {algorithmLabel[row.algorithm] ?? row.algorithm}
                  <small className="mono">{row.model_id}</small>
                </td>
                <td>{decimal(row.paired_metrics.brier)}</td>
                <td>{decimal(row.paired_metrics.log_loss)}</td>
                <td>
                  {percent(row.brier_skill)}
                  {row.brier_skill_reason && (
                    <small>{reportReason(row.brier_skill_reason)}</small>
                  )}
                </td>
                <td>{percent(row.paired_metrics.recall)}</td>
                <td>{percent(row.paired_metrics.precision)}</td>
                <td>
                  {row.paired_forecasts} / {row.excluded_forecasts}
                </td>
                <td>{row.paired_cell_windows.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!report?.comparisons.length && (
        <p className="empty-inline">
          No forecast windows in the loaded report scope.
        </p>
      )}
      {report?.comparisons.map((row) => (
        <details className="comparison-evidence" key={row.model_id}>
          <summary>
            {algorithmLabel[row.algorithm] ?? row.algorithm} ·{" "}
            {row.model_id.slice(0, 8)} · references and exclusions
          </summary>
          <p>
            {row.reference_model_count} reference model versions ·{" "}
            {row.reference_forecast_count} reference forecasts ·{" "}
            {row.reference_evaluation_count} reference evaluations
            {row.mixed_reference_models
              ? " · Mixed reference model versions across windows"
              : ""}
            .
          </p>
          <p>
            All windows: {row.total_forecasts}; evaluated:{" "}
            {row.evaluated_forecasts}; scoreable: {row.scoreable_forecasts};
            unavailable: {row.unavailable_forecasts} (
            {row.unevaluated_forecasts} unevaluated,{" "}
            {row.unavailable_evaluation_forecasts} unavailable evaluations,{" "}
            {row.invalid_evidence_forecasts} invalid).
          </p>
          <p>
            All grid cell-windows:{" "}
            {row.total_cell_windows?.toLocaleString() ?? "Unknown"}; observed:{" "}
            {row.observed_cell_windows.toLocaleString()}; unscored:{" "}
            {row.unscored_cell_windows?.toLocaleString() ?? "Unknown"}; known
            missing: {row.missing_cell_windows.toLocaleString()}. Matched:{" "}
            {row.paired_cell_windows.toLocaleString()} observed and{" "}
            {row.paired_missing_cell_windows.toLocaleString()} known missing.
            Missing and unscored cells do not become negative labels.
          </p>
          <p>
            <strong>Paired Brier difference uncertainty:</strong>{" "}
            {row.uncertainty.status === "available" &&
            row.uncertainty.interval95 ? (
              <>
                {decimal(row.uncertainty.mean_difference)}; experimental 95%
                interval [{decimal(row.uncertainty.interval95[0])},{" "}
                {decimal(row.uncertainty.interval95[1])}]. Candidate minus
                reference; negative favors the candidate.
              </>
            ) : (
              <>N/A — {reportReason(row.uncertainty.reason)}.</>
            )}{" "}
            No winning model is established.
          </p>
          {Object.keys(row.primary_exclusion_reasons).length > 0 && (
            <ul>
              {Object.entries(row.primary_exclusion_reasons).map(
                ([reason, count]) => (
                  <li key={reason}>
                    {reportReason(reason)}: {count} windows
                  </li>
                ),
              )}
            </ul>
          )}
          <div className="table-scroll">
            <table aria-label={`Reference decisions for ${row.model_id}`}>
              <thead>
                <tr>
                  <th>Candidate window / forecast</th>
                  <th>Candidate evaluation</th>
                  <th>Chosen reference</th>
                  <th>Decision</th>
                </tr>
              </thead>
              <tbody>
                {row.decisions.map((decision) => (
                  <tr key={decision.forecast_id}>
                    <td>
                      {windowLabel(decision.start, decision.end)}
                      <small className="mono">{decision.forecast_id}</small>
                    </td>
                    <td>
                      {decision.evaluation_version === null
                        ? "Not evaluated"
                        : `Version ${decision.evaluation_version}`}
                      <small className="mono">{decision.evaluation_id}</small>
                    </td>
                    <td>
                      {decision.reference_forecast_id ? (
                        <>
                          Model{" "}
                          <span className="mono">
                            {decision.reference_model_id}
                          </span>
                          <br />
                          Forecast{" "}
                          <span className="mono">
                            {decision.reference_forecast_id}
                          </span>
                          <br />
                          Evaluation v{
                            decision.reference_evaluation_version
                          }{" "}
                          <span className="mono">
                            {decision.reference_evaluation_id}
                          </span>
                          {decision.self_reference && (
                            <small>Self-reference</small>
                          )}
                        </>
                      ) : (
                        "No compatible reference"
                      )}
                    </td>
                    <td>
                      {decision.status === "paired"
                        ? "Paired"
                        : decision.reasons.map(reportReason).join("; ")}
                      <details>
                        <summary>
                          Reference rejections:{" "}
                          {decision.reference_rejection_count}
                          {" · "}
                          {decision.reference_rejections_omitted} omitted
                          examples
                        </summary>
                        <p>
                          {decision.status === "paired"
                            ? "These rejections were examined before the chosen reference was selected. Search stopped at selection; later candidates are not included."
                            : decision.reason?.startsWith("candidate_")
                              ? "Candidate evidence prevented reference selection; no reference search was performed."
                              : "No compatible reference was selected. These diagnostics cover all reference candidates examined for this window."}
                        </p>
                        <p>
                          Showing {decision.reference_rejections.length}{" "}
                          earliest rejected-reference examples (up to{" "}
                          {decision.reference_rejection_limit});{" "}
                          {decision.reference_rejections_omitted} additional
                          identities omitted. Totals and reason counts include
                          every examined rejection.
                        </p>
                        {Object.keys(decision.reference_rejection_reasons)
                          .length ? (
                          <>
                            <p>
                              Rejection reasons across all examined incompatible
                              candidates (reasons may overlap):
                            </p>
                            <ul>
                              {Object.entries(
                                decision.reference_rejection_reasons,
                              ).map(([reason, count]) => (
                                <li key={reason}>
                                  {reportReason(reason)}: {count}
                                </li>
                              ))}
                            </ul>
                          </>
                        ) : (
                          <p>No reference rejections were recorded.</p>
                        )}
                        {decision.reference_rejections.length > 0 && (
                          <ol>
                            {decision.reference_rejections.map((rejection) => (
                              <li key={rejection.forecast_id}>
                                <span className="mono">
                                  {rejection.forecast_id}
                                </span>
                                :{" "}
                                {rejection.reasons.map(reportReason).join("; ")}
                              </li>
                            ))}
                          </ol>
                        )}
                      </details>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ))}
      <p className="panel-note">
        Scores above use only exact candidate/reference pairs. Period scores and
        selected-window calibration have separate scopes.
      </p>
    </section>
  );
}
