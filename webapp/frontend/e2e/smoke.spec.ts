import { expect, test } from "@playwright/test";
const BE = "http://127.0.0.1:10850";
test.describe('Fleet Audit', () => {
    test('Backend health', async ({ request }) => {
        const resp = await request.get(BE + '/health');
        expect(resp.status()).toBe(200);
    });
    test('Frontend loads', async ({ page }) => {
        await page.goto("/", { timeout: 15000 });
        await expect(page.locator("#root")).toBeVisible();
    });
});
