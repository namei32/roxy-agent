import test from "node:test";
import assert from "node:assert/strict";
import { resolveHomeMessage } from "./mobile-home-state.ts";
import { pushMobileSurface, replaceMobileSurface, readMobileSurfaceHistoryState } from "./mobile-surface-history.ts";

test("home links never use same text or IDs from another session", () => {
  const target = { sessionId: "mobile:a", messageId: "a:3", deliveryId: "delivery-3" };
  const messages = [{ sessionId: "mobile:b", id: "a:3", content: "same" }, { sessionId: "mobile:a", id: "a:4", content: "same" }];
  assert.equal(resolveHomeMessage(target, "mobile:a", messages), undefined);
  assert.equal(resolveHomeMessage(target, "mobile:b", [{ sessionId: "mobile:a", id: "a:3" }]), undefined);
  assert.equal(resolveHomeMessage(target, "mobile:a", [...messages, { sessionId: "mobile:a", id: "a:3" }]), "a:3");
});

test("delivered proactive alias works before canonical history merge", () => {
  const target = { sessionId: "mobile:a", messageId: "a:3", deliveryId: "delivery-3" };
  const alias = { sessionId: "mobile:a", id: "proactive:delivery-3" };
  assert.equal(resolveHomeMessage(target, "mobile:a", [alias]), alias.id);
  assert.equal(resolveHomeMessage(target, "mobile:a", [alias, { sessionId: "mobile:a", id: "a:3" }]), "a:3");
  assert.equal(resolveHomeMessage({ ...target, deliveryId: undefined }, "mobile:a", [alias]), undefined);
});

test("home, conversations, tools and message navigation retain a traversable history", () => {
  const entries = [];
  const history = { pushState: (entry) => entries.push(entry), replaceState: (entry) => { entries[entries.length ? entries.length - 1 : 0] = entry; } };
  replaceMobileSurface(history, { kind: "home" });
  pushMobileSurface(history, { kind: "chat" });
  pushMobileSurface(history, { kind: "tools" });
  pushMobileSurface(history, { kind: "conversations" });
  assert.deepEqual(entries.map(readMobileSurfaceHistoryState).map((surface) => surface.kind), ["home", "chat", "tools", "conversations"]);
});

import { mobileSurfaceHistoryDepth, pushMobileDialog, isMobileDialogHistoryState } from "./mobile-surface-history.ts";

test("opening a letter replaces its modal entry so Back returns to the house", () => {
  const entries = [];
  const history = { get state() { return entries.at(-1); }, pushState: (entry) => entries.push(entry), replaceState: (entry) => { entries[Math.max(0, entries.length - 1)] = entry; } };
  replaceMobileSurface(history, { kind: "home" });
  assert.equal(mobileSurfaceHistoryDepth(history.state), 0);
  pushMobileDialog(history, { kind: "home" });
  assert.ok(isMobileDialogHistoryState(history.state));
  assert.equal(mobileSurfaceHistoryDepth(history.state), 1);
  pushMobileSurface(history, { kind: "chat" });
  assert.equal(entries.length, 2);
  assert.equal(mobileSurfaceHistoryDepth(history.state), 1);
  assert.equal(isMobileDialogHistoryState(history.state), false);
  entries.pop();
  assert.equal(readMobileSurfaceHistoryState(history.state).kind, "home");
  assert.equal(mobileSurfaceHistoryDepth(history.state), 0);
});
