// Render the running dashboard to PNG files for the README.
// Usage (from repo root): cd frontend && node scripts/screenshots.mjs
// Requires:
//   - dev server running on :5173
//   - `npx playwright install chromium` once
//
// Override the dashboard URL with MM_BASE_URL=...

import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdir } from "node:fs/promises";

// This file lives at frontend/scripts/screenshots.mjs; resolve docs/screenshots
// relative to the repo root rather than the script's own directory.
const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "..", "docs", "screenshots");
await mkdir(outDir, { recursive: true });

const BASE = process.env.MM_BASE_URL || "http://127.0.0.1:5173";
const VIEWPORT = { width: 1440, height: 960 };

const shots = [
  {
    file: "dashboard-mindmap.png",
    description: "Review page (Mind map tab) — the one README hero shot",
    setup: async (page) => {
      await page.goto(BASE, { waitUntil: "networkidle" });
      // Pre-suppress the post-ingest speaker-assignment modal by
      // marking every meeting in the directory as "seen" — the modal
      // only fires for unseen meetings. Same key the React app uses.
      await page.evaluate(() => {
        const ids = [];
        for (let i = 1; i < 100; i += 1) ids.push(i);
        sessionStorage.setItem("mm-post-ingest-seen", JSON.stringify(ids));
      });
      // Reload so React picks up the seeded sessionStorage.
      await page.reload({ waitUntil: "networkidle" });
      // Dismiss any other backdrop modals via Escape (Onboarding etc.).
      for (let i = 0; i < 2; i += 1) {
        await page.keyboard.press("Escape");
        await page.waitForTimeout(150);
      }
      await page.waitForSelector(".mm-meta-strip-slim", { timeout: 10_000 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(400);
    },
  },
];

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 });

for (const shot of shots) {
  const page = await context.newPage();
  try {
    await shot.setup(page);
    await page.waitForTimeout(400);
    const path = join(outDir, shot.file);
    await page.screenshot({ path, fullPage: false });
    console.log("✓", shot.file, "—", shot.description);
  } catch (error) {
    console.error("✗", shot.file, "—", error.message);
  } finally {
    await page.close();
  }
}

await browser.close();
console.log(`\nWrote ${shots.length} screenshots to docs/screenshots/`);
