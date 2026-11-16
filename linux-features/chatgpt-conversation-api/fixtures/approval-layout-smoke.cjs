#!/usr/bin/env node
"use strict";

// Offline visual QA only: the browser receives a synthetic API. It cannot
// approve a real batch or reach the native IPC bridge.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function main() {
  const [playwrightPath, executablePath, output] = process.argv.slice(2);
  if (!playwrightPath || !executablePath || !output) throw Error("Usage: approval-layout-smoke.cjs PLAYWRIGHT_MODULE HEADLESS_CHROMIUM OUTPUT_DIR");
  const { chromium } = require(path.resolve(playwrightPath));
  const destination = path.resolve(output); fs.mkdirSync(destination, { recursive: true });
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--disable-gpu"], ignoreDefaultArgs: ["--hide-scrollbars"] });
  const results = [];
  try {
    for (const [width, height, colorScheme] of [[960, 720, "dark"], [640, 360, "light"], [400, 300, "dark"], [320, 240, "light"]]) {
      const page = await browser.newPage({ viewport: { width, height }, colorScheme });
      const details = JSON.stringify({ plan_id: "synthetic-plan", sha256: "synthetic-hash", operations:
        Array.from({ length: 200 }, (_, index) => ({ type: "import_chat", reason: `Synthetic operation ${index}`, history: '<script>never execute source text</script> ' + "Long archived reference text. ".repeat(100) })) }, null, 2);
      await page.addInitScript(review => {
        window.syntheticChoices = [];
        window.hydexApproval = { read: async () => review, choose: async (...choice) => { window.syntheticChoices.push(choice); return true; } };
      }, { plan_id: "synthetic-plan", sha256: "synthetic-hash", heading: "Apply 200 operations?",
        description: "Review this exact batch. This is an offline layout test, with no native approval authority.",
        caveat: "Created chats remain; imports restore text only.", details, approve_label: "Approve this exact batch" });
      await page.goto(pathToFileURL(path.join(__dirname, "../runtime/approval.html")).href);
      const approve = page.locator("#approve"), cancel = page.locator("#cancel");
      await approve.waitFor(); await page.waitForFunction(() => !document.getElementById("approve").disabled);
      assert.equal(await page.locator("#batch").textContent(), details);
      assert.equal(await page.evaluate(() => document.activeElement.id), "cancel");
      const layout = await page.locator(".details").evaluate(element => ({
        overflow: getComputedStyle(element).overflowY, height: element.clientHeight, contentHeight: element.scrollHeight,
      }));
      assert.equal(layout.overflow, "scroll"); assert.ok(layout.height > 0 && layout.contentHeight > layout.height);
      const before = {};
      for (const [name, button] of [["approve", approve], ["cancel", cancel]]) {
        const bounds = await button.boundingBox(); assert.ok(bounds, name);
        assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= width && bounds.y + bounds.height <= height, name);
        before[name] = bounds;
      }
      await page.screenshot({ path: path.join(destination, `${width}x${height}-${colorScheme}.png`) });
      await page.locator(".details").evaluate(element => { element.scrollTop = element.scrollHeight; });
      assert.deepEqual(await approve.boundingBox(), before.approve); assert.deepEqual(await cancel.boundingBox(), before.cancel);
      await page.evaluate(() => document.getElementById("approve").click());
      assert.equal(await page.evaluate(() => window.syntheticChoices.length), 0);
      // Trusted input is used only against this offline synthetic API.
      await page.keyboard.press("Escape");
      assert.deepEqual(await page.evaluate(() => window.syntheticChoices), [["cancel", "synthetic-plan", "synthetic-hash"]]);
      results.push({ width, height, colorScheme, detailBytes: Buffer.byteLength(details), layout, buttons: before, passed: true });
      await page.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(destination, "report.json"), JSON.stringify({ syntheticOnly: true, cases: results }, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ passed: results.length, nativeApprovalRequests: 0, report: path.join(destination, "report.json") }) + "\n");
}
main().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
