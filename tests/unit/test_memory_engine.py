from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from akasha.domain.diffusion import IndexedMaxHeap, residual_push
from akasha.domain.features import (
    FeaturePool,
    _sparsemax,
)
from akasha.domain.graph import HubRecord, MEMBERSHIP, DynamicMemoryGraph
from akasha.domain.model import MemoryConfig, SeedEvidence, Turn
from akasha.domain.readout import (
    PatternCompletion,
    RecallItem,
    _competitive_route_union,
    _independent_address_mass,
)


def test_indexed_heap_has_one_entry_and_stable_ties() -> None:
    residual = np.zeros(5, dtype=np.float64)
    heap = IndexedMaxHeap(5, residual)
    residual[3] = 0.5
    residual[1] = 0.5
    heap.update(3)
    heap.update(1)
    residual[3] += 0.2
    heap.update(3)

    assert len(heap.nodes) == 2
    assert heap.pop() == 3
    assert heap.pop() == 1


def test_absent_evidence_does_not_activate_every_historical_turn() -> None:
    logits = np.zeros(4_347, dtype=np.float64)

    assert _sparsemax(logits) == ()


def test_context_dependence_selects_independent_address_route() -> None:
    assert _independent_address_mass(0.0) == 1.0
    assert _independent_address_mass(0.1) == 1.0
    assert _independent_address_mass(0.5) == pytest.approx(0.5)
    assert _independent_address_mass(0.9) == 0.0
    assert _independent_address_mass(1.0) == 0.0


def test_competitive_route_union_preserves_baseline_and_rejects_weak_tail() -> None:
    semantic = _completion(
        RecallItem(0, 0.01, ("sharp_completion",), ()),
        RecallItem(1, 1.0, ("basin_completion",), ("base",)),
    )
    address = _completion(
        RecallItem(2, 0.8, ("basin_completion",), ("address",)),
        RecallItem(3, 0.01, ("basin_completion",), ("address",)),
        RecallItem(4, 0.9, ("basin_direct",), ("address",)),
    )

    merged = _competitive_route_union(semantic, address, 1.0)

    assert tuple(item.node_id for item in merged.items) == (1, 2, 0)
    assert {item.node_id for item in semantic.items} <= {
        item.node_id for item in merged.items
    }


def test_residual_push_is_a_fixed_point_lower_bound() -> None:
    config = MemoryConfig(tolerance=1e-12)
    graph = DynamicMemoryGraph(2, config)
    graph._add_edge(  # noqa: SLF001 - mathematical fixture
        source=0,
        target=2,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=0,
        initial_weight=1.0,
        observed_credit=1.0,
        recurrent_credit=0.0,
    )
    graph._add_edge(  # noqa: SLF001 - mathematical fixture
        source=1,
        target=2,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=0,
        initial_weight=1.0,
        observed_credit=1.0,
        recurrent_credit=0.0,
    )
    result = residual_push(
        graph,
        ((0, 1.0),),
        0,
        restart=config.restart,
        tolerance=config.tolerance,
        capture_paths=False,
    )
    transition = _transition_matrix(graph, event=0, seed_node=0)
    seed = np.array([1.0, 0.0, 0.0, 0.0])
    fixed = np.linalg.solve(
        np.eye(4) - (1.0 - config.restart) * transition.T,
        config.restart * seed,
    )

    assert np.all(result.reserve <= fixed + 1e-12)
    assert np.linalg.norm(fixed - result.reserve, ord=1) <= 2e-12
    assert result.residual_l1 <= config.tolerance


def test_graph_has_one_relation_state_without_fast_slow_or_tag() -> None:
    graph = DynamicMemoryGraph(2, MemoryConfig())

    assert graph.weight == []
    assert not hasattr(graph, "fast")
    assert not hasattr(graph, "slow")
    assert not hasattr(graph, "tag")


def test_exact_repeated_query_has_zero_global_surprise() -> None:
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    turns = [
        _turn(0, vector, "重复 天气"),
        _turn(1, vector, "重复 天气"),
    ]
    pool = FeaturePool(turns)
    dense = pool.dense_scores(turns[1].user_dense, 1)
    bm25 = pool.bm25_scores({"重复": 0.5, "天气": 0.5}, 1)

    assert pool.query_surprise(turns[1], 1, dense, bm25) == pytest.approx(0.0)


