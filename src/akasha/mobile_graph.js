function mgPager(scope, action = "page") {
  if (!scope || scope.total <= scope.page_size) return "";
  return `<nav class="akmg-pager" aria-label="分页">
    <button type="button" data-mg="${action}" data-page="${scope.page - 1}" ${scope.page === 0 ? "disabled" : ""}>上一页</button>
    <span>${scope.start + 1}–${scope.end} / ${scope.total}</span>
    <button type="button" data-mg="${action}" data-page="${scope.page + 1}" ${scope.next_page === null ? "disabled" : ""}>下一页</button></nav>`;
}

function mgNodeRows(nodes, action) {
  return `<ol class="akmg-node-list">${nodes.map((node, index) => `<li>
    <button type="button" data-mg="${action}" data-node="${escapeHtml(node.id)}">
      <span class="akmg-node-number ${node.kind === "hub" ? "akmg-node-number-hub" : ""}">${index + 1}</span>
      <span><strong>${escapeHtml(node.label)}</strong><small>${escapeHtml(shortTime(node.ts))} · ${node.kind === "hub"
        ? `${node.member_count} 段记忆 · ${node.shared_count} 段共享`
        : node.group_count > 1 ? `共享成员 · 属于 ${node.group_count} 个关联组` : `${node.group_count} 个关联组`}</small></span>
      <span aria-hidden="true">›</span>
    </button></li>`).join("")}</ol>`;
}

