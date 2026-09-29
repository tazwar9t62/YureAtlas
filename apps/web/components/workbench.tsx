"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  BarChart3,
  Check,
  ChevronLeft,
  ChevronRight,
  Database,
  FlaskConical,
  Layers,
  Map,
  Play,
  RefreshCw,
  SlidersHorizontal,
  Square,
  Terminal,
  X,
} from "lucide-react";
import {
  algorithmLabel,
  client,
  dateLabel,
  decimal,
  percent,
  request,
  type CellDetail,
  type Evaluation,
  type Experiment,
  type Forecast,
  type Job,
  type MapData,
  type MetricReport,
  type Model,
  type Scores,
  type SourceReport,
} from "@/lib/api";
import { Calibration, HistoryChart, ScoreStrip } from "./scores";

const ForecastMap = dynamic(() => import("./forecast-map"), {
  ssr: false,
  loading: () => <div className="map-placeholder">Loading local map…</div>,
});
type Tab = "atlas" | "evaluation" | "models" | "operations";
const nav = [
  { id: "atlas", label: "Forecast atlas", icon: Map },
  { id: "evaluation", label: "Evaluation", icon: BarChart3 },
  { id: "models", label: "Model lab", icon: FlaskConical },
  { id: "operations", label: "Data & operations", icon: Database },
] as const;
const modeLabel: Record<string, string> = {
  synthetic_demo: "Synthetic demonstration",
  prospective: "Prospective forecast",
  retrospective_revised: "Historical · revised catalogue",
};