def test_oja_competition_strengthens_core_and_weakens_unsupported_member() -> None:
    graph = DynamicMemoryGraph(3, MemoryConfig(learning_rate=0.5))
    edges = [
        graph._add_edge(  # noqa: SLF001 - Oja fixture
            source=node,
            target=3,
            kind=MEMBERSHIP,
            bidirectional=True,
            event=0,
            initial_weight=1.0 / 3.0,
            observed_credit=1.0 / 3.0,
            recurrent_credit=0.0,
        )
        for node in range(3)
    ]
    graph.hub_members[3] = edges
    observed = np.asarray([True, True, False, False, False, False])
    activity = np.asarray([1.0, 1.0, 0.0, 0.0, 0.0, 0.0])

    graph._adapt_exposed_hubs(1, observed, activity)  # noqa: SLF001

    assert graph.weight[0] > 1.0 / 3.0
    assert graph.weight[1] > 1.0 / 3.0
    assert graph.weight[2] < 1.0 / 3.0
    assert sum(graph.weight[edge] for edge in edges) <= 1.0


def test_integrated_activity_uses_adaptive_sparsity_without_fixed_top_k() -> None:
    graph = DynamicMemoryGraph(4, MemoryConfig())
    activity = np.asarray([1.0, 0.9, 0.01, 0.005, 0.0, 0.0, 0.0, 0.0])
    turn_nodes = np.asarray([0, 1, 2, 3])
    integrated = graph._integrated_members(  # noqa: SLF001 - competition fixture
        turn_nodes,
        activity,
    )
    weights = dict(integrated)

    assert tuple(weights) == (0, 1)
    assert weights[0] > weights[1] > 0.0
    assert sum(weights.values()) == pytest.approx(1.0)

    equal = graph._integrated_members(  # noqa: SLF001 - equal-evidence fixture
        turn_nodes,
        np.asarray([1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]),
    )
    assert len(equal) == 4


def test_repeated_activation_cannot_exceed_connection_budget() -> None:
    graph = DynamicMemoryGraph(3, MemoryConfig(learning_rate=1.0))
    edges = [
        graph._add_edge(  # noqa: SLF001 - normalization fixture
            source=node,
            target=3,
            kind=MEMBERSHIP,
            bidirectional=True,
            event=0,
            initial_weight=1.0 / 3.0,
            observed_credit=1.0 / 3.0,
            recurrent_credit=0.0,
        )
        for node in range(3)
    ]
    graph.hub_members[3] = edges
    observed = np.asarray([True, True, True, False, False, False])
    activity = np.asarray([1.0, 1.0, 1.0, 0.0, 0.0, 0.0])

    for event in range(1, 62):
        graph._adapt_exposed_hubs(event, observed, activity)  # noqa: SLF001

    assert sum(graph.weight[edge] for edge in edges) == pytest.approx(1.0)


def test_parallel_episode_hubs_share_one_turn_side_connection_budget() -> None:
    graph = DynamicMemoryGraph(3, MemoryConfig())
    edges = [
        graph._add_edge(  # noqa: SLF001 - source budget fixture
            source=0,
            target=hub,
            kind=MEMBERSHIP,
            bidirectional=True,
            event=event,
            initial_weight=0.6,
            observed_credit=0.6,
            recurrent_credit=0.0,
        )
        for event, hub in enumerate((3, 4, 5))
    ]

    inhibited = graph._normalize_membership_source(0)  # noqa: SLF001

    assert inhibited == pytest.approx(0.8)
    assert sum(graph.weight[edge] for edge in edges) == pytest.approx(1.0)


@pytest.mark.parametrize("learning_rate", [0.25, 0.5, 1.0])
def test_feedback_reinforcement_uses_graph_learning_rate(
    learning_rate: float,
) -> None:
    """Apply one feedback event in the graph's existing log-weight dynamics."""

    # 1. Build one unconstrained target membership below both local budgets.
    graph = DynamicMemoryGraph(
        2,
        MemoryConfig(learning_rate=learning_rate),
    )
    edge = graph._add_edge(  # noqa: SLF001 - feedback rule fixture
        source=0,
        target=2,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=0,
        initial_weight=0.2,
        observed_credit=0.2,
        recurrent_credit=0.0,
    )
    graph.hub_members[2] = [edge]
    graph.hubs = [
        HubRecord(
            node_id=2,
            created_event=0,
            current_turn_id=0,
            threshold=0.5,
            innovation_mass=1.0,
            member_edge_ids=(edge,),
        )
    ]

    # 2. Verify boost is integrated by the existing plasticity rate.
    graph.reinforce_feedback_nodes((0,), 3.0)

    credit = learning_rate * np.log(3.0)
    assert graph.weight[edge] == pytest.approx(0.2 * np.exp(credit))
    assert graph.support_credit[edge] == pytest.approx(credit)
    assert graph.independent_credit[edge] == pytest.approx(credit)


