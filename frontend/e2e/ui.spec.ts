import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

async function chat(page: Page, text: string) {
  const replies = page.getByTestId("assistant-reply");
  const before = await replies.count();
  await page.getByTestId("chat-input").fill(text);
  await page.getByTestId("chat-send").click();
  await expect(replies).toHaveCount(before + 1);
  return replies.nth(before);
}

// AC-UI-01: create a scenario via chat, tweak a field in the form, run, pin a chart,
// export a report.
test("AC-UI-01 chat draft -> form tweak -> run -> pin chart -> export report", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("scenario-wh_baseline").click();
  await expect(page.getByTestId("lever-jacks")).toHaveValue("12");

  const reply = await chat(page, "add 3 jacks and compare to baseline with 2 replications");
  await expect(reply).toContainText("Pallet jack operators: 12 -> 15");
  const card = page.getByTestId("draft-card");
  await expect(card).toContainText("not run");

  await card.getByTestId("edit-draft").click(); // chat and form share the draft
  await expect(page.getByTestId("lever-jacks")).toHaveValue("15");
  await page.getByTestId("lever-jacks").fill("16"); // tweak a field in the form
  await expect(page.getByTestId("editor-diff")).toContainText("16");
  await page.getByTestId("run-length").fill("1800");
  await page.getByTestId("run").click();

  await expect(page.getByTestId("run-monitor")).toBeVisible();
  await expect(page.getByTestId("job-status")).toHaveText("completed", { timeout: 150_000 });
  await page.getByTestId("open-results").click();
  const findings = page.getByTestId("findings-card");
  await expect(findings).toContainText("Pallets per labor hour");
  await expect(page.getByTestId("trust-bar")).toContainText("n = 2 replications");
  await expect(page.getByTestId("trust-bar")).toContainText("95% confidence");

  await page.getByTestId("tab-canvas").click();
  await page.getByTestId("chart-metric").selectOption("pallets_per_labor_hour");
  await page.getByTestId("chart-render").click();
  await expect(page.getByTestId("chart-preview").locator("svg")).toBeVisible();
  await page.getByTestId("pin-chart").click();
  await expect(page.getByTestId("pinned-chart")).toHaveCount(1);
  await page.getByTestId("analysis-name").fill("e2e analysis");
  await page.getByTestId("save-analysis").click();

  await page.getByTestId("tab-compare").click();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("export-report").click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/report\.html$/);
  const body = await (await download.createReadStream())?.toArray();
  expect(Buffer.concat(body ?? []).toString()).toContain("<html");

  const analyses = await page.request.get("/api/v1/analyses");
  expect((await analyses.json()).map((a: { name: string }) => a.name)).toContain("e2e analysis");
});

// AC-UI-02: the 2D replay scrubber plays back a completed run from its event log and
// matches the logged agent positions at sampled timestamps.
test("AC-UI-02 replay scrubber matches logged positions", async ({ page, request }) => {
  const sub = await request.post("/api/v1/experiments", {
    data: {
      experiment: { id: "e2e_replay", replications: 1, run_length: 900 },
      scenario: { example: "scenarios/warehouse/baseline.yaml" },
    },
  });
  const job = await sub.json();
  await expect
    .poll(async () => (await (await request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 120_000 })
    .toBe("completed");

  await page.goto("/");
  await page.getByTestId("scenario-wh_baseline").click();
  await page.getByRole("button", { name: new RegExp(job.id) }).click();
  await page.getByTestId("tab-facility").click();
  await expect(page.getByTestId("floorplan")).toBeVisible();

  const seen = new Map<string, Set<string>>();
  for (const t of [0, 37.5, 120, 333.3, 600, 899]) {
    await page.getByTestId("scrub-time").fill(String(t));
    const snap = await (
      await request.get(`/api/v1/jobs/${job.id}/variants/baseline/replications/0/snapshot?t=${t}`)
    ).json();
    const agents = Object.entries(snap.agents as Record<string, { position: number[] | null }>);
    expect(agents.length).toBeGreaterThan(0);
    for (const [id, a] of agents) {
      if (!a.position) continue;
      const dot = page.locator(`circle[data-agent="${id}"]`);
      await expect(dot).toHaveAttribute("data-x", a.position[0].toFixed(4));
      await expect(dot).toHaveAttribute("data-y", a.position[1].toFixed(4));
      const key = `${a.position[0].toFixed(4)},${a.position[1].toFixed(4)}`;
      seen.set(id, (seen.get(id) ?? new Set()).add(key));
    }
  }
  // the run is really replayed: agents are at different places at different times
  const moved = [...seen.values()].filter((s) => s.size > 2).length;
  expect(moved).toBeGreaterThan(3);
});

// AC-IFC-02 in the web UI: the reference facility plays a scripted scenario, a disruption
// is injected live and the KPI HUD updates.
test("AC-IFC-02 live demo with beats, live injection and KPI HUD", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("scenario-wh_demo").click();
  await page.getByTestId("tab-live").click();
  await page.getByTestId("live-start").click();
  await page.getByTestId("live-play").click();
  await expect(page.getByTestId("captions")).toContainText("intro");
  await expect(page.getByTestId("kpi-hud").locator(".hud-item").first()).toBeVisible();
  await expect(page.getByTestId("live-plan").locator("circle.agent")).toHaveCount(12);
  await page.getByTestId("inject-type").selectOption("resource_unavailable");
  await page.getByTestId("inject-target").fill("type:pallet_jack_operator");
  await page.getByTestId("inject-count").fill("2");
  await page.getByTestId("inject-go").click();
  await expect(page.getByTestId("active-disruption").first()).toBeVisible();
  const before = Number((await page.getByTestId("live-clock").textContent())?.match(/t = (\d+)/)?.[1]);
  await page.getByTestId("live-step").click();
  await expect
    .poll(async () => Number((await page.getByTestId("live-clock").textContent())?.match(/t = (\d+)/)?.[1]))
    .toBeGreaterThan(before);
  await expect(page.getByTestId("kpi-hud")).toContainText("Pallets per labor hour");
});
