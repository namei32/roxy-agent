import assert from "node:assert/strict";
import test from "node:test";

import { installMobileBridge } from "./mobile-bridge.ts";

const EXPECTED_METHODS = [
  "requestSnapshot", "selectSession", "removeUnavailableSession", "createSession",
  "restartPairing", "reloadFromServer", "exportDiagnostics", "openSettings",
  "chooseAttachments", "removeAttachment", "retryAttachment", "continueMeteredTransfer",
  "retryFailedMessage", "saveReadingPosition", "markSessionReadThrough", "navigationTargetHandled",
  "retryDownloadedAttachment", "touchDownloadedAttachment", "openDownloadedAttachment",
  "shareDownloadedAttachment", "saveDownloadedAttachment", "setWebHistoryActive", "dismissError",
  "shareText", "saveComposerDraft", "commitSharedText", "rejectSharedText", "sendMessage",
  "copyText", "performActionHaptic", "sendCommand", "refreshRuntimeInspection",
  "openRuntimeDocument", "openRuntimeMcp", "openRuntimeJob", "clearRuntimeInspectionDetail",
  "stopTurn", "queryPluginUi", "cancelPluginUiOwner", "setTheme", "setModelSelection", "reportHealthy",
];

function installFor(url, transportName = "RoxyNativeTransport") {
  const messages = [];
  globalThis.window = {
    location: { href: url },
    [transportName]: { postMessage: (message) => messages.push(JSON.parse(message)) },
  };
  installMobileBridge();
  return {
    bridge: window.RoxyNative,
    legacyBridge: window.AkashicNative,
    messages,
  };
}

test("embedded and remote WebUI install one generation-bound native surface", () => {
  const remote = installFor("https://mobile.invalid/mobile.html?generation_id=remote-gen&nonce=remote-nonce");
  const embedded = installFor("file:///android_asset/mobile.html?generation_id=embedded&nonce=baseline");
  assert.deepEqual(Object.keys(remote.bridge).sort(), [...EXPECTED_METHODS].sort());
  assert.deepEqual(Object.keys(embedded.bridge).sort(), [...EXPECTED_METHODS].sort());
  assert.equal(remote.legacyBridge, remote.bridge);
  assert.equal(embedded.legacyBridge, embedded.bridge);

  assert.throws(() => remote.bridge.selectSession(), /expects 1 args/);
  remote.bridge.requestSnapshot();
  embedded.bridge.reportHealthy();
  assert.deepEqual(remote.messages[0], {
    v: 1,
    generation_id: "remote-gen",
    nonce: "remote-nonce",
    method: "requestSnapshot",
    args: [],
  });
  assert.deepEqual(embedded.messages[0], {
    v: 1,
    generation_id: "embedded",
    nonce: "baseline",
    method: "reportHealthy",
    args: [],
  });
  delete globalThis.window;
});

test("legacy native transport remains an alias of the Roxy bridge", () => {
  const installed = installFor(
    "file:///android_asset/mobile.html?generation_id=legacy&nonce=legacy-nonce",
    "AkashicNativeTransport",
  );
  assert.equal(installed.legacyBridge, installed.bridge);
  installed.bridge.requestSnapshot();
  assert.equal(installed.messages[0].generation_id, "legacy");
  delete globalThis.window;
});

test("an injected direct bridge is mirrored across both names", () => {
  const canonical = { requestSnapshot() {} };
  globalThis.window = {
    location: { href: "file:///android_asset/mobile.html" },
    RoxyNative: canonical,
  };
  installMobileBridge();
  assert.equal(window.RoxyNative, canonical);
  assert.equal(window.AkashicNative, canonical);

  const legacy = { requestSnapshot() {} };
  globalThis.window = {
    location: { href: "file:///android_asset/mobile.html" },
    AkashicNative: legacy,
  };
  installMobileBridge();
  assert.equal(window.RoxyNative, legacy);
  assert.equal(window.AkashicNative, legacy);
  delete globalThis.window;
});
