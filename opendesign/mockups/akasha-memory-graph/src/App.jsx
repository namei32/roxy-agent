import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowRight, BatteryFull, Check, ChevronRight, CircleDot, Layers3, Network, RotateCcw, Signal, Wifi } from 'lucide-react';
import Overview from './Overview.jsx';
import LocalGraph from './LocalGraph.jsx';
import Detail from './Detail.jsx';
import { HelpSheet, SelectedMemory, TopBar } from './components.jsx';
import { createFixture, firstTurn, neighbors } from './data.js';

const STORAGE_KEY = 'roxy.akasha.graph-prototype.v1';
const steps = [
  { id: 'overview', number: '01', name: '图概览', subtitle: '从全貌找到一段记忆', tip: '试着搜索“只读”，或直接点选图中的蓝色节点。' },
  { id: 'local', number: '02', name: '局部关联', subtitle: '沿着连接继续探索', tip: '点选相邻节点，再点“查看详情”。右侧定位按钮可切换探索中心。' },
  { id: 'detail', number: '03', name: '节点详情', subtitle: '回到记忆的原始来源', tip: '展开原始对话，核对关联；“在图中定位”可以返回探索。' },
];
function savedState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    return { view: steps.some(s => s.id === saved.view) ? saved.view : 'overview', scenario: ['standard','dense','empty','error'].includes(saved.scenario) ? saved.scenario : 'standard', rootId: typeof saved.rootId === 'string' ? saved.rootId : 't1', selectedId: typeof saved.selectedId === 'string' ? saved.selectedId : 't1', detailId: typeof saved.detailId === 'string' ? saved.detailId : 't1', revision: Number.isInteger(saved.revision) ? Math.min(99, Math.max(12,saved.revision)) : 12 };
  } catch { return { view: 'overview', scenario: 'standard', rootId: 't1', selectedId: 't1', detailId: 't1', revision: 12 }; }
}

