import assert from "node:assert/strict";
import test from "node:test";
import { mobileViewportBounds } from "./mobile-viewport.ts";

test("panned visual viewport keeps the app inside the visible region", () => {
  assert.deepEqual(mobileViewportBounds(890, { height: 548, offsetTop: 342, scale: 1 }), { height: 548, top: 342 });
});
test("native resize does not double-subtract an unpanned visual viewport", () => {
  assert.deepEqual(mobileViewportBounds(548, { height: 250, offsetTop: 0, scale: 1 }), { height: 548, top: 0 });
  assert.deepEqual(mobileViewportBounds(890), { height: 890, top: 0 });
});
test("pinch zoom is not mistaken for the keyboard", () => {
  assert.deepEqual(mobileViewportBounds(890, { height: 445, offsetTop: 120, scale: 2 }), { height: 890, top: 0 });
});