def test_feedback_reinforcement_preserves_local_conductance_budgets() -> None:
    """Keep repeated feedback inside the addressed episode and source budgets."""

    # 1. Give the target a weak own episode and a stronger unrelated episode.
    graph = DynamicMemoryGraph(2, MemoryConfig())
    own_edge = graph._add_edge(  # noqa: SLF001 - feedback budget fixture
        source=0,
        target=2,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=0,
        initial_weight=0.2,
        observed_credit=0.2,
        recurrent_credit=0.0,
    )
    peer_edge = graph._add_edge(  # noqa: SLF001 - feedback budget fixture
        source=1,
        target=2,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=0,
        initial_weight=0.8,
        observed_credit=0.8,
        recurrent_credit=0.0,
    )
    other_edge = graph._add_edge(  # noqa: SLF001 - feedback budget fixture
        source=0,
        target=3,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=1,
        initial_weight=0.7,
        observed_credit=0.7,
        recurrent_credit=0.0,
    )
    graph.hub_members = {
        2: [own_edge, peer_edge],
        3: [other_edge],
    }
    graph.hubs = [
        HubRecord(
            node_id=2,
            created_event=0,
            current_turn_id=0,
            threshold=0.5,
            innovation_mass=1.0,
            member_edge_ids=(own_edge, peer_edge),
        )
    ]

    # 2. Repeated confirmation may reallocate, but cannot create a black hole.
    for _ in range(100):
        graph.reinforce_feedback_nodes((0,), 3.0)

    assert graph.weight[own_edge] > 0.2
    assert sum(graph.weight[edge] for edge in (own_edge, peer_edge)) <= 1.0
    assert sum(graph.weight[edge] for edge in (own_edge, other_edge)) <= 1.0
    assert len(graph.weight) == 3


def test_feedback_reinforcement_does_not_fall_back_to_other_episodes() -> None:
    """Leave a target unchanged when it did not create an episode of its own."""

    graph = DynamicMemoryGraph(2, MemoryConfig())
    edge = graph._add_edge(  # noqa: SLF001 - feedback locality fixture
        source=0,
        target=3,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=1,
        initial_weight=0.7,
        observed_credit=0.7,
        recurrent_credit=0.0,
    )
    graph.hub_members[3] = [edge]

    graph.reinforce_feedback_nodes((0,), 3.0)

    assert graph.weight[edge] == 0.7
    assert graph.support_credit[edge] == 0.0


def test_synaptic_resource_fatigues_in_burst_and_recovers_after_gap() -> None:
    def build_graph() -> tuple[DynamicMemoryGraph, list[int]]:
        graph = DynamicMemoryGraph(
            2,
            MemoryConfig(
                learning_rate=1.0,
                forgetting_enabled=False,
            ),
        )
        edges = [
            graph._add_edge(  # noqa: SLF001 - metaplasticity fixture
                source=node,
                target=2,
                kind=MEMBERSHIP,
                bidirectional=True,
                event=0,
                initial_weight=0.5,
                observed_credit=0.5,
                recurrent_credit=0.0,
            )
            for node in range(2)
        ]
        graph.hub_members[2] = edges
        return graph, edges

    burst, burst_edges = build_graph()
    spaced, spaced_edges = build_graph()
    observed = np.asarray([True, False, False, False])
    activity = np.asarray([1.0, 0.0, 0.0, 0.0])

    burst._adapt_exposed_hubs(1, observed, activity)  # noqa: SLF001
    spaced._adapt_exposed_hubs(1, observed, activity)  # noqa: SLF001
    burst_before = burst.weight[burst_edges[0]]
    spaced_before = spaced.weight[spaced_edges[0]]
    spaced.elapsed_seconds += 10.0

    burst._adapt_exposed_hubs(2, observed, activity)  # noqa: SLF001
    spaced._adapt_exposed_hubs(2, observed, activity)  # noqa: SLF001

    assert spaced.weight[spaced_edges[0]] - spaced_before > (
        burst.weight[burst_edges[0]] - burst_before
    )
    assert spaced.resource[spaced_edges[0]] > burst.resource[burst_edges[0]]


