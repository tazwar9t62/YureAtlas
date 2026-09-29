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

export function ScoreStrip({ scores }: { scores: Scores | null }) {
  return (
    <div className="score-strip">
      <div>
        <span title="True positive affected cells divided by all affected cells. N/A when none are affected.">
          Affected cells identified
        </span>
        <strong>{percent(scores?.recall)}</strong>
        <small>Cell recall · {scores?.positives ?? 0} affected cells</small>
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
        <small>{scores?.n?.toLocaleString() ?? 0} eligible cell-days</small>
      </div>
    </div>
  );
}
export function Calibration({ scores }: { scores: Scores | null }) {
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
        <h3>Probability calibration</h3>
        <span className="muted">Predicted vs observed</span>
      </div>
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
  const grouped: Record<string, Record<string, string | number | null>> = {};
  for (const row of report?.series ?? []) {
    const date = row.date.slice(0, 10);
    grouped[date] ??= { date };
    grouped[date][row.model_id] = row.metrics.brier;
  }
  return (
    <section className="panel chart-panel">
      <div className="panel-heading">
        <h3>Score history</h3>
        <span className="muted">Brier · lower is better</span>
      </div>
      <div className="chart">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={Object.values(grouped)}
            margin={{ top: 16, right: 24, bottom: 12, left: 8 }}
          >
            <CartesianGrid stroke="#263747" strokeDasharray="3 5" />
            <XAxis
              dataKey="date"
              tickFormatter={(v) => String(v).slice(5)}
              stroke="#8fa8bb"
            />
            <YAxis
              stroke="#8fa8bb"
              tickFormatter={(v) => Number(v).toFixed(3)}
            />
            <Tooltip
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
                stroke={["#6ad6df", "#c4b690", "#879bee", "#d38f80"][i]}
                dot={{ r: 3 }}
                connectNulls={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
