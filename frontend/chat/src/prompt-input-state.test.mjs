import test from "node:test";
import assert from "node:assert/strict";

import { shouldSubmitPromptInputKey } from "./prompt-input-state.ts";

const enter = (overrides = {}) => ({
  key: "Enter",
  shiftKey: false,
  isComposing: false,
  nativeIsComposing: false,
  nativeKeyCode: 13,
  ...overrides,
});

test("普通 Enter 提交消息", () => {
  assert.equal(shouldSubmitPromptInputKey(enter()), true);
});

test("输入法组合期间的 Enter 不提交消息", () => {
  assert.equal(shouldSubmitPromptInputKey(enter({ isComposing: true })), false);
  assert.equal(shouldSubmitPromptInputKey(enter({ nativeIsComposing: true })), false);
  assert.equal(shouldSubmitPromptInputKey(enter({ nativeKeyCode: 229 })), false);
});

test("Shift+Enter 和其他按键不提交消息", () => {
  assert.equal(shouldSubmitPromptInputKey(enter({ shiftKey: true })), false);
  assert.equal(shouldSubmitPromptInputKey({ ...enter(), key: "a" }), false);
});
