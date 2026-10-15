import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from zhs.app import main
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.runtime import OperationFailed, TimeLimitExceeded, course_run
from zhs.config import DEFAULT_CONFIG
from zhs.workflow import execute_tasks, fetch_courses


def setUpModule():
    logger.setLevel("CRITICAL")


class WorkflowTests(unittest.TestCase):
    def test_all_course_types_are_discovered_and_executed(self):
        client = Mock()
        client.getZhidaoList.return_value = [{"courseName": "z", "secret": "z1"}]
        client.getHikeList.return_value = [{"courseName": "h", "courseId": 2}]
        client.getZhidaoAiList.return_value = [{"courseName": "a", "courseId": 3, "classId": 4}]
        courses, errors = fetch_courses(client)
        self.assertEqual(errors, [])
        result = execute_tasks(client, courses, {"ai": {}}, no_exam=True)
        self.assertEqual(len(result.succeeded), 3)
        client.fuckZhidaoCourse.assert_called_once_with("z1")
        client.fuckHikeCourse.assert_called_once_with("2")
        client.fuckAiCourse.assert_called_once_with("3", "4", aiConfig={}, no_exam=True)

    def test_videos_are_not_skipped(self):
        client = Mock()
        videos = ["v1", "v2", "v3"]
        result = execute_tasks(client, [{"id": "c"}], {}, videos=videos)
        self.assertEqual(len(result.succeeded), 3)
        self.assertEqual([call.args[1] for call in client.fuckVideo.call_args_list], videos)
        self.assertEqual(videos, ["v1", "v2", "v3"])

    def test_failed_video_is_retried_in_another_course(self):
        client = Mock()
        client.fuckVideo.side_effect = [ValueError("not in course"), None]
        result = execute_tasks(client, [{"id": "c1"}, {"id": "c2"}], {}, videos=["v1"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.succeeded), 1)

    def test_exhausted_candidate_deadline_does_not_hide_video_in_another_course(self):
        client = Mock()
        client.fuckVideo.side_effect = [TimeLimitExceeded("limit"), None]
        result = execute_tasks(client, [{"id": "c1"}, {"id": "c2"}], {}, videos=["v1"])
        self.assertEqual(len(result.succeeded), 1)
        self.assertEqual(result.skipped, [])

    def test_failures_and_limits_have_distinct_results(self):
        client = Mock()
        client.fuckCourse.side_effect = [RuntimeError("broken"), TimeLimitExceeded("limit"), None]
        result = execute_tasks(client, [{"id": str(i)} for i in range(3)], {})
        self.assertEqual(
            (len(result.failed), len(result.skipped), len(result.succeeded)), (1, 1, 1)
        )
        self.assertEqual(result.exit_code, 1)

    def test_ai_failure_does_not_exit_successfully(self):
        config = ObjDict(copy.deepcopy(DEFAULT_CONFIG), default=None)
        config.ai.enabled = False
        client = MagicMock()
        client.fuckAiCourse.side_effect = RuntimeError("simulated failure")
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("zhs.config.load_config", return_value=config),
                patch("zhs.clients.course.ZHSClient", return_value=client),
                patch("zhs.app.restore_cookies", return_value=True),
                patch(
                    "zhs.common.utils.getRealPath",
                    side_effect=lambda name: str(Path(directory) / name),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(main(["-ai", "1", "2", "--once"]), 1)
        client.close.assert_called_once()

    def test_partial_failure_propagates_from_course_wrapper(self):
        class Client:
            _startCourse = Mock()

            @course_run
            def run(self, RAC_id):
                self._course_failures.append("broken resource")

        with self.assertRaises(OperationFailed):
            Client().run(RAC_id="c")

    def test_failed_fetch_keeps_existing_execution_file(self):
        config = ObjDict(copy.deepcopy(DEFAULT_CONFIG), default=None)
        client = MagicMock()
        client.getZhidaoList.side_effect = RuntimeError("offline")
        client.getHikeList.return_value = []
        client.getZhidaoAiList.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            execution = Path(directory) / "execution.json"
            execution.write_text('[{"id":"old"}]', encoding="utf-8")
            with (
                patch("zhs.config.load_config", return_value=config),
                patch("zhs.clients.course.ZHSClient", return_value=client),
                patch("zhs.app.restore_cookies", return_value=True),
                patch(
                    "zhs.common.utils.getRealPath",
                    side_effect=lambda name: str(Path(directory) / name),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(main(["--fetch"]), 1)
            self.assertEqual(json.loads(execution.read_text()), [{"id": "old"}])

    def test_keyboard_interrupt_has_distinct_exit_code(self):
        config = ObjDict(copy.deepcopy(DEFAULT_CONFIG), default=None)
        client = MagicMock()
        client.getZhidaoList.return_value = []
        client.getHikeList.return_value = [{"courseId": 1}]
        client.getZhidaoAiList.return_value = []
        client.fuckHikeCourse.side_effect = KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("zhs.config.load_config", return_value=config),
                patch("zhs.clients.course.ZHSClient", return_value=client),
                patch("zhs.app.restore_cookies", return_value=True),
                patch(
                    "zhs.common.utils.getRealPath",
                    side_effect=lambda name: str(Path(directory) / name),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(main(["-c", "1"]), 130)
        client.close.assert_called_once()
