import React, { useEffect, useId, useRef, useState } from 'react';
import { Minus, Plus, Scan } from 'lucide-react';
import { IconButton } from './components.jsx';

function localPositions(nodes, rootId) {
  const result = { [rootId]: [180, 150] };
  const others = nodes.filter(n => n.id !== rootId);
  others.forEach((node, i) => {
    const angle = -Math.PI / 2 + i * Math.PI * 2 / Math.max(others.length, 1);
    const radius = others.length > 12 ? 50 + Math.floor(i / 10)*28 : 96;
    result[node.id] = [180 + Math.cos(angle)*radius, 150 + Math.sin(angle)*radius];
  });
  return result;
}

export default function Graph({ nodes, edges, rootId, selectedId, onSelect, mode = 'overview', dense = false, filter = 'all', focusOnly = false, layoutKey = '' }) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const gesture = useRef(null);
  const moved = useRef(false);
  const pointerMap = useRef(new Map());
  const pinch = useRef(null);
  const pendingTap = useRef(null);
  const svgRef = useRef(null);
  const worldRef = useRef(null);
  const marker = useId().replace(/:/g, '');
  const height = mode === 'overview' ? 390 : 300;
  const positions = mode === 'local' ? localPositions(nodes, rootId) : Object.fromEntries(nodes.map(n => [n.id, n.position]));
  useEffect(() => { setZoom(1); setPan({ x: 0, y: 0 }); }, [rootId, mode, dense, filter, layoutKey]);
  const focusedIds = new Set([selectedId, ...edges.filter(e => e.source === selectedId || e.target === selectedId).flatMap(e => [e.source,e.target])]);

  // Resolve in screen pixels: transparent hit circles must not steal taps,
  // and the minimum 44px touch diameter stays constant while zooming out.
  const nearestNode = (clientX, clientY) => {
    const matrix = worldRef.current?.getScreenCTM();
    if (!matrix) return undefined;
    const scale = Math.hypot(matrix.a, matrix.b);
    let closest, bestDistance = Infinity;
    for (const node of nodes) {
      const position = positions[node.id];
      if (!position) continue;
      const point = new DOMPoint(...position).matrixTransform(matrix);
      const distance = Math.hypot(point.x-clientX, point.y-clientY);
      const visualRadius = node.aggregate ? (selectedId && node.id !== selectedId ? 17 : 26) : node.id === rootId ? 17 : node.id === selectedId ? 12 : node.type === 'hub' ? 11 : dense ? 4.5 : 7;
      if (distance <= Math.max(22, visualRadius*scale) && distance < bestDistance) {
        closest = node.id; bestDistance = distance;
      }
    }
    return closest;
  };

  const pointerDown = event => {
    pendingTap.current = null;
    pointerMap.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    svgRef.current?.setPointerCapture(event.pointerId);
    if (pointerMap.current.size === 2) {
      const [a, b] = [...pointerMap.current.values()];
      pinch.current = { distance: Math.hypot(a.x-b.x, a.y-b.y), zoom };
      gesture.current = null;
      moved.current = true;
    } else {
      gesture.current = { x: event.clientX, y: event.clientY, pan, target: nearestNode(event.clientX, event.clientY) };
      moved.current = false;
    }
  };

  const pointerMove = event => {
    if (!pointerMap.current.has(event.pointerId)) return;
    pointerMap.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pinch.current && pointerMap.current.size === 2) {
      const [a, b] = [...pointerMap.current.values()];
      setZoom(Math.max(0.65, Math.min(2.5, pinch.current.zoom * Math.hypot(a.x-b.x, a.y-b.y) / pinch.current.distance)));
      return;
    }
    const start = gesture.current;
    if (!start) return;
    const dx = event.clientX - start.x, dy = event.clientY - start.y;
    if (Math.hypot(dx, dy) > 5) moved.current = true;
    if (moved.current) {
      const matrix = svgRef.current.getScreenCTM();
      const scale = Math.hypot(matrix.a, matrix.b);
      setPan({ x: Math.max(-320, Math.min(320, start.pan.x + dx/scale)), y: Math.max(-320, Math.min(320, start.pan.y + dy/scale)) });
    }
  };

  const pointerUp = event => {
    const start = gesture.current;
    pointerMap.current.delete(event.pointerId);
    pendingTap.current = start?.target && !moved.current && !pinch.current ? start.target : null;
    gesture.current = null;
    if (pointerMap.current.size < 2) pinch.current = null;
    if (svgRef.current?.hasPointerCapture(event.pointerId)) svgRef.current.releasePointerCapture(event.pointerId);
  };

  const click = event => {
    const target = pendingTap.current || (event.detail === 0 ? event.target.closest('[data-node]')?.dataset.node : null);
    pendingTap.current = null;
    event.preventDefault();
    event.stopPropagation();
    // Change screens only inside the final click, so the touch-generated click
    // cannot land on a newly mounted button at the same screen coordinates.
    if (target) onSelect(target);
  };

  return <div className={`graph-panel ${mode} ${dense ? 'dense' : ''} ${focusOnly ? 'focus-only' : ''}`}>
    <svg ref={svgRef} viewBox={`0 0 360 ${height}`} className="memory-graph" aria-label={mode === 'overview' ? '记忆图概览，可点选节点' : '一跳记忆关联图，可点选节点和拖动'} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onClick={click} onPointerCancel={() => { gesture.current = null; pinch.current = null; pendingTap.current = null; pointerMap.current.clear(); }}>
      <defs><pattern id={`dots-${marker}`} width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="0.75" fill="currentColor"/></pattern><marker id={`arrow-${marker}`} markerWidth="6" markerHeight="6" refX="4.8" refY="3" orient="auto"><polygon points="0,0 6,3 0,6" fill="currentColor"/></marker></defs>
      <rect width="360" height={height} fill={`url(#dots-${marker})`} className="graph-grid"/>
      <g ref={worldRef} transform={`translate(${180 + pan.x},${height/2 + pan.y}) scale(${zoom}) translate(-180,-${height/2})`}>
        {edges.map(e => {
          const a = positions[e.source], b = positions[e.target];
          if (!a || !b) return null;
          const active = e.source === selectedId || e.target === selectedId;
          const length = Math.hypot(b[0]-a[0], b[1]-a[1]) || 1;
          const inset = mode === 'local' ? 16 : 11;
          return <line key={e.id} x1={a[0]+(b[0]-a[0])/length*inset} y1={a[1]+(b[1]-a[1])/length*inset} x2={b[0]-(b[0]-a[0])/length*inset} y2={b[1]-(b[1]-a[1])/length*inset} className={`graph-edge ${e.type} ${active ? 'active' : ''}`} strokeWidth={active ? 1.8 : dense ? 0.75 : 1.2} markerEnd={e.type === 'temporal' ? `url(#arrow-${marker})` : undefined}/>;
        })}
        {[...nodes].sort((a,b) => (a.id === selectedId ? 1 : 0) - (b.id === selectedId ? 1 : 0)).map(node => {
          const point = positions[node.id];
          if (!point) return null;
          const selected = node.id === selectedId;
          const center = node.id === rootId && mode === 'local';
          const radius = node.aggregate ? (selectedId && !selected ? 17 : 26) : center ? 17 : selected ? 12 : node.type === 'hub' ? 11 : dense ? 4.5 : 7;
          const showLabel = !dense || selected || node.type === 'hub' || mode === 'local' && nodes.length <= 12;
          return <g key={node.id} transform={`translate(${point[0]},${point[1]})`} className={`graph-node ${node.type} ${selected ? 'selected' : ''} ${center ? 'center' : ''} ${node.aggregate ? 'aggregate' : ''} ${focusOnly && !focusedIds.has(node.id) ? 'muted-node' : ''}`} role="button" tabIndex={0} aria-label={`${node.type === 'hub' ? '关联组' : '记忆'}：${node.title}${node.aggregate ? `，${node.memberCount} 段记忆，点击展开或收起` : ''}`} aria-pressed={selected} data-node={node.id} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(node.id); } }}>
            <circle r="30" className="node-hit"/>
            {(selected || center) && <circle r={radius+10} className="node-halo"/>}
            {node.type === 'hub' ? <rect x={-radius+2} y={-radius+2} width={(radius-2)*2} height={(radius-2)*2} rx="3" transform="rotate(45)" className="node-shape"/> : <circle r={radius} className="node-shape"/>}
            {node.aggregate && <text y="4" textAnchor="middle" className="group-count">{node.memberCount}</text>}
            {center && !node.aggregate && <circle r="5" fill="white" opacity="0.96"/>}
            {showLabel && <text y={radius+21} textAnchor="middle" className="node-label">{node.aggregate ? node.title : mode === 'local' && center ? '当前中心' : node.short}</text>}
          </g>;
        })}
      </g>
    </svg>
    <div className="graph-toolbar" aria-label="图缩放工具"><IconButton label="缩小图" disabled={zoom <= 0.65} onClick={() => setZoom(z => Math.max(0.65, z-0.2))}><Minus size={17}/></IconButton><span aria-live="polite">{Math.round(zoom*100)}%</span><IconButton label="放大图" disabled={zoom >= 2.5} onClick={() => setZoom(z => Math.min(2.5, z+0.2))}><Plus size={17}/></IconButton><span className="toolbar-rule"/><IconButton label="重置图位置" onClick={() => { setZoom(1); setPan({x:0,y:0}); }}><Scan size={17}/></IconButton></div>
  </div>;
}