export default function Workbench() {
  const [tab, setTab] = useState<Tab>("atlas");
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [experimentId, setExperimentId] = useState("");
  const [models, setModels] = useState<Model[]>([]);
  const [forecasts, setForecasts] = useState<Forecast[]>([]);
  const [modelId, setModelId] = useState("");
  const [windowStart, setWindowStart] = useState("");
  const [mode, setMode] = useState("synthetic_demo");
  const [data, setData] = useState<MapData | null>(null);
  const [cell, setCell] = useState<CellDetail | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [sources, setSources] = useState<SourceReport | null>(null);
  const [report, setReport] = useState<MetricReport | null>(null);
  const [versions, setVersions] = useState<Evaluation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [showCells, setShowCells] = useState(true);
  const [showEvents, setShowEvents] = useState(true);
  const [compare, setCompare] = useState(false);
  const [baselineData, setBaselineData] = useState<MapData | null>(null);
  const [period, setPeriod] = useState(30);
  const experiment = experiments.find((e) => e.id === experimentId);
  const availableForecasts = forecasts.filter((f) => f.mode === mode);
  const windows = [...new Set(availableForecasts.map((f) => f.start))].sort();
  const forecast = availableForecasts.find(
    (f) => f.model_id === modelId && f.start === windowStart,
  );
  const selectedModel = models.find((m) => m.id === modelId);
  const evaluation = data?.evaluation;
  const scores = evaluation?.metrics as unknown as Scores | undefined;
  const pending = jobs.find((j) => ["queued", "running"].includes(j.status));
  const red = Number(experiment?.config.red ?? 0.05),
    yellow = Number(experiment?.config.yellow ?? 0.01);

  const refreshIndex = useCallback(async () => {
    const [experimentResult, jobResult, sourceResult] = await Promise.all([
      client.GET("/experiments"),
      request<Job[]>("/jobs"),
      request<SourceReport>("/sources"),
    ]);
    if (experimentResult.error) throw new Error("Could not load experiments");
    const list = experimentResult.data ?? [];
    setExperiments(list);
    setJobs(jobResult);
    setSources(sourceResult);
    setExperimentId(
      (current) =>
        current ||
        list.find((e) => e.name === "synthetic_japan_m4_v1")?.id ||
        list[0]?.id ||
        "",
    );
  }, []);
  useEffect(() => {
    refreshIndex()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [refreshIndex, refresh]);
  useEffect(() => {
    let stopped = false;
    const timer = setInterval(async () => {
      try {
        const next = await request<Job[]>("/jobs");
        if (stopped) return;
        setJobs((old) => {
          if (
            next.some(
              (j) =>
                ["completed", "failed", "cancelled"].includes(j.status) &&
                old.find((o) => o.id === j.id && o.status !== j.status),
            )
          )
            setRefresh((r) => r + 1);
          return next;
        });
      } catch {
        /* Main refresh retains the useful last known state. */
      }
    }, 3000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (!experimentId) return;
    let cancelled = false;
    setData(null);
    setCell(null);
    Promise.all([
      request<Model[]>(`/models?experiment_id=${experimentId}`),
      request<Forecast[]>(
        `/forecasts?experiment_id=${experimentId}&limit=1000`,
      ),
    ])
      .then(([m, f]) => {
        if (cancelled) return;
        setModels(m);
        setForecasts(f);
        setModelId((current) =>
          m.some((row) => row.id === current)
            ? current
            : (m.find(
                (row) =>
                  row.algorithm === "historical_rate" &&
                  row.status === "available",
              )?.id ??
              m[0]?.id ??
              ""),
        );
        setMode((current) =>
          f.some((row) => row.mode === current)
            ? current
            : (f[0]?.mode ??
              (experiments.find((e) => e.id === experimentId)?.synthetic
                ? "synthetic_demo"
                : "prospective")),
        );
        setWindowStart((current) =>
          f.some((row) => row.start === current)
            ? current
            : (f[0]?.start ?? ""),
        );
      })
      .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [experimentId, refresh]); // Experiment changes reset server-derived selections.
  useEffect(() => {
    if (!forecast) {
      setData(null);
      return;
    }
    let cancelled = false;
    setCell(null);
    setData(null);
    Promise.all([
      request<MapData>(`/forecasts/${forecast.id}/map`),
      request<Evaluation[]>(`/forecasts/${forecast.id}/evaluations`),
    ])
      .then(([map, history]) => {
        if (!cancelled) {
          setData(map);
          setVersions(history);
        }
      })
      .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [forecast?.id, refresh]);
  useEffect(() => {
    if (!experimentId) return;
    let cancelled = false;
    request<MetricReport>(
      `/metrics?experiment_id=${experimentId}&mode=${mode}&days=${period}`,
    )
      .then((r) => {
        if (!cancelled) setReport(r);
      })
      .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [experimentId, mode, period, refresh]);
  useEffect(() => {
    const baselineModel = models.find(
      (m) =>
        m.algorithm === "historical_rate" &&
        availableForecasts.some(
          (f) => f.model_id === m.id && f.start === windowStart,
        ),
    );
    const baseline = availableForecasts.find(
      (f) => f.model_id === baselineModel?.id && f.start === windowStart,
    );
    if (!compare || !baseline) {
      setBaselineData(null);
      return;
    }
    let cancelled = false;
    request<MapData>(`/forecasts/${baseline.id}/map`)
      .then((d) => {
        if (!cancelled) setBaselineData(d);
      })
      .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [compare, modelId, windowStart, mode, models, forecasts]);

  async function action(path: string, body: unknown = {}) {
    setSending(true);
    setError(null);
    try {
      const job = await request<Job>(path, body);
      setNotice(
        `${job.kind} job ${job.status}. Progress is shown in Data & operations.`,
      );
      setRefresh((r) => r + 1);
      return job;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setSending(false);
    }
  }
  const selectCell = useCallback(
    async (id: string) => {
      if (!forecast) return;
      try {
        setCell(
          await request<CellDetail>(
            `/forecasts/${forecast.id}/cells/${encodeURIComponent(id)}`,
          ),
        );
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [forecast?.id],
  );
  const hotCells = useMemo(
    () =>
      [...(data?.cells.features ?? [])].sort(
        (a, b) =>
          Number(b.properties?.probability) - Number(a.properties?.probability),
      ),
    [data],
  );

  return (
    <div className="workbench">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="YureAtlas home">
          <svg viewBox="0 0 32 32" width="32" height="32" aria-hidden="true">
            <path
              d="M3 23 9 9l8 5 6-11 6 7M3 29l7-13 8 6 8-12M4 8l7-4 9 4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
            />
            <path
              d="M3 23h26M9 9v19M18 13v15"
              stroke="currentColor"
              strokeOpacity=".3"
              fill="none"
            />
          </svg>
          <span>
            YureAtlas<small>FORECAST. OBSERVE. IMPROVE.</small>
          </span>
        </a>
        <div className="nav-caption">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {nav.map((item) => (
            <button
              key={item.id}
              className={tab === item.id ? "nav-item active" : "nav-item"}
              onClick={() => setTab(item.id)}
              aria-current={tab === item.id ? "page" : undefined}
            >
              <item.icon size={18} />
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="installation">
            <i className={`dot ${sources?.worker?.online ? "cyan" : "grey"}`} />
            <span>
              Local installation
              <small>
                {sources?.worker?.online ? "Worker running" : "Worker offline"}
              </small>
            </span>
          </div>
          <p className="notice">
            Experimental forecast — not an official warning.
          </p>
          <span className="version">RESEARCH BUILD / 0.1</span>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="muted">Workspace</span>
            <ChevronRight size={13} />
            <span>Japan region</span>
          </div>
          <span className="local-badge">
            <i className="dot cyan" />
            LOCAL RESEARCH
          </span>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <p className="eyebrow">
                YUREATLAS /{" "}
                {tab === "atlas"
                  ? "DAILY OCCURRENCE FORECASTS"
                  : tab === "evaluation"
                    ? "OBSERVE & MEASURE"
                    : tab === "models"
                      ? "REPRODUCIBLE EXPERIMENTS"
                      : "LOCAL DATA PIPELINE"}
              </p>
              <h1>{nav.find((n) => n.id === tab)?.label}</h1>
              <p className="subtitle">
                {tab === "atlas"
                  ? "A probability map. A permanent record. A testable hypothesis."
                  : tab === "evaluation"
                    ? "Compare saved probabilities with subsequently recorded earthquakes."
                    : tab === "models"
                      ? "Train, compare, and promote versioned model candidates."
                      : "Keep catalogue coverage, revisions, and background work in view."}
              </p>
            </div>
            <button
              className="icon-button"
              onClick={() => {
                setError(null);
                setRefresh((r) => r + 1);
              }}
              aria-label="Refresh dashboard"
            >
              <RefreshCw size={18} />
            </button>
          </div>
          {error && (
            <div className="banner error" role="alert">
              <span>{error}</span>
              <button
                onClick={() => {
                  setError(null);
                  setRefresh((r) => r + 1);
                }}
              >
                Retry
              </button>
              <button
                className="icon-button"
                aria-label="Dismiss error"
                onClick={() => setError(null)}
              >
                <X size={15} />
              </button>
            </div>
          )}
          {notice && (
            <div className="banner" role="status">
              <Check size={16} />
              <span>{notice}</span>
              <button
                className="icon-button"
                aria-label="Dismiss notification"
                onClick={() => setNotice(null)}
              >
                <X size={15} />
              </button>
            </div>
          )}
          {pending && (
            <div className="job-progress" role="status">
              <Activity size={16} />
              <span>
                {pending.kind} · {pending.status}
              </span>
              <progress value={pending.progress} max={1} />
              <span>{Math.round(pending.progress * 100)}%</span>
              <button onClick={() => setTab("operations")}>View job</button>
            </div>
          )}
          {loading ? (
            <div className="loading-panel">
              Connecting to the local research archive…
            </div>
          ) : (
            <>
              <div className="filters">
                <label className="experiment-filter">
                  Experiment
                  <select
                    aria-label="Experiment"
                    value={experimentId}
                    onChange={(e) => setExperimentId(e.target.value)}
                  >
                    <option value="" disabled>
                      Select an experiment
                    </option>
                    {experiments.map((e) => (
                      <option key={e.id} value={e.id}>
                        {e.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Evidence mode
                  <select
                    aria-label="Evidence mode"
                    value={mode}
                    onChange={(e) => {
                      setMode(e.target.value);
                      setWindowStart(
                        forecasts.find((f) => f.mode === e.target.value)
                          ?.start ?? "",
                      );
                    }}
                  >
                    {(experiment?.synthetic
                      ? ["synthetic_demo"]
                      : ["prospective", "retrospective_revised"]
                    ).map((m) => (
                      <option key={m} value={m}>
                        {modeLabel[m]}
                      </option>
                    ))}
                  </select>
                </label>
                {(tab === "atlas" || tab === "evaluation") && (
                  <label>
                    Model
                    <select
                      aria-label="Model"
                      value={modelId}
                      onChange={(e) => setModelId(e.target.value)}
                    >
                      <option value="" disabled>
                        No fitted model
                      </option>
                      {models.map((m) => (
                        <option key={m.id} value={m.id}>
                          {algorithmLabel[m.algorithm]} · {m.id.slice(0, 6)}
                          {m.status !== "available" ? " · unavailable" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
              {!forecasts.length && tab === "atlas" && (
                <section className="first-run panel">
                  <div>
                    <span className="eyebrow">START AN EXPERIMENT</span>
                    <h2>Every forecast starts with a catalogue.</h2>
                    <p>
                      Explore the complete workflow with artificial data, or
                      import USGS records for a separate real-data experiment.
                    </p>
                  </div>
                  <div className="first-run-actions">
                    <button
                      className="primary"
                      disabled={sending}
                      onClick={() => action("/demo")}
                    >
                      <Play size={16} />
                      Open synthetic demo
                    </button>
                    <button
                      className="secondary"
                      onClick={() => setTab("operations")}
                    >
                      <Database size={16} />
                      Set up real catalogue
                    </button>
                    <small>
                      Demo forecasts and scores are computed locally. No
                      credentials required.
                    </small>
                  </div>
                </section>
              )}
              {tab === "atlas" && (
                <>
                  <div className="atlas-meta">
                    <div>
                      <span
                        className={`badge ${experiment?.synthetic ? "demo" : ""}`}
                      >
                        {modeLabel[mode]}
                      </span>
                      <span className="muted">
                        M ≥ {String(experiment?.config.target_magnitude ?? 4)} ·
                        24 hours · EPSG:6933
                      </span>
                    </div>
                    <span className="muted">
                      {forecast
                        ? `${dateLabel(forecast.start, true)} → ${dateLabel(forecast.end, true)} JST`
                        : "No forecast published"}
                    </span>
                  </div>
                  <div
                    className={
                      compare ? "atlas-layout comparing" : "atlas-layout"
                    }
                  >
                    <section className="map-panel panel">
                      <div className="map-toolbar">
                        <div>
                          <Layers size={15} />
                          <span>Map layers</span>
                        </div>
                        <label>
                          <input
                            type="checkbox"
                            checked={showCells}
                            onChange={(e) => setShowCells(e.target.checked)}
                          />{" "}
                          Probabilities
                        </label>
                        <label>
                          <input
                            type="checkbox"
                            checked={showEvents}
                            onChange={(e) => setShowEvents(e.target.checked)}
                          />{" "}
                          Observed events
                        </label>
                        <label>
                          <input
                            type="checkbox"
                            checked={compare}
                            onChange={(e) => setCompare(e.target.checked)}
                          />{" "}
                          Compare baseline
                        </label>
                      </div>
                      <div className={compare ? "paired-maps" : ""}>
                        <div>
                          {compare && (
                            <div className="map-title">
                              {algorithmLabel[selectedModel?.algorithm ?? ""] ??
                                "Selected model"}
                            </div>
                          )}
                          <ForecastMap
                            data={data}
                            onSelect={selectCell}
                            selected={cell?.cell_id ?? null}
                            showCells={showCells}
                            showEvents={showEvents}
                            redThreshold={red}
                            yellowThreshold={yellow}
                          />
                        </div>
                        {compare && (
                          <div>
                            <div className="map-title">
                              Historical rate · identical scale and window
                            </div>
                            <ForecastMap
                              data={baselineData}
                              onSelect={selectCell}
                              selected={cell?.cell_id ?? null}
                              showCells={showCells}
                              showEvents={showEvents}
                              redThreshold={red}
                              yellowThreshold={yellow}
                            />
                          </div>
                        )}
                      </div>
                      <div className="history-control">
                        <button
                          className="icon-button"
                          aria-label="Previous day"
                          disabled={windows.indexOf(windowStart) <= 0}
                          onClick={() =>
                            setWindowStart(
                              windows[windows.indexOf(windowStart) - 1],
                            )
                          }
                        >
                          <ChevronLeft size={18} />
                        </button>
                        <input
                          aria-label="Forecast history"
                          type="range"
                          min={0}
                          max={Math.max(0, windows.length - 1)}
                          value={Math.max(0, windows.indexOf(windowStart))}
                          disabled={!windows.length}
                          onChange={(e) =>
                            setWindowStart(windows[Number(e.target.value)])
                          }
                        />
                        <button
                          className="icon-button"
                          aria-label="Next day"
                          disabled={
                            !windows.length ||
                            windows.indexOf(windowStart) >= windows.length - 1
                          }
                          onClick={() =>
                            setWindowStart(
                              windows[windows.indexOf(windowStart) + 1],
                            )
                          }
                        >
                          <ChevronRight size={18} />
                        </button>
                        <span>
                          {windowStart
                            ? dateLabel(windowStart)
                            : "No saved windows"}
                        </span>
                      </div>
                    </section>
                    <aside className="cell-panel panel">
                      <div className="panel-heading">
                        <h3>Cell inspector</h3>
                        <span className="eyebrow">50 KM GRID</span>
                      </div>
                      <label className="cell-select">
                        Select a cell
                        <select
                          aria-label="Inspect cell"
                          value={cell?.cell_id ?? ""}
                          onChange={(e) => selectCell(e.target.value)}
                        >
                          <option value="">Choose on map or select here</option>
                          {hotCells.map((c) => (
                            <option
                              key={c.properties?.cell_id}
                              value={c.properties?.cell_id}
                            >
                              {c.properties?.cell_id} ·{" "}
                              {percent(Number(c.properties?.probability), 2)}
                            </option>
                          ))}
                        </select>
                      </label>
                      {cell ? (
                        <>
                          <div className="cell-probability">
                            <span>CELL {cell.cell_id}</span>
                            <strong>{percent(cell.probability, 2)}</strong>
                            <p>Probability of at least one event</p>
                            <span className={`badge band-${cell.band}`}>
                              {cell.band} display band
                            </span>
                          </div>
                          <dl className="details">
                            <dt>Target</dt>
                            <dd>{cell.target}</dd>
                            <dt>Catalogue</dt>
                            <dd>{cell.source}</dd>
                            <dt>Model</dt>
                            <dd>
                              {algorithmLabel[cell.model]}
                              <small className="mono">
                                {cell.model_id.slice(0, 8)}
                              </small>
                            </dd>
                            <dt>Input cutoff</dt>
                            <dd>{dateLabel(cell.input_cutoff, true)} JST</dd>
                            <dt>Observed events</dt>
                            <dd>
                              {cell.observed_count ?? "Unknown"}
                              {cell.outcome !== "unknown" && (
                                <span className="badge">{cell.outcome}</span>
                              )}
                            </dd>
                          </dl>
                          <div className="cell-history">
                            <span className="eyebrow">
                              PREVIOUS SAVED PROBABILITIES
                            </span>
                            {cell.history.length ? (
                              <div className="mini-bars">
                                {[...cell.history].reverse().map((h) => (
                                  <div
                                    key={h.start}
                                    title={`${dateLabel(h.start)}: ${percent(h.probability, 2)}`}
                                  >
                                    <i
                                      style={{
                                        height: `${Math.max(3, Math.min(75, h.probability * 400))}px`,
                                      }}
                                    />
                                    <small>
                                      {new Date(h.start).getUTCDate()}
                                    </small>
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <p className="muted">
                                No earlier forecast for this model.
                              </p>
                            )}
                          </div>
                        </>
                      ) : (
                        <div className="inspector-empty">
                          <Map size={30} />
                          <h3>Inspect the evidence.</h3>
                          <p>
                            Select any cell to see its saved probability,
                            source, and observed outcome.
                          </p>
                        </div>
                      )}
                    </aside>
                  </div>
                  <div className="section-heading">
                    <h2>Forecast meets observation</h2>
                    <span className="badge">
                      {evaluation
                        ? evaluation.status.replaceAll("_", " ")
                        : "Not yet evaluated"}
                      {evaluation ? ` · revision ${evaluation.version}` : ""}
                    </span>
                  </div>
                  <ScoreStrip scores={scores ?? null} />
                  <div className="atlas-foot">
                    <p>
                      {scores?.missing
                        ? `${scores.missing.toLocaleString()} cells excluded: observation coverage unavailable.`
                        : "Display bands are fixed experimental thresholds, not measures of shaking or damage."}
                    </p>
                    <button
                      className="text-button"
                      onClick={() => setTab("evaluation")}
                    >
                      Explore evaluation <ArrowRight size={15} />
                    </button>
                  </div>
                  {forecast && (
                    <div className="provenance-line">
                      <span>
                        Issued {dateLabel(forecast.issued_at, true)} JST · saved
                        input cutoff {dateLabel(forecast.input_cutoff, true)}{" "}
                        JST
                      </span>
                      <a href={`/api/exports/forecasts/${forecast.id}`}>
                        <ArrowDownToLine size={14} />
                        Export forecast
                      </a>
                    </div>
                  )}
                </>
              )}
              {tab === "evaluation" && (
                <>
                  <div className="section-heading">
                    <h2>Matched observation windows</h2>
                    <label className="inline-label">
                      Period
                      <select
                        aria-label="Score period"
                        value={period}
                        onChange={(e) => setPeriod(Number(e.target.value))}
                      >
                        {[
                          [1, "Daily"],
                          [7, "7 days"],
                          [30, "30 days"],
                          [90, "90 days"],
                          [0, "Whole experiment"],
                        ].map(([n, label]) => (
                          <option key={n} value={n}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <ScoreStrip
                    scores={
                      report?.comparisons.find((r) => r.model_id === modelId)
                        ?.metrics ?? null
                    }
                  />
                  <div className="chart-grid">
                    <HistoryChart report={report} />
                    <Calibration scores={scores ?? null} />
                  </div>
                  <ComparisonTable report={report} />
                  <div className="chart-grid">
                    <section className="panel">
                      <div className="panel-heading">
                        <h3>Selected window · confusion matrix</h3>
                        <span className="muted">
                          {windowStart
                            ? dateLabel(windowStart)
                            : "Select a saved window"}
                        </span>
                      </div>
                      <div className="confusion-grid">
                        {[
                          ["Affected cells identified", scores?.tp, "TP"],
                          ["False alarms", scores?.fp, "FP"],
                          ["Missed affected cells", scores?.fn, "FN"],
                          ["Correct quiet cells", scores?.tn, "TN"],
                        ].map(([label, value, key]) => (
                          <button
                            key={key}
                            onClick={() => {
                              setTab("atlas");
                              const found = data?.cells.features.find(
                                (c) => c.properties?.outcome === key,
                              );
                              if (found) selectCell(found.properties!.cell_id);
                            }}
                            disabled={!value}
                          >
                            <span>{label}</span>
                            <strong>{value ?? "N/A"}</strong>
                            <small>{key} · inspect a cell</small>
                          </button>
                        ))}
                      </div>
                      <p className="panel-note">
                        Binary cell-day labels. Several earthquakes in one cell
                        count as one affected cell.
                      </p>
                    </section>
                    <section className="panel">
                      <div className="panel-heading">
                        <h3>Evaluation archive</h3>
                        <span className="muted">Immutable versions</span>
                      </div>
                      {versions.length ? (
                        versions.map((v) => (
                          <details className="revision" key={v.id}>
                            <summary>
                              Revision {v.version} ·{" "}
                              {v.status.replaceAll("_", " ")}
                            </summary>
                            <p>{v.reason}</p>
                            <pre>{JSON.stringify(v.metrics, null, 2)}</pre>
                          </details>
                        ))
                      ) : (
                        <p className="empty-inline">
                          No evaluations for this forecast window.
                        </p>
                      )}
                      <p className="panel-note">
                        Provisional scores remain provisional until the declared
                        catalogue policy supports finalization.
                      </p>
                    </section>
                  </div>
                  {experimentId && (
                    <a
                      className="export-link"
                      href={`/api/exports/metrics?experiment_id=${experimentId}&mode=${mode}`}
                    >
                      <ArrowDownToLine size={15} />
                      Export comparison report
                    </a>
                  )}
                </>
              )}
              {tab === "models" && (
                <ModelLab
                  models={models}
                  experiment={experiment}
                  action={action}
                  sending={sending}
                  report={report}
                  onError={setError}
                  onRefresh={() => setRefresh((r) => r + 1)}
                />
              )}
              {tab === "operations" && (
                <Operations
                  experiment={experiment}
                  sources={sources}
                  jobs={jobs}
                  action={action}
                  sending={sending}
                  onError={setError}
                  onRefresh={() => setRefresh((r) => r + 1)}
                />
              )}
            </>
          )}
          <footer>
            <span>YureAtlas · occurrence forecasting research</span>
            <span>All times displayed in Japan Standard Time (UTC+9)</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

function ComparisonTable({ report }: { report: MetricReport | null }) {
  return (
    <section className="panel comparison-panel">
      <div className="panel-heading">
        <h3>Model comparison</h3>
        <span className="muted">
          Matching dates, catalogue revisions, and eligible cells
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
              <th>Paired / excluded days</th>
            </tr>
          </thead>
          <tbody>
            {report?.comparisons.map((row) => (
              <tr key={row.model_id}>
                <td>
                  {algorithmLabel[row.algorithm]}
                  <small className="mono">{row.model_id.slice(0, 8)}</small>
                </td>
                <td>{decimal(row.paired_metrics.brier)}</td>
                <td>{decimal(row.paired_metrics.log_loss)}</td>
                <td>{percent(row.brier_skill)}</td>
                <td>{percent(row.paired_metrics.recall)}</td>
                <td>{percent(row.paired_metrics.precision)}</td>
                <td>
                  {row.paired_days} / {row.excluded_days}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!report?.comparisons.length && (
        <p className="empty-inline">
          No comparable completed forecasts. Run a backtest or wait for a
          prospective window to close.
        </p>
      )}
      <p className="panel-note">
        Brier skill compares identical pairs with the historical baseline.{" "}
        {report?.comparisons.some((c) => c.uncertainty.status === "available")
          ? "Paired seven-day block intervals are included in the exported report."
          : "Insufficient consecutive days for paired time-block uncertainty; no winner is declared."}
      </p>
    </section>
  );
}

type Action = (path: string, body?: unknown) => Promise<Job | undefined>;
function ModelLab({
  models,
  experiment,
  action,
  sending,
  report,
  onError,
  onRefresh,
}: {
  models: Model[];
  experiment: Experiment | undefined;
  action: Action;
  sending: boolean;
  report: MetricReport | null;
  onError: (s: string) => void;
  onRefresh: () => void;
}) {
  const [fit, setFit] = useState(180),
    [tune, setTune] = useState(30),
    [calibrate, setCalibrate] = useState(30),
    [testDays, setTestDays] = useState(7);
  const [start, setStart] = useState("2024-01-16");
  const [reason, setReason] = useState(
    "Explicit local research selection after reviewing matched comparisons",
  );
  return (
    <>
      <section className="panel training-panel">
        <div>
          <span className="eyebrow">CHRONOLOGICAL PARTITIONS</span>
          <h2>Fit candidates. Preserve the record.</h2>
          <p className="muted">
            All cells from a day stay together. Test outcomes are excluded from
            model selection.
          </p>
        </div>
        <div className="training-fields">
          {[
            ["Fit days", fit, setFit],
            ["Tune days", tune, setTune],
            ["Calibration days", calibrate, setCalibrate],
            ["Test days", testDays, setTestDays],
          ].map(([label, value, setter]) => (
            <label key={label as string}>
              {label as string}
              <input
                type="number"
                min={label === "Fit days" ? 30 : label === "Test days" ? 1 : 7}
                max={label === "Fit days" ? 730 : 180}
                value={value as number}
                onChange={(e) =>
                  (setter as (v: number) => void)(Number(e.target.value))
                }
              />
            </label>
          ))}
          <label>
            First test day · JST
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
        </div>
        <div className="button-row">
          <button
            className="primary"
            disabled={!experiment || sending}
            onClick={() =>
              action("/training-runs", {
                experiment_id: experiment?.id,
                fit_days: fit,
                tune_days: tune,
                calibration_days: calibrate,
                ...(experiment?.synthetic
                  ? { end: "2024-01-15T14:30:00Z" }
                  : {}),
              })
            }
          >
            <FlaskConical size={16} />
            Train candidates
          </button>
          <button
            className="secondary"
            disabled={!experiment || sending || !start}
            onClick={() =>
              action("/backtests", {
                experiment_id: experiment?.id,
                start: `${start}T00:00:00+09:00`,
                fit_days: fit,
                tune_days: tune,
                calibration_days: calibrate,
                test_days: testDays,
              })
            }
          >
            <Play size={16} />
            Run historical backtest
          </button>
        </div>
      </section>
      <div className="section-heading">
        <h2>Version registry</h2>
        <span className="muted">{models.length} saved versions</span>
      </div>
      <label className="promotion-reason">
        Promotion rationale
        <input
          value={reason}
          minLength={5}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      <div className="model-grid">
        {models.map((m) => (
          <section className="panel model-card" key={m.id}>
            <div className="panel-heading">
              <h3>{algorithmLabel[m.algorithm]}</h3>
              <span
                className={`badge ${m.status === "available" ? "available" : ""}`}
              >
                {m.status.replaceAll("_", " ")}
              </span>
            </div>
            <p className="mono muted">{m.id}</p>
            <dl className="details">
              <dt>Training cutoff</dt>
              <dd>{dateLabel(m.training_cutoff, true)} JST</dd>
              <dt>Available since</dt>
              <dd>{dateLabel(m.available_at, true)} JST</dd>
              <dt>Artifact SHA-256</dt>
              <dd className="hash">
                {m.artifact
                  ? m.artifact.split("/").at(-1)?.split(".")[0]
                  : "No fitted artifact"}
              </dd>
            </dl>
            {Boolean(m.metadata.reason) && (
              <p className="model-reason">{String(m.metadata.reason)}</p>
            )}
            {m.algorithm === "calibrated_hybrid" && (
              <p className="panel-note">
                Historical rate + ML. ETAS is not a component.
              </p>
            )}
            {m.algorithm === "etas_discrete_space" && (
              <p className="panel-note">
                Fitted research implementation. External scientific validation
                pending.
              </p>
            )}
            <details>
              <summary>Partitions, parameters & diagnostics</summary>
              <pre>{JSON.stringify(m.metadata, null, 2)}</pre>
            </details>
            <button
              className="secondary"
              disabled={m.status !== "available" || reason.length < 5}
              onClick={async () => {
                try {
                  await request("/model-promotions", {
                    model_id: m.id,
                    reason,
                  });
                  onRefresh();
                } catch (e) {
                  onError((e as Error).message);
                }
              }}
            >
              Promote for future forecasts <ArrowRight size={14} />
            </button>
          </section>
        ))}
      </div>
      {!models.length && (
        <p className="empty-inline">
          Import enough history, then train the first set of candidates.
        </p>
      )}
      <ComparisonTable report={report} />
    </>
  );
}

function Operations({
  experiment,
  sources,
  jobs,
  action,
  sending,
  onError,
  onRefresh,
}: {
  experiment: Experiment | undefined;
  sources: SourceReport | null;
  jobs: Job[];
  action: Action;
  sending: boolean;
  onError: (s: string) => void;
  onRefresh: () => void;
}) {
  const [historyStart, setHistoryStart] = useState("2015-01-01");
  const [importStart, setImportStart] = useState("2023-01-01"),
    [importEnd, setImportEnd] = useState("2024-01-01"),
    [namespace, setNamespace] = useState("h2023"),
    [format, setFormat] = useState("jma"),
    [file, setFile] = useState<File | null>(null);
  async function upload() {
    if (!file) return;
    try {
      const exp =
        experiment?.config.provider === "jma"
          ? experiment
          : await request<Experiment>("/experiments", {
              name: `jma_mj4_${namespace.replace(/[^a-z0-9_]/g, "_")}_v1`,
              provider: "jma",
            });
      const form = new FormData();
      form.set("experiment_id", exp.id);
      form.set("start", `${importStart}T00:00:00+09:00`);
      form.set("end", `${importEnd}T00:00:00+09:00`);
      form.set("namespace", namespace);
      form.set("format", format);
      form.set("file", file);
      const response = await fetch("/api/imports", {
        method: "POST",
        headers: { "X-YureAtlas-Request": "1" },
        body: form,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail);
      onRefresh();
    } catch (e) {
      onError((e as Error).message);
    }
  }
  return (
    <>
      <div className="operations-grid">
        <section className="panel operation-card">
          <Database size={22} />
          <h2>USGS real catalogue</h2>
          <p>
            Import preferred-magnitude earthquake records into a separate
            real-data experiment.
          </p>
          <label>
            Historical start · UTC
            <input
              type="date"
              value={historyStart}
              onChange={(e) => setHistoryStart(e.target.value)}
            />
          </label>
          <div className="button-row">
            <button
              className="primary"
              disabled={sending}
              onClick={() =>
                action("/ingestions", {
                  ...(experiment?.config.provider === "usgs"
                    ? { experiment_id: experiment.id }
                    : {}),
                  start: `${historyStart}T00:00:00Z`,
                })
              }
            >
              Import history <ArrowRight size={15} />
            </button>
            <button
              className="secondary"
              disabled={sending}
              onClick={() =>
                action(
                  "/ingestions",
                  experiment?.config.provider === "usgs"
                    ? { experiment_id: experiment.id }
                    : {},
                )
              }
            >
              <RefreshCw size={15} />
              Refresh recent
            </button>
          </div>
          <small>
            Complete retrieval coverage does not establish scientific catalogue
            completeness.
          </small>
        </section>
        <section className="panel operation-card">
          <Activity size={22} />
          <h2>Run the research loop</h2>
          <p>
            Jobs run in the separate local worker and keep their progress across
            restarts.
          </p>
          <div className="stack-buttons">
            <button
              className="secondary"
              onClick={() => action("/demo")}
              disabled={sending}
            >
              <Play size={15} />
              Open synthetic demo
            </button>
            <button
              className="secondary"
              disabled={sending || !experiment || experiment.synthetic}
              onClick={() =>
                action("/forecasts", { experiment_id: experiment?.id })
              }
            >
              <ArrowRight size={15} />
              Issue future 24h forecast
            </button>
            <button
              className="secondary"
              disabled={sending || !experiment}
              onClick={() =>
                action("/evaluations", { experiment_id: experiment?.id })
              }
            >
              <Check size={15} />
              Evaluate completed windows
            </button>
          </div>
          <small>
            Manual forecasts start 30 minutes after request time. Daily
            publications are scheduled before midnight JST.
          </small>
        </section>
      </div>
      <section className="panel">
        <div className="panel-heading">
          <h3>Catalogue health</h3>
          <span className="muted">
            {sources?.worker?.online
              ? "Worker connected"
              : "Start worker to process queued jobs"}
          </span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>State</th>
                <th>Event identities</th>
                <th>Last complete retrieval</th>
                <th>Coverage reports</th>
              </tr>
            </thead>
            <tbody>
              {sources?.sources.map((source) => (
                <tr key={source.id}>
                  <td>
                    {source.provider}
                    <small>{source.name}</small>
                  </td>
                  <td>
                    <span className="badge">{source.status}</span>
                  </td>
                  <td>{source.event_count.toLocaleString()}</td>
                  <td>
                    {source.details.last_success
                      ? dateLabel(source.details.last_success, true)
                      : "No complete retrieval"}
                  </td>
                  <td>
                    <details>
                      <summary>Inspect intervals</summary>
                      <div className="coverage-list">
                        {source.coverage.map((c, i) => (
                          <div key={i}>
                            <strong>
                              {dateLabel(c.start)} → {dateLabel(c.end)}
                            </strong>
                            <span className="badge">{c.status}</span>
                            <pre>{JSON.stringify(c.report, null, 2)}</pre>
                          </div>
                        ))}
                      </div>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel import-panel">
        <div className="panel-heading">
          <h3>Import a JMA catalogue</h3>
          <span className="muted">
            Official fixed-width file or canonical CSV
          </span>
        </div>
        <p className="panel-note">
          JMA data remain separate. Provide the uncompressed file and its
          coverage interval. Stable namespace + line number identifies official
          records.
        </p>
        <div className="import-fields">
          <label>
            Format
            <select value={format} onChange={(e) => setFormat(e.target.value)}>
              <option value="jma">JMA 96-byte records</option>
              <option value="csv">Canonical CSV</option>
            </select>
          </label>
          <label>
            File namespace
            <input
              value={namespace}
              onChange={(e) => setNamespace(e.target.value)}
            />
          </label>
          <label>
            Coverage start · JST
            <input
              type="date"
              value={importStart}
              onChange={(e) => setImportStart(e.target.value)}
            />
          </label>
          <label>
            Coverage end · exclusive
            <input
              type="date"
              value={importEnd}
              onChange={(e) => setImportEnd(e.target.value)}
            />
          </label>
          <label>
            Uncompressed file · max 32 MiB
            <input
              type="file"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <button
            className="secondary"
            disabled={!file || !namespace}
            onClick={upload}
          >
            Validate & import
          </button>
        </div>
      </section>
      <div className="section-heading">
        <h2>Background jobs</h2>
        <span className="muted">Durable PostgreSQL queue</span>
      </div>
      <section className="panel">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Job / created JST</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Attempts</th>
                <th>Result</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td>
                    {j.kind}
                    <small>{dateLabel(j.created_at, true)}</small>
                  </td>
                  <td>
                    <span
                      className={`badge ${j.status === "completed" ? "available" : j.status === "failed" ? "failed" : ""}`}
                    >
                      {j.status}
                    </span>
                  </td>
                  <td>
                    <progress value={j.progress} max={1} />
                  </td>
                  <td>{j.attempts}</td>
                  <td>
                    {j.error ? (
                      <span className="error-text">{j.error}</span>
                    ) : (
                      <details>
                        <summary>Details</summary>
                        <pre>{JSON.stringify(j.result, null, 2)}</pre>
                      </details>
                    )}
                  </td>
                  <td>
                    {["queued", "running"].includes(j.status) && (
                      <button
                        className="icon-button"
                        aria-label={`Cancel ${j.kind} job`}
                        onClick={() => action(`/jobs/${j.id}/cancel`)}
                      >
                        <Square size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!jobs.length && (
          <p className="empty-inline">
            No queued work. Start an import, demo, or training run.
          </p>
        )}
      </section>
      <section className="schedule-panel panel">
        <Terminal size={20} />
        <div>
          <h3>Local schedule · Asia/Tokyo</h3>
          <p>
            Ingest every 15 minutes · publish next day at 23:40 with a 23:30
            cutoff · evaluate at 06:00 · reconcile at 06:30 · retrain Sundays at
            03:00.
          </p>
          <small>
            Schedules run while your computer and worker are running. Missed
            publication deadlines stay missed.
          </small>
        </div>
        <span className="badge">
          {sources?.schedule_enabled ? "Enabled" : "Disabled"}
        </span>
      </section>
    </>
  );
}
