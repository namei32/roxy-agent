import React from 'react';
import { ChevronRight, Focus } from 'lucide-react';
import Graph from './Graph.jsx';
import { GraphLegend, MemoryRow, Pager, SnapshotLine } from './components.jsx';
import { relationLabel } from './data.js';
import { localProjection, memberships } from './projection.js';

export default function LocalGraph({ fixture, rootId, selectedId, onSelect, onCenter, onHelp, trail, state, onState }) {
  const root = fixture.nodes.find(n => n.id === rootId);
  if (!root) return null;
  const graph = localProjection(fixture, rootId, state.filter, state.page);
  const activeSelected = graph.nodes.some(n => n.id === selectedId) ? selectedId : rootId;
  const changePage = page => { onState({ ...state, page }); onSelect(rootId); };
  const changeFilter = filter => { onState({ ...state, filter, page: 0 }); onSelect(rootId); };
  const groups = root.type === 'turn' ? memberships(fixture,rootId) : [];
  return <div className="screen local-screen" data-screen="local">
    <div className="local-heading"><h1>{root.title}</h1><div className="local-meta"><span>一跳关联</span><span>{graph.batch.total} 个相邻节点 · 当前 {graph.batch.items.length} 个</span></div></div>
    {groups.length > 1 && <p className="shared-inline">共享记忆 · 同时属于 {groups.length} 个关联组</p>}
    {trail.length > 1 && <div className="breadcrumb"><span>探索路径</span>{trail.slice(-3).map((id,i) => <React.Fragment key={id+'-'+i}><ChevronRight size={12}/><button type="button" onClick={() => onCenter(id)}>{fixture.nodes.find(n => n.id === id)?.short || '记忆'}</button></React.Fragment>)}</div>}
    <div className="segmented" role="group" aria-label="关系类型筛选">{[['all','全部关系'],['membership','共同关联'],['temporal','时间关联']].map(([value,label]) => <button type="button" key={value} aria-pressed={state.filter === value} onClick={() => changeFilter(value)}>{label}</button>)}</div>
    <div className="focus-row"><span>当前页 {graph.edges.length} / {graph.totalEdges} 条关联</span><button type="button" aria-pressed={state.focus} onClick={() => onState({ ...state, focus: !state.focus })}><Focus size={15}/>{state.focus ? '聚焦选中连线' : '显示本页连线'}</button></div>
    <Pager batch={graph.batch} subject="相邻记忆" onPage={changePage}/>
    <Graph nodes={graph.nodes} edges={graph.edges} rootId={rootId} selectedId={activeSelected} onSelect={onSelect} mode="local" filter={state.filter} focusOnly={state.focus} layoutKey={graph.batch.page}/>
    <GraphLegend onHelp={onHelp}/>
    {!graph.batch.total && <p className="inline-empty">当前关系类型下没有相邻节点。</p>}
    {graph.batch.pages > 1 && <p className="overlap-note">其余相邻记忆可翻页查看，翻页不会删除关联。</p>}
    <section className="neighbor-list"><div className="section-heading"><h2>本页相邻记忆与关联组</h2><span>{graph.batch.items.length}</span></div>{graph.batch.items.map(n => { const e = graph.edges.find(e => e.source === n.id || e.target === n.id); const groups = n.type === 'turn' ? memberships(fixture,n.id).length : 0; return <MemoryRow key={n.id} node={n} compact subtitle={relationLabel(e.type) + ' · ' + e.weight.toFixed(2) + (groups > 1 ? ' · 共享成员' : '')} onClick={() => onSelect(n.id)}/>; })}</section>
    <Pager batch={graph.batch} subject="相邻记忆" onPage={changePage}/>
    <SnapshotLine revision={fixture.revision}/>
  </div>;
}
