"""Regression tests for process death and failures at persistence boundaries."""

import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from zhs.clients.course import ZHSClient
from zhs.clients.exam import ExamCtx
from zhs.clients.materials import PptToTxt
from zhs.common.checkpoints import track_submissions
from zhs.common.locking import FileLock
from zhs.common.runtime import SubmissionUncertainError, TaskSummary
from zhs.journal import RunJournal


class SubmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = ZHSClient(proxies={})
        self.addCleanup(self.client.close)
        self.exam = ExamCtx(self.client, 1, 2, 3, 4, progress_view=False)
        self.addCleanup(self.exam.close)
        self.exam.openExam = Mock()
        self.exam.readAnswerCache = Mock()
        self.exam.setAnswer = Mock()
        self.exam.saveAnswer = Mock(return_value=True)
        self.exam.getSheetContent = Mock(return_value=[{"questionId": 1}])
        self.question = {
            "id": 1,
            "questionType": 1,
            "optionVos": [{"id": 9, "content": "x"}],
        }
        self.client.zhidaoAiExamQuery = Mock(return_value={})

    def journal(self):
        return RunJournal.create(self.root, [{"id": "1", "classId": "2", "type": "ai"}], {})

    def test_checkpoint_is_durable_before_request_and_reviewed_after_grading(self):
        journal = self.journal()

        def request(*args, **kwargs):
            data = json.loads(journal.path.read_text())
            self.assertEqual(data["tasks"][0]["submissions"][0]["state"], "submitting")

        self.client.zhidaoAiExamQuery.side_effect = request
        self.exam.getQuestionContent = Mock(
            side_effect=[self.question, dict(self.question, userAnswerVo=[{"isCorrect": 1}])]
        )

        def execute(*args, **kwargs):
            self.assertEqual(self.exam.startFuck(), (True, 1, 1))
            return TaskSummary(succeeded=["exam"])

        self.assertEqual(journal.execute(self.client, {}, execute).exit_code, 0)
        loaded = RunJournal.load(self.root)
        self.assertEqual(loaded.data["tasks"][0]["submissions"][0]["state"], "reviewed")
        self.assertEqual(loaded.data["tasks"][0]["submissions"][0]["correct_questions"], 1)
        self.assertEqual(loaded.data["tasks"][0]["submissions"][0]["total_questions"], 1)
        with self.assertRaises(SubmissionUncertainError):
            self.exam.submitExam()
        self.assertEqual(self.client.zhidaoAiExamQuery.call_count, 1)

    def test_failed_checkpoint_prevents_request(self):
        with track_submissions(Mock(side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                self.exam.submitExam()
        self.client.zhidaoAiExamQuery.assert_not_called()

    def test_post_submission_grading_failure_is_not_retried(self):
        journal = self.journal()
        self.exam.getQuestionContent = Mock(side_effect=[self.question, RuntimeError("offline")])
        with self.assertRaises(SubmissionUncertainError):
            journal.execute(self.client, {}, lambda *args, **kwargs: self.exam.startFuck())
        loaded = RunJournal.load(self.root)
        retry = Mock()
        result = loaded.execute(self.client, {}, retry)
        retry.assert_not_called()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(loaded.data["tasks"][0]["status"], "pending")
        self.assertEqual(self.client.zhidaoAiExamQuery.call_count, 1)

    def test_missing_grade_is_pending_instead_of_a_new_attempt(self):
        self.exam.getQuestionContent = Mock(side_effect=[self.question, self.question])
        with self.assertRaises(SubmissionUncertainError):
            self.exam.startFuck()
        self.assertEqual(self.exam._submission_state, "accepted")

    def test_abrupt_process_exit_preserves_submission_barrier(self):
        journal = self.journal()
        script = (
            "import os,sys\n"
            "from zhs.journal import RunJournal\n"
            "from zhs.common.checkpoints import submission_checkpoint\n"
            "def execute(*args, **kwargs):\n"
            " submission_checkpoint('a'*32, 'submitting', exam_test_id='3')\n"
            " os._exit(19)\n"
            "RunJournal.load(sys.argv[1]).execute(None, {}, execute)\n"
        )
        process = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.root)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(process.returncode, 19, process.stderr)
        loaded = RunJournal.load(self.root, journal.data["id"])
        history = loaded.data["tasks"][0]["history"]
        self.assertEqual(history[0]["error_type"], "ProcessInterrupted")
        self.assertIsNone(history[0]["duration_seconds"])
        executor = Mock()
        result = loaded.execute(None, {}, executor)
        executor.assert_not_called()
        self.assertEqual(len(result.pending), 1)


class LockFailureTests(unittest.TestCase):
    def lock_patch(self, error):
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        return patch(target, side_effect=error)

    def test_invalid_timeouts_fail_immediately(self):
        for timeout in (-1, float("nan"), float("inf"), True, "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                FileLock("unused", timeout=timeout)

    def test_io_error_closes_stream_and_is_not_misreported_as_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = MagicMock()
            with (
                patch("pathlib.Path.open", return_value=stream),
                self.lock_patch(OSError(errno.EBADF, "bad descriptor")),
                self.assertRaises(OSError) as caught,
            ):
                FileLock(Path(directory) / "lock", timeout=30).__enter__()
            self.assertEqual(caught.exception.errno, errno.EBADF)
            stream.close.assert_called_once()

    def test_interrupt_during_contention_closes_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = MagicMock()
            with (
                patch("pathlib.Path.open", return_value=stream),
                self.lock_patch(OSError(errno.EACCES, "busy")),
                patch("zhs.common.locking.time.sleep", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                FileLock(Path(directory) / "lock", timeout=30).__enter__()
            stream.close.assert_called_once()


class DownloadRecoveryTests(unittest.TestCase):
    def client(self, root, **kwargs):
        with patch("zhs.clients.materials.OpenAI"):
            client = PptToTxt("test-key", cache_dir=root, **kwargs)
        self.addCleanup(client.close)
        return client

    def response(self, client, chunks, headers):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = headers
        response.iter_content.return_value = chunks
        client._session.get = Mock(return_value=response)

    def test_incomplete_empty_and_invalid_length_downloads_leave_no_cache(self):
        for chunks, headers in (
            ([b"short"], {"Content-Length": "10"}),
            ([], {}),
            ([b"x"], {"Content-Length": "-1"}),
            ([b"x"], {"Content-Length": "invalid"}),
            ([b"long"], {"Content-Length": "1"}),
        ):
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as root:
                client = self.client(root)
                self.response(client, chunks, headers)
                with self.assertRaises(ValueError):
                    client._download("https://example.test/file.pptx")
                self.assertEqual(list(Path(root).rglob("*.part")), [])
                self.assertEqual(list(Path(root).rglob("*.pptx")), [])

    def test_compressed_response_uses_decoded_size_without_false_truncation(self):
        with tempfile.TemporaryDirectory() as root:
            client = self.client(root)
            self.response(client, [b"decoded"], {"Content-Length": "2", "Content-Encoding": "gzip"})
            path = client._download("https://example.test/file.pptx")
            self.assertEqual(path.read_bytes(), b"decoded")

    def test_slow_download_exceeding_total_budget_is_discarded(self):
        with tempfile.TemporaryDirectory() as root:
            client = self.client(root, download_timeout_seconds=5)
            self.response(client, [b"x"], {})
            with patch("zhs.clients.materials.time.monotonic", side_effect=[0, 6]):
                with self.assertRaises(TimeoutError):
                    client._download("https://example.test/file.pptx")
            self.assertEqual(list(Path(root).rglob("*.part")), [])

    def test_invalid_configuration_does_not_allocate_network_clients(self):
        for options in ({"max_cache_files": 1.5}, {"download_timeout_seconds": 0}):
            with (
                patch("zhs.clients.materials.create_session") as session,
                patch("zhs.clients.materials.OpenAI") as api,
                self.assertRaises(ValueError),
            ):
                PptToTxt("test-key", **options)
            session.assert_not_called()
            api.assert_not_called()

    def test_corrupt_remote_manifest_fails_before_network_clients_are_allocated(self):
        import hashlib

        with tempfile.TemporaryDirectory() as root:
            scope = hashlib.sha256(b"https://api.moonshot.cn/v1/test-key").hexdigest()
            manifest = Path(root) / f"remote-{scope}.json"
            manifest.write_text(json.dumps({"owned": {"id": "file", "size": -1, "created_at": 1}}))
            with (
                patch("zhs.clients.materials.create_session") as session,
                patch("zhs.clients.materials.OpenAI") as api,
                self.assertRaises(ValueError),
            ):
                PptToTxt("test-key", cache_dir=root)
            session.assert_not_called()
            api.assert_not_called()

    def test_sdk_constructor_failure_closes_created_session(self):
        with tempfile.TemporaryDirectory() as root:
            with (
                patch("zhs.clients.materials.create_session") as session,
                patch("zhs.clients.materials.OpenAI", side_effect=RuntimeError("bad endpoint")),
                self.assertRaises(RuntimeError),
            ):
                PptToTxt("test-key", cache_dir=root)
            session.return_value.close.assert_called_once()


class JournalBoundaryTests(unittest.TestCase):
    def test_stale_instance_cannot_overwrite_new_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RunJournal.create(root, [{"id": "c", "type": "hike"}], {})
            stale = RunJournal.load(root)
            journal.execute(None, {}, Mock(return_value=TaskSummary(succeeded=["c"])))
            with self.assertRaisesRegex(ValueError, "重新加载"):
                stale.save()
            executor = Mock()
            with self.assertRaisesRegex(ValueError, "重新加载"):
                stale.execute(None, {}, executor)
            executor.assert_not_called()
            self.assertEqual(RunJournal.load(root).data["tasks"][0]["attempts"], 1)

    def test_same_journal_execution_is_exclusive_across_processes(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RunJournal.create(root, [{"id": "c", "type": "hike"}], {})

            def execute(*args, **kwargs):
                script = (
                    "import sys\n"
                    "from zhs.journal import RunJournal\n"
                    "from zhs.common.locking import LockBusyError\n"
                    "try: RunJournal.load(sys.argv[1]).execute(None, {}, None)\n"
                    "except LockBusyError: sys.exit(23)\n"
                )
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, root],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 23, result.stderr)
                return TaskSummary(succeeded=["c"])

            journal.execute(None, {}, execute)

    def test_empty_executor_result_is_a_failure(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RunJournal.create(root, [{"id": "c", "type": "hike"}], {})
            result = journal.execute(None, {}, Mock(return_value=TaskSummary()))
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(journal.data["tasks"][0]["status"], "failed")

    def test_completed_run_retains_summary_without_reexecution(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RunJournal.create(root, [{"id": "c", "type": "hike"}], {})
            journal.execute(None, {}, Mock(return_value=TaskSummary(succeeded=["hike:c"])))
            executor = Mock()
            result = RunJournal.load(root).execute(None, {}, executor)
            executor.assert_not_called()
            self.assertEqual(result.succeeded, ["hike:c"])

    def test_report_cannot_destroy_existing_unrelated_file(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RunJournal.create(Path(root) / "runs", [], {})
            target = Path(root) / "config.json"
            original = '{"enabled": true}'
            target.write_text(original)
            with self.assertRaises(ValueError):
                journal.export(target)
            self.assertEqual(target.read_text(), original)

    def test_report_can_be_updated_after_execution(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RunJournal.create(Path(root) / "runs", [{"id": "c", "type": "hike"}], {})
            target = Path(root) / "report.json"
            journal.export(target)
            journal.execute(None, {}, Mock(return_value=TaskSummary(succeeded=["c"])))
            journal.export(target)
            self.assertEqual(json.loads(target.read_text())["status"], "finished")
