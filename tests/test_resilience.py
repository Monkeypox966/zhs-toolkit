import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from zhs.app import main
from zhs.clients.course import ZHSClient
from zhs.clients.exam import ExamCtx
from zhs.clients.materials import PptToTxt
from zhs.common.models import ObjDict
from zhs.common.runtime import OperationFailed, PendingAnswerError
from zhs.common.storage import atomic_write_json
from zhs.config import DEFAULT_CONFIG, load_config
from zhs.journal import RunJournal


class PopupTests(unittest.TestCase):
    def setUp(self):
        self.client = ZHSClient(proxies={})
        self.addCleanup(self.client.close)
        self.client._sleep = Mock()
        self.client.saveLessonPopupExamSaveAnswer = Mock()
        self.client.answerZhidao = Mock(return_value="10")
        self.point = ObjDict({"questionIds": [1, 2]})
        self.response = ObjDict(
            {
                "lessonTestQuestionUseInterfaceDtos": [
                    {"testQuestion": {"questionId": 1}},
                    {"testQuestion": {"questionId": 2}},
                ]
            }
        )

    def test_transient_popup_failure_is_retried_and_all_questions_are_processed(self):
        self.client.lessonPopoupExam = Mock(side_effect=[RuntimeError("temporary"), self.response])
        self.client._process_popup_point("course", "video", self.point)
        self.assertEqual(self.client.lessonPopoupExam.call_count, 2)
        self.assertEqual(self.client.saveLessonPopupExamSaveAnswer.call_count, 2)

    def test_exhausted_popup_failure_propagates(self):
        self.client.lessonPopoupExam = Mock(side_effect=RuntimeError("offline"))
        with self.assertRaises(OperationFailed):
            self.client._process_popup_point("course", "video", self.point)
        self.assertEqual(self.client.lessonPopoupExam.call_count, 3)
        self.client.saveLessonPopupExamSaveAnswer.assert_not_called()

    def test_popup_at_video_end_is_not_lost_when_playback_already_reached_end(self):
        self.client.progressbar_view = False
        self.client.end_thre = 1
        self.client._checkCookies = Mock()
        self.client._sessionReady = Mock()
        self.client.getZhidaoContext = Mock(
            return_value=ObjDict(
                {
                    "fucked_time": 0,
                    "videos": {
                        "video": {
                            "studyTotalTime": 10,
                            "watchState": 0,
                            "videoSec": 10,
                            "videoId": 9,
                            "name": "video",
                        }
                    },
                }
            )
        )
        self.client.prelearningNote = Mock(return_value=ObjDict({"studiedLessonDto": {"id": 1}}))
        self.client.loadVideoPointerInfo = Mock(
            return_value=ObjDict({"questionPoint": [{"timeSec": 10, "questionIds": [1, 2]}]})
        )
        self.client.watchVideo = Mock()
        self.client.threeDimensionalCourseWare = Mock()
        self.client.saveDatabaseIntervalTimeV2 = Mock()
        self.client.lessonPopoupExam = Mock(return_value=self.response)
        with patch("zhs.clients.shared.random", return_value=1):
            self.client.fuckZhidaoVideo("course", "video")
        self.assertEqual(self.client.saveLessonPopupExamSaveAnswer.call_count, 2)


class LocalCacheTests(unittest.TestCase):
    def client(self, directory, **kwargs):
        with patch("zhs.clients.materials.OpenAI"):
            client = PptToTxt("test-key", cache_dir=directory, **kwargs)
        self.addCleanup(client.close)
        return client

    def cache_file(self, root, character, age=0, directory="downloads", size=10):
        path = Path(root) / directory / (character * 64 + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"a" * size)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_local_lru_count_quota_keeps_newest_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory, local_max_cache_files=1)
            old = self.cache_file(directory, "a", age=30)
            new = self.cache_file(directory, "b")
            notes = old.parent / "notes.txt"
            notes.write_text("keep")
            client._manage_local_cache()
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(notes.exists())
            self.assertTrue(client._lock_path.parent.exists())

    def test_local_size_quota_and_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory, local_max_cache_size_mb=0.00001, local_max_age_days=1)
            expired = self.cache_file(directory, "a", age=172800)
            old = self.cache_file(directory, "b", age=60, size=10)
            newest = self.cache_file(directory, "c", size=10)
            client._manage_local_cache()
            self.assertFalse(expired.exists())
            self.assertFalse(old.exists())
            self.assertTrue(newest.exists())

    def test_stale_owned_partial_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory)
            downloads = Path(directory) / "downloads"
            downloads.mkdir()
            partial = downloads / ".zhs-old.part"
            partial.write_text("partial")
            os.utime(partial, (0, 0))
            other = downloads / "someone-else.part"
            other.write_text("keep")
            client._manage_local_cache()
            self.assertFalse(partial.exists())
            self.assertTrue(other.exists())

    def test_corrupt_text_cache_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory)
            local = self.cache_file(directory, "a")
            client._download = Mock(return_value=local)
            import hashlib

            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            text = Path(directory) / "texts" / f"{digest}.json"
            text.parent.mkdir()
            text.write_text("{broken")
            client._client.files.create.return_value.id = "owned"
            client._client.files.content.return_value.text = "rebuilt text"
            self.assertEqual(client.parseTxt("https://example.test/ppt"), "rebuilt text")
            self.assertEqual(json.loads(text.read_text())["content"], "rebuilt text")


