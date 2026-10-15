import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from zhs.common.locking import FileLock, LockBusyError
from zhs.common.runtime import TaskSummary
from zhs.journal import RunJournal


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.courses = [{"id": "1", "type": "hike"}, {"id": "2", "type": "hike"}]

    def create(self):
        return RunJournal.create(self.root, self.courses, {"no_exam": True, "limit": 0})

    def test_resume_skips_successes_and_keeps_attempts(self):
        journal = self.create()
        executor = Mock(
            side_effect=[
                TaskSummary(succeeded=["1"]),
                TaskSummary(failed=["2"], error_types=["RuntimeError"]),
            ]
        )
        self.assertEqual(journal.execute(None, {}, executor).exit_code, 1)
        loaded = RunJournal.load(self.root)
        retry = Mock(return_value=TaskSummary(succeeded=["2"]))
        self.assertEqual(loaded.execute(None, {}, retry).exit_code, 0)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual([t["attempts"] for t in loaded.data["tasks"]], [1, 2])
        self.assertEqual(
            [attempt["status"] for attempt in loaded.data["tasks"][1]["history"]],
            ["failed", "succeeded"],
        )
        self.assertEqual(loaded.data["status"], "finished")

    def test_interrupt_is_saved_and_can_resume(self):
        journal = self.create()
        with self.assertRaises(KeyboardInterrupt):
            journal.execute(None, {}, Mock(side_effect=KeyboardInterrupt()))
        saved = json.loads(journal.path.read_text())
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["tasks"][0]["status"], "interrupted")
        loaded = RunJournal.load(self.root)
        executor = Mock(return_value=TaskSummary(succeeded=["ok"]))
        loaded.execute(None, {}, executor)
        self.assertEqual(executor.call_count, 2)

    def test_abandoned_running_task_becomes_interrupted(self):
        journal = self.create()
        journal.data["tasks"][0]["status"] = "running"
        journal.save()
        self.assertEqual(RunJournal.load(self.root).data["tasks"][0]["status"], "interrupted")

    def test_retry_failed_only_selects_failed_and_pending(self):
        journal = self.create()
        journal.data["tasks"][0]["status"] = "succeeded"
        journal.data["tasks"][1]["status"] = "pending"
        executor = Mock(return_value=TaskSummary(succeeded=["ok"]))
        journal.execute(None, {}, executor, retry_failed=True)
        self.assertEqual(executor.call_count, 1)

    def test_uncertain_submission_is_not_automatically_repeated(self):
        journal = self.create()
        journal.data["tasks"][0].update(
            status="pending", error_type="SubmissionUncertainError", error="verify server"
        )
        journal.data["tasks"][1]["status"] = "succeeded"
        executor = Mock()
        result = journal.execute(None, {}, executor)
        executor.assert_not_called()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(result.pending), 1)

    def test_video_plan_is_per_video_with_all_candidate_courses(self):
        journal = RunJournal.create(self.root, self.courses, {}, videos=["a", "b", "a"])
        self.assertEqual(len(journal.data["tasks"]), 2)
        self.assertEqual(len(journal.data["tasks"][0]["courses"]), 2)

    def test_report_is_json_and_does_not_store_account_configuration(self):
        journal = self.create()
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            journal.export(report)
            data = json.loads(report.read_text())
        self.assertEqual(data["id"], journal.data["id"])
        self.assertNotIn("password", data["options"])

    def test_report_cannot_overwrite_journal_files(self):
        journal = self.create()
        with self.assertRaises(ValueError):
            journal.export(self.root / "latest.json")

    def test_corrupt_record_and_path_traversal_fail(self):
        with self.assertRaises(ValueError):
            RunJournal.load(self.root, "../record")
        journal = self.create()
        journal.data["options"]["speed"] = float("nan")
        journal.path.write_text(json.dumps(journal.data), encoding="utf-8")
        with self.assertRaises(ValueError):
            RunJournal.load(self.root)


class ProcessLockTests(unittest.TestCase):
    def test_competing_lock_fails_then_can_be_acquired_after_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with FileLock(path):
                with self.assertRaises(LockBusyError):
                    with FileLock(path):
                        pass
            with FileLock(path):
                pass

    def test_lock_is_enforced_between_real_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            script = (
                "import sys; from zhs.common.locking import FileLock,LockBusyError\n"
                "try:\n with FileLock(sys.argv[1]): pass\n"
                "except LockBusyError: sys.exit(23)\n"
            )
            with FileLock(path):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 23, result.stderr)
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, str(path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
