import { readFile } from "node:fs/promises";
import { expect, test, type Page, type Route } from "@playwright/test";
import type { components } from "../lib/schema";
import type { MapData } from "../lib/api";
import {
  experiment,
  forecasts,
  models,
  pooled,
  reportFixture,
} from "./fixtures/reports";

type Report = components["schemas"]["MetricReport"];
type MockState = {
  metrics: URL[];
  maps: string[];
  exported: number;
  delivered: Report[];
  unexpected: string[];
};
type MetricHandler = (
  route: Route,
  url: URL,
  state: MockState,
) => Promise<boolean>;

test.skip(
  process.env.YUREATLAS_MOCK_UI !== "1" || !process.env.WEB_URL,
  "Use an isolated frontend, explicit WEB_URL and YUREATLAS_MOCK_UI=1. Every API call is intercepted.",
);
test.use({ timezoneId: "America/Los_Angeles" });
test.setTimeout(60_000);

function mapFixture(id: string, mismatch = false): MapData {
  const brier =
    id === "chosen-reference" ? 0.25 : id === "candidate-two" ? 0.165 : 0.265;
  return {
    cells: {
      type: "FeatureCollection",
      features: [0, 1].map((index) => ({
        type: "Feature",
        properties: {
          cell_id: "cell-" + index,
          probability: id === "chosen-reference" ? 0.5 : 0.2,
          band: "yellow",
          observed_count: index,
          outcome: index ? "FN" : "TN",
        },
        geometry: {
          type: "Polygon",
          coordinates: [
            [
              [140 + index, 35],
              [140.5 + index, 35],
              [140.5 + index, 35.5],
              [140 + index, 35.5],
              [140 + index, 35],
            ],
          ],
        },
      })),
    },
    events: { type: "FeatureCollection", features: [] },
    evaluation: {
      id: mismatch ? "newer-map-evaluation" : "evaluation-" + id,
      forecast_id: id,
      snapshot_id: "observation-snapshot",
      version: mismatch ? 2 : 1,
      status: "provisional",
      reason: "Mock archived evaluation",
      metrics: {
        ...pooled(brier),
        calibration: [
          { lower: 0, upper: 0.5, count: 2, predicted: 0.25, observed: 0.5 },
        ],
      },
    },
  };
}

async function openWorkbench(
  page: Page,
  options: {
    metricHandler?: MetricHandler;
    mismatch?: boolean;
    mapFailure?: boolean;
  } = {},
) {
  const state: MockState = {
    metrics: [],
    maps: [],
    exported: 0,
    delivered: [],
    unexpected: [],
  };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let result: unknown;
    if (path === "/api/experiments") result = [experiment];
    else if (path === "/api/models") result = models;
    else if (path === "/api/forecasts") result = forecasts;
    else if (path === "/api/jobs") result = [];
    else if (path === "/api/sources")
      result = { sources: [], worker: null, schedule_enabled: false };
    else if (path === "/api/metrics") {
      state.metrics.push(url);
      if (await options.metricHandler?.(route, url, state)) return;
      const report = reportFixture(url.searchParams);
      state.delivered.push(report);
      result = report;
    } else if (path === "/api/exports/metrics") {
      state.exported += 1;
      result = { changed_server_report: true };
    } else if (/^\/api\/forecasts\/[^/]+\/map$/.test(path)) {
      const id = path.split("/")[3];
      state.maps.push(id);
      if (options.mapFailure && id === "candidate-one") {
        await route.fulfill({
          status: 503,
          json: { detail: "Mock archived map unavailable" },
        });
        return;
      }
      result = mapFixture(id, options.mismatch && id === "candidate-one");
    } else if (/^\/api\/forecasts\/[^/]+\/evaluations$/.test(path)) {
      const id = path.split("/")[3];
      result = [
        mapFixture(id, options.mismatch && id === "candidate-one").evaluation,
      ];
    } else {
      state.unexpected.push(path);
      result = [];
    }
    await route.fulfill({ json: result });
  });
  await page.goto("/");
  await expect(page.getByLabel("Experiment", { exact: true })).toHaveValue(
    experiment.id,
  );
  await expect(page.getByLabel("Evidence mode", { exact: true })).toHaveValue(
    "retrospective_revised",
  );
  await page
    .getByLabel("Model", { exact: true })
    .selectOption("candidate-model");
  await page
    .getByLabel("Saved forecast window", { exact: true })
    .selectOption("candidate-one");
  return state;
}

async function evaluationScreen(page: Page) {
  await page.getByRole("button", { name: "Evaluation", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Displayed report scope" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Download displayed report" }),
  ).toBeEnabled();
}