class ConcurrentAnswerCacheTests(unittest.TestCase):
    def test_stale_instances_merge_only_their_own_changes(self):
        client = ZHSClient(proxies={})
        self.addCleanup(client.close)
        first = ExamCtx(client, 1, 2, 3, 4, aiConfig={})
        second = ExamCtx(client, 1, 2, 5, 6, aiConfig={})
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "zhs.clients.exam.getRealPath", side_effect=lambda name: str(Path(directory) / name)
            ):
                first.readAnswerCache(3)
                second.readAnswerCache(5)
                first.setAnswer(10, 1, {"answer": "100"})
                second.setAnswer(20, 1, {"answer": "200"})
                first.setAnswer(10, 1, {"answer": "101"})
                second.setAnswer(30, 1, {"answer": "300"})
                data = json.loads(Path(second.getAllAnswerpath()).read_text())
                self.assertEqual(data["10"]["answer"], "101")
                self.assertEqual(set(data), {"10", "20", "30"})


class ApplicationResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = ObjDict(copy.deepcopy(DEFAULT_CONFIG), default=None)
        self.config.ai.enabled = False
        self.client = MagicMock()
        self.client.uuid = "test-account"

    def run_app(self, arguments):
        with (
            patch("zhs.config.load_config", return_value=self.config),
            patch("zhs.clients.course.ZHSClient", return_value=self.client),
            patch("zhs.app.restore_cookies", return_value=True),
            patch("zhs.common.utils.getRealPath", side_effect=lambda name: str(self.root / name)),
            patch("builtins.print"),
        ):
            return main(["--once", *arguments])

    def test_failed_run_resumes_and_exports_report(self):
        self.client.fuckAiCourse.side_effect = [RuntimeError("temporary"), None]
        self.assertEqual(self.run_app(["-ai", "1", "2", "--noexam"]), 1)
        report = self.root / "report.json"
        self.assertEqual(self.run_app(["--resume", "--report", str(report)]), 0)
        data = json.loads(report.read_text())
        self.assertEqual(data["tasks"][0]["attempts"], 2)
        self.assertTrue(data["options"]["no_exam"])
        self.assertEqual(data["status"], "finished")

    def test_pending_answer_is_persisted_without_success(self):
        self.client.fuckAiCourse.side_effect = PendingAnswerError("provider unavailable")
        self.assertEqual(self.run_app(["-ai", "1", "2"]), 1)
        journal = RunJournal.load(self.root / "var/runs")
        self.assertEqual(journal.data["tasks"][0]["status"], "pending")
        self.assertEqual(journal.data["tasks"][0]["error_type"], "PendingAnswerError")

    def test_dry_run_saves_plan_without_processing(self):
        self.assertEqual(self.run_app(["-ai", "1", "2", "--dry-run"]), 0)
        self.client.fuckAiCourse.assert_not_called()

        journal = RunJournal.load(self.root / "var/runs")
        self.assertEqual(journal.data["tasks"][0]["status"], "queued")

    def test_unwritable_report_fails_before_course_execution(self):
        report = self.root / "occupied.json"
        report.mkdir()
        self.assertEqual(self.run_app(["-ai", "1", "2", "--report", str(report)]), 1)
        self.client.fuckAiCourse.assert_not_called()

    def test_resume_refuses_a_different_account(self):
        self.assertEqual(self.run_app(["-ai", "1", "2", "--dry-run"]), 0)
        self.client.uuid = "another-account"
        self.assertEqual(self.run_app(["--resume"]), 1)
        self.client.fuckAiCourse.assert_not_called()


class VideoQueueDeadlineTests(unittest.TestCase):
    def test_course_deadline_is_shared_by_its_video_tasks(self):
        client = ZHSClient(proxies={}, limit=1)
        self.addCleanup(client.close)
        clock = [0]

        def process(course_id, video_id, **kwargs):
            clock[0] = 50 if video_id == "v1" else 65
            client._checkTimeLimit(course_id)

        client.fuckVideo = Mock(side_effect=process)
        with tempfile.TemporaryDirectory() as directory:
            journal = RunJournal.create(
                directory, [{"id": "c", "type": "hike"}], {}, videos=["v1", "v2"]
            )
            from zhs.workflow import execute_tasks

            with patch("zhs.clients.course.time.monotonic", side_effect=lambda: clock[0]):
                result = journal.execute(client, {}, execute_tasks)
            self.assertEqual(len(result.succeeded), 1)
            self.assertEqual(len(result.skipped), 1)


class StrictConfigurationTests(unittest.TestCase):
    def test_json_boolean_strings_and_nonfinite_cache_settings_fail(self):
        for changed in (
            {"tree_view": "false"},
            {"cache": {"max_size_mb": -1}},
            {"cache": {"max_files": 1.5}},
            {"qr_extra": {"show_in_terminal": 1}},
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                changed = dict(changed, config_version="1.4.0")
                atomic_write_json(path, changed)
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_atomic_storage_rejects_nonstandard_json_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            atomic_write_json(path, {"valid": True})
            with self.assertRaises(ValueError):
                atomic_write_json(path, {"nan": float("nan")})
            self.assertEqual(json.loads(path.read_text()), {"valid": True})
