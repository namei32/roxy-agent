// Run against serve_akasha_graph_fixture.py --turns 1000. Browser faults below
// exercise stale/late responses; ordinary graph results come from real SQLite.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright-core";

const url = process.argv[2];
const output = process.argv[3];
if (!url || !output || new URL(url).hostname !== "127.0.0.1") throw new Error("Usage: node scripts/check_akasha_mobile_graph.mjs http://127.0.0.1:PORT/ OUTPUT_DIR");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1, permissions: ["clipboard-read", "clipboard-write"] });
const page = await context.newPage(), checks = [], errors = [];
page.on("pageerror", error => errors.push(error.message));
const shot = async name => page.screenshot({ path: join(output, `${name}.png`) });
const step = async (name, fn) => { await fn(); checks.push(name); };
const ready = selector => page.locator(selector).waitFor();
try {
  await page.goto(url);
  await ready(".akmg-summary");
  await step("collapsed-overview", async () => {
    assert.match(await page.locator(".akmg-summary").innerText(), /1000/);
    assert.equal(await page.locator("[data-graph-node]").count(), 2);
    await shot("overview");
  });
  await step("group-paging-and-shared-node", async () => {
    await page.locator("[data-mg=group]").last().click(); await ready("[data-mg=collapse]");
    assert.equal(await page.locator("[data-graph-node]").count(), 9);
    await page.locator(".akmg-pager button").last().click();
    await page.waitForFunction(() => document.querySelector(".akmg-pager")?.textContent.includes("9–16"));
    await shot("expanded-page-two");
    await page.locator("[data-mg=local]").nth(4).click(); await ready("[data-mg=detail]");
    assert.match(await page.locator(".akmg-shared").innerText(), /2 个关联组/);
    const box = await page.locator("[data-mg=detail]").boundingBox();
    assert.ok(box.y >= 0 && box.y + box.height <= 844 && box.height >= 44);
    await shot("local");
  });
  await step("touch-does-not-click-through", async () => {
    for (let i = 0; i < 6; i += 1) {
      const node = page.locator("[data-graph-node]").nth(i % 3);
      const box = await node.boundingBox();
      await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
      assert.equal(await page.locator(".akmg-detail-header").count(), 0);
      await page.locator("[data-mg=detail]").click(); await ready(".akmg-detail-header");
      await page.locator("[data-mg=back]").click(); await ready("[data-mg=detail]");
    }
  });
  await step("pinch-reset-and-nearest-touch", async () => {
    const cdp = await context.newCDPSession(page), box = await page.locator("svg").boundingBox();
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    const touch = async (type, spread) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: type === "touchEnd" ? [] : [{ x: x - spread, y, id: 1 }, { x: x + spread, y, id: 2 }] });
    await touch("touchStart", 80); await touch("touchMove", 30); await touch("touchEnd", 0);
    assert.equal(await page.locator("[data-graph-zoom]").innerText(), "65%");
    const point = await page.locator("[data-graph-node]").first().evaluate(node => {
      const p = new DOMPoint(0, 0).matrixTransform(node.getScreenCTM()); return { x: p.x, y: p.y, id: node.dataset.graphNode };
    });
    await page.touchscreen.tap(point.x + 19, point.y);
    assert.equal(await page.locator("[data-mg=detail]").getAttribute("data-node"), point.id);
    assert.equal(await page.locator("[data-graph-zoom]").innerText(), "65%");
    await page.locator("[data-mg=detail]").click(); await ready(".akmg-detail-header");
    await page.locator("[data-mg=back]").click(); await ready("[data-mg=detail]");
    assert.equal(await page.locator("[data-graph-zoom]").innerText(), "65%");
    await page.locator("[data-graph-reset]").click();
    assert.equal(await page.locator("[data-graph-zoom]").innerText(), "100%");
    await shot("touch-reset"); await cdp.detach();
  });
  await step("back-preserves-group-page", async () => {
    await page.locator("[data-mg=back]").click();
    assert.match(await page.locator(".akmg-pager").innerText(), /9–16/);
    await page.locator("[data-mg=collapse]").click(); await ready(".akmg-search");
  });
  await step("full-search-and-lossless-long-source", async () => {
    await page.locator("input[type=search]").fill("北极星");
    await page.locator("[data-mg-search] button").click(); await ready("[data-mg=local]");
    await page.locator("[data-mg=local]").click(); await ready("[data-mg=detail]");
    await page.locator("[data-mg=detail]").click(); await ready("[data-mg=source-toggle]");
    await page.locator("[data-mg=source-toggle]").click();
    assert.equal(await page.locator("[data-mg=copy]").isDisabled(), true);
    await shot("long-source");
    while (await page.locator("[data-mg=source-more]").count()) {
      const before = await page.locator(".akmg-sources").innerText();
      const response = page.waitForResponse(r => r.url().endsWith("/query") && r.request().postDataJSON().method === "graph.source");
      await page.locator("[data-mg=source-more]").first().click(); await response;
      await page.waitForFunction(prior => document.querySelector(".akmg-sources")?.innerText !== prior, before);
    }
    await page.locator("[data-mg=copy]").click();
    await page.waitForFunction(() => document.querySelector(".akmg-notice")?.textContent.includes("已复制"));
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    assert.equal(copied, `你\n北极星秘密项目 ${"🧭星\0".repeat(2100)}\n\nRoxy\n<img src=x onerror="window.attacked=true">${"回复📖".repeat(1500)}`);
    assert.equal(await page.evaluate(() => window.attacked), undefined);
  });
  await step("stale-refresh-and-inspector", async () => {
    await page.route("**/query", async route => {
      const value = route.request().postDataJSON();
      if (value.method === "graph.neighbors") {
        await route.fulfill({ json: { schema: "akasha.memory-graph.v1", status: "stale", revision: "f".repeat(64), message: "记忆图已更新，请刷新" } });
      } else await route.continue();
    });
    await page.locator("[data-mg=local]").last().click(); await ready("[role=alert]");
    assert.match(await page.locator("[role=alert]").innerText(), /记忆图已更新/);
    await shot("stale"); await page.unroute("**/query");
    await page.locator(".akmg-top [data-mg=refresh]").click(); await ready("[data-mg=detail]");
    await page.locator("[data-akasha-tab=inspector]").click(); await ready(".akasha-mobile-inspector");
    assert.match(await page.locator(".akasha-mobile-inspector").innerText(), /没有可检查/);
    await shot("inspector");
  });
  await step("late-result-cannot-reopen-closed-view", async () => {
    const priorCount = await page.evaluate(() => window.queryLog.filter(q => q.method === "graph.overview").length);
    let release;
    const delayed = new Promise(resolve => { release = resolve; });
    await page.route("**/query", async route => {
      if (route.request().postDataJSON().method === "graph.overview") await delayed;
      await route.continue();
    });
    await page.locator("[data-akasha-tab=graph]").click();
    await page.locator("[data-akasha-tab=inspector]").click(); release();
    await ready(".akasha-mobile-inspector");
    await page.waitForFunction(count => window.queryLog.filter(q => q.method === "graph.overview").length > count, priorCount);
    assert.equal(await page.locator(".akmg-summary").count(), 0);
    await page.unroute("**/query");
  });
  assert.deepEqual(errors, []);
  await writeFile(join(output, "report.json"), JSON.stringify({ status: "passed", url, checks, errors, viewport: { width: 390, height: 844 }, queries: await page.evaluate(() => window.queryLog), realDevice: false }, null, 2));
  console.log(JSON.stringify({ status: "passed", checks }));
} finally {
  await browser.close();
}