def test_new_association_is_not_suppressed_by_unrelated_synaptic_fatigue() -> None:
    graph = DynamicMemoryGraph(3, MemoryConfig())
    graph.prepare_retrieval(
        0,
        None,
        SeedEvidence((), {}, 0.5, 0.5, 1.0),
    )
    integrated = ((0, 0.5), (1, 0.5))
    observed = np.asarray([True, True, False, False, False, False])

    hub, written = graph._create_hub(  # noqa: SLF001 - allocation fixture
        event=0,
        integrated=integrated,
        observed=observed,
        threshold=0.5,
        write_gain=1.0,
    )

    assert hub == 3
    assert written == pytest.approx(1.0)
    assert [graph.weight[edge] for edge in graph.hub_members[hub]] == [
        pytest.approx(0.5),
        pytest.approx(0.5),
    ]


def test_unreinforced_relation_loses_accessibility_without_losing_raw_weight() -> None:
    graph = DynamicMemoryGraph(2, MemoryConfig())
    for _ in range(4):
        for gap in (10.0, 30.0, 100.0, 300.0):
            graph._observe_recurrence(gap, 1.0)  # noqa: SLF001
    graph.prepare_retrieval(
        0,
        None,
        SeedEvidence((), {}, 0.5, 0.5, 1.0),
    )
    edge = graph._add_edge(  # noqa: SLF001 - forgetting fixture
        source=0,
        target=2,
        kind=MEMBERSHIP,
        bidirectional=True,
        event=0,
        initial_weight=1.0,
        observed_credit=1.0,
        recurrent_credit=0.0,
    )

    graph.prepare_retrieval(
        1,
        99.0,
        SeedEvidence((), {}, 0.5, 0.5, 1.0),
    )

    assert graph.weight[edge] == 1.0
    assert 0.0 < graph.retention_factor(edge) < 1.0
    assert graph.effective_weight(edge) == pytest.approx(graph.retention_factor(edge))


def test_recurrence_prior_separates_within_and_across_burst_timescales() -> None:
    graph = DynamicMemoryGraph(2, MemoryConfig())

    for _ in range(20):
        for gap in (60.0, 90.0, 86_400.0, 172_800.0):
            graph._observe_recurrence(gap, 1.0)  # noqa: SLF001

    assert graph.short_recurrence_tau_seconds < 120.0
    assert graph.long_recurrence_tau_seconds > 80_000.0
    assert graph.retention_tau_seconds == graph.long_recurrence_tau_seconds


def test_retrieved_coactivation_partially_reconsolidates_relation() -> None:
    graph = DynamicMemoryGraph(3, MemoryConfig(learning_rate=0.5))
    for _ in range(4):
        for gap in (10.0, 30.0, 100.0, 300.0):
            graph._observe_recurrence(gap, 1.0)  # noqa: SLF001
    graph.prepare_retrieval(
        0,
        None,
        SeedEvidence((), {}, 0.5, 0.5, 1.0),
    )
    edges = [
        graph._add_edge(  # noqa: SLF001 - reconsolidation fixture
            source=node,
            target=3,
            kind=MEMBERSHIP,
            bidirectional=True,
            event=0,
            initial_weight=0.5,
            observed_credit=0.5,
            recurrent_credit=0.0,
        )
        for node in (0, 1)
    ]
    graph.hub_members[3] = edges

    graph.prepare_retrieval(
        1,
        99.0,
        SeedEvidence(
            ((0, 1.0),),
            {"query_dense": frozenset({0})},
            0.5,
            0.5,
            1.0,
        ),
    )
    observed = np.asarray([True, True, False, False, False, False])
    activity = np.asarray([1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
    graph._adapt_exposed_hubs(1, observed, activity)  # noqa: SLF001
    supported_at = graph.last_support_seconds.copy()
    strong_credit = graph.support_credit.copy()

    assert all(0.0 < value < 99.0 for value in supported_at)
    assert all(value > 0.0 for value in strong_credit)
    assert graph.independent_credit == pytest.approx([1.0, 1.0])

    graph.prepare_retrieval(
        2,
        99.0,
        SeedEvidence((), {}, 0.5, 0.5, 1.0),
    )
    recurrent = np.asarray([0.05, 0.05, 0.0, 0.0, 0.0, 0.0])
    graph._adapt_exposed_hubs(2, observed, recurrent)  # noqa: SLF001

    assert all(
        before < after < graph.elapsed_seconds
        for before, after in zip(supported_at, graph.last_support_seconds)
    )
    assert all(
        after > before for before, after in zip(strong_credit, graph.support_credit)
    )
    assert graph.independent_credit == pytest.approx([1.0, 1.0])
    assert all(graph.retention_factor(edge) < 1.0 for edge in edges)


def test_rebuild_database_is_independent_of_python_hash_seed(tmp_path: Path) -> None:
    index = tmp_path / "sparse.db"
    _write_sparse_fixture(index)
    hashes = []
    for seed in ("0", "42", "random"):
        output = tmp_path / f"memory-{seed}.db"
        environment = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(Path(__file__).parents[2] / "src"),
                        os.environ.get("PYTHONPATH", ""),
                    ),
                )
            ),
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "akasha.cli",
                "--index",
                str(index),
                "--db-path",
                str(output),
                "--seq",
            ],
            check=True,
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
        )
        hashes.append(hashlib.sha256(output.read_bytes()).hexdigest())

    assert len(set(hashes)) == 1


