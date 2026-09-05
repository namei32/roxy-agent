"""Select complete, explicitly scoped pages from a validated graph snapshot."""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from .graph_contract import (
    GROUP_SIZE,
    NEIGHBOR_SIZE,
    SEARCH_SIZE,
    SOURCE_SIZE,
    GraphUnavailable,
    page_info,
    public_id,
    response,
)


class GraphQueries:
    def __init__(self, connection: sqlite3.Connection, revision: str) -> None:
        self.db, self.revision = connection, revision

    def query(self, method: str, payload: dict[str, Any]) -> dict[str, object]:
        """Dispatch an already validated read request inside its transaction."""

        page = payload.get("page", 0)
        if method == "graph.overview":
            return self.overview(page)
        if method == "graph.search":
            return self.search(payload["query"].strip(), page)
        root = self.resolve(payload["node_id"])
        if root is None:
            return self.result("not_found", message="这段记忆已不在当前图中")
        if method == "graph.source":
            return self.result(
                "ready", source=self.source(root, payload["field"], payload["offset"])
            )
        if method == "graph.detail":
            return self.detail(root, page)
        if method == "graph.group" and root["kind"] != "hub":
            raise ValueError("只有关联组可以展开组内成员")
        return self.neighbors(
            root,
            page,
            payload.get("filter", "all"),
            GROUP_SIZE if method == "graph.group" else NEIGHBOR_SIZE,
        )

    def result(self, status: str, **values: object) -> dict[str, object]:
        return response(status, revision=self.revision, **values)

    def count(self, query: str, values: tuple[Any, ...] = ()) -> int:
        return int(self.db.execute(query, values).fetchone()[0])

    def resolve(self, identity: str) -> dict[str, Any] | None:
        kind = identity.split(":", 1)[0]
        if kind == "turn":
            row = self.db.execute(
                "SELECT node_id FROM turn_nodes WHERE graph_id('turn',turn_id)=?",
                (identity,),
            ).fetchone()
        else:
            row = self.db.execute(
                """
                SELECT h.node_id FROM hub_nodes h JOIN turn_nodes t ON t.node_id=h.created_event
                WHERE graph_id('hub',t.turn_id)=?
            """,
                (identity,),
            ).fetchone()
        return None if row is None else {"kind": kind, "node_id": row[0]}

    def node(self, ref: dict[str, Any]) -> dict[str, Any]:
        """Project a stable identity and bounded display fields, never vectors."""

        number = ref["node_id"]
        if ref["kind"] == "hub":
            row = self.db.execute(
                """
                SELECT t.turn_id, t.started_at FROM hub_nodes h
                JOIN turn_nodes t ON t.node_id=h.created_event WHERE h.node_id=?
            """,
                (number,),
            ).fetchone()
            identity = public_id("hub", row["turn_id"])
            return {
                "id": identity,
                "kind": "hub",
                "label": "关联组 " + identity[4:10],
                "ts": row["started_at"],
                "origin_id": public_id("turn", row["turn_id"]),
                "member_count": self.count(
                    "SELECT COUNT(DISTINCT turn_node_id) FROM hub_memberships WHERE hub_node_id=?",
                    (number,),
                ),
                "shared_count": self.count(
                    """
                        SELECT COUNT(DISTINCT a.turn_node_id) FROM hub_memberships a
                        WHERE a.hub_node_id=? AND EXISTS (
                            SELECT 1 FROM hub_memberships b WHERE b.turn_node_id=a.turn_node_id
                            AND b.hub_node_id!=a.hub_node_id)
                    """,
                    (number,),
                ),
            }
        row = self.db.execute(
            """
            SELECT t.turn_id, t.started_at, substr(s.user_text,1,100) AS user_preview,
                   substr(s.assistant_text,1,100) AS assistant_preview
            FROM turn_nodes t JOIN sparse.sparse_turns s ON s.turn_id=t.turn_id
            WHERE t.node_id=?
        """,
            (number,),
        ).fetchone()
        return {
            "id": public_id("turn", row["turn_id"]),
            "kind": "turn",
            "label": row["user_preview"] or row["assistant_preview"] or "（空正文）",
            "preview": row["assistant_preview"],
            "ts": row["started_at"],
            "group_count": self.count(
                "SELECT COUNT(DISTINCT hub_node_id) FROM hub_memberships WHERE turn_node_id=?",
                (number,),
            ),
        }

    def overview(self, page: int) -> dict[str, object]:
        """Page groups independently of their overlapping member counts."""

        turns = self.count("SELECT COUNT(*) FROM turn_nodes")
        groups = self.count("SELECT COUNT(*) FROM hub_nodes")
        refs: list[dict[str, Any]] = [
            {"node_id": row[0], "kind": "hub"}
            for row in self.db.execute(
                "SELECT node_id FROM hub_nodes ORDER BY created_event DESC, node_id LIMIT ? OFFSET ?",
                (GROUP_SIZE, page * GROUP_SIZE),
            )
        ]
        recent = [
            self.node({"node_id": row[0], "kind": "turn"})
            for row in self.db.execute(
                "SELECT node_id FROM turn_nodes ORDER BY node_id DESC LIMIT 3"
            ).fetchall()
        ]
        latest = self.db.execute(
            "SELECT turn_id,committed_at FROM turn_nodes ORDER BY node_id DESC LIMIT 1"
        ).fetchone()
        return self.result(
            "ready" if turns else "empty",
            nodes=[self.node(r) for r in refs],
            edges=[],
            recent=recent,
            scope=page_info(groups, page, GROUP_SIZE),
            totals={
                "turns": turns,
                "groups": groups,
                "edges": self.count("SELECT COUNT(*) FROM hub_memberships")
                + self.count("SELECT COUNT(*) FROM temporal_edges"),
            },
            included_through=(
                None
                if latest is None
                else {"id": public_id("turn", latest[0]), "ts": latest[1]}
            ),
        )

    def search(self, query: str, page: int) -> dict[str, object]:
        predicate = """FROM turn_nodes t JOIN sparse.sparse_turns s ON s.turn_id=t.turn_id
            WHERE instr(lower(s.user_text||char(10)||s.assistant_text), lower(?))>0
               OR instr(lower(t.turn_id),lower(?))>0"""
        total = self.count("SELECT COUNT(*) " + predicate, (query, query))
        rows = self.db.execute(
            "SELECT t.node_id "
            + predicate
            + " ORDER BY t.node_id DESC LIMIT ? OFFSET ?",
            (query, query, SEARCH_SIZE, page * SEARCH_SIZE),
        ).fetchall()
        return self.result(
            "ready",
            items=[self.node({"node_id": r[0], "kind": "turn"}) for r in rows],
            scope=page_info(total, page, SEARCH_SIZE),
            query=query,
        )

    def neighbor_refs(
        self, root: dict[str, Any], relation: str
    ) -> tuple[str, dict[str, int]]:
        number = root["node_id"]
        if root["kind"] == "hub":
            return (
                "SELECT DISTINCT turn_node_id node_id,'turn' kind FROM hub_memberships WHERE hub_node_id=:root"
                + (" AND 0" if relation == "temporal" else "")
            ), {"root": number}
        queries: list[str] = []
        if relation in ("all", "membership"):
            queries.append(
                "SELECT hub_node_id node_id,'hub' kind FROM hub_memberships WHERE turn_node_id=:root"
            )
        if relation in ("all", "temporal"):
            queries.extend(
                [
                    "SELECT target_node_id node_id,'turn' kind FROM temporal_edges WHERE source_node_id=:root",
                    "SELECT source_node_id node_id,'turn' kind FROM temporal_edges WHERE target_node_id=:root",
                ]
            )
        return " UNION ".join(queries), {"root": number}

    def neighbors(
        self, root: dict[str, Any], page: int, relation: str, size: int
    ) -> dict[str, object]:
        query, params = self.neighbor_refs(root, relation)
        total = int(
            self.db.execute("SELECT COUNT(*) FROM (" + query + ")", params).fetchone()[
                0
            ]
        )
        refs: list[dict[str, Any]] = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM ("
                + query
                + ") ORDER BY kind, node_id LIMIT :limit OFFSET :offset",
                {**params, "limit": size, "offset": page * size},
            ).fetchall()
        ]
        refs = [root, *[ref for ref in refs if ref != root]]
        nodes = [self.node(ref) for ref in refs]
        return self.result(
            "ready",
            root_id=nodes[0]["id"],
            nodes=nodes,
            edges=self.edges(refs, nodes, relation),
            filter=relation,
            scope=page_info(total, page, size),
        )

    def edges(
        self, refs: list[dict[str, Any]], nodes: list[dict[str, Any]], relation: str
    ) -> list[dict[str, Any]]:
        """Return every stored edge of the requested types between page endpoints."""

        lookup = {ref["node_id"]: node["id"] for ref, node in zip(refs, nodes)}
        slots = ",".join("?" for _ in lookup)
        values = (*lookup, *lookup)
        rows: list[sqlite3.Row] = []
        if relation in ("all", "membership"):
            rows.extend(
                self.db.execute(
                    f"""
                SELECT edge_id,turn_node_id source,hub_node_id target,
                       effective_weight strength,'membership' kind
                FROM hub_memberships WHERE turn_node_id IN ({slots}) AND hub_node_id IN ({slots})
                ORDER BY edge_id
            """,
                    values,
                ).fetchall()
            )
        if relation in ("all", "temporal"):
            rows.extend(
                self.db.execute(
                    f"""
                SELECT edge_id,source_node_id source,target_node_id target,
                       effective_weight strength,relation_type kind
                FROM temporal_edges WHERE source_node_id IN ({slots}) AND target_node_id IN ({slots})
                ORDER BY edge_id
            """,
                    values,
                ).fetchall()
            )
        edges: list[dict[str, Any]] = []
        for row in rows:
            strength = float(row["strength"])
            if not math.isfinite(strength) or strength < 0:
                raise GraphUnavailable("记忆关联强度无效")
            edges.append(
                {
                    "id": "edge:" + str(row["edge_id"]),
                    "source": lookup[row["source"]],
                    "target": lookup[row["target"]],
                    "kind": row["kind"],
                    "strength": strength,
                    "directed": row["kind"] != "membership",
                }
            )
        return edges

    def detail(self, root: dict[str, Any], page: int) -> dict[str, object]:
        related = self.neighbors(root, page, "membership", GROUP_SIZE)
        sources = (
            {}
            if root["kind"] == "hub"
            else {field: self.source(root, field, 0) for field in ("user", "assistant")}
        )
        return self.result(
            "ready", node=self.node(root), related=related, sources=sources
        )

    def source(
        self, root: dict[str, Any], field: str, offset: int
    ) -> dict[str, object]:
        """Return a contiguous code-point page from validated canonical text."""

        if root["kind"] != "turn":
            raise ValueError("关联组没有独立原文")
        column = "user_text" if field == "user" else "assistant_text"
        row = self.db.execute(
            f"""
            SELECT graph_length(s.{column}) total,graph_text_page(s.{column},?,?) text
            FROM turn_nodes t JOIN sparse.sparse_turns s ON s.turn_id=t.turn_id
            WHERE t.node_id=?
        """,
            (offset, SOURCE_SIZE, root["node_id"]),
        ).fetchone()
        if offset > row["total"]:
            raise ValueError("原文偏移超出范围")
        end = offset + len(row["text"])
        return {
            "field": field,
            "offset": offset,
            "text": row["text"],
            "total_chars": row["total"],
            "next_offset": end if end < row["total"] else None,
        }