test("scope changes preserve JST exclusive bounds and download the exact loaded report", async ({
  page,
}, testInfo) => {
  const state = await openWorkbench(page);
  await evaluationScreen(page);
  await page.getByLabel("Score period").selectOption("7");
  await expect
    .poll(() => state.metrics.at(-1)?.searchParams.get("days"))
    .toBe("7");
  await expect(
    page.getByRole("button", { name: "Download displayed report" }),
  ).toBeEnabled();
  await page.getByLabel("Evidence mode").selectOption("prospective");
  await expect
    .poll(() => state.metrics.at(-1)?.searchParams.get("mode"))
    .toBe("prospective");
  await page.getByLabel("Evidence mode").selectOption("retrospective_revised");
  await page.getByLabel("Score period").selectOption("custom");
  await page
    .getByLabel("Report start date (JST, inclusive)")
    .fill("2025-01-01");
  await page.getByLabel("Report end date (JST, exclusive)").fill("2025-01-03");
  await expect
    .poll(() => state.metrics.at(-1)?.searchParams.get("end"))
    .toBe("2025-01-03T00:00:00+09:00");
  const query = state.metrics.at(-1)!.searchParams;
  expect(query.has("days")).toBe(false);
  expect(new Date(query.get("start")!).toISOString()).toBe(
    "2024-12-31T15:00:00.000Z",
  );
  expect(new Date(query.get("end")!).toISOString()).toBe(
    "2025-01-02T15:00:00.000Z",
  );
  await expect(
    page.getByRole("region", { name: "Displayed report scope" }),
  ).toContainText("start inclusive, end exclusive");
  const expected = reportFixture(query);
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download displayed report" }).click();
  const download = await downloadEvent;
  const saved = testInfo.outputPath("displayed-report.json");
  await download.saveAs(saved);
  expect(JSON.parse(await readFile(saved, "utf8"))).toEqual(expected);
  expect(state.exported).toBe(0);
  expect(state.unexpected).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("report-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByLabel("Report start date (JST, inclusive)"),
  ).toBeVisible();
  const controls = await page.locator(".report-date-controls").boundingBox();
  expect(controls!.x).toBeGreaterThanOrEqual(0);
  expect(controls!.x + controls!.width).toBeLessThanOrEqual(391);
  await page.screenshot({
    path: testInfo.outputPath("report-mobile.png"),
    fullPage: true,
  });
});

for (const staleResult of ["success", "error"] as const) {
  test(`a late ${staleResult} from an earlier period cannot replace the current report`, async ({
    page,
  }, testInfo) => {
    let held: Route | undefined;
    const state = await openWorkbench(page, {
      metricHandler: async (route, url) => {
        if (url.searchParams.get("days") === "7") {
          held = route;
          return true;
        }
        return false;
      },
    });
    await evaluationScreen(page);
    await page.getByLabel("Score period").selectOption("7");
    await expect.poll(() => Boolean(held)).toBe(true);
    await expect(
      page.getByRole("button", { name: "Download displayed report" }),
    ).toBeDisabled();
    await page.getByLabel("Score period").selectOption("90");
    await expect
      .poll(() => state.metrics.at(-1)?.searchParams.get("days"))
      .toBe("90");
    await expect(
      page.getByRole("button", { name: "Download displayed report" }),
    ).toBeEnabled();
    if (staleResult === "success")
      await held!.fulfill({
        json: reportFixture(
          new URLSearchParams("days=7&mode=retrospective_revised"),
        ),
      });
    else
      await held!.fulfill({
        status: 500,
        json: { detail: "Obsolete seven-day failure" },
      });
    await page
      .getByText("Report identity and policies", { exact: true })
      .click();
    await expect(page.getByText("9".repeat(64), { exact: true })).toBeVisible();
    await expect(page.getByText("7".repeat(64), { exact: true })).toHaveCount(
      0,
    );
    await expect(
      page.getByText("Obsolete seven-day failure", { exact: true }),
    ).toHaveCount(0);
    const event = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "Download displayed report" })
      .click();
    const downloaded = await event;
    const saved = testInfo.outputPath(`latest-after-${staleResult}.json`);
    await downloaded.saveAs(saved);
    expect(JSON.parse(await readFile(saved, "utf8"))).toEqual(
      reportFixture(new URLSearchParams("days=90&mode=retrospective_revised")),
    );
    expect(state.exported).toBe(0);
  });
}

