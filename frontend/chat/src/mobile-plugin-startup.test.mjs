import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";
import { build } from "esbuild";
import { chromium } from "playwright-core";

test("cached and fast plugins render while a separate import stalls or fails", async () => {
  const bundle = await build({ stdin: { contents: `
    import React from "react";
    import { createRoot } from "react-dom/client";
    import { receiveMobilePluginCatalog, MobilePluginDashboard } from "./mobile-plugin-runtime.tsx";
    window.receive = receiveMobilePluginCatalog;
    window.calls = [];
    window.RoxyNative = { queryPluginUi: (...args) => window.calls.push(args), cancelPluginUiOwner: () => {} };
    createRoot(document.getElementById("root")).render(React.createElement(React.Fragment, {},
      ...["fast", "slow", "bad", "pending"].map(pluginId => React.createElement(MobilePluginDashboard, {key: pluginId, pluginId}))));
  `, resolveDir: new URL(".", import.meta.url).pathname, loader: "tsx" }, bundle: true, format: "esm", write: false });
  let releaseSlow;
  const slow = new Promise(resolve => { releaseSlow = resolve; });
  const server = createServer(async (request, response) => {
    if (request.url === "/") { response.setHeader("Content-Type", "text/html"); response.end('<div id="root"></div><script type="module" src="/bundle.js"></script>'); return; }
    response.setHeader("Content-Type", "text/javascript");
    if (request.url === "/bundle.js") { response.end(bundle.outputFiles[0].text); return; }
    if (request.url === "/slow.js") await slow;
    response.end(request.url === "/bad.js" ? 'throw new Error("isolated failure")' : `export default {slots: {}, dashboard: {mount(host) { host.textContent = ${JSON.stringify(request.url === "/slow.js" ? "slow ready" : "fast ready")}; }}};`);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.waitForFunction(() => window.receive);
    await page.evaluate(() => window.receive({ catalogRevision: "a".repeat(64), updating: true, scope: "fixture", plugins: ["fast", "slow", "bad", "pending"].map(id => ({ id, revision: "1", moduleUrl: `/${id}.js`, navigation: { label: id, description: "" }, slots: [], ready: id !== "pending" })) }));
    await page.getByText("fast ready", { exact: true }).waitFor({ timeout: 3000 });
    await page.getByText("isolated failure", { exact: true }).waitFor({ timeout: 3000 });
    assert.equal(await page.getByText("slow ready", { exact: true }).count(), 0);
    assert.equal(await page.evaluate(() => window.calls.filter(args => args[9] === "assets" && args[5] === "pending").length), 1);
    releaseSlow();
    await page.getByText("slow ready", { exact: true }).waitFor({ timeout: 3000 });
    assert.equal(await page.getByText("fast ready", { exact: true }).count(), 1);
  } finally {
    releaseSlow();
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
});
