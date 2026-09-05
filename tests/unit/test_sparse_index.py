from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from akasha.infrastructure.sparse_index import (
    AppendOnlyViolation,
    BuildConfig,
    audit_source_embeddings,
    build_sparse_index,
)
from akasha.infrastructure.loader import load_turns


class SparseIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.source = root / "sessions.db"
        self.output = root / "sparse-index.db"
        self._create_source()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_builds_and_incrementally_appends_without_future_statistics(self) -> None:
        self._insert_turn(0, "alpha project", "alpha answer", [1, 0], [1, 0])
        self._insert_turn(2, "alpha followup", "second answer", [0.9, 0.1], [0.8, 0.2])

        first = build_sparse_index(self.source, self.output, self._config())
        self.assertEqual(first.indexed_turns, 2)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sparse_turns"), 2)
        first_evidence = self._scalar(
            """
            SELECT evidence_json FROM sparse_features
            WHERE family='lex_user' AND feature_id='alpha'
            ORDER BY turn_id LIMIT 1
            """
        )
        self.assertIn('"prior_docs": 0', first_evidence)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM turn_dense"), 4)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM turn_terms"),
            self._scalar(
                "SELECT COUNT(*) FROM sparse_features WHERE family IN ('lex_user', 'lex_assistant')"
            ),
        )
        second_time_evidence = self._scalar(
            """
            SELECT evidence_json FROM sparse_features
            WHERE family='time_channel'
            ORDER BY turn_id DESC LIMIT 1
            """
        )
        self.assertIn('"prior_count": 0', second_time_evidence)

        self._insert_turn(4, "beta topic", "third answer", [0, 1], [0, 1])
        second = build_sparse_index(self.source, self.output, self._config())
        self.assertEqual(second.indexed_turns, 1)
        self.assertEqual(second.skipped_existing_turns, 2)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sparse_turns"), 3)
        third_time_evidence = self._scalar(
            """
            SELECT evidence_json FROM sparse_features
            WHERE family='time_channel'
            ORDER BY turn_id DESC LIMIT 1
            """
        )
        self.assertIn('"prior_count": 1', third_time_evidence)

    def test_rejects_new_historical_turn_during_incremental_update(self) -> None:
        self._insert_turn(2, "later", "later answer", [1, 0], [1, 0])
        build_sparse_index(self.source, self.output, self._config())
        self._insert_turn(0, "earlier", "earlier answer", [0, 1], [0, 1])

        with self.assertRaises(AppendOnlyViolation):
            build_sparse_index(self.source, self.output, self._config())

    def test_indexes_turn_even_when_dense_cache_is_missing(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            (
                "test:0",
                "test:one",
                0,
                "user",
                "missing dense",
                None,
                base.isoformat(),
            ),
            (
                "test:1",
                "test:one",
                1,
                "assistant",
                "still indexed",
                None,
                (base + timedelta(seconds=5)).isoformat(),
            ),
        ]
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        result = build_sparse_index(self.source, self.output, self._config())
        audit = audit_source_embeddings(self.source, self._config())

        self.assertEqual(result.discovered_turns, 1)
        self.assertEqual(result.indexed_turns, 1)
        self.assertEqual(result.turns_missing_embeddings, 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sparse_turns"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM turn_dense"), 0)
        self.assertFalse(audit.complete)
        self.assertEqual(
            [issue.reason for issue in audit.issues],
            ["missing", "missing"],
        )

    def test_excludes_legacy_interrupted_turn_without_embeddings(self) -> None:
        """Treat only the exact interrupted assistant marker as non-learning."""

        # 1. Preserve one legacy interrupted turn without inventing embeddings.
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "test:0",
                        "test:one",
                        0,
                        "user",
                        "unfinished request",
                        None,
                        base.isoformat(),
                    ),
                    (
                        "test:1",
                        "test:one",
                        1,
                        "assistant",
                        "[interrupted]",
                        None,
                        (base + timedelta(seconds=5)).isoformat(),
                    ),
                ],
            )
        self._insert_turn(
            2,
            "completed request",
            "answer mentions [interrupted]",
            [1, 0],
            [1, 0],
        )

        # 2. Audit and index only the completed turn.
        audit = audit_source_embeddings(self.source, self._config())
        result = build_sparse_index(self.source, self.output, self._config())

        self.assertTrue(audit.complete)
        self.assertEqual(audit.eligible_turns, 1)
        self.assertEqual(audit.excluded_interrupted_turns, 1)
        self.assertEqual(result.discovered_turns, 1)
        self.assertEqual(result.excluded_interrupted_turns, 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sparse_turns"), 1)
        self.assertEqual(
            self._scalar(
                "SELECT value FROM metadata "
                "WHERE key='turns_excluded_interrupted'"
            ),
            "1",
        )

    def test_canonicalizes_message_feedback_into_causal_turn_targets(self) -> None:
        """Resolve Message markers once so online and replay consume one input."""

        # 1. Store a correction as forget-old plus remember-current.
        self._insert_turn(
            0,
            "old claim",
            "old answer",
            [1, 0],
            [1, 0],
        )
        self._insert_turn(
            2,
            "unrelated",
            "unrelated answer",
            [0, 1],
            [0, 1],
        )
        self._insert_turn(
            4,
            "the correction",
            "acknowledged",
            [0.8, 0.2],
            [0.8, 0.2],
            user_extra={
                "akasha_forget": {
                    "schema_version": 1,
                    "action": "forget",
                    "target_turn_ids": ["test:0::test:1"],
                },
                "akasha_reinforce": {
                    "schema_version": 1,
                    "action": "remember",
                    "target_turn_ids": ["current_turn"],
                    "boost": 3.0,
                },
            },
        )

        build_sparse_index(self.source, self.output, self._config())
        turns = load_turns(self.output)

        self.assertEqual(turns[2].feedback.forget_nodes, (0,))
        self.assertEqual(turns[2].feedback.remember_nodes, (2,))
        self.assertEqual(turns[2].feedback.remember_boost, 3.0)
        with closing(sqlite3.connect(self.output)) as connection:
            row = connection.execute(
                """
                SELECT remember_targets_json, forget_targets_json
                FROM sparse_turns WHERE turn_id='test:4::test:5'
                """
            ).fetchone()
        self.assertEqual(
            row,
            ('["test:4::test:5"]', '["test:0::test:1"]'),
        )

    def test_feedback_change_invalidates_an_existing_sparse_turn(self) -> None:
        """Keep marker payloads inside the append-only source digest."""

        self._insert_turn(
            0,
            "stable",
            "stable answer",
            [1, 0],
            [1, 0],
        )
        build_sparse_index(self.source, self.output, self._config())
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute(
                "UPDATE messages SET extra = ? WHERE id = 'test:0'",
                (
                    json.dumps(
                        {
                            "akasha_reinforce": {
                                "schema_version": 1,
                                "action": "remember",
                                "target_turn_ids": ["current_turn"],
                                "boost": 3.0,
                            }
                        }
                    ),
                ),
            )

        with self.assertRaises(AppendOnlyViolation):
            build_sparse_index(self.source, self.output, self._config())

    def test_forget_marker_requires_a_historical_target(self) -> None:
        self._insert_turn(
            0,
            "cannot forget itself",
            "answer",
            [1, 0],
            [1, 0],
            user_extra={
                "akasha_forget": {
                    "schema_version": 1,
                    "action": "forget",
                    "target_turn_ids": ["current_turn"],
                }
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            "not causally available",
        ):
            build_sparse_index(self.source, self.output, self._config())

    def _config(self) -> BuildConfig:
        return BuildConfig()

    def _create_source(self) -> None:
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE sessions (key TEXT PRIMARY KEY, metadata TEXT);
                INSERT INTO sessions VALUES ('test:one', '{}');
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    extra TEXT,
                    ts TEXT NOT NULL,
                    UNIQUE(session_key, seq)
                );
                CREATE TABLE message_embeddings (
                    message_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    PRIMARY KEY(message_id, model)
                );
                """
            )

    def _insert_turn(
        self,
        seq: int,
        user_text: str,
        assistant_text: str,
        user_vector: list[float],
        assistant_vector: list[float],
        user_extra: dict[str, object] | None = None,
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            (
                f"test:{seq}",
                "test:one",
                seq,
                "user",
                user_text,
                (
                    None
                    if user_extra is None
                    else json.dumps(user_extra)
                ),
                (base + timedelta(minutes=seq)).isoformat(),
            ),
            (
                f"test:{seq + 1}",
                "test:one",
                seq + 1,
                "assistant",
                assistant_text,
                None,
                (base + timedelta(minutes=seq, seconds=5)).isoformat(),
            ),
        ]
        embeddings = [
            (
                f"test:{seq}",
                hashlib.sha256(user_text.encode()).hexdigest(),
                "text-embedding-v4",
                np.asarray(user_vector, dtype=np.float32).tobytes(),
                2,
            ),
            (
                f"test:{seq + 1}",
                hashlib.sha256(assistant_text.encode()).hexdigest(),
                "text-embedding-v4",
                np.asarray(assistant_vector, dtype=np.float32).tobytes(),
                2,
            ),
        ]
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.executemany(
                "INSERT INTO message_embeddings VALUES (?, ?, ?, ?, ?)",
                embeddings,
            )

    def _scalar(self, query: str):
        connection = sqlite3.connect(self.output)
        try:
            return connection.execute(query).fetchone()[0]
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
