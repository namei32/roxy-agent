// The fixture is the complete synthetic catalog. Only these bounded projections
// are drawn; their pagination does not mutate or discard the catalog.
import { neighbors } from './data.js';

export const MEMBER_PAGE_SIZE = 8;
export const NEIGHBOR_PAGE_SIZE = 10;
export const SEARCH_PAGE_SIZE = 12;
const byIdentity = (a, b) => a.id.localeCompare(b.id, 'en', { numeric: true });

export function memberships(fixture, id) {
  const ids = new Set(neighbors(fixture, id, 'membership').flatMap(e => [e.source, e.target]));
  return fixture.nodes.filter(n => n.type === 'hub' && ids.has(n.id)).sort(byIdentity);
}

export function groupMembers(fixture, id) {
  const ids = new Set(neighbors(fixture, id, 'membership').flatMap(e => [e.source, e.target]));
  return fixture.nodes.filter(n => n.type === 'turn' && ids.has(n.id)).sort(byIdentity);
}

export function groupSummaries(fixture) {
  const counts = new Map();
  for (const e of fixture.edges) if (e.type === 'membership') {
    counts.set(e.source, (counts.get(e.source) || 0) + 1);
  }
  return fixture.nodes.filter(n => n.type === 'hub').sort(byIdentity).map(hub => {
    const members = groupMembers(fixture, hub.id);
    return { ...hub, memberCount: members.length, sharedCount: members.filter(n => counts.get(n.id) > 1).length };
  });
}

export function paginate(items, requestedPage, size) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const page = Math.max(0, Math.min(Number.isInteger(requestedPage) ? requestedPage : 0, pages - 1));
  const start = page * size;
  return { items: items.slice(start, start + size), total: items.length, page, pages, start: items.length ? start + 1 : 0, end: Math.min(start + size, items.length), hasPrevious: page > 0, hasNext: page < pages - 1 };
}

export function overviewProjection(fixture, expandedId = null, page = 0) {
  const groups = groupSummaries(fixture);
  const expanded = groups.find(n => n.id === expandedId);
  const batch = paginate(expanded ? groupMembers(fixture, expanded.id) : [], page, MEMBER_PAGE_SIZE);
  const members = batch.items.map((n, i) => ({ ...n, position: [42 + (i % 4) * 92, i < 4 ? 271 : 347] }));
  let otherIndex = 0;
  const nodes = groups.map((n, i) => {
    let position;
    if (!expanded) {
      const columns = groups.length <= 4 ? 2 : 3;
      const rows = Math.ceil(groups.length / columns);
      position = [columns === 2 ? 96 + (i % columns) * 168 : 66 + (i % columns) * 114, 80 + Math.floor(i / columns) * (250 / Math.max(1, rows - 1))];
    } else if (n.id === expanded.id) position = [180, 185];
    else { const j = otherIndex++; position = [45 + (j % 4) * 90, j < 4 ? 41 : 105]; }
    return { ...n, position, aggregate: true };
  }).concat(members);
  const memberIds = new Set(members.map(n => n.id));
  const edges = fixture.edges.filter(e => e.type === 'membership' && memberIds.has(e.source));
  return { nodes, edges, groups, batch, expandedId: expanded?.id || null, revision: fixture.revision, totalMemories: fixture.nodes.filter(n => n.type === 'turn').length };
}

export function localProjection(fixture, rootId, filter = 'all', page = 0) {
  const root = fixture.nodes.find(n => n.id === rootId);
  const allEdges = neighbors(fixture, rootId, filter);
  const ids = new Set(allEdges.flatMap(e => [e.source, e.target]));
  const adjacent = fixture.nodes.filter(n => n.id !== rootId && ids.has(n.id)).sort((a,b) => (a.type === 'hub' ? 0 : 1) - (b.type === 'hub' ? 0 : 1) || byIdentity(a,b));
  const batch = paginate(adjacent, page, NEIGHBOR_PAGE_SIZE);
  const nodes = root ? [root, ...batch.items] : [];
  const visibleIds = new Set(nodes.map(n => n.id));
  return { nodes, edges: allEdges.filter(e => visibleIds.has(e.source) && visibleIds.has(e.target)), batch, totalEdges: allEdges.length, revision: fixture.revision };
}

export function searchCatalog(fixture, query, page = 0) {
  const needle = query.trim().toLowerCase();
  const results = needle ? fixture.nodes.filter(n => n.type === 'turn' && `${n.id} ${n.title} ${n.user} ${n.assistant}`.toLowerCase().includes(needle)).sort(byIdentity) : [];
  return paginate(results, page, SEARCH_PAGE_SIZE);
}