def _completion(*items: RecallItem) -> PatternCompletion:
    return PatternCompletion(
        items=items,
        active_basin_count=1,
        sharp_completion_count=sum(
            "sharp_completion" in item.sources for item in items
        ),
        basin_direct_count=sum("basin_direct" in item.sources for item in items),
        basin_completion_count=sum(
            "basin_completion" in item.sources for item in items
        ),
        relative_tail_count=sum("relative_tail" in item.sources for item in items),
        pushes=1,
        residual_l1=0.0,
    )


def _transition_matrix(
    graph: DynamicMemoryGraph,
    *,
    event: int,
    seed_node: int,
) -> np.ndarray:
    matrix = np.zeros((graph.max_nodes, graph.max_nodes), dtype=np.float64)
    for source in range(graph.max_nodes):
        transitions, unspread = graph.transitions(source, event)
        for target, probability, _ in transitions:
            matrix[source, target] += probability
        matrix[source, seed_node] += unspread
    return matrix


def _turn(node_id: int, vector: np.ndarray, text: str) -> Turn:
    return Turn(
        node_id=node_id,
        turn_id=f"s:{node_id}",
        session_key="s",
        user_seq=node_id * 2,
        user_message_id=f"s:{node_id}:user",
        assistant_message_id=f"s:{node_id}:assistant",
        started_at=f"2026-01-01T00:0{node_id}:00+00:00",
        committed_at=f"2026-01-01T00:0{node_id}:30+00:00",
        user_text=text,
        assistant_text="",
        user_dense=vector,
        assistant_dense=vector,
        user_terms=tuple((term, 1) for term in text.split()),
        assistant_terms=(),
        inter_gap_seconds=None if node_id == 0 else 60.0,
    )


def _write_sparse_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        from akasha.infrastructure.sparse_index.schema import SCHEMA, INDEX_VERSION

        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("embedding_model", "fixture"),
                ("index_version", INDEX_VERSION),
                ("turns_missing_embeddings", "0"),
            ],
        )
        for index in range(4):
            turn_id = f"s:{index}::s:{index + 1}"
            connection.execute(
                """
                INSERT INTO sparse_turns
                VALUES (?, 's', ?, ?, ?, ?, ?, ?, ?, '[]', '[]', 1.0, 'fixture')
                """,
                (
                    turn_id,
                    index * 2,
                    f"s:{index}:user",
                    f"s:{index}:assistant",
                    f"2026-01-01T00:0{index}:00+00:00",
                    f"2026-01-01T00:0{index}:30+00:00",
                    f"user shared {index % 2}",
                    f"assistant shared {index % 2}",
                ),
            )
            vector = np.asarray(
                [1.0, 0.2 * index + 0.1],
                dtype=np.float32,
            )
            vector /= np.linalg.norm(vector)
            connection.executemany(
                "INSERT INTO turn_dense VALUES (?, ?, ?, ?, 2)",
                [
                    (turn_id, "user", f"s:{index}:user", vector.tobytes()),
                    (turn_id, "assistant", f"s:{index}:assistant", vector.tobytes()),
                ],
            )
            connection.executemany(
                "INSERT INTO turn_terms VALUES (?, ?, ?, ?)",
                [
                    (turn_id, "user", "shared", 1),
                    (turn_id, "assistant", str(index % 2), 1),
                ],
            )
        connection.commit()
    finally:
        connection.close()
