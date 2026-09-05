// Entirely synthetic. These objects do not describe the user's actual memory.
const examples = [
  ['t1', '记忆图的第一版', '09-05', '10:24', '我想在手机上看见 Akasha 的记忆图，先做哪些功能？', '第一版可以从图概览、局部关联和节点详情开始。先把浏览做清楚：找到一段记忆，看见它的连接，再回到原始对话。所有查看操作都保持只读。\n\n概览提供搜索和当前显示范围；局部图一次只展开一层，并保留探索的中心。点选以后，底部给出这段记忆的标题和详情入口。\n\n详情需要保留完整来源。较长的回复可以先折叠，再由用户展开阅读；不能让图上的短标题取代原始表达。界面中的关联强度也不应被解释为事实可信度。', 'h1'],
  ['t2', '从用户问题开始', '09-04', '21:16', '开发这个插件，应该先写文档还是先画界面？', '先写清楚用户要完成什么，再做可点击的原型。用具体的操作路径检验需求，然后根据界面实际需要确定数据接口。', 'h1'],
  ['t3', '只读查询的边界', '09-04', '20:02', '打开记忆图会不会影响下一次学习？', '浏览应只读取已发布的快照。搜索、选中和刷新只改变展示，不应调整关联权重，也不应创建待提交的学习上下文。\n\n正式实现时，需要在隔离样例中观察查询前后的数据库和写入集合，确认浏览请求没有触发学习。仅仅给页面加上“只读”标签，还不能作为这个结论的证据。\n\n如果探索途中出现新的发布版本，界面应提示刷新，并在同一版本下核对节点和详情。本原型先用示例状态把这条操作路径展示出来。', 'h1'],
  ['t4', '关联不等于事实', '09-03', '18:42', '图里的一条边，能不能理解成这两件事有因果关系？', '边表示系统保存的关联。共同出现或时间相近都可能形成关联，它不能单独证明因果，也不是一项事实的可信度。理解时仍要回到来源。', 'h1'],
  ['t5', '移动端的三屏流程', '09-04', '19:30', '手机屏幕小，怎样查看复杂的记忆图？', '概览负责定位，局部图展示选中记忆附近的连接，详情负责阅读。每次只展开一层关系，让用户能知道自己从哪里来到这里。', 'h2'],
  ['t6', '让密集的图可读', '09-03', '16:05', '节点越来越多的时候，怎样避免一团乱？', '先提供搜索和局部展开，再用缩放与关系类型筛选辅助阅读。需要明确当前显示范围，屏幕上没显示的内容并不代表被删除。', 'h2'],
  ['t7', '刷新后保留选择', '09-02', '22:13', '刷新时，正在查看的节点能保留下来吗？', '用稳定的记忆身份保留选择，避免依赖数组位置。新快照返回后核对身份；如果目标已经不存在，应明确提示并解除旧选择。', 'h2'],
  ['t8', '先原型，再联调', '09-02', '10:00', '原型阶段要连接真实数据库吗？', '可以先使用明确标注的示例数据把操作走通。交互确定后，在隔离环境验证真实读取链路，再进入完整联调。', 'h2'],
  ['t9', '周末去山里走走', '09-01', '20:18', '这个周末想安排一次轻松的徒步。', '可以选一条半天往返、路况清晰的路线，留出休息和返程时间。出发前再核对天气、开放情况和交通。', 'h3'],
  ['t10', '轻装徒步清单', '08-31', '18:52', '短途徒步怎样准备背包？', '先准备饮水、简单食物、防晒和基础应急物品，按天气补充雨具。装备数量以路线和实际负重为准。', 'h3'],
  ['t11', '为阅读留半小时', '08-30', '21:00', '希望每天能留一点时间阅读。', '可以从一个容易保持的时段开始，先读半小时。读完记下一个问题或一个想继续探索的方向。', 'h3'],
  ['t12', '把想法记下来', '08-30', '19:30', '阅读时总会冒出一些零散想法。', '先保留当时的表述和出处，之后再整理关联。记录的价值也包括让你能够回到那个思考发生的时刻。', 'h3'],
  ['t13', '看见记忆的来源', '09-03', '11:12', '节点详情最重要的内容是什么？', '优先给出原始对话、时间和关联对象。展示的信息要足以让用户判断这段记忆的意思，不让压缩后的标题替代原文。', 'h1'],
];

const positions = {
  t1: [196, 145], t2: [106, 45], t3: [56, 130], t4: [81, 210],
  t5: [298, 134], t6: [314, 235], t7: [233, 277], t8: [263, 55],
  t9: [117, 309], t10: [46, 313], t11: [175, 357], t12: [40, 244],
  t13: [150, 222], h1: [120, 130], h2: [264, 203], h3: [111, 265],
};

