import { useState } from "react";
import { BookOpenText, House, MessageCircle, MessageSquarePlus, Puzzle, Search, Settings, Wrench, FileText, LibraryBig, Wifi, RefreshCw, ScanLine } from "lucide-react";
import { ConversationNavigation } from "./conversation-navigation";

interface Session {
  id: string; title: string; lastMessagePreview?: string; lastMessageAt?: number;
  unreadCount: number; isRunning: boolean; isAvailable: boolean;
}

export function MobileRootNavigation({ current, onSelect }: {
  current: "home" | "conversations" | "tools";
  onSelect: (kind: "home" | "conversations" | "tools") => void;
}) {
  return <nav className="mobile-root-navigation" aria-label="主导航">
    {([ ["home", "小屋", House], ["conversations", "对话", MessageCircle], ["tools", "工具", Wrench] ] as const).map(([kind, label, Icon]) =>
      <button key={kind} type="button" aria-current={current === kind ? "page" : undefined} onClick={() => onSelect(kind)}>
        <span><Icon size={22} aria-hidden="true" /></span><strong>{label}</strong>
      </button>)}
  </nav>;
}

/** 会话目录只消费原生摘要，搜索不读取或改变聊天已读。 */
export function MobileHomeConversations({ sessions, selectedId, onOpen, onCreate }: {
  sessions: readonly Session[]; selectedId?: string; onOpen: (id: string) => void; onCreate: () => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = sessions.filter((session) => `${session.title}\n${session.lastMessagePreview ?? ""}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <section className="mobile-root-page mobile-home-conversations" aria-label="对话">
    <header><div><small>延续每一次交谈</small><h1>对话</h1></div><button type="button" aria-label="新建对话" onClick={onCreate}><MessageSquarePlus size={24} /></button></header>
    <label className="mobile-home-search"><Search size={20} aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索会话与最近消息" aria-label="搜索会话" /></label>
    <ConversationNavigation destinations={[]} sessions={filtered.map((session) => ({
      id: session.id, title: session.title || "未命名会话", active: session.id === selectedId,
      preview: session.isAvailable ? session.lastMessagePreview || "还没有消息" : "电脑端已不存在 · 本机保留历史",
      unavailable: !session.isAvailable,
      updatedLabel: session.lastMessageAt ? new Date(session.lastMessageAt).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" }) : undefined,
      state: session.isRunning ? <span className="session-running" aria-label="正在处理" /> : session.unreadCount > 0 ? <strong className="session-unread" aria-label={`${session.unreadCount} 条未读`}>{session.unreadCount > 99 ? "99+" : session.unreadCount}</strong> : undefined,
    }))} onSessionActivate={onOpen} actions={[]} />
    {filtered.length === 0 ? <p className="mobile-home-empty">{query ? "没有匹配的会话" : "还没有对话，和 Roxy 打个招呼吧。"}</p> : null}
  </section>;
}

export function MobileHomeTools({ onRuntime, onPlugins, onSettings, onDiagnostics, connectionLabel, onResync, onPairing, canResync, isResyncing }: {
  onRuntime: () => void; onPlugins: () => void; onSettings: () => void; onDiagnostics: () => void; connectionLabel: string;
  onResync: () => void; onPairing: () => void; canResync: boolean; isResyncing: boolean;
}) {
  return <section className="mobile-root-page mobile-home-tools" aria-label="工具">
    <header><div><small>需要的时候，都在这里</small><h1>工具</h1></div><BookOpenText size={30} aria-hidden="true" /></header>
    <div className="mobile-home-tool-grid">
      <button type="button" onClick={onRuntime}><LibraryBig /><strong>知识与运行</strong><small>记忆 · 文件 · 定时任务 · MCP</small></button>
      <button type="button" onClick={onPlugins}><Puzzle /><strong>插件</strong><small>心情、主动反馈与所有看板</small></button>
      <button type="button" onClick={onSettings}><Settings /><strong>设置</strong><small>模型 · 主题 · 通知 · 连接</small></button>
      <button type="button" onClick={onDiagnostics}><FileText /><strong>诊断报告</strong><small>连接与运行状态</small></button>
    </div>
    <div className="mobile-home-tool-grid" aria-label="连接维护">
      <button type="button" onClick={onResync} disabled={!canResync}><RefreshCw /><strong>{isResyncing ? "正在重新同步" : "重新同步"}</strong><small>从电脑重新读取聊天记录</small></button>
      <button type="button" onClick={onPairing}><ScanLine /><strong>重新配对</strong><small>扫描电脑上的配对码</small></button>
    </div>
    <p className="mobile-home-connection"><Wifi size={18} aria-hidden="true" />{connectionLabel}</p>
  </section>;
}
