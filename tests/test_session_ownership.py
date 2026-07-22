import json
import os
import tempfile
import unittest
from pathlib import Path

import ie_mcp


class SessionPathTests(unittest.TestCase):
    def test_paths_are_scoped_to_the_configured_session(self):
        self.assertTrue(hasattr(ie_mcp, "build_session_paths"), "session-scoped paths are required")
        with tempfile.TemporaryDirectory() as root:
            paths = ie_mcp.build_session_paths(
                env={"IE_SESSION_ID": "Agent 1 / GTA", "IE_SESSION_ROOT": root},
                pid=42,
            )

            self.assertEqual(paths.session_id, "Agent-1-GTA")
            self.assertEqual(paths.directory, Path(root) / "Agent-1-GTA")
            self.assertEqual(paths.log_file, paths.directory / "ie-mcp.log")
            self.assertEqual(paths.lock_file, paths.directory / "session.lock")
            self.assertEqual(paths.owner_file, paths.directory / "owner.json")

    def test_default_session_id_is_unique_to_the_mcp_process(self):
        self.assertTrue(hasattr(ie_mcp, "build_session_paths"), "automatic process IDs are required")
        with tempfile.TemporaryDirectory() as root:
            paths = ie_mcp.build_session_paths(env={"IE_SESSION_ROOT": root}, pid=314)

            self.assertEqual(paths.session_id, "pid-314")


class SessionLeaseTests(unittest.TestCase):
    def test_same_session_id_is_exclusive_but_different_sessions_are_parallel(self):
        self.assertTrue(hasattr(ie_mcp, "SessionLease"), "atomic per-session leases are required")
        with tempfile.TemporaryDirectory() as root:
            first_paths = ie_mcp.build_session_paths(
                env={"IE_SESSION_ID": "agent-a", "IE_SESSION_ROOT": root}, pid=101
            )
            second_paths = ie_mcp.build_session_paths(
                env={"IE_SESSION_ID": "agent-b", "IE_SESSION_ROOT": root}, pid=202
            )
            first = ie_mcp.SessionLease(first_paths, pid=101, pid_alive=lambda pid: pid == 101)
            duplicate = ie_mcp.SessionLease(first_paths, pid=202, pid_alive=lambda pid: pid == 101)
            second = ie_mcp.SessionLease(second_paths, pid=202, pid_alive=lambda pid: True)

            first.acquire()
            second.acquire()
            with self.assertRaisesRegex(RuntimeError, "already owned"):
                duplicate.acquire()

            first.release()
            second.release()

    def test_stale_session_lease_is_reclaimed_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ie_mcp.build_session_paths(
                env={"IE_SESSION_ID": "agent-a", "IE_SESSION_ROOT": root}, pid=202
            )
            paths.directory.mkdir(parents=True)
            paths.lock_file.write_text(
                json.dumps({"session_id": "agent-a", "owner_pid": 101}), encoding="utf-8"
            )
            lease = ie_mcp.SessionLease(paths, pid=202, pid_alive=lambda _pid: False)

            lease.acquire()

            lock = json.loads(paths.lock_file.read_text(encoding="utf-8"))
            self.assertEqual(lock["owner_pid"], 202)
            lease.release()


class CleanupTests(unittest.TestCase):
    def _write_owner(self, root, session_id, **values):
        directory = Path(root) / session_id
        directory.mkdir(parents=True)
        (directory / "owner.json").write_text(
            json.dumps({"session_id": session_id, **values}), encoding="utf-8"
        )

    def test_cleanup_kills_only_processes_owned_by_stale_sessions(self):
        self.assertTrue(hasattr(ie_mcp, "cleanup_stale_sessions"), "scoped cleanup is required")
        with tempfile.TemporaryDirectory() as root:
            self._write_owner(
                root,
                "live-agent",
                owner_pid=11,
                driver_pid=101,
                edge_pid=201,
                profile="live-profile",
            )
            self._write_owner(
                root,
                "stale-agent",
                owner_pid=22,
                driver_pid=102,
                edge_pid=202,
                profile="stale-profile",
            )
            browser_rows = [
                {"pid": 101, "kind": "iedriver", "profile": None},
                {"pid": 201, "kind": "edge-ie", "profile": "live-profile"},
                {"pid": 102, "kind": "iedriver", "profile": None},
                {"pid": 202, "kind": "edge-ie", "profile": "stale-profile"},
                {"pid": 999, "kind": "edge-ie", "profile": "untracked"},
            ]
            killed = []

            result = ie_mcp.cleanup_stale_sessions(
                Path(root),
                browser_rows=browser_rows,
                pid_alive=lambda pid: pid == 11,
                kill=lambda pids: killed.extend(pids) or list(pids),
            )

            self.assertEqual(killed, [102, 202])
            self.assertEqual(result["killed"], [102, 202])
            self.assertEqual(result["skipped_live_sessions"], ["live-agent"])
            self.assertEqual(result["untracked_process_pids"], [101, 201, 999])

    def test_cleanup_preserves_reused_pids_when_the_edge_profile_does_not_match(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_owner(
                root,
                "stale-agent",
                owner_pid=22,
                driver_pid=102,
                edge_pid=202,
                profile="old-profile",
            )
            browser_rows = [
                {"pid": 102, "kind": "iedriver", "profile": None},
                {"pid": 202, "kind": "edge-ie", "profile": "new-agent-profile"},
            ]
            killed = []

            result = ie_mcp.cleanup_stale_sessions(
                Path(root),
                browser_rows=browser_rows,
                pid_alive=lambda _pid: False,
                kill=lambda pids: killed.extend(pids) or list(pids),
            )

            self.assertEqual(killed, [])
            self.assertEqual(result["untracked_process_pids"], [102, 202])


if __name__ == "__main__":
    unittest.main()
