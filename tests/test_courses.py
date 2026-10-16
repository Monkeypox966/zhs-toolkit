import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from zhs.clients.course import Fucker
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.runtime import OperationFailed, SubmissionUncertainError, TimeLimitExceeded


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

    def test_ai_knowledge_reporting_uses_platform_node_type(self):
        self.client._checkCookies = Mock()
        self.client._sessionReady = Mock()
        self.client.zhidaoQuery = Mock()
        self.client.reportAiVideoProcess(1, 2, 3, 4, 5)
        payload = self.client.zhidaoQuery.call_args.args[1]
        self.assertEqual(payload["nodeType"], 1)

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
        with patch("zhs.clients.ai_courses.ExamCtx") as factory:
            factory.return_value.startFuck.return_value = (False, 0, 1)
            with self.assertRaises(OperationFailed):
                self.client.fuckAiCourse(1, 2, {"max_exam_attempts": 2})
            self.assertEqual(factory.return_value.startFuck.call_count, 2)

    def test_noexam_does_not_create_exam_or_query_paper(self):
        self.ai_knowledge()
        self.client.queryAiExam = Mock()
        with patch("zhs.clients.ai_courses.ExamCtx") as factory:
            self.client.fuckAiCourse(1, 2, {}, no_exam=True)
            factory.assert_not_called()
        self.client.queryAiExam.assert_not_called()

    def test_uncertain_exam_does_not_block_later_knowledge_points(self):
        self.ai_knowledge()
        nodes = self.client.getAiKnowlegePoints.return_value.cakeThemeList[0].knowledgeList
        nodes.append(ObjDict({"knowledgeId": 2, "knowledgeName": "second", "studyProgress": 100}))
        self.client.queryAiExam = Mock(
            return_value={"paperId": 1, "examTestId": 2, "masteryScore": 0}
        )
        with patch("zhs.clients.ai_courses.ExamCtx") as factory:
            factory.return_value.startFuck.side_effect = [
                SubmissionUncertainError("grading"),
                (True, 1, 1),
            ]
            with self.assertRaises(SubmissionUncertainError):
                self.client.fuckAiCourse(1, 2, {})
            self.assertEqual(factory.return_value.startFuck.call_count, 2)
        self.assertIn(("1", "2", "2"), self.client._completed_ai_knowledge)

    def test_zero_duration_text_is_opened_instead_of_silently_skipped(self):
        self.client.fuckFile = Mock()
        self.client._traverse(
            "1",
            ObjDict(
                {
                    "id": 2,
                    "name": "text",
                    "dataType": 1,
                    "studyTime": 0,
                    "totalTime": 0,
                    "childList": [],
                }
            ),
        )
        self.client.fuckFile.assert_called_once_with("1", 2)

    def test_material_conversion_receives_local_cache_policy(self):
        self.ai_knowledge()
        self.client.cache_policy = {"max_files": 2, "max_size_mb": 3, "max_age_days": 4}
        self.client.queryAiExam = Mock(
            return_value={"paperId": 1, "examTestId": 2, "masteryScore": 0}
        )
        self.client.listKnowledgeResources = Mock(
            return_value=ObjDict(
                {
                    "resourceList": [
                        {
                            "resourcesDetail": {
                                "resourcesType": 1,
                                "resourcesDistributeType": 4,
                                "resourcesName": "slides",
                                "resourcesUrl": "https://example.test/ppt",
                            }
                        }
                    ]
                }
            )
        )
        config = {"ppt_processing": {"provide_to_ai": True, "moonShot": {"api_key": "test"}}}
        with (
            patch("zhs.clients.ai_courses.PptToTxt") as materials,
            patch("zhs.clients.ai_courses.ExamCtx") as exam,
        ):
            exam.return_value.startFuck.return_value = (True, 1, 1)
            self.client.fuckAiCourse(1, 2, config)
            self.assertEqual(materials.call_args.kwargs["local_max_cache_files"], 2)
            self.assertEqual(materials.call_args.kwargs["local_max_cache_size_mb"], 3)
            self.assertEqual(materials.call_args.kwargs["local_max_age_days"], 4)

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

    def test_ai_unfinished_video_resumes_remaining_playback(self):
        for kind, distribution in ((1, 3), (2, 2)):
            with self.subTest(kind=kind):
                self.ai_knowledge(progress=0)
                self.client.listKnowledgeResources = Mock(
                    return_value=ObjDict(
                        {
                            "resourceList": [
                                {
                                    "studyStatus": 0,
                                    "studyTotalTime": 390,
                                    "schedule": 81,
                                    "resourcesDetail": {
                                        "resourcesName": "partial video",
                                        "resourcesType": kind,
                                        "resourcesDistributeType": distribution,
                                        "resourcesFileId": 5,
                                        "resourcesTime": "481",
                                    },
                                }
                            ]
                        }
                    )
                )
                self.client.fuckAiVideo = Mock()
                self.client.fuckAiCourse(1, 2, {}, no_exam=True)
                self.client.fuckAiVideo.assert_called_once_with(1, 2, 5, 1, 1, startAt=390)

    def test_ai_full_video_is_not_replayed_when_aggregate_is_zero(self):
        self.ai_knowledge(progress=0)
        self.client.listKnowledgeResources = Mock(
            return_value=ObjDict(
                {
                    "resourceList": [
                        {
                            "studyStatus": 1,
                            "studyTotalTime": 481,
                            "schedule": 100,
                            "resourcesDetail": {
                                "resourcesName": "done video",
                                "resourcesType": 1,
                                "resourcesDistributeType": 3,
                                "resourcesFileId": 5,
                                "resourcesTime": "481",
                            },
                        }
                    ]
                }
            )
        )
        self.client.fuckAiVideo = Mock()
        self.client.fuckAiCourse(1, 2, {}, no_exam=True)
        self.client.fuckAiVideo.assert_not_called()

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
