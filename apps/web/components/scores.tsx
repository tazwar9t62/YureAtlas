"use client";
import {
  ResponsiveContainer,
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  LineChart,
  Line,
} from "recharts";
import {
  decimal,
  percent,
  type Scores,
  type MetricReport,
  algorithmLabel,
} from "@/lib/api";
import { exactJst, windowLabel } from "@/lib/reports";

export function ScoreStrip({ scores }: { scores: Scores | null }) {
  return (
    <div className="score-strip">
      <div>
        <span title="True positive affected cells divided by all affected cells. N/A when none are affected.">
          Affected cells identified
        </span>
        <strong>{percent(scores?.recall)}</strong>
        <small>
          Cell recall · {scores ? scores.positives : "Unknown"} affected cells
        </small>
      </div>
      <div>
        <span>Precision</span>
        <strong>{percent(scores?.precision)}</strong>
        <small>Hits among red cells</small>
      </div>
      <div>
        <span>
          Brier score <em>↓</em>
        </span>
        <strong>{decimal(scores?.brier)}</strong>
        <small>Lower is better</small>
      </div>
      <div>
        <span>Red area coverage</span>
        <strong>{percent(scores?.red_coverage)}</strong>
        <small>
          {scores ? scores.n.toLocaleString() : "Unknown"} eligible cell-windows
        </small>
      </div>
    </div>
  );
}
export function Calibration({
  scores,
  window,
  evaluationVersion,
}: {
  scores: Scores | null;
  window?: string | null;
  evaluationVersion?: number;
}) {
  const data = scores?.calibration
    ?.filter((b) => b.count > 0)
    .map((b) => ({
      predicted: b.predicted,
      observed: b.observed,
      count: b.count,
    }));
  return (
    <section className="panel chart-panel">
      <div className="panel-heading">
        <h3>Selected-window probability calibration</h3>
        <span className="muted">Predicted vs observed</span>
      </div>
      <p className="panel-note">
        {window ?? "Select a saved forecast window"}
        {evaluationVersion !== undefined
          ? ` · evaluation v${evaluationVersion}`
          : ""}
        . This uses the selected window, independently of the report period.
      </p>
      {data?.length ? (
        <div className="chart">
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 16, right: 24, bottom: 12, left: 8 }}>
              <CartesianGrid stroke="#263747" strokeDasharray="3 5" />
              <XAxis
                type="number"
                dataKey="predicted"
                domain={[0, 1]}
                name="Predicted"
                tickFormatter={(v) => `${Math.round(v * 100)}%`}
                stroke="#8fa8bb"
              />
              <YAxis
                type="number"
                dataKey="observed"
                domain={[0, 1]}
                name="Observed"
                tickFormatter={(v) => `${Math.round(v * 100)}%`}
                stroke="#8fa8bb"
              />
              <Tooltip
                contentStyle={{
                  background: "#132334",
                  border: "1px solid #375063",
                }}
              />
              <ReferenceLine
                segment={[
                  { x: 0, y: 0 },
                  { x: 1, y: 1 },
                ]}
                stroke="#5e7486"
                strokeDasharray="4 4"
              />
              <Scatter data={data} fill="#6ad6df" />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="empty-inline">
          Calibration needs a completed, covered forecast window.
        </p>
      )}
    </section>
  );
}
export function HistoryChart({ report }: { report: MetricReport | null }) {
  const algorithms = [...new Set(report?.series.map((r) => r.model_id) ?? [])];
  const rows = [...(report?.series ?? [])].sort(
    (a, b) =>
      Date.parse(a.start) - Date.parse(b.start) ||
      Date.parse(a.end) - Date.parse(b.end) ||
      a.forecast_id.localeCompare(b.forecast_id),
  );
  const grouped = new Map<string, Record<string, string | number | null>>();
  const occurrences = new Map<string, number>();
  let duplicates = 0;
  for (const row of rows) {
    const window = `${row.start}|${row.end}`;
    const modelWindow = `${window}|${row.model_id}`;
    const occurrence = occurrences.get(modelWindow) ?? 0;
    occurrences.set(modelWindow, occurrence + 1);
    if (occurrence) duplicates += 1;
    const key = `${window}|${occurrence}`;
    const point = grouped.get(key) ?? { key, start: row.start, end: row.end };
    point[row.model_id] = row.metrics.brier;
    grouped.set(key, point);
  }
  const points = [...grouped.values()].sort(
    (a, b) =>
      Date.parse(String(a.start)) - Date.parse(String(b.start)) ||
      Date.parse(String(a.end)) - Date.parse(String(b.end)) ||
      String(a.key).localeCompare(String(b.key)),
  );
  return (
    <section className="panel chart-panel">
      <div className="panel-heading">
        <h3>All-valid-period score history</h3>
        <span className="muted">Brier · lower is better</span>
      </div>
      <div className="chart">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={points}
            margin={{ top: 16, right: 24, bottom: 12, left: 8 }}
          >
            <CartesianGrid stroke="#263747" strokeDasharray="3 5" />
            <XAxis
              dataKey="key"
              tickFormatter={(v) => {
                const point = grouped.get(String(v));
                return point ? exactJst(String(point.start)).slice(0, -3) : "";
              }}
              stroke="#8fa8bb"
            />
            <YAxis
              stroke="#8fa8bb"
              tickFormatter={(v) => Number(v).toFixed(3)}
            />
            <Tooltip
              labelFormatter={(value) => {
                const point = grouped.get(String(value));
                return point
                  ? windowLabel(String(point.start), String(point.end))
                  : String(value);
              }}
              contentStyle={{
                background: "#132334",
                border: "1px solid #375063",
              }}
            />
            {algorithms.map((a, i) => (
              <Line
                key={a}
                type="linear"
                dataKey={a}
                name={`${algorithmLabel[report?.series.find((r) => r.model_id === a)?.algorithm ?? ""]} · ${a.slice(0, 6)}`}
                stroke={["#6ad6df", "#c4b690", "#879bee", "#d38f80"][i % 4]}
                dot={{ r: 3 }}
                connectNulls={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {duplicates > 0 && (
        <p className="panel-note">
          {duplicates} additional entries share an exact model/window. Each
          archived forecast is shown separately.
        </p>
      )}
      <details className="history-evidence">
        <summary>Inspect {rows.length} scored forecast windows</summary>
        <div className="table-scroll">
          <table aria-label="Score history evidence">
            <thead>
              <tr>
                <th>Model / forecast</th>
                <th>Start JST · inclusive</th>
                <th>End JST · exclusive</th>
                <th>Brier</th>
                <th>Evaluation</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.forecast_id}>
                  <td>
                    {algorithmLabel[row.algorithm] ?? row.algorithm}
                    <small className="mono">{row.model_id}</small>
                    <small className="mono">{row.forecast_id}</small>
                  </td>
                  <td>
                    <time dateTime={row.start}>{exactJst(row.start)}</time>
                  </td>
                  <td>
                    <time dateTime={row.end}>{exactJst(row.end)}</time>
                  </td>
                  <td>{decimal(row.metrics.brier)}</td>
                  <td>
                    v{row.evaluation_version}
                    <small className="mono">{row.evaluation_id}</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  );
}
