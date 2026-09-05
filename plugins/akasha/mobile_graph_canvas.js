function drawMemoryGraph(host, data, options) {
  const width = 360;
  const height = options.aggregate ? Math.max(140, Math.ceil(data.nodes.length / 2) * 76 + 25) : 296;
  const points = new Map();
  data.nodes.forEach((node, index) => {
    if (options.aggregate) {
      points.set(node.id, { x: 95 + (index % 2) * 170, y: 45 + Math.floor(index / 2) * 76 });
    } else if (node.id === data.root_id) {
      points.set(node.id, { x: 180, y: 148 });
    } else {
      const angle = -Math.PI / 2 + (index - 1) * Math.PI * 2 / Math.max(1, data.nodes.length - 1);
      points.set(node.id, { x: 180 + Math.cos(angle) * 122, y: 148 + Math.sin(angle) * 106 });
    }
  });
  const label = (node) => node.kind === "hub" ? "关联组" : "记忆";
  host.innerHTML = `<svg class="akmg-canvas" viewBox="0 0 ${width} ${height}" aria-label="${options.aggregate ? "关联组概览" : "当前范围的记忆关系"}">
    <defs><marker id="akmg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor"/></marker></defs>
    <g data-graph-plane>
      ${data.edges.map((edge) => {
        const a = points.get(edge.source), b = points.get(edge.target);
        if (!a || !b) throw new Error("图关系缺少端点");
        const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
        const dx = (b.x - a.x) * 25 / length, dy = (b.y - a.y) * 25 / length;
        const active = !options.focus || !options.selected || edge.source === options.selected || edge.target === options.selected;
        return `<line x1="${a.x + dx}" y1="${a.y + dy}" x2="${b.x - dx}" y2="${b.y - dy}" class="akmg-edge ${edge.directed ? "akmg-edge-temporal" : ""}" opacity="${active ? 0.65 : 0.13}" ${edge.directed ? 'marker-end="url(#akmg-arrow)"' : ""}/>`;
      }).join("")}
      ${data.nodes.map((node, index) => {
        const p = points.get(node.id);
        const selected = node.id === options.selected;
        const text = options.aggregate ? String(node.member_count) : String(index + 1);
        return `<g transform="translate(${p.x},${p.y})" tabindex="0" role="button" data-graph-node="${escapeHtml(node.id)}" aria-label="${escapeHtml(`${label(node)}：${node.label}${node.kind === "hub" ? `，${node.member_count} 个成员` : ""}`)}" aria-pressed="${selected}" class="akmg-node ${node.kind === "hub" ? "akmg-node-hub" : ""} ${selected ? "akmg-node-selected" : ""}">
          <circle r="25" class="akmg-hit"/>
          ${node.kind === "hub" ? '<rect x="-18" y="-18" width="36" height="36" rx="8" transform="rotate(45)"/>' : '<circle r="22"/>'}
          <text text-anchor="middle" dominant-baseline="central">${text}</text>
          ${options.aggregate ? `<text class="akmg-group-label" y="40" text-anchor="middle">${escapeHtml(node.label)}</text>` : ""}
        </g>`;
      }).join("")}
    </g></svg>
    <div class="akmg-canvas-tools"><span data-graph-zoom>100%</span><button type="button" data-graph-reset>重置视图</button></div>`;
  const svg = host.querySelector("svg"), plane = host.querySelector("[data-graph-plane]");
  host.prepend(host.querySelector(".akmg-canvas-tools"));
  const pointers = new Map();
  let { scale = 1, tx = 0, ty = 0 } = options.view || {};
  let moved = false, start = null, pinch = null, pendingSelection = null;
  const update = () => {
    plane.setAttribute("transform", `translate(${tx},${ty}) scale(${scale})`);
    host.querySelector("[data-graph-zoom]").textContent = `${Math.round(scale * 100)}%`;
    options.onViewChange?.({ scale, tx, ty });
  };
  update();
  const local = (x, y) => new DOMPoint(x, y).matrixTransform(svg.getScreenCTM().inverse());
  const centerDistance = () => {
    const [a, b] = [...pointers.values()];
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, distance: Math.hypot(a.x - b.x, a.y - b.y) };
  };
  svg.addEventListener("pointerdown", (event) => {
    pendingSelection = null;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    svg.setPointerCapture(event.pointerId);
    if (pointers.size === 1) {
      start = { ...local(event.clientX, event.clientY), x0: event.clientX, y0: event.clientY, tx, ty };
      // DOMPoint properties are not enumerable in every WebView.
      start.x = local(event.clientX, event.clientY).x;
      start.y = local(event.clientX, event.clientY).y;
      moved = false;
    } else if (pointers.size === 2) {
      const p = centerDistance(), at = local(p.x, p.y);
      pinch = { ...p, scale, anchorX: (at.x - tx) / scale, anchorY: (at.y - ty) / scale };
      moved = true;
    }
  });
  svg.addEventListener("pointermove", (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2 && pinch) {
      const p = centerDistance(), at = local(p.x, p.y);
      scale = Math.max(0.65, Math.min(2.5, pinch.scale * p.distance / Math.max(1, pinch.distance)));
      tx = at.x - pinch.anchorX * scale; ty = at.y - pinch.anchorY * scale;
      update();
    } else if (pointers.size === 1 && start && !pinch) {
      if (Math.hypot(event.clientX - start.x0, event.clientY - start.y0) > 6) moved = true;
      if (moved) {
        const at = local(event.clientX, event.clientY);
        tx = start.tx + at.x - start.x; ty = start.ty + at.y - start.y; update();
      }
    }
  });
  const finish = (event, cancelled = false) => {
    if (!pointers.has(event.pointerId)) return;
    const canSelect = !cancelled && !moved && pointers.size === 1;
    pointers.delete(event.pointerId);
    if (!pointers.size) { start = null; pinch = null; }
    if (!canSelect) return;
    const matrix = plane.getScreenCTM();
    let chosen, nearest = Infinity;
    data.nodes.forEach((node) => {
      const p = points.get(node.id), screen = new DOMPoint(p.x, p.y).matrixTransform(matrix);
      const distance = Math.hypot(screen.x - event.clientX, screen.y - event.clientY);
      const radius = Math.max(22, 25 * Math.hypot(matrix.a, matrix.b));
      if (distance <= radius && distance < nearest) { chosen = node.id; nearest = distance; }
    });
    pendingSelection = chosen || null;
  };
  svg.addEventListener("pointerup", (event) => finish(event));
  svg.addEventListener("pointercancel", (event) => finish(event, true));
  svg.addEventListener("click", (event) => {
    event.preventDefault(); event.stopPropagation();
    const id = pendingSelection; pendingSelection = null;
    if (id) options.onSelect(id);
  });
  svg.addEventListener("keydown", (event) => {
    const id = event.target.closest("[data-graph-node]")?.dataset.graphNode;
    if (id && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); options.onSelect(id); }
  });
  host.querySelector("[data-graph-reset]").addEventListener("click", () => {
    scale = 1; tx = 0; ty = 0; update();
  });
}
