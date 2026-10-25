import { expect, test, type Page } from "@playwright/test";
import { emptyReport } from "./fixtures/reports";

test.skip(
  process.env.YUREATLAS_MOCK_UI !== "1" || !process.env.WEB_URL,
  "Run against an isolated frontend with WEB_URL and YUREATLAS_MOCK_UI=1; all API requests are mocked.",
);
test.setTimeout(30_000);

async function importScreen(page: Page, catalogueNamespace?: string) {
  const experiment = {
    id: "selected-experiment",
    name: "jma_mj4_h2023_v1",
    synthetic: false,
    source_id: "source",
    grid_id: "grid",
    config_hash: "config",
    config: {
      provider: "jma",
      ...(catalogueNamespace
        ? { catalogue_namespace: catalogueNamespace }
        : {}),
    },
  };
  const created: Record<string, string>[] = [];
  const imports: Record<string, string>[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    let result: unknown = [];
    if (path === "/api/experiments") {
      if (request.method() === "POST") {
        const spec = request.postDataJSON();
        created.push(spec);
        result = {
          ...experiment,
          id: "collection-experiment",
          name: spec.name,
          config: spec,
        };
      } else result = [experiment];
    } else if (path === "/api/imports") {
      const body = request.postDataBuffer();
      const form = await new Request(request.url(), {
        method: "POST",
        headers: request.headers(),
        body: Uint8Array.from(body!).buffer,
      }).formData();
      imports.push(
        Object.fromEntries(
          [...form.entries()]
            .filter(([key]) => key !== "file")
            .map(([key, value]) => [key, String(value)]),
        ),
      );
      result = { id: "import-job", kind: "import", status: "queued" };
    } else if (path === "/api/sources") {
      result = { sources: [], worker: null, schedule_enabled: false };
    } else if (path === "/api/metrics") {
      result = emptyReport({
        experimentId: experiment.id,
        mode: url.searchParams.get("mode") as ReturnType<
          typeof emptyReport
        >["mode"],
      });
    }
    await route.fulfill({ json: result });
  });
  await page.goto("/");
  await expect(page.getByLabel("Experiment", { exact: true })).toHaveValue(
    experiment.id,
  );
  await page
    .getByRole("button", { name: "Data & operations", exact: true })
    .click();
  await page.getByLabel("Uncompressed file · max 32 MiB").setInputFiles({
    name: "catalogue.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("A fixture intercepted before any backend import."),
  });
  return { created, imports, experiment };
}

for (const selectedCollection of [undefined, "another_collection"]) {
  test(`imports into an isolated collection when selected collection is ${selectedCollection ?? "legacy"}`, async ({
    page,
  }) => {
    const { created, imports, experiment } = await importScreen(
      page,
      selectedCollection,
    );
    const button = page.getByRole("button", { name: "Validate & import" });
    await page
      .getByLabel("Catalogue collection", { exact: true })
      .fill("Invalid Collection");
    await expect(button).toBeDisabled();
    const collection = "archive_" + "x".repeat(72);
    await page
      .getByLabel("Catalogue collection", { exact: true })
      .fill(collection);
    await expect(
      page.getByLabel("Declare complete catalogue coverage"),
    ).not.toBeChecked();
    await button.click();
    await expect.poll(() => imports.length).toBe(1);
    expect(created).toHaveLength(1);
    expect(created[0].catalogue_namespace).toBe(collection);
    expect(created[0].name).not.toBe(experiment.name);
    expect(created[0].name.length).toBeLessThanOrEqual(80);
    expect(imports[0]).toMatchObject({
      experiment_id: "collection-experiment",
      namespace: "h2023",
      coverage_declared: "false",
    });
  });
}

test("reuses the matching collection and sends an explicit coverage declaration", async ({
  page,
}) => {
  const { created, imports, experiment } = await importScreen(page, "jma_main");
  const declaration = page.getByLabel("Declare complete catalogue coverage");
  await expect(
    page.getByLabel("Catalogue collection", { exact: true }),
  ).toHaveValue("jma_main");
  await declaration.check();
  await page.getByLabel("Coverage end · exclusive").fill("2023-12-31");
  await expect(declaration).not.toBeChecked();
  await declaration.check();
  await page.getByRole("button", { name: "Validate & import" }).click();
  await expect.poll(() => imports.length).toBe(1);
  expect(created).toEqual([]);
  expect(imports[0]).toMatchObject({
    experiment_id: experiment.id,
    namespace: "h2023",
    end: "2023-12-31T00:00:00+09:00",
    coverage_declared: "true",
  });
  expect(imports[0].submission_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  await page.getByRole("button", { name: "Validate & import" }).click();
  await expect.poll(() => imports.length).toBe(2);
  expect(imports[1].submission_id).not.toBe(imports[0].submission_id);
});
