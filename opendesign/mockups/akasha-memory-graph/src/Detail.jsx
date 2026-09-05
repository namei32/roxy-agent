import React, { useState } from 'react';
import { ArrowUpRight, Check, Copy, Expand, LocateFixed, MessageSquareText } from 'lucide-react';
import { IconButton, MemoryRow, SnapshotLine } from './components.jsx';
import { neighbors, relationLabel, typeLabel } from './data.js';

export default function Detail({ fixture, nodeId, onLocate, onDetails, notify }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const node = fixture.nodes.find(n => n.id === nodeId);
  if (!node) return null;
  const edges = neighbors(fixture, node.id).sort((a,b) => b.weight-a.weight);
  const related = edges.map(e => ({ edge: e, node: fixture.nodes.find(n => n.id === (e.source === node.id ? e.target : e.source)) }));
  const copy = async () => {
    try {
      const content = node.type === 'hub' ? `${node.title}\n${related.map(item => item.node.title).join('\n')}\n（Akasha 原型示例数据）` : `${node.title}\n你：${node.user}\nAkasha：${node.assistant}\n（Akasha 原型示例数据）`;
      await navigator.clipboard.writeText(content);
      setCopied(true); notify('已复制示例记忆');
    } catch { notify('浏览器未允许复制，可展开原文后选取文字'); }
  };
  return <div className="screen detail-screen" data-screen="detail">
    <div className="detail-heading"><div className="detail-type-row"><span className={`type-badge ${node.type}`}>{typeLabel(node)}</span><IconButton label="复制示例记忆" onClick={copy}>{copied ? <Check size={18}/> : <Copy size={18}/>}</IconButton></div><h1>{node.title}</h1><p>{node.date} {node.time}<span>·</span>{node.type === 'hub' ? '共同激活形成的模式' : node.source}</p></div>
    {node.type === 'turn' ? <section className="source-section" aria-labelledby="source-title"><div className="section-heading"><h2 id="source-title"><MessageSquareText size={16}/>原始对话</h2><span>示例来源</span></div><div className="conversation-source"><div className="source-message user"><span className="speaker">你</span><p>{node.user}</p></div><div className="source-message assistant"><span className="speaker">AKASHA</span><p className={expanded ? '' : 'collapsed-source'}>{node.assistant}</p></div><button type="button" className="text-button" aria-expanded={expanded} onClick={() => setExpanded(v=>!v)}>{expanded ? '收起原文' : '展开完整原文'}<Expand size={14}/></button></div></section> : <section className="hub-explanation"><span className="hub-large-mark"/><h2>把共同激活的记忆连在一起</h2><p>这个关联组连接了 {related.length} 段记忆。它表示一种关联模式，本身没有独立的对话原文。</p></section>}
    <section className="detail-relations"><div className="section-heading"><h2>{node.type === 'hub' ? '组内记忆' : '这段记忆的关联'}</h2><span>{related.length} 条</span></div>{related.map(({node: n, edge}) => <MemoryRow key={edge.id} node={n} compact subtitle={relationLabel(edge.type)} right={edge.weight.toFixed(2)} onClick={() => onDetails(n.id)}/>)}<p className="strength-note">数值为关联强度，不代表事实可信度。</p></section>
    <div className="detail-bottom"><button type="button" className="primary-button full" onClick={() => onLocate(node.id)}><LocateFixed size={18}/>在图中定位<ArrowUpRight size={17}/></button><SnapshotLine revision={fixture.revision}/></div>
  </div>;
}
