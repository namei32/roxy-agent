export interface PromptInputKeyboardEvent {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
  nativeIsComposing: boolean;
  nativeKeyCode: number;
}

/** 只有非输入法候选确认的普通 Enter 才提交消息。 */
export function shouldSubmitPromptInputKey(event: PromptInputKeyboardEvent) {
  return event.key === "Enter"
    && !event.shiftKey
    && !event.isComposing
    && !event.nativeIsComposing
    && event.nativeKeyCode !== 229;
}
