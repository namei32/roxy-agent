import React from 'react';
import { ArrowUpRight, Layers3 } from 'lucide-react';
import Graph from './Graph.jsx';
import { GraphLegend, MemoryRow, SearchField, SnapshotLine, StateView } from './components.jsx';

export default function Overview({ fixture, search, onSearch, onOpen, onHelp, onRefresh, status, dense }) {
  const turns = fixture.nodes.filter(n => n.type === 'turn');
  const hubs = fixture.nodes.filter(n => n.type === 'hub');
  const query = search.trim().toLowerCase();
  const results = query ? turns.filter(n => `${n.title} ${n.user} ${n.assistant}`.toLowerCase().includes(query)) : [];
  return <div className="screen overview-screen" data-screen="overview">
    <div className="screen-intro"><div className="eyebrow">AKASHA MEMORY</div><h1>记忆图</h1><p>回到每段记忆，看见它们的连接。</p></div>
    <SearchField value={search} onChange={onSearch}/>
    {fixture.truncated && <p className="search-scope">仅搜索与展开当前 {fixture.nodes.length} 个节点范围</p>}
    {status !== 'ready' ? <StateView kind={status} onRetry={onRefresh}/> : !fixture.nodes.length ? <StateView kind="empty" onRetry={onRefresh}/> : query ? <section className="search-results"><div className="section-heading"><h2>搜索结果</h2><span>{results.length} 条</span></div>{results.length ? results.map(n => <MemoryRow key={n.id} node={n} onClick={() => onOpen(n.id)} subtitle={`${n.date} · ${n.user.slice(0,23)}…`}/>) : <StateView kind="empty" hasQuery onRetry={() => onSearch('')}/>}</section> : <>
      <div className="overview-summary"><div><strong>{turns.length}</strong><span>回合记忆</span></div><div><strong>{hubs.length}</strong><span>关联组</span></div><span className="summary-caption">{fixture.truncated ? '当前显示范围' : '已发布的记忆'}<span className="summary-dot"/></span></div>
      <section className="graph-overview-wrap" aria-label="图概览"><Graph nodes={fixture.nodes} edges={fixture.edges} selectedId={dense ? undefined : 't1'} onSelect={onOpen} dense={dense}/><GraphLegend onHelp={onHelp}/></section>
      {fixture.truncated && <div className="bounded-notice"><Layers3 size={15}/><span>显示 {fixture.nodes.length} / {fixture.totalNodes} 个节点 · {fixture.edges.length} / {fixture.totalEdges} 条关联</span></div>}
      <div className="graph-hint"><span>点选节点，探索一跳关联</span><ArrowUpRight size={15}/></div>
      <section className="recent-memories"><div className="section-heading"><h2>最近的记忆</h2><span>{fixture.edges.length} 条关联</span></div>{[...turns].sort((a,b) => `${b.date} ${b.time}`.localeCompare(`${a.date} ${a.time}`)).slice(0,3).map(n => <MemoryRow key={n.id} node={n} onClick={() => onOpen(n.id)}/>)}</section>
      <SnapshotLine revision={fixture.revision}/>
    </>}
  </div>;
}
