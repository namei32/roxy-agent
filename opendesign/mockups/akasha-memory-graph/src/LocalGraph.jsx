import React, { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import Graph from './Graph.jsx';
import { GraphLegend, MemoryRow, SnapshotLine } from './components.jsx';
import { relatedNodes, relationLabel } from './data.js';

export default function LocalGraph({ fixture, rootId, selectedId, onSelect, onCenter, onHelp, trail }) {
  const [filter, setFilter] = useState('all');
  const root = fixture.nodes.find(n => n.id === rootId);
  const node = fixture.nodes.find(n => n.id === selectedId) || root;
  if (!root || !node) return null;
  const graph = relatedNodes(fixture, rootId, filter);
  const activeSelected = graph.nodes.some(n => n.id === selectedId) ? selectedId : rootId;
  const items = graph.nodes.filter(n => n.id !== rootId);
  const changeFilter = value => { setFilter(value); onSelect(rootId); };
  return <div className="screen local-screen" data-screen="local">
    <div className="local-heading"><h1>{root.title}</h1><div className="local-meta"><span>一跳关联</span><span>{graph.nodes.length-1} 个相邻节点</span></div></div>
    {trail.length > 1 && <div className="breadcrumb"><span>探索路径</span>{trail.slice(-3).map((id,i) => <React.Fragment key={`${id}-${i}`}><ChevronRight size={12}/><button type="button" onClick={() => onCenter(id)}>{fixture.nodes.find(n => n.id === id)?.short || '记忆'}</button></React.Fragment>)}</div>}
    <div className="segmented" role="group" aria-label="关系类型筛选">{[['all','全部关系'],['membership','共同关联'],['temporal','时间关联']].map(([value,label]) => <button type="button" key={value} aria-pressed={filter === value} onClick={() => changeFilter(value)}>{label}</button>)}</div>
    <Graph nodes={graph.nodes} edges={graph.edges} rootId={rootId} selectedId={activeSelected} onSelect={onSelect} mode="local" dense={graph.nodes.length > 14} filter={filter}/>
    <GraphLegend onHelp={onHelp}/>
    {!items.length && <p className="inline-empty">这段记忆在当前筛选下没有相邻节点。</p>}
    <section className="neighbor-list"><div className="section-heading"><h2>相邻记忆与关联组</h2><span>{items.length}</span></div>{items.map(n => { const e = graph.edges.find(e => e.source === n.id || e.target === n.id); return <MemoryRow key={n.id} node={n} compact subtitle={`${relationLabel(e.type)} · 关联强度 ${e.weight.toFixed(2)}`} onClick={() => onSelect(n.id)}/>; })}</section>
    <SnapshotLine revision={fixture.revision}/>
  </div>;
}