function App() {
  const [initial] = useState(savedState);
  const [view, setView] = useState(initial.view);
  const [scenario, setScenario] = useState(initial.scenario);
  const [rootId, setRootId] = useState(initial.rootId);
  const [selectedId, setSelectedId] = useState(initial.selectedId);
  const [detailStack, setDetailStack] = useState([initial.detailId]);
  const [revision, setRevision] = useState(initial.revision);
  const [search, setSearch] = useState('');
  const [trail, setTrail] = useState([initial.rootId]);
  const [help, setHelp] = useState(false);
  const [toast, setToast] = useState('');
  const [status, setStatus] = useState('loading');
  const [reload, setReload] = useState(0);
  const [recovered, setRecovered] = useState(false);
  const [stale, setStale] = useState(false);
  const contentRef = useRef(null);
  const fixture = useMemo(() => createFixture(scenario, revision), [scenario, revision]);
  const fallbackId = firstTurn(fixture)?.id;
  const hasNode = id => fixture.nodes.some(n=>n.id===id);
  const currentRoot = hasNode(rootId) ? rootId : fallbackId;
  const currentSelected = hasNode(selectedId) ? selectedId : currentRoot;
  const selectedNode = fixture.nodes.find(node => node.id === currentSelected);
  const detailId = hasNode(detailStack.at(-1)) ? detailStack.at(-1) : currentSelected;
  const activeView = fixture.nodes.length && status === 'ready' ? view : 'overview';
  const step = steps.find(s=>s.id===activeView);
  const closeHelp = useCallback(() => setHelp(false), []);
  const showHelp = useCallback(() => setHelp(true), []);
  const notify = useCallback(message => setToast(message), []);

  useEffect(() => {
    setStatus('loading');
    const timer = setTimeout(() => {
      setStatus(scenario === 'error' && !recovered ? 'error' : 'ready');
      if (reload > 0 && (scenario !== 'error' || recovered)) {
        setStale(false);
        setToast(scenario === 'empty' ? '目前还没有已发布的记忆' : '示例快照已更新，浏览选择已保留');
      }
    }, 650);
    return () => clearTimeout(timer);
  }, [scenario, reload, recovered]);

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ view, scenario, rootId: currentRoot || 't1', selectedId: currentSelected || 't1', detailId: detailId || 't1', revision })); } catch { /* Private mode still supports this in-memory demo. */ }
  }, [view, scenario, currentRoot, currentSelected, detailId, revision]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 2900);
    return () => clearTimeout(timer);
  }, [toast]);

  useEffect(() => { contentRef.current?.scrollTo({ top: 0 }); }, [activeView, detailId, currentRoot]);

  const refresh = () => {
    if (status === 'loading') return;
    if (scenario === 'error') setRecovered(true);
    if (scenario !== 'empty') setRevision(value => Math.min(99, value+1));
    setReload(value => value+1);
  };
  const changeScenario = value => {
    setScenario(value); setRevision(12); setRecovered(false); setStale(false);
    setView('overview'); setRootId('t1'); setSelectedId('t1'); setDetailStack(['t1']); setTrail(['t1']); setSearch(''); setToast(''); setReload(0);
  };
  const openLocal = id => {
    if (stale) return notify('快照已变化，请先刷新后继续探索');
    setRootId(id); setSelectedId(id); setTrail([id]); setView('local');
  };
  const centerOn = id => {
    if (stale) return notify('快照已变化，请先刷新后继续探索');
    setRootId(id); setSelectedId(id); setTrail(items => [...items, id].slice(-8));
  };
  const openDetails = id => {
    if (stale) return notify('快照已变化，请先刷新后查看详情');
    setDetailStack([id]); setView('detail');
  };
  const back = () => {
    if (view === 'detail' && detailStack.length > 1) return setDetailStack(stack => stack.slice(0,-1));
    setView(view === 'detail' ? 'local' : 'overview');
  };
  const jump = target => {
    if (target !== 'overview' && stale) return notify('请先刷新示例快照');
    if (target === 'detail') setDetailStack([currentSelected]);
    setView(target);
  };
  const reset = () => { changeScenario('standard'); setReload(value=>value+1); notify('已回到第一屏'); };

  return <div className="prototype-shell">
    <header className="studio-header"><a className="studio-brand" href="../../../opendesign/" aria-label="打开原型目录"><span className="brand-glyph"><Network size={20}/></span><strong>roxy</strong><span className="brand-separator"/><span>AKASHA</span></a><span className="studio-version"><span/>交互原型 · 01</span></header>
    <main className="prototype-stage">
      <aside className="journey-panel"><div className="eyebrow">MEMORY, CONNECTED</div><h1>探索记忆<br/>之间的连接<span className="title-period">.</span></h1><p className="studio-description">从一张图出发，<br/>回到每段记忆的来源。</p>
        <nav className="journey-nav" aria-label="原型屏幕">{steps.map(item => <button type="button" key={item.id} aria-current={activeView === item.id ? 'step' : undefined} onClick={() => jump(item.id)} disabled={item.id !== 'overview' && (!fixture.nodes.length || status !== 'ready')}><span className="step-number">{item.number}</span><span><strong>{item.name}</strong><small>{item.subtitle}</small></span><ChevronRight size={17}/></button>)}</nav>
        <div className="journey-note"><span className="note-rule"/><p>查看、选择与拖动，<br/>只改变你眼前的视图。</p></div>
      </aside>

      <section className="device-stage" aria-label="手机原型">
        <div className="device-label"><span>ROXY MOBILE</span><span>{step.number} / 03</span></div>
        <div className="phone-frame"><div className="phone-camera" aria-hidden="true"/><div className="phone-display"><div className="phone-status" aria-hidden="true"><span>9:41</span><div><Signal size={14}/><Wifi size={14}/><BatteryFull size={19}/></div></div>
          <TopBar view={activeView} detailBack={detailStack.length > 1} onBack={back} onRefresh={refresh} loading={status === 'loading'} onHelp={showHelp}/>
          <div className="demo-ribbon"><span className="demo-label-dot"/>示例数据<span>浏览不会修改记忆</span></div>
          {stale && <div className="version-notice" role="status"><span>图已有新版本，刷新后继续探索</span><button type="button" onClick={refresh}>刷新</button></div>}
          <div className="phone-content" ref={contentRef} key={activeView}>
            {activeView === 'overview' && <Overview fixture={fixture} search={search} onSearch={setSearch} onOpen={openLocal} onHelp={showHelp} onRefresh={refresh} status={status} dense={scenario === 'dense'}/>}
            {activeView === 'local' && <LocalGraph key={currentRoot} fixture={fixture} rootId={currentRoot} selectedId={currentSelected} onSelect={setSelectedId} onCenter={centerOn} onHelp={showHelp} trail={trail.filter(hasNode)}/>}
            {activeView === 'detail' && <Detail key={detailId} fixture={fixture} nodeId={detailId} onLocate={openLocal} onDetails={id => { if (stale) return notify('请先刷新示例快照'); setDetailStack(stack=>[...stack,id]); }} notify={notify}/>}
          </div>
          {activeView === 'local' && selectedNode && <div className="local-dock"><SelectedMemory node={selectedNode} count={neighbors(fixture, currentSelected).length} onDetails={() => openDetails(currentSelected)} onCenter={() => centerOn(currentSelected)} isCenter={currentSelected === currentRoot}/></div>}
          {toast && <div className="toast" role="status"><Check size={15}/><span>{toast}</span></div>}
          <HelpSheet open={help} onClose={closeHelp}/><div className="phone-gesture" aria-hidden="true"><span/></div>
        </div></div>
        <p className="device-caption"><CircleDot size={12}/>可以直接点击、搜索和拖动</p>
      </section>

      <aside className="demo-panel"><div className="demo-panel-heading"><span className="eyebrow">TRY IT YOURSELF</span><span className="panel-rule"/></div><h2>{step.name}</h2><p className="current-tip">{step.tip}</p><div className="demo-control"><label htmlFor="scenario">演示场景</label><select id="scenario" value={scenario} onChange={e=>changeScenario(e.target.value)}><option value="standard">普通记忆图</option><option value="dense">密集图 · 100 个节点</option><option value="empty">空图</option><option value="error">读取失败</option></select></div>
        <button type="button" className="demo-action" disabled={status !== 'ready' || !fixture.nodes.length || stale} onClick={() => {setStale(true); notify('已模拟一次新的图发布');}}><Layers3 size={16}/><span>模拟版本更新</span><ArrowRight size={15}/></button>
        <button type="button" className="demo-action" onClick={reset}><RotateCcw size={16}/><span>重新体验</span><ArrowRight size={15}/></button>
        <div className="prototype-footnote"><span className="mini-label">关于这个原型</span><p>所有对话、节点和关联均为示例。刷新模拟快照切换，不连接真实记忆库。</p></div>
      </aside>
    </main><footer className="studio-footer"><span>AKASHA / MEMORY EXPLORER</span><span>探索 · 理解 · 回溯</span></footer>
  </div>;
}

createRoot(document.getElementById('root')).render(<App/>);
