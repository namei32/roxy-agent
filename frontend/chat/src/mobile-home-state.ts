export interface HomeMessageTarget {
  sessionId: string;
  messageId?: string;
  deliveryId?: string;
}

/** 只接受同一会话的精确身份；兼容现有 Android 主动投递别名。 */
export function resolveHomeMessage(
  target: HomeMessageTarget,
  selectedSessionId: string | undefined,
  messages: readonly { id: string; sessionId: string }[],
): string | undefined {
  if (selectedSessionId !== target.sessionId || !target.messageId) return undefined;
  const canonical = messages.find((message) => message.sessionId === target.sessionId && message.id === target.messageId);
  if (canonical) return canonical.id;
  // v0.8.32 在历史合并前使用 proactive:<delivery_id>；这不是正文匹配。
  const alias = target.deliveryId ? `proactive:${target.deliveryId}` : undefined;
  return alias ? messages.find((message) => message.sessionId === target.sessionId && message.id === alias)?.id : undefined;
}
