import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from zhs.clients.course import Fucker
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.runtime import OperationFailed, TimeLimitExceeded


def setUpModule():
    logger.setLevel("CRITICAL")


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.client = Fucker(proxies={}, tree_view=False, progressbar_view=False)
        self.addCleanup(self.client.close)

    def mock_hike(self, total=1):
        self.client._checkCookies = Mock()
        self.client._sessionReady = Mock()
        self.client._sleep = Mock()
        self.client.watchVideo = Mock()
        self.client.getHikeContext = Mock(return_value=SimpleNamespace(fucked_time=0))
        self.client.stuViewFile = Mock(return_value=SimpleNamespace(dataId=1, totalTime=total))
        self.client.speed = 1
        self.client.end_thre = 1

    def test_hike_stops_at_exact_end_time(self):
        self.mock_hike()
        self.client.saveStuStudyRecord = Mock(return_value=1)
        self.client.fuckHikeVideo("c", "v")
        self.client.saveStuStudyRecord.assert_called_once()

    def test_hike_stalled_progress_fails_instead_of_looping(self):
        self.mock_hike()
        self.client.saveStuStudyRecord = Mock(return_value=0)
        with self.assertRaises(OperationFailed):
            self.client.fuckHikeVideo("c", "v")
        self.assertEqual(self.client.saveStuStudyRecord.call_count, 3)

    def test_wall_clock_deadline(self):
        self.client.limit = 1
        with patch("zhs.clients.course.time.monotonic", return_value=0):
            self.client._startCourse("c")
        with patch("zhs.clients.course.time.monotonic", return_value=60):
            with self.assertRaises(TimeLimitExceeded):
                self.client._checkTimeLimit("c")

    def test_hike_checks_deadline_inside_loop(self):
        self.mock_hike(total=100)
        self.client._sleep.side_effect = TimeLimitExceeded("limit")
        with self.assertRaises(TimeLimitExceeded):
            self.client.fuckHikeVideo("c", "v")

    def test_ai_report_failure_propagates(self):
        self.client._checkCookies = Mock()
        self.client._sessionReady = Mock()
        self.client.zhidaoQuery = Mock(side_effect=RuntimeError("offline"))
        with self.assertRaises(OperationFailed):
            self.client.reportAiVideoProcess(1, 2, 3, 4, 5)

    def ai_knowledge(self, progress=100):
        self.client._sleep = Mock()
        self.client.getAiKnowlegePoints = Mock(
            return_value=ObjDict(
                {
                    "courseName": "course",
                    "cakeThemeList": [
                        {
                            "themeName": "theme",
                            "knowledgeList": [
                                {
                                    "knowledgeId": 1,
                                    "knowledgeName": "node",
                                    "studyProgress": progress,
                                }
                            ],
                        }
                    ],
                }
            )
        )

    def test_ai_attempts_are_bounded_even_when_score_stays_between_30_and_90(self):
        self.ai_knowledge()
        self.client.queryAiExam = Mock(
            return_value={"paperId": 1, "examTestId": 2, "masteryScore": 50}
        )
        with patch("zhs.clients.course.ExamCtx") as factory:
            factory.return_value.startFuck.return_value = (False, 0, 1)
            with self.assertRaises(OperationFailed):
                self.client.fuckAiCourse(1, 2, {"max_exam_attempts": 2})
            self.assertEqual(factory.return_value.startFuck.call_count, 2)

    def test_noexam_does_not_create_exam_or_query_paper(self):
        self.ai_knowledge()
        self.client.queryAiExam = Mock()
        with patch("zhs.clients.course.ExamCtx") as factory:
            self.client.fuckAiCourse(1, 2, {}, no_exam=True)
            factory.assert_not_called()
        self.client.queryAiExam.assert_not_called()

    def test_resource_failure_marks_course_failed(self):
        self.ai_knowledge(progress=0)
        self.client.listKnowledgeResources = Mock(
            return_value=ObjDict(
                {
                    "resourceList": [
                        {
                            "studyStatus": 0,
                            "resourcesDetail": {
                                "resourcesName": "video",
                                "resourcesType": 1,
                                "resourcesDistributeType": 3,
                                "resourcesFileId": 5,
                            },
                        }
                    ]
                }
            )
        )
        self.client.fuckAiVideo = Mock(side_effect=RuntimeError("report failed"))
        with self.assertRaises(OperationFailed):
            self.client.fuckAiCourse(1, 2, {}, no_exam=True)

    def test_ai_playback_advances_at_low_speed(self):
        self.client._checkCookies = Mock()
        self.client._sessionReady = Mock()
        self.client._sleep = Mock()
        self.client.watchVideo = Mock()
        self.client.speed = 0.1
        self.client.zhidaoQuery = Mock(return_value=SimpleNamespace(data=[SimpleNamespace(time=1)]))
        self.client.reportAiVideoProcess = Mock()
        self.client.fuckAiVideo(1, 2, 3, 4)
        self.assertLessEqual(self.client.reportAiVideoProcess.call_count, 6)

    def test_qr_refresh_is_bounded(self):
        self.client._sessionReady = Mock()
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.side_effect = [
            {"qrToken": "t", "img": ""},
            {"status": 2},
            {"qrToken": "t", "img": ""},
            {"status": 2},
        ]
        self.client.session.get = Mock(return_value=response)
        with patch.object(self.client._stop_event, "wait", return_value=False):
            with self.assertRaises(TimeLimitExceeded):
                self.client._qrlogin(Mock(), max_attempts=2)
        self.assertEqual(self.client.session.get.call_count, 4)
