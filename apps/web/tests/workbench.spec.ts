import { expect, test } from "@playwright/test";

test("computed demo, cell evidence, evaluation, and durable job", async ({
  page,
  request,
}) => {
  const faults: string[] = [];
  page.on("pageerror", (e) => faults.push(e.message));
  const seeded = await request.post("/api/demo", {
    headers: { "X-YureAtlas-Request": "1" },
    data: {},
  });
  expect(seeded.status()).toBe(202);
  const job = await seeded.json();
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/jobs/${job.id}`);
        return (await response.json()).status;
      },
      { timeout: 600_000, intervals: [2000] },
    )
    .toBe("completed");
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Forecast atlas", exact: true }),
  ).toBeVisible();
  const experiments = await (await request.get("/api/experiments")).json();
  const demo = experiments.find(
    (e: { name: string }) => e.name === "synthetic_japan_m4_v1",
  );
  await page.getByLabel("Experiment", { exact: true }).selectOption(demo.id);
  await expect(
    page.getByText("Synthetic demonstration", { exact: true }).last(),
  ).toBeVisible();
  await expect(
    page.getByTestId("forecast-map").locator("canvas"),
  ).toBeVisible();
  await expect
    .poll(() => page.getByLabel("Inspect cell").locator("option").count())
    .toBeGreaterThan(1);
  await page.getByLabel("Inspect cell").selectOption({ index: 1 });
  await expect(
    page.getByText("Probability of at least one event", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Observed events", { exact: true }).last(),
  ).toBeVisible();
  await page.getByLabel("Observed events", { exact: true }).uncheck();
  await page.getByLabel("Observed events", { exact: true }).check();
  await page.screenshot({
    path: "test-results/atlas-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Evaluation", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Model comparison", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: /Historical rate/ }).first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Data & operations", exact: true })
    .click();
  const pending = page.waitForResponse(
    (r) =>
      r.url().endsWith("/api/evaluations") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Evaluate completed windows" })
    .click();
  const evaluate = await (await pending).json();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/jobs/${evaluate.id}`)).json()).status,
      { timeout: 60000 },
    )
    .toBe("completed");
  expect(faults).toEqual([]);
});

test("mobile layout stays within viewport and exposes map controls", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByLabel("Experiment", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Japan region probability map" }),
  ).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "test-results/atlas-mobile.png",
    fullPage: true,
  });
});

test("loaded demo works with all external requests blocked", async ({
  page,
}) => {
  const external: string[] = [];
  await page.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) {
      external.push(url.href);
      return route.abort();
    }
    return route.continue();
  });
  await page.goto("/");
  await expect
    .poll(() => page.getByLabel("Inspect cell").locator("option").count())
    .toBeGreaterThan(1);
  await expect(
    page.getByTestId("forecast-map").locator("canvas"),
  ).toBeVisible();
  expect(external).toEqual([]);
});
