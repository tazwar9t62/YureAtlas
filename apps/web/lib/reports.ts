import type { MetricReport } from "./api";

export type ReportPeriod = "1" | "7" | "30" | "90" | "0" | "custom";

export function reportQuery(
  experimentId: string,
  mode: string,
  period: ReportPeriod,
  start: string,
  end: string,
): { query: string | null; error: string | null } {
  if (!experimentId) return { query: null, error: null };
  const parameters = new URLSearchParams({ experiment_id: experimentId, mode });
  if (period === "custom") {
    if (!start || !end)
      return { query: null, error: "Choose both report dates in JST." };
    const a = `${start}T00:00:00+09:00`;
    const b = `${end}T00:00:00+09:00`;
    const span = Date.parse(b) - Date.parse(a);
    if (!Number.isFinite(span) || span <= 0 || span > 3650 * 86400000)
      return {
        query: null,
        error: "Choose an end after the start, at most 3,650 days apart.",
      };
    parameters.set("start", a);
    parameters.set("end", b);
  } else parameters.set("days", period);
  return { query: parameters.toString(), error: null };
}

export function reportMatchesQuery(
  report: MetricReport,
  query: string,
): boolean {
  const expected = new URLSearchParams(query);
  if (
    report.experiment.id !== expected.get("experiment_id") ||
    report.mode !== expected.get("mode")
  )
    return false;
  if (expected.has("days"))
    return (
      report.scope.requested_days === Number(expected.get("days")) &&
      report.scope.requested_start === null &&
      report.scope.requested_end === null
    );
  return (
    report.scope.kind === "explicit" &&
    Date.parse(report.scope.requested_start ?? "") ===
      Date.parse(expected.get("start") ?? "") &&
    Date.parse(report.scope.requested_end ?? "") ===
      Date.parse(expected.get("end") ?? "")
  );
}

export const exactJst = (value: string) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(new Date(value));

export const windowLabel = (start: string, end: string) =>
  `${exactJst(start)} → ${exactJst(end)} JST`;

const reasonLabels: Record<string, string> = {
  no_pairs: "No matched forecast windows",
  reference_brier_zero: "Reference Brier is zero; skill is undefined",
  insufficient_days: "Fewer than 28 consecutive matched days",
  nonconsecutive_windows: "Matched windows have gaps or overlaps",
  non_24h_windows: "Matched windows are not all 24 hours",
  duplicate_windows:
    "Duplicate matched windows prevent daily-block uncertainty",
  invalid_scores: "Score evidence is invalid",
  invalid_windows: "Window evidence is invalid",
  candidate_not_evaluated: "Candidate has not been evaluated",
  candidate_observations_unavailable: "Candidate observations are unavailable",
  candidate_evidence_invalid: "Candidate evidence is invalid",
  reference_not_found: "No historical-rate reference for this window",
  reference_not_evaluated: "Reference has not been evaluated",
  reference_observations_unavailable: "Reference observations are unavailable",
  reference_evidence_invalid: "Reference evidence is invalid",
  revision_mismatch: "Observation revisions differ",
  cell_mismatch: "Eligible cells differ",
  label_mismatch: "Observed cell labels differ",
  window_mismatch: "Forecast window bounds differ",
  grid_mismatch: "Grid identities differ",
  mode_mismatch: "Evidence modes differ",
  experiment_mismatch: "Experiments differ",
};
export const reportReason = (reason: string | null | undefined) =>
  reason
    ? (reasonLabels[reason] ?? reason.replaceAll("_", " "))
    : "Unavailable";

export function downloadReport(report: MetricReport) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `yureatlas-metrics-${report.report_id}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