function mountMemoryGraph(host, context) {
  if (!context.capabilities?.queryTransports?.includes("https")) {
    host.innerHTML = '<p class="akasha-mobile-error">当前客户端不支持记忆图的数据通道，请更新客户端后查看。</p>';
    return undefined;
  }
  let active = true, serial = 0, busy = false, data = null, overview = null;
  let route = { method: "graph.overview", params: {} }, selected = null, focus = true;
  let queryText = "", sourceOpen = false, notice = "", failure = null;
  const trail = [];

  const request = async (method, params) => {
    const result = await context.query(method, params, { cache: "none", transport: "https" });
    if (result.schema !== "akasha.memory-graph.v1" || !["ready", "empty", "stale", "unavailable", "disabled", "not_found"].includes(result.status)) {
      throw new Error("记忆图响应格式不受支持");
    }
    if (result.status === "ready" && !/^[0-9a-f]{64}$/.test(result.revision)) throw new Error("记忆图版本缺失");
    if (result.status === "ready" && params.revision && result.revision !== params.revision) throw new Error("记忆图版本不一致，请刷新");
    return result;
  };
  const save = () => ({ route, data, selected, sourceOpen, scroll: host.getBoundingClientRect().top });
  const go = async (method, params = {}, { remember = true } = {}) => {
    if (!active) return;
    if (remember && data && !failure) { trail.push(save()); if (trail.length > 32) trail.shift(); }
    route = { method, params }; selected = null; sourceOpen = false; notice = "";
    const token = ++serial;
    busy = true; failure = null; render();
    try {
      const value = await request(method, params);
      if (!active || token !== serial) return;
      if (!["ready", "empty"].includes(value.status)) { failure = value; busy = false; render(); return; }
      data = value; selected = value.root_id || null;
      if (method === "graph.overview") overview = value;
      busy = false; render();
    } catch (error) {
      if (!active || token !== serial) return;
      busy = false; failure = { status: "error", message: error.message || "读取失败，请重试" }; render();
    }
  };
  const back = () => {
    ++serial; busy = false; failure = null; notice = "";
    const previous = trail.pop();
    if (!previous) { go("graph.overview", {}, { remember: false }); return; }
    ({ route, data, selected, sourceOpen } = previous); render();
    host.scrollIntoView({ block: "start" });
  };
  const revision = () => data?.revision || overview?.revision;
  const navigate = (method, nodeId, more = {}) => go(method, { revision: revision(), node_id: nodeId, ...more });
  const refresh = async () => {
    const previous = route, token = ++serial;
    busy = true; failure = null; notice = ""; render();
    try {
      const fresh = await request("graph.overview", {});
      if (!active || token !== serial) return;
      trail.length = 0; overview = fresh; data = fresh;
      if (fresh.status !== "ready") {
        route = { method: "graph.overview", params: {} }; busy = false;
        failure = fresh.status === "empty" ? null : fresh; render(); return;
      }
      const next = previous.method === "graph.overview"
        ? { method: "graph.overview", params: {} }
        : { method: previous.method, params: { ...previous.params, revision: fresh.revision, page: 0 } };
      go(next.method, next.params, { remember: false });
    } catch (error) {
      if (!active || token !== serial) return;
      busy = false; failure = { status: "error", message: error.message }; render();
    }
  };
  const loadSource = async (field) => {
    const prior = data.sources[field];
    if (prior.next_offset === null || busy) return;
    const token = ++serial; busy = true;
    try {
      const value = await request("graph.source", { revision: revision(), node_id: data.node.id, field, offset: prior.next_offset });
      if (!active || token !== serial) return;
      if (value.status !== "ready") { failure = value; busy = false; render(); return; }
      const part = value.source;
      if (part.field !== field || part.offset !== prior.next_offset || part.total_chars !== prior.total_chars) throw new Error("原文分段不连续，请刷新");
      data.sources[field] = { ...part, offset: 0, text: prior.text + part.text };
      busy = false; render();
    } catch (error) {
      if (!active || token !== serial) return;
      busy = false; notice = error.message; render();
    }
  };
  const copySource = async () => {
    try {
      const text = `你\n${data.sources.user.text}\n\nRoxy\n${data.sources.assistant.text}`;
      await navigator.clipboard.writeText(text);
      if (active) { notice = "已复制完整原文"; render(); }
    } catch { if (active) { notice = "复制未完成，可长按原文选择复制"; render(); } }
  };

  function summary() {
    if (!overview?.totals) return "";
    return `<div class="akmg-summary"><div><strong>${overview.totals.turns}</strong><span>段记忆（已去重）</span></div>
      <div><strong>${overview.totals.groups}</strong><span>个关联组</span></div></div>
      <p class="akmg-caption">最近纳入：${escapeHtml(shortTime(overview.included_through?.ts))} · 发布 ${escapeHtml(overview.revision.substring(0, 8))}</p>`;
  }
  function sources() {
    if (data.node.kind === "hub") return '<p class="akmg-explanation">关联组由记忆关系形成，没有独立原文或自动生成的主题。</p>';
    const complete = Object.values(data.sources).every((item) => item.next_offset === null);
    return `<section class="akmg-sources"><button type="button" data-mg="source-toggle" aria-expanded="${sourceOpen}">${sourceOpen ? "收起原文" : "展开原文"}</button>
      ${sourceOpen ? Object.entries(data.sources).map(([field, item]) => `<article><h3>${field === "user" ? "原始输入" : "原始回复"}</h3>
        <div class="akmg-source-text">${escapeHtml(item.text || "（空正文）")}</div>
        <p class="akmg-caption">已显示 ${[...item.text].length} / ${item.total_chars} 字符</p>
        ${item.next_offset !== null ? `<button type="button" data-mg="source-more" data-field="${field}">继续阅读${field === "user" ? "输入" : "回复"}</button>` : ""}</article>`).join("") : ""}
      ${sourceOpen ? `<button type="button" data-mg="copy" ${complete ? "" : "disabled"}>${complete ? "复制完整原文" : "读完原文后可复制全文"}</button>` : ""}</section>`;
  }
  function detailBody() {
    const node = data.node, related = data.related.nodes.filter((item) => item.id !== node.id);
    return `<header class="akmg-detail-header"><span class="akmg-kind">${node.kind === "hub" ? "关联组" : "回合记忆"}</span>
      <h2>${escapeHtml(node.label)}</h2><p>${escapeHtml(shortTime(node.ts))}</p></header>
      ${node.group_count > 1 ? `<p class="akmg-shared">共享成员：同一段记忆属于 ${node.group_count} 个关联组。</p>` : ""}
      ${sources()}<section><h3>${node.kind === "hub" ? "组内成员" : "所属关联组"}</h3>
      ${mgPager(data.related.scope)}${mgNodeRows(related, "local")}
      ${related.length ? "" : '<p class="akmg-caption">暂无成员关系。</p>'}</section>
      <button class="akmg-primary" type="button" data-mg="local" data-node="${escapeHtml(node.id)}">在局部图中定位</button>`;
  }
  function overviewBody() {
    const group = route.method === "graph.group";
    return `${summary()}<form class="akmg-search" data-mg-search><label class="akmg-sr-only" for="akmg-search">搜索全部已发布记忆</label>
      <input id="akmg-search" type="search" maxlength="200" placeholder="搜索全部已发布记忆" value="${escapeHtml(queryText)}"><button type="submit">搜索</button></form>
      <div class="akmg-section-title"><h3>${group ? "组内记忆" : "关联组概览"}</h3>
      ${group ? `<button type="button" data-mg="detail" data-node="${escapeHtml(data.root_id)}">组详情</button><button type="button" data-mg="collapse">收起</button>` : ""}</div>
      <p class="akmg-caption">${group ? `当前显示 ${data.scope.end - data.scope.start} / ${data.scope.total} 个成员` : "点击关联组展开。成员可以共享，组内计数不可相加。"}</p>
      ${mgPager(data.scope)}<div data-mg-canvas></div>
      ${mgNodeRows(group ? data.nodes.filter((n) => n.id !== data.root_id) : data.nodes, group ? "local" : "group")}
      ${data.nodes.length ? "" : '<p class="akmg-caption">尚无关联组，可从最近记忆或搜索开始。</p>'}
      ${!group && overview.recent?.length ? `<h3>最近记忆</h3>${mgNodeRows(overview.recent, "local")}` : ""}`;
  }
  function localBody() {
    const node = data.nodes.find((item) => item.id === selected) || data.nodes[0];
    const incident = data.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
    return `<h2>局部记忆图</h2><p class="akmg-caption">一跳邻居：当前 ${data.scope.end - data.scope.start} / ${data.scope.total} 个 · 发布 ${escapeHtml(data.revision.substring(0, 8))}</p>
      <div class="akmg-filters" aria-label="关联类型">${[["all", "全部"], ["membership", "成员关系"], ["temporal", "时间关系"]].map(([key, name]) => `<button type="button" data-mg="filter" data-filter="${key}" aria-pressed="${data.filter === key}">${name}</button>`).join("")}</div>
      <label class="akmg-focus"><input type="checkbox" data-mg-focus ${focus ? "checked" : ""}>聚焦所选记忆的连线</label>
      ${mgPager(data.scope)}<div data-mg-canvas></div>
      <p class="akmg-caption">圆形为记忆，菱形为关联组；虚线箭头表示有方向的时间关系。</p>
      <section class="akmg-selection"><strong>${escapeHtml(node.label)}</strong>
        ${node.group_count > 1 ? `<p class="akmg-shared">共享成员 · ${node.group_count} 个关联组</p>` : ""}
        <div class="akmg-selection-actions"><button class="akmg-primary" type="button" data-mg="detail" data-node="${escapeHtml(node.id)}">查看详情</button>
        <button type="button" data-mg="local" data-node="${escapeHtml(node.id)}">以此为中心</button></div></section>
      <details class="akmg-relations"><summary>所选节点在当前范围内的 ${incident.length} 条关系</summary><ul>${incident.map((edge) => {
        const from = data.nodes.find((n) => n.id === edge.source), to = data.nodes.find((n) => n.id === edge.target);
        return `<li>${escapeHtml(from.label)} ${edge.directed ? "→" : "↔"} ${escapeHtml(to.label)}<small>${edge.kind === "membership" ? "成员关系" : "时间关系"} · 关联强度 ${Number(edge.strength).toPrecision(3)}</small></li>`;
      }).join("")}</ul></details>
      ${mgNodeRows(data.nodes, "select")}`;
  }
  function render() {
    if (!active) return;
    const isOverview = ["graph.overview", "graph.group"].includes(route.method);
    let body;
    if (busy && !sourceOpen) body = '<p class="akmg-state" role="status">正在读取已发布记忆…</p>';
    else if (failure) body = `<section class="akmg-state" role="alert"><h2>${({ stale: "记忆图已更新", unavailable: "记忆数据暂不可用", disabled: "Akasha 尚未启用", not_found: "记忆已不在当前图中" })[failure.status] || "读取失败"}</h2>
      <p>${escapeHtml(failure.message || "请稍后重试")}</p><button type="button" data-mg="refresh">刷新记忆图</button><button type="button" data-mg="home">返回概览</button></section>`;
    else if (!data || data.status === "empty") body = '<section class="akmg-state"><h2>记忆图还是空的</h2><p>完成对话并发布记忆后，可在这里查看。</p></section>';
    else if (route.method === "graph.detail") body = detailBody();
    else if (route.method === "graph.search") body = `<h2>搜索结果</h2><p class="akmg-caption">“${escapeHtml(queryText)}” · 全部已发布记忆中共 ${data.scope.total} 条</p>${mgPager(data.scope)}${mgNodeRows(data.items, "local")}${data.items.length ? "" : '<p class="akmg-state">没有找到匹配的记忆。</p>'}`;
    else body = isOverview ? overviewBody() : localBody();
    host.innerHTML = `<div class="akmg"><header class="akmg-top"><button type="button" data-mg="back" ${trail.length ? "" : "disabled"}>返回</button><strong>Akasha 记忆图</strong><button type="button" data-mg="refresh">刷新</button></header>
      ${notice ? `<p class="akmg-notice" role="status">${escapeHtml(notice)}</p>` : ""}${body}
      <p class="akmg-footnote">只读浏览 · 关联强度不代表事实可信度或因果关系</p></div>`;
    const canvas = host.querySelector("[data-mg-canvas]");
    if (canvas && data) drawMemoryGraph(canvas, data, {
      aggregate: route.method === "graph.overview", selected, focus,
      onSelect: (id) => {
        if (route.method === "graph.overview") navigate("graph.group", id);
        else if (route.method === "graph.group" && id !== data.root_id) navigate("graph.neighbors", id);
        else { selected = id; render(); }
      },
    });
    host.querySelector("[data-mg-search]")?.addEventListener("submit", (event) => {
      event.preventDefault(); queryText = host.querySelector("input[type=search]").value.trim();
      if (queryText) go("graph.search", { revision: revision(), query: queryText });
    });
    host.querySelector("[data-mg-focus]")?.addEventListener("change", (event) => { focus = event.target.checked; render(); });
    host.querySelectorAll("[data-mg]").forEach((button) => button.addEventListener("click", () => {
      const action = button.dataset.mg, id = button.dataset.node;
      if (action === "back") back();
      else if (action === "refresh") refresh();
      else if (action === "home") { trail.length = 0; go("graph.overview", {}, { remember: false }); }
      else if (action === "collapse") back();
      else if (action === "group") navigate("graph.group", id);
      else if (action === "local") navigate("graph.neighbors", id);
      else if (action === "detail") navigate("graph.detail", id);
      else if (action === "select") { selected = id; render(); }
      else if (action === "filter") go("graph.neighbors", { ...route.params, filter: button.dataset.filter, page: 0 }, { remember: false });
      else if (action === "page") go(route.method, { ...route.params, page: Number(button.dataset.page) }, { remember: false });
      else if (action === "source-toggle") { sourceOpen = !sourceOpen; render(); }
      else if (action === "source-more") loadSource(button.dataset.field);
      else if (action === "copy") copySource();
    }));
  }
  go("graph.overview", {}, { remember: false });
  return () => { active = false; ++serial; trail.length = 0; data = null; overview = null; host.replaceChildren(); };
}
