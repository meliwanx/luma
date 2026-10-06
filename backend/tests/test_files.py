"""Unit coverage for the soft-deleted file sweep."""

import unittest
from unittest.mock import MagicMock, patch

# Keep the module import order consistent with the PostgreSQL-backed suite.
from tests import pg  # noqa: F401

from app.services import files


class _Cursor:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self, owner, rows=()):
        self.owner = owner
        self.rows = rows
        self.queries = []

    def __enter__(self):
        self.owner.open_connections += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.owner.open_connections -= 1
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if query.lstrip().upper().startswith("SELECT"):
            return _Cursor(self.rows)
        return _Cursor()


class SweepDeletedFilesTests(unittest.TestCase):
    def setUp(self):
        self.open_connections = 0
        self.rows = [
            {
                "id": "file-1",
                "storage": "local",
                "storage_key": "user/file-1",
            }
        ]
        self.connections = []

    def _connection(self):
        connection = _Connection(self, self.rows)
        self.connections.append(connection)
        return connection

    def test_delete_runs_after_candidate_connection_is_returned(self):
        storage = MagicMock()

        def delete(key):
            self.assertEqual(self.open_connections, 0)

        storage.delete.side_effect = delete
        with patch.object(files, "cache_set_if_absent_strict", return_value=True) as acquire, patch.object(
            files, "cache_delete_strict"
        ) as release, patch.object(files, "get_connection", side_effect=self._connection), patch.object(
            files, "storage_for_row", return_value=storage
        ):
            result = files.sweep_deleted_files()

        self.assertEqual(result, {"deleted": 1, "failed": 0})
        acquire.assert_called_once_with(files.FILE_SWEEP_LOCK_KEY, "1", files.FILE_SWEEP_LOCK_TTL_SECONDS)
        release.assert_called_once_with(files.FILE_SWEEP_LOCK_KEY)
        self.assertEqual(self.open_connections, 0)
        self.assertEqual(len(self.connections), 2)

    def test_occupied_redis_lock_skips_database_and_storage(self):
        with patch.object(files, "cache_set_if_absent_strict", return_value=False), patch.object(
            files, "get_connection"
        ) as get_connection, patch.object(files, "storage_for_row") as storage_for_row:
            result = files.sweep_deleted_files()

        self.assertEqual(result, {"deleted": 0, "failed": 0})
        get_connection.assert_not_called()
        storage_for_row.assert_not_called()

    def test_grace_period_is_enforced_by_candidate_query(self):
        self.rows = []
        with patch.object(files, "cache_set_if_absent_strict", return_value=True), patch.object(
            files, "cache_delete_strict"
        ), patch.object(files, "get_connection", side_effect=self._connection) as get_connection, patch.object(
            files, "storage_for_row"
        ) as storage_for_row:
            result = files.sweep_deleted_files()

        self.assertEqual(result, {"deleted": 0, "failed": 0})
        storage_for_row.assert_not_called()
        query, params = self.connections[0].queries[0]
        self.assertIn("deleted_at IS NOT NULL AND deleted_at < ?", query)
        self.assertEqual(len(params), 2)
        self.assertEqual(params[1], 20)
        get_connection.assert_called_once()


if __name__ == "__main__":
    unittest.main()
