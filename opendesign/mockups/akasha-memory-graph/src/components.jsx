import React, { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowUpRight, ChevronRight, CircleHelp, Copy, Focus, LockKeyhole, Network, RefreshCw, Search, X } from 'lucide-react';
import { typeLabel } from './data.js';

export function IconButton({ label, children, className = '', ...props }) {
  return <button type="button" className={`icon-button ${className}`} aria-label={label} title={label} {...props}>{children}</button>;
}

export function TopBar({ view, onBack, onRefresh, loading, onHelp, detailBack = false }) {
  return <header className="app-topbar">
    <div className="topbar-leading">{view === 'overview' ? <span className="app-mark"><Network size={22} strokeWidth={1.7}/></span> : <IconButton label={view === 'detail' ? detailBack ? '返回上一段记忆' : '返回局部关联' : '返回记忆图'} onClick={onBack}><ArrowLeft size={21}/></IconButton>}
      <span>{view === 'overview' ? 'Akasha' : view === 'local' ? '局部关联' : '记忆详情'}</span>
    </div>
    <div className="topbar-actions"><span className="read-only"><LockKeyhole size={11}/>只读</span>
      {view === 'overview' ? <IconButton label="刷新记忆图" onClick={onRefresh} disabled={loading}><RefreshCw size={19} className={loading ? 'spin' : ''}/></IconButton> : <IconButton label="了解记忆图" onClick={onHelp}><CircleHelp size={20}/></IconButton>}
    </div>
  </header>;
}

export function SearchField({ value, onChange }) {
  return <div className="search-field"><Search size={19}/><input aria-label="搜索记忆" value={value} onChange={e => onChange(e.target.value)} placeholder="搜索全部示例记忆…" autoComplete="off" spellCheck="false"/>
    {value && <IconButton label="清空搜索" onClick={() => onChange('')}><X size={17}/></IconButton>}
    {!value && <span className="search-hint">关键词</span>}
  </div>;
}

export function Pager({ batch, onPage, subject = '记忆' }) {
  if (batch.pages <= 1) return null;
  return <nav className="paging-row" aria-label={`${subject}分页`}>
    <button type="button" disabled={!batch.hasPrevious} aria-label={`上一批${subject}`} onClick={() => onPage(batch.page-1)}><ArrowLeft size={17}/></button>
    <span><strong>{batch.start}–{batch.end}</strong> / {batch.total}<small>{subject} · 第 {batch.page+1} / {batch.pages} 页</small></span>
    <button type="button" disabled={!batch.hasNext} aria-label={`下一批${subject}`} onClick={() => onPage(batch.page+1)}><ChevronRight size={18}/></button>
  </nav>;
}

export function MemoryRow({ node, subtitle, onClick, right, compact = false }) {
  return <button type="button" className={`memory-row ${compact ? 'compact' : ''}`} onClick={onClick}>
    <span className={`memory-row-icon ${node.type}`}><span/></span>
    <span className="memory-row-copy"><strong>{node.title}</strong><small>{subtitle || `${node.date} ${node.time} · ${typeLabel(node)}`}</small></span>
    {right && <span className="row-value">{right}</span>}<ChevronRight size={17} className="row-chevron"/>
  </button>;
}

export function SnapshotLine({ revision, stale = false }) {
  return <div className={`snapshot-line ${stale ? 'stale' : ''}`}><span className="status-dot"/>{stale ? '新快照已到达，请刷新' : `示例快照 V${revision} · 截至 09-05 ${revision > 12 ? '10:32' : '10:24'}`}</div>;
}

export function GraphLegend({ onHelp }) {
  return <div className="graph-legend"><span><i className="legend-turn"/>回合记忆</span><span><i className="legend-hub"/>关联组</span><button type="button" onClick={onHelp} aria-label="查看图例说明"><CircleHelp size={16}/></button></div>;
}

export function SelectedMemory({ node, count, onDetails, onCenter, isCenter }) {
  return <section className="selection-panel" aria-label="选中记忆">
    <div className="selection-meta"><span className={`type-badge ${node.type}`}>{typeLabel(node)}</span><span>{node.date} · {count} 条关联</span></div>
    <h2>{node.title}</h2>
    <p>{node.type === 'hub' ? '共同激活的记忆在这里汇集，点开查看成员。' : node.user}</p>
    <div className="selection-actions"><button type="button" className="primary-button" onClick={onDetails}>查看详情<ArrowUpRight size={17}/></button>
      <IconButton label={isCenter ? '当前已是中心节点' : '以此记忆为中心展开一跳'} disabled={isCenter} onClick={onCenter}><Focus size={21}/></IconButton>
    </div>
  </section>;
}

export function HelpSheet({ open, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement;
    ref.current?.focus();
    const key = event => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'Tab') {
        const items = ref.current?.querySelectorAll('button, [href], input, [tabindex="0"]');
        if (!items?.length) return;
        if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items[items.length-1].focus(); }
        else if (!event.shiftKey && document.activeElement === items[items.length-1]) { event.preventDefault(); items[0].focus(); }
      }
    };
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); previous?.focus?.(); };
  }, [open, onClose]);
  if (!open) return null;
  return <div className="sheet-backdrop" onClick={onClose}><section className="help-sheet" role="dialog" aria-modal="true" aria-labelledby="help-title" ref={ref} tabIndex={-1} onClick={e => e.stopPropagation()}>
    <div className="sheet-handle"/><div className="sheet-heading"><h2 id="help-title">怎样阅读这张图</h2><IconButton label="关闭说明" onClick={onClose}><X size={20}/></IconButton></div>
    <dl className="legend-explanation"><div><dt><i className="legend-turn"/>回合记忆</dt><dd>一段已完成的对话，详情保留它的原始表达。</dd></div><div><dt><i className="legend-hub"/>关联组</dt><dd>共同激活形成的模式节点，它本身不是一段原话。</dd></div><div><dt><span className="sample-line"/>实线 · 共同关联</dt><dd>表示一段记忆属于一个关联组。</dd></div><div><dt><span className="sample-line temporal"/>箭头虚线 · 时间关联</dt><dd>表示已保存的有向时间关系，不证明因果。</dd></div></dl>
    <p className="help-note">组内数字是成员数。同一段记忆可以属于多个关联组，各组数量不能直接相加。折叠、翻页和聚焦只改变显示；搜索覆盖完整示例集合。关联强度不是事实可信度。</p><button type="button" className="primary-button full" onClick={onClose}>明白了</button>
  </section></div>;
}

export function StateView({ kind, onRetry, hasQuery = false }) {
  if (kind === 'loading') return <div className="loading-state" role="status"><span className="loading-orbit"><Network size={28}/></span><h2>正在读取记忆图</h2><p>连接已发布的记忆与关联…</p><div className="skeleton-line"/><div className="skeleton-line short"/></div>;
  return <div className="empty-state" role="status"><span className={`state-symbol ${kind}`}><Network size={32} strokeWidth={1.3}/></span><h2>{kind === 'error' ? '记忆图暂时不可用' : hasQuery ? '没有找到这段记忆' : '记忆从一次对话开始'}</h2><p>{kind === 'error' ? '未能读取已发布的快照，请稍后重试。' : hasQuery ? '换一个关键词，或清空搜索看看全部记忆。' : '当前还没有已发布的记忆。新的记忆就绪后，会在这里显示。'}</p><button type="button" className="secondary-button" onClick={onRetry}>{kind === 'error' ? '重新读取' : hasQuery ? '清空搜索' : '刷新看看'}<RefreshCw size={16}/></button></div>;
}