test("server reference identity drives map comparison and every saved same-day forecast remains selectable", async ({
  page,
}) => {
  const state = await openWorkbench(page);
  const windows = page.getByLabel("Saved forecast window");
  await expect(windows.locator("option")).toHaveCount(2);
  await windows.selectOption("candidate-two");
  await expect.poll(() => state.maps.includes("candidate-two")).toBe(true);
  await windows.selectOption("candidate-one");
  await expect.poll(() => state.maps.at(-1)).toBe("candidate-one");
  const mapsBeforeComparison = state.maps.length;
  await page.getByLabel("Compare baseline", { exact: true }).check();
  await expect
    .poll(() => state.maps.slice(mapsBeforeComparison))
    .toContain("chosen-reference");
  expect(state.maps.slice(mapsBeforeComparison)).not.toContain(
    "wrong-reference",
  );
  await expect(page.getByLabel("Matched Brier difference")).toHaveAttribute(
    "aria-disabled",
    "false",
  );
  await expect(page.getByLabel("Matched Brier difference")).toHaveText(
    "0.01500",
  );
  await evaluationScreen(page);
  await expect(
    page.getByRole("heading", {
      name: "Selected-window probability calibration",
    }),
  ).toBeVisible();
  await page
    .getByText("Inspect 3 scored forecast windows", { exact: true })
    .click();
  const history = page.getByRole("table", { name: "Score history evidence" });
  await expect(history.getByRole("row")).toHaveCount(4);
  for (const [id, score] of [
    ["candidate-one", "0.26500"],
    ["candidate-two", "0.16500"],
  ]) {
    const row = history
      .getByRole("row")
      .filter({ has: page.getByText(id, { exact: true }) });
    await expect(row).toContainText(score);
    await expect(row).toContainText("evaluation-" + id);
    await expect(row.locator("time").first()).toHaveAttribute(
      "datetime",
      forecasts[0].start,
    );
    await expect(row.locator("time").last()).toHaveAttribute(
      "datetime",
      forecasts[0].end,
    );
  }
  await page.getByText(/Gradient boosting.*references and exclusions/).click();
  const decisions = page.getByRole("table", {
    name: "Reference decisions for candidate-model",
  });
  await expect(
    decisions.getByText("candidate-one", { exact: true }),
  ).toBeVisible();
  await expect(
    decisions.getByText("candidate-two", { exact: true }),
  ).toBeVisible();
  await expect(
    decisions.getByText("chosen-reference", { exact: true }),
  ).toHaveCount(2);
  await expect(
    decisions.getByText("evaluation-chosen-reference", { exact: true }),
  ).toHaveCount(2);
  await page
    .getByText(/Historical rate.*wrong-ba.*references and exclusions/)
    .click();
  await expect(
    page.getByRole("table", {
      name: "Reference decisions for wrong-baseline-model",
    }),
  ).toContainText("No compatible reference");
  await expect(
    page.getByRole("table", {
      name: "Reference decisions for wrong-baseline-model",
    }),
  ).toContainText(/invalid/i);
  expect(state.unexpected).toEqual([]);
});

test("map evaluation mismatch disables the displayed difference while retaining reference provenance", async ({
  page,
}) => {
  const state = await openWorkbench(page, { mismatch: true });
  await page.getByLabel("Compare baseline", { exact: true }).check();
  await expect.poll(() => state.maps.includes("chosen-reference")).toBe(true);
  await expect(page.getByLabel("Matched Brier difference")).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await expect(page.getByLabel("Matched Brier difference")).toHaveText("N/A");
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: /evaluation.*changed|changed.*evaluation/i }),
  ).toBeVisible();
});

test("bounded rejection examples retain complete counts and explain where reference search stopped", async ({
  page,
}) => {
  const state = await openWorkbench(page, {
    metricHandler: async (route, url, state) => {
      const report = reportFixture(url.searchParams);
      const diagnostics = {
        reference_rejection_limit: 20,
        reference_rejection_count: 25,
        reference_rejections_omitted: 5,
        reference_rejection_reasons: {
          revision_mismatch: 24,
          label_mismatch: 1,
        },
        reference_rejections: Array.from({ length: 20 }, (_, index) => ({
          forecast_id: `rejected-reference-${String(index + 1).padStart(2, "0")}`,
          reasons: ["revision_mismatch"],
        })),
      };
      for (const decision of [
        ...report.decisions,
        ...report.comparisons.flatMap((row) => row.decisions),
      ]) {
        if (decision.forecast_id === "candidate-one")
          Object.assign(decision, diagnostics);
      }
      state.delivered.push(report);
      await route.fulfill({ json: report });
      return true;
    },
  });
  await evaluationScreen(page);
  await page.getByText(/Gradient boosting.*references and exclusions/).click();
  const decision = page
    .getByRole("table", { name: "Reference decisions for candidate-model" })
    .getByRole("row")
    .filter({ has: page.getByText("candidate-one", { exact: true }) });
  await expect(
    decision.getByText("chosen-reference", { exact: true }),
  ).toBeVisible();
  await decision
    .getByText("Reference rejections: 25 · 5 omitted examples", { exact: true })
    .click();
  await expect(decision).toContainText(
    "Showing 20 earliest rejected-reference examples (up to 20); 5 additional identities omitted.",
  );
  await expect(decision).toContainText(
    "Search stopped at selection; later candidates are not included.",
  );
  await expect(
    decision.getByText("Observed cell labels differ: 1", { exact: true }),
  ).toBeVisible();
  const examples = decision.locator("ol").getByRole("listitem");
  await expect(examples).toHaveCount(20);
  await expect(examples.first()).toContainText("rejected-reference-01");
  await expect(examples.last()).toContainText("rejected-reference-20");
  await expect(decision.locator("ol")).not.toContainText(
    "Observed cell labels differ",
  );
  expect(state.unexpected).toEqual([]);
});

test("an unavailable selected map leaves the difference disabled and explains the failure", async ({
  page,
}) => {
  const state = await openWorkbench(page, { mapFailure: true });
  await page.getByLabel("Compare baseline", { exact: true }).check();
  await expect(page.getByLabel("Matched Brier difference")).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await expect(page.getByLabel("Matched Brier difference")).toHaveText("N/A");
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Selected forecast map unavailable" }),
  ).toContainText("Mock archived map unavailable");
  expect(state.unexpected).toEqual([]);
});