function turn(row) {
  const [id, title, date, time, user, assistant, group] = row;
  return { id, type: 'turn', title, date, time, user, assistant, group,
    source: '示例会话 · ' + (group === 'h3' ? '生活与阅读' : 'Agent 开发'),
    position: positions[id], short: title.length > 6 ? title.slice(0, 6) : title };
}

const edge = (source, target, type, weight) => ({ id: `${type}:${source}:${target}`, source, target, type, weight });

export function createFixture(scenario = 'standard', revision = 12) {
  if (scenario === 'empty') return { nodes: [], edges: [], totalNodes: 0, totalEdges: 0, revision, truncated: false };
  if (scenario === 'dense') return denseFixture(revision);
  const turns = examples.map(turn);
  const hubs = ['h1', 'h2', 'h3'].map((id, i) => ({ id, type: 'hub', title: `关联组 0${i + 1}`, short: `组 0${i + 1}`, date: '09-05', time: '10:24', position: positions[id], creatorId: ['t1', 't5', 't9'][i] }));
  const edges = turns.map((n, i) => edge(n.id, n.group, 'membership', [0.86, 0.72, 0.88, 0.58, 0.81, 0.67, 0.76, 0.61, 0.79, 0.74, 0.45, 0.57, 0.80][i]));
  edges.push(edge('t2', 't1', 'temporal', 0.71), edge('t3', 't1', 'temporal', 0.82), edge('t4', 't3', 'temporal', 0.48), edge('t8', 't5', 'temporal', 0.76), edge('t6', 't5', 'temporal', 0.62), edge('t7', 't1', 'temporal', 0.54), edge('t10', 't9', 'temporal', 0.66), edge('t12', 't11', 'temporal', 0.31), edge('t13', 't5', 'temporal', 0.63));
  if (revision > 12) {
    turns.push({ ...turn(['t14', '为记忆图补充版本', '09-05', '10:32', '怎样知道当前看到的是哪次更新？', '在概览和详情显示同一个快照版本，刷新时再一起切换。页面里的布局变化不应该改变记忆图。', 'h1']), position: [202, 51], short: '补充版本' });
    edges.push(edge('t14', 'h1', 'membership', 0.77), edge('t1', 't14', 'temporal', 0.69));
  }
  const nodes = [...turns, ...hubs];
  return { nodes, edges, totalNodes: nodes.length, totalEdges: edges.length, revision, truncated: false };
}

function denseFixture(revision) {
  const hubs = Array.from({ length: 4 }, (_, i) => ({ id: `dh${i}`, type: 'hub', title: `关联组 ${String(i + 1).padStart(2, '0')}`, short: `组 ${i + 1}`, date: '09-05', time: '10:24', position: [[102,105],[265,110],[105,285],[266,278]][i] }));
  const turns = Array.from({ length: 96 }, (_, i) => {
    const seed = turn(examples[i % examples.length]);
    const group = hubs[i % 4];
    const angle = i * 2.399963;
    const radius = 24 + Math.sqrt(Math.floor(i / 4)) * 15;
    return { ...seed, id: `dense-${i + 1}`, group: group.id, title: `${seed.title} · ${String(i + 1).padStart(2, '0')}`, short: `${i + 1}`, position: [Math.max(24, Math.min(336, group.position[0] + Math.cos(angle)*radius)), Math.max(24, Math.min(366, group.position[1] + Math.sin(angle)*radius))] };
  });
  const edges = turns.map((n, i) => edge(n.id, n.group, 'membership', Number((0.4 + (i % 11)*0.05).toFixed(2))));
  for (let i = 1; i < turns.length; i++) edges.push(edge(turns[i].id, turns[i-1].id, 'temporal', 0.55));
  return { nodes: [...turns, ...hubs], edges, totalNodes: 164 + (revision > 12 ? 1 : 0), totalEdges: 319, revision, truncated: true };
}

export function neighbors(fixture, id, filter = 'all') {
  return fixture.edges.filter(e => (e.source === id || e.target === id) && (filter === 'all' || e.type === filter));
}

export function relatedNodes(fixture, id, filter = 'all') {
  const edges = neighbors(fixture, id, filter);
  const ids = new Set(edges.flatMap(e => [e.source, e.target]));
  ids.add(id);
  return { nodes: fixture.nodes.filter(n => ids.has(n.id)), edges };
}

export const typeLabel = node => node?.type === 'hub' ? '关联组' : '回合记忆';
export const relationLabel = type => type === 'membership' ? '共同关联' : '时间关联';
export const firstTurn = fixture => fixture.nodes.find(n => n.type === 'turn');
