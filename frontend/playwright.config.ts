import { defineConfig } from "@playwright/test";

// The API serves the built SPA (SIMCORE_FRONTEND) so tests hit one origin, exactly
// like the Docker image. Example scenarios are seeded into a fresh data dir.
const PORT = Number(process.env.E2E_PORT ?? 8765);
const ROOT = new URL("..", import.meta.url).pathname;

export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {},
  },
  webServer: {
    command:
      `rm -rf .e2e-data && SIMCORE_DATA=.e2e-data SIMCORE_EXAMPLES=examples ` +
      `SIMCORE_FRONTEND=frontend/dist SIMCORE_PORT=${PORT} SIMCORE_REASONING=heuristic ` +
      `SIMCORE_CPU_WORKERS=2 .venv/bin/python -m services.api`,
    cwd: ROOT,
    url: `http://127.0.0.1:${PORT}/api/v1/health`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
