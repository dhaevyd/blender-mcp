import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  timeout: 60000,
  retries: 1,
  use: { baseURL: "http://127.0.0.1:8084", headless: true, screenshot: "only-on-failure" },
  webServer: {
    command: "npm run dev",
    port: 8084,
    timeout: 120000,
    reuseExistingServer: !process.env.CI,
  },
});
