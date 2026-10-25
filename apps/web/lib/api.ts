import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

export const client = createClient<paths>({ baseUrl: "/api" });
export type Experiment = components["schemas"]["ExperimentView"];
export type Forecast = components["schemas"]["ForecastView"];
export type Model = components["schemas"]["ModelView"];
export type Job = components["schemas"]["JobView"];
export type Evaluation = components["schemas"]["EvaluationView"];
export type MetricReport = components["schemas"]["MetricReport"];
export interface Scores {
  n: number;
  missing: number;
  positives: number;
  tp: number;
  fp: number;
  tn: number;
  fn: number;
  brier: number | null;
  log_loss: number | null;
  recall: number | null;
  precision: number | null;
  red_coverage: number | null;
  average_precision?: number | null;
  calibration?:
    | {
        lower: number;
        upper: number;
        count: number;
        predicted: number | null;
        observed: number | null;
      }[]
    | null;
}
export interface MapData {
  cells: GeoJSON.FeatureCollection<GeoJSON.Polygon>;
  events: GeoJSON.FeatureCollection<GeoJSON.Point>;
  evaluation: Evaluation | null;
}
export interface CellDetail {
  cell_id: string;
  probability: number;
  band: string;
  expected_count: number | null;
  target: string;
  source: string;
  model: string;
  model_id: string;
  model_artifact: string;
  input_cutoff: string;
  observed_count: number | null;
  outcome: string;
  history: { start: string; probability: number }[];
}
export interface SourceReport {
  sources: {
    id: string;
    name: string;
    provider: string;
    status: string;
    attribution: string;
    event_count: number;
    details: { last_success?: string; error?: string };
    coverage: {
      start: string;
      end: string;
      status: string;
      report: Record<string, unknown>;
    }[];
  }[];
  worker: { heartbeat: string; online: boolean } | null;
  schedule_enabled: boolean;
}
export async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(
    `/api${path}`,
    body === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-YureAtlas-Request": "1",
          },
          body: JSON.stringify(body),
        },
  );
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : JSON.stringify(result.detail ?? "Request failed"),
    );
  return result as T;
}
export const percent = (value: number | null | undefined, digits = 1) =>
  value == null ? "N/A" : `${(value * 100).toFixed(digits)}%`;
export const decimal = (value: number | null | undefined, digits = 5) =>
  value == null ? "N/A" : value.toFixed(digits);
export const dateLabel = (value: string, time = false) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Tokyo",
    day: "2-digit",
    month: "short",
    year: "numeric",
    ...(time ? ({ hour: "2-digit", minute: "2-digit" } as const) : {}),
  }).format(new Date(value));
export const algorithmLabel: Record<string, string> = {
  historical_rate: "Historical rate",
  hist_gradient_boosting: "Gradient boosting",
  calibrated_hybrid: "Calibrated hybrid",
  etas_discrete_space: "ETAS · research",
};
