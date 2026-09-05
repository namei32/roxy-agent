import React from 'react';
import { ChevronDown, ChevronRight, Layers3, ArrowUpRight } from 'lucide-react';
import Graph from './Graph.jsx';
import { GraphLegend, MemoryRow, Pager, SearchField, SnapshotLine, StateView } from './components.jsx';
import { memberships, overviewProjection, searchCatalog } from './projection.js';

export default function Overview({ fixture, search, onSearch, onOpen, onHelp, onRefresh, status, state, onState }) {
  const turns = fixture.nodes.filter(n => n.type === 'turn');
  const query = search.trim();
  const projection = overviewProjection(fixture, state.expandedGroup, state.groupPage);
  const results = searchCatalog(fixture, query, state.searchPage);
  const expanded = projection.groups.find(n => n.id === projection.expandedId);
  const toggle = id => onState({ ...state, expandedGroup: state.expandedGroup === id ? null : id, groupPage: 0 });
  const selectNode = id => fixture.nodes.find(n => n.id === id)?.type === 'hub' ? toggle(id) : onOpen(id);
  return <div className="screen overview-screen" data-screen="overview">
    <div className="screen-intro"><div className="eyebrow">AKASHA MEMORY</div><h1>记忆图</h1><p>先看关联组，再走进一段记忆。</p></div>
    <SearchField value={search} onChange={onSearch}/>
    <p className="search-scope">搜索全部 {turns.length.toLocaleString()} 段示例记忆，包含未展开的内容</p>
    {status !== 'ready' ? <StateView kind={status} onRetry={onRefresh}/> : !turns.length ? <StateView kind="empty" onRetry={onRefresh}/> : query ? <section className="search-results">
      <div className="section-heading"><h2>全范围搜索结果</h2><span>{results.total} 条</span></div>
      {results.total ? <><Pager batch={results} subject="搜索结果" onPage={page => onState({ ...state, searchPage: page })}/>{results.items.map(n => <MemoryRow key={n.id} node={n} onClick={() => onOpen(n.id)} subtitle={n.date + ' · ' + memberships(fixture,n.id).length + ' 个关联组'}/>)}</> : <StateView kind="empty" hasQuery onRetry={() => onSearch('')}/>}</section> : <>
      <div className="overview-summary"><div><strong>{turns.length.toLocaleString()}</strong><span>段记忆</span></div><div><strong>{projection.groups.length}</strong><span>关联组</span></div><span className="summary-caption">记忆总数已去重</span></div>
      <div className="overview-mode-line"><Layers3 size={15}/><span>{expanded ? expanded.title + ' · 展开 ' + projection.batch.items.length + ' / ' + expanded.memberCount + ' 段' : '关联组已折叠 · 点选一个组展开'}</span>{expanded && <button type="button" onClick={() => toggle(expanded.id)}>收起</button>}</div>
      {expanded && <Pager batch={projection.batch} subject="组内记忆" onPage={page => onState({ ...state, groupPage: page })}/>}
      <section className="graph-overview-wrap" aria-label="聚合图概览"><Graph nodes={projection.nodes} edges={projection.edges} selectedId={projection.expandedId} onSelect={selectNode} focusOnly={Boolean(expanded)} layoutKey={(projection.expandedId || 'collapsed') + projection.batch.page}/><GraphLegend onHelp={onHelp}/></section>
      <p className="overlap-note">关联组可以共享记忆；折叠只改变显示。</p>
      {expanded && <section className="expanded-members" aria-label="展开的组成员">
        <button type="button" className="secondary-button full" onClick={() => onOpen(expanded.id)}>查看这个组的一跳关联<ArrowUpRight size={16}/></button>
        {projection.batch.items.map(n => { const count = memberships(fixture,n.id).length; return <MemoryRow key={n.id} node={n} compact onClick={() => onOpen(n.id)} subtitle={count > 1 ? '共享成员 · 同时属于 ' + count + ' 个关联组' : '属于当前关联组'}/>; })}
      </section>}
      <section className="group-directory"><div className="section-heading"><h2>按关联组探索</h2><span>成员可重叠</span></div>
        {projection.groups.map(group => <button type="button" key={group.id} className="group-row" aria-expanded={group.id === projection.expandedId} aria-label={group.title + (group.id === projection.expandedId ? '，收起成员' : '，展开成员')} onClick={() => toggle(group.id)}><span className="group-row-mark"/><span><strong>{group.title}</strong><small>{group.memberCount} 段记忆 · {group.sharedCount} 段为共享成员</small></span>{group.id === projection.expandedId ? <ChevronDown size={18}/> : <ChevronRight size={18}/>}</button>)}
      </section>
      <section className="recent-memories"><div className="section-heading"><h2>最近的记忆</h2></div>{[...turns].sort((a,b) => (b.date + b.time).localeCompare(a.date + a.time)).slice(0,3).map(n => <MemoryRow key={n.id} node={n} onClick={() => onOpen(n.id)}/>)}</section>
      <SnapshotLine revision={fixture.revision}/>
    </>}
  </div>;
}
